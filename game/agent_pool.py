"""A warm Cortex Code agent, held open between visitors.

WHY THIS EXISTS
---------------
`cortex exec` is a one-shot CI/CD entry point. It has no `--resume`, no
`--session` and no `--daemon`, so every call is a cold Node process that re-reads
config, authenticates, and discovers skills and MCP servers before it thinks
about the prompt. Measured on a trivial prompt:

    cortex exec                                         22.7s
    cortex exec --no-mcp                                 19.4s
    + --no-history --effort minimal --max-turns 1        18.1s

So roughly **18 seconds is startup**, not inference, and flags recover under five
of it. On a five-minute visit where the Workshop stop was measured at a 26s
median, that is the single largest slice of the visitor's time.

`cortex mcp serve` runs the same binary in server mode over stdio. Same measured
work, warm:

    startup, once                                         1.3s
    first call                                            9.4s
    second call                                           6.0s

That is the entire point of this module: pay the startup once, when the booth
opens, instead of once per visitor.

THE LOCK IS NOT OPTIONAL
------------------------
Two `tools/call` requests were issued on one process without waiting. `id=10`
asked the agent to reply ALPHA; `id=20` asked it to reply BRAVO. **Both came back
ALPHA.** The second request received the first one's payload.

On a stand that is the worst class of bug available to us: one visitor's content
in another visitor's document, with nothing raising an error. So this pool holds a
single mutex and permits exactly one in-flight call per process, always. Do not
"optimise" that away. If concurrency is ever needed, run a second process.

WHAT IT DOES NOT CARRY
----------------------
The agent is stateless between calls - a codeword stored in one call was answered
with NO CONTEXT in the next - so warming cannot leak one visitor's data into the
next visitor's turn. Verified before this was built, because on a shared booth
laptop that had to be true rather than hoped for.

Ambient MCP servers are not exposed: this laptop has eleven configured and the
agent reported none of them. It does hold `bash`, `edit`, `write`, `sql_execute`
and `web_fetch`, which is why `allowed_tools` defaults to a narrow list here.

IT MUST SURVIVE A CORTEX VERSION THAT CANNOT SERVE IT
-----------------------------------------------------
The booth runs on a borrowed laptop with whatever `cortex` is installed on it,
and `cortex mcp serve` is not a stable contract. Four failure modes were tested
with stub binaries on PATH, and all four now degrade in BOUNDED time so that
`run_turn`'s next layer (`cortex exec`) answers the visitor:

    no `cortex` on PATH at all          start() -> False        0.0s
    starts then exits immediately       start() -> False        0.0s
    serves, but no cortex_code_agent    detected at startup     0.1s/turn
    serves, initialises, never replies  TimeoutError            bounded

The third and fourth were the dangerous ones. A version that exposes no
`cortex_code_agent` is now detected ONCE by `_check_capability()` at startup and
latched in `self.unsupported`, so `ask()` declines instantly rather than spending
a slice of every visitor's budget rediscovering it.

The fourth was a real defect, not a hypothetical: `_read_until()` computed a
deadline but then blocked in `self._p.stdout.readline()`, which has no timeout, so
the deadline was only ever checked BETWEEN lines. A CLI that started and then went
quiet hung forever - and it hung *underneath* the whole fallback chain, so there
was no exec, no COMPLETE and no precomputed answer, just a visitor watching an
empty bubble. stdout is now drained by a daemon thread into a queue, which is what
makes the deadline real. Do not "simplify" that back to a direct readline().

`cortex resume [session_id]` appeared at top level in v1.1.91, but `cortex exec`
still has no `--resume`/`--session`, so everything above still holds. Verified
against v1.1.91: start 1.9s, then calls at 5.1s / 3.9s / 3.9s.
"""

import json
import os
import queue
import subprocess
import threading
import time
import uuid

# One in-flight call per process. See the module docstring - this is load
# bearing, not defensive.
_START_TIMEOUT = 40
_DEFAULT_TIMEOUT = 75
_LIST_TIMEOUT = 10
_AGENT_TOOL = "cortex_code_agent"

# The agent has bash, edit, write and sql_execute available. A booth kiosk taking
# free text from strangers should not, so the default is nothing at all: every
# prompt the booth sends is answerable from the prompt itself, with the closed
# lists already injected by context.shortlist_block().
SAFE_TOOLS = []


class CocoAgent:
    """One warm `cortex mcp serve` process, serialised by a mutex."""

    def __init__(self, connection=None, model=None, workdir=None,
                 allowed_tools=None, log=None, binary=None):
        self.connection = connection
        self.model = model
        self.workdir = workdir or os.getcwd()
        # `cortex` may not be on PATH on a borrowed laptop, and server.py's exec
        # and marketplace paths both honour config `coco.binary`. This one used
        # to hardcode "cortex", so an operator who set an absolute path got a
        # working exec and a warm agent that silently never started.
        self.binary = binary or "cortex"
        self.allowed_tools = (SAFE_TOOLS if allowed_tools is None
                              else list(allowed_tools))
        self.log = log or (lambda *a, **k: None)

        self._p = None
        self._q = None                       # lines from the reader thread
        self._reader = None
        self._call_lock = threading.Lock()   # one in-flight tools/call
        self._io_lock = threading.Lock()     # guards process creation/teardown
        self._next_id = 1
        self._calls = 0
        self._failures = 0
        self.started_at = 0
        self.unsupported = False             # CLI answered but cannot serve us

    # ------------------------------------------------------------- lifecycle

    def _spawn(self):
        cmd = [self.binary, "mcp", "serve"]
        self.log("pool: starting %s" % " ".join(cmd))
        p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            cwd=self.workdir)
        self._p = p
        self._next_id = 1
        # Drain stdout on a thread so _read_until's deadline can actually fire.
        # readline() on the pipe blocks with no timeout, so a CLI that starts
        # and then goes quiet - a version that does not speak this protocol -
        # used to hang the turn forever, BELOW the fallback chain: no exec, no
        # COMPLETE, no precomputed answer, just a visitor watching nothing.
        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._drain, args=(p, self._q),
                                        daemon=True)
        self._reader.start()
        # Handshake. Without notifications/initialized the server accepts the
        # connection but never answers a tools/call.
        self._send({"jsonrpc": "2.0", "id": self._take_id(),
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {},
                               "clientInfo": {"name": "loco4coco",
                                              "version": "1"}}})
        self._read_until(1, timeout=_START_TIMEOUT)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # Prove the CLI can actually serve us BEFORE a visitor is waiting. Some
        # versions start `mcp serve` happily but expose no cortex_code_agent, and
        # discovering that per-turn spends a visitor's budget on a dead layer.
        self._check_capability()
        self.started_at = time.time()
        self.log("pool: ready" if not self.unsupported
                 else "pool: process up but NOT usable - falling through to exec")

    @staticmethod
    def _drain(p, q):
        try:
            for line in p.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)          # sentinel: stdout closed

    def _check_capability(self):
        """Confirm the tool this class depends on exists. Never fatal."""
        try:
            cid = self._take_id()
            self._send({"jsonrpc": "2.0", "id": cid, "method": "tools/list",
                        "params": {}})
            msg = self._read_until(cid, timeout=_LIST_TIMEOUT)
            names = [t.get("name") for t in
                     ((msg.get("result") or {}).get("tools") or [])]
            if _AGENT_TOOL not in names:
                self.unsupported = True
                self.log("pool: this cortex exposes no %s (saw: %s) - the warm "
                         "agent is UNAVAILABLE on this version, every turn will "
                         "use exec instead"
                         % (_AGENT_TOOL, ", ".join(n for n in names if n) or "none"))
        except Exception as e:
            # A CLI that will not list tools may still answer a call; do not
            # condemn it on this evidence, just say so.
            self.log("pool: could not verify %s (%s: %s) - continuing"
                     % (_AGENT_TOOL, type(e).__name__, e))

    def start(self):
        with self._io_lock:
            if self.alive():
                return True
            try:
                self._spawn()
                return True
            except Exception as e:
                self.log("pool: start failed: %s" % e)
                self._kill_locked()
                return False

    def alive(self):
        return self._p is not None and self._p.poll() is None

    def _kill_locked(self):
        p, self._p = self._p, None
        # Drop the queue with the process: a stale sentinel or a half-read reply
        # left over from a dead process would desynchronise the next one's ids.
        self._q, self._reader = None, None
        if not p:
            return
        for fn in (p.kill, p.wait):
            try:
                fn()
            except Exception:
                pass

    def stop(self):
        with self._io_lock:
            self._kill_locked()

    def restart(self):
        self.stop()
        return self.start()

    # ------------------------------------------------------------------- io

    def _take_id(self):
        i, self._next_id = self._next_id, self._next_id + 1
        return i

    def _send(self, obj):
        self._p.stdin.write(json.dumps(obj) + "\n")
        self._p.stdin.flush()

    def _read_until(self, want_id, timeout):
        """Read lines until the reply with want_id arrives.

        Only ever called with the call lock held, so there is exactly one
        outstanding id and no correlation ambiguity. A reply carrying a different
        id is a protocol violation, and it is logged rather than silently used -
        that is precisely the failure mode this class is shaped to avoid.
        """
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("no reply within %ss" % timeout)
            if not self.alive():
                raise RuntimeError("agent process exited")
            try:
                line = self._q.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue          # re-check the deadline and liveness
            if line is None:
                raise RuntimeError("agent closed stdout")
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" not in msg:
                continue          # a notification
            if msg["id"] != want_id:
                self.log("pool: DISCARDING reply for id=%s while awaiting %s"
                         % (msg["id"], want_id))
                continue
            return msg

    # ----------------------------------------------------------------- calls

    def ask(self, prompt, timeout=None, model=None):
        """Run one prompt. Returns (ok, text, meta).

        Serialised: a second caller waits. Never raises - the caller has a
        fallback chain and a visitor in front of them, so a failure here must be
        an ordinary return value.
        """
        t0 = time.time()
        timeout = timeout or _DEFAULT_TIMEOUT
        meta = {"transport": "warm", "id": str(uuid.uuid4())[:8]}

        # A CLI that starts `mcp serve` but exposes no cortex_code_agent cannot
        # ever answer, so decline immediately rather than spending a slice of the
        # visitor's budget proving it again on every turn. The caller reads this
        # as an ordinary failure and drops to exec.
        if self.unsupported:
            meta.update({"waited": 0.0,
                         "error": "warm agent unsupported on this cortex version"})
            return False, "", meta

        # Waiting for the lock is part of the visitor's wall clock, so it is
        # measured separately from the call itself.
        got = self._call_lock.acquire(timeout=timeout)
        if not got:
            meta.update({"waited": round(time.time() - t0, 2),
                         "error": "pool busy"})
            return False, "", meta
        meta["waited"] = round(time.time() - t0, 2)
        try:
            if not self.alive() and not self.start():
                meta["error"] = "agent unavailable"
                return False, "", meta

            args = {"prompt": prompt, "workdir": self.workdir}
            if self.connection:
                args["connection"] = self.connection
            if model or self.model:
                args["model"] = model or self.model
            # `[]` and `None` mean different things here, so this cannot be a
            # truthiness test. An empty list is an instruction - "no tools at
            # all" - and dropping it would leave the agent holding bash, write,
            # edit and sql_execute while taking free text from strangers.
            # `cortex_code_agent` accepts no effort or max_turns argument, so
            # denying tools is also the only lever available for stopping it
            # spending the visitor's time in an agentic loop it does not need.
            if self.allowed_tools is not None:
                args["allowed_tools"] = self.allowed_tools

            call_id = self._take_id()
            sent = time.time()
            try:
                self._send({"jsonrpc": "2.0", "id": call_id,
                            "method": "tools/call",
                            "params": {"name": "cortex_code_agent",
                                       "arguments": args}})
                msg = self._read_until(call_id, timeout=timeout)
            except Exception as e:
                self._failures += 1
                meta.update({"error": "%s: %s" % (type(e).__name__, e),
                             "seconds": round(time.time() - sent, 2)})
                # A broken pipe or a half-read reply leaves the stream out of
                # step with the ids, so the process is no longer trustworthy.
                self.log("pool: dropping process after %s" % meta["error"])
                self.stop()
                return False, "", meta

            self._calls += 1
            meta["seconds"] = round(time.time() - sent, 2)
            if "error" in msg:
                self._failures += 1
                meta["error"] = str(msg["error"])[:300]
                return False, "", meta
            text = _text_of(msg.get("result") or {})
            if not text.strip():
                self._failures += 1
                meta["error"] = "empty reply"
                return False, "", meta
            # log_turn reads meta["ok"] for TURNS.SUCCEEDED; without it every
            # warm answer was recorded as a failure.
            meta["ok"] = True
            return True, text, meta
        finally:
            self._call_lock.release()

    # ----------------------------------------------------------------- admin

    def warm(self):
        """Start the process and take one cheap turn.

        The first call after startup is measurably slower than the rest (9.4s
        against 6.0s), so the booth spends that before doors open rather than on
        the first visitor.
        """
        if not self.start():
            return False, "could not start"
        ok, text, meta = self.ask("Reply with exactly: READY", timeout=60)
        return ok, ("warm in %ss" % meta.get("seconds")
                    if ok else meta.get("error", "warm failed"))

    def stats(self):
        return {"alive": self.alive(), "calls": self._calls,
                "failures": self._failures,
                "uptime": (round(time.time() - self.started_at)
                           if self.started_at else 0),
                "busy": self._call_lock.locked()}


def _text_of(result):
    """Pull the text out of an MCP tool result."""
    if isinstance(result, str):
        return result
    parts = []
    for c in (result.get("content") or []):
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text") or "")
        elif isinstance(c, str):
            parts.append(c)
    return "\n".join(parts).strip()


# A single module-level agent, because the booth serves one visitor at a time on
# one laptop. Deliberately not a pool of N: see the docstring on concurrency.
_agent = None
_agent_lock = threading.Lock()


def get_agent(connection=None, model=None, workdir=None, log=None,
              binary=None):
    global _agent
    with _agent_lock:
        if _agent is None:
            _agent = CocoAgent(connection=connection, model=model,
                               workdir=workdir, log=log, binary=binary)
        return _agent
