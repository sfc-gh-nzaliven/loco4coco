#!/usr/bin/env python3
"""Loco4CoCo arcade companion - local server.

The visitor drives a penguin round an arctic map. At each location the browser
posts their choices here; this server prompts the REAL Cortex Code via
`cortex exec` and streams what CoCo is doing back to the screen. Nothing in the
browser talks to Snowflake or to an MCP directly.

    browser --(HTTP)--> server --(cortex exec)--> CoCo --> Snowflake / Gmail MCP

Two exec profiles, deliberately different:

  conversational turns : --no-mcp   (no tools needed, and skipping MCP startup
                                     is the single biggest latency win)
  the postbox send     : --bypass   (MCP on; without --bypass the Gmail tool
                                     call is AUTO-REJECTED in exec mode)

Delivery is Route A behind a transport interface. See config.delivery.

  python3 server.py                 # use config.json
  python3 server.py --port 5000     # override
"""

import argparse
import base64
import binascii
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The booth's closed lists, with their own three-layer fallback (Snowflake, the
# committed bundle, then this repo's markdown). Kept in a separate module because
# the ops pre-flight and the loader both need it without starting a web server.
import context as bctx
import agent_pool
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
PLUGIN_ROOT = os.path.dirname(HERE)
GUIDES_PATH = os.path.join(PLUGIN_ROOT, "skills", "loco4coco",
                           "references", "guides-index.md")
FEATURES_PATH = os.path.join(PLUGIN_ROOT, "skills", "loco4coco",
                             "references", "feature-docs.md")
MARKET_PATH = os.path.join(PLUGIN_ROOT, "skills", "loco4coco",
                           "references", "marketplace-index.md")
MARKET_JSON_PATH = os.path.join(PLUGIN_ROOT, "skills", "loco4coco",
                                "references", "marketplace.json")
LISTING_URL = "https://app.snowflake.com/marketplace/listing/"

_lock = threading.Lock()
# Bad venue names already warned about, so resolve_venue() complains once per
# value rather than once per request. See the warning in resolve_venue().
_venue_warned = set()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

BLANK_STATE = {
    "stage": "attract",
    "session_id": "",
    "location": "",
    "thinking": False,
    "transport": "",
    "reasoning": [],
    "reply": "",
    "narrative": "",
    "turns": [],
    "visitor": {"first_name": "", "company": "", "email": "", "industry": "",
                "problem": "", "company_country": "", "residency": ""},
    "held": [],
    "platforms": [],
    "joined": [],
    "joined_listings": [],
    "poc": {},
    "unlocked": ["library"],
    "blueprint_url": "",
    "card_url": "",
    "draft_created": False,
    "queued": False,
    "email_sent": False,
    "logged": False,
    "ask_used": False,
    "log_error": "",
    "coco_seconds": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "job_id": None,
    "started_at": None,
    "updated_at": None,
    # True while a background workshop draft is running (see _compose).
    "workshop_pending": False,
    # Optional bonus objectives. Written ONLY by the bonus helpers under
    # _bonus_lock, so their read-modify-write never races the main flow.
    "bonus": {"next": None, "gen": 0, "generating_since": None,
              "offered": 0, "answered": [], "missed": [], "late": False,
              "failed": 0},
}

# Coarse industry inference. Instant and free, which matters against a
# five-minute clock; the visitor always gets to correct it with one click.
INDUSTRY_HINTS = {
    "healthcare": ["nhs", "hospital", "trust", "health", "clinic", "care",
                   "medical", "pharma", "biotech", "gp ", "surgery", "patient"],
    "financial": ["bank", "capital", "invest", "insur", "asset", "fund",
                  "lloyds", "barclays", "hsbc", "natwest", "santander",
                  "financ", "fintech", "pension", "building society", "credit"],
    "retail": ["retail", "tesco", "sainsbury", "asda", "morrison", "aldi",
               "lidl", "marks", "boots", "shop", "store", "consumer",
               "unilever", "nestle", "diageo", "brand", "grocer", "fashion"],
    "public": ["council", "borough", "government", "ministry", "department",
               "authority", "police", "dwp", "hmrc", "defra", "agency",
               "public", "county", "city of", "gov.uk", "school", "university"],
    "manufacturing": ["manufactur", "factory", "industri", "engineer",
                      "automotive", "aerospace", "rolls", "bae", "jaguar",
                      "steel", "chemical", "plant", "production"],
    "energy": ["energy", "power", "utilit", "water", "grid", "national grid",
               "shell", "bp ", "centrica", "octopus", "sse", "electric",
               "gas", "renewab", "nuclear", "thames"],
    "media": ["media", "broadcast", "bbc", "sky", "itv", "telecom", "vodafone",
              "bt ", "o2", "three", "ee ", "publish", "music", "studio",
              "entertain", "advertis", "agency", "game"],
}


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return resolve_venue(json.load(f))


def resolve_venue(cfg):
    """Expand a single `event.venue` toggle into city, language, cloud region
    and marketplace profile, so the operator changes ONE key to move the booth
    from London to Paris or Berlin. Those are the three events; Frankfurt is the
    ACCOUNT's region and is deliberately not a venue.

    The venue's values overwrite event.city/language/region in place, so every
    existing reader (region filter, templating, the index builder) sees the
    resolved values without knowing venues exist. `event.market_profile` is set
    for the marketplace loader. If no venue is set, or it is unknown, the event
    block is left exactly as written - so a hand-configured event still works.
    """
    ev = cfg.get("event") or {}
    venue = (ev.get("venue") or "").strip().lower()
    venues = cfg.get("venues") or {}
    v = venues.get(venue)
    if v:
        for k in ("city", "language", "marketplace_region"):
            if v.get(k):
                ev[k] = v[k]
        ev["market_profile"] = v.get("market_profile") or "uk"
    else:
        if venue:
            # A venue that is SET but not in the map is the one silent failure in
            # this function: nothing is overwritten, market_profile falls back to
            # 'uk', and the booth serves London's datasets under whatever city
            # happens to be written in the event block. /api/venue refuses an
            # unknown venue, so this can only be reached by hand-editing the
            # file - which is exactly when nobody is watching. Warned once per
            # bad value rather than per request, because load_config() runs on
            # every request and a flooded log is a log nobody reads.
            if venue not in _venue_warned:
                _venue_warned.add(venue)
                known = ", ".join(k for k in venues if not k.startswith("_"))
                print("[loco4coco] WARNING: event.venue=%r is not a known venue "
                      "(%s). Serving market_profile=%r and the event block as "
                      "written - check /admin reads the city you expect."
                      % (venue, known, ev.get("market_profile") or "uk"))
        ev.setdefault("market_profile", "uk")
    # Accept a pre-rename config rather than serving an empty stall. `region` was
    # renamed to `marketplace_region` because two different regions exist and the
    # old name did not say which one it meant: this one biases which Marketplace
    # datasets are RECOMMENDED, and has nothing to do with the account's region.
    if not ev.get("marketplace_region") and ev.get("region"):
        ev["marketplace_region"] = ev["region"]
    ev.pop("region", None)
    cfg["event"] = ev
    return cfg



def read_state():
    with _lock:
        if not os.path.exists(STATE_PATH):
            return dict(BLANK_STATE)
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # A half-written file must never crash the booth display.
            return dict(BLANK_STATE)
    merged = dict(BLANK_STATE)
    merged.update(data)
    return merged


def purge_temp_artifacts():
    """Delete the visitor-named blueprint files this process writes to the OS
    temp dir (loco4coco-<name>-<ts>.html/.docx). They carry the visitor's first
    name, so on a shared booth they must not outlive the visit - called on reset
    so nothing about one visitor is left on disk for the next. Returns a count."""
    removed = 0
    tmp = tempfile.gettempdir()
    try:
        for n in os.listdir(tmp):
            if n.startswith("loco4coco-") and n.endswith((".html", ".docx")):
                try:
                    os.remove(os.path.join(tmp, n))
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def write_state(patch, replace=False):
    """Shallow-merge a patch into state.json, atomically.

    `replace` writes the patch wholesale. Needed for reset: `visitor` and `poc`
    are merged sub-dicts, so passing an empty one would leave the previous
    visitor's POC in place - which is exactly how one visitor's name leaked
    into the next visitor's session.
    """
    with _lock:
        current = dict(BLANK_STATE)
        if os.path.exists(STATE_PATH) and not replace:
            try:
                with open(STATE_PATH, encoding="utf-8") as f:
                    current.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
        for k, v in patch.items():
            if k in ("visitor", "poc") and isinstance(v, dict) and not replace:
                sub = dict(current.get(k) or {})
                sub.update(v)
                current[k] = sub
            else:
                current[k] = v
        if current.get("stage") not in (None, "attract") and not current.get("started_at"):
            current["started_at"] = time.time()
        current["updated_at"] = time.time()
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=1)
        os.replace(tmp, STATE_PATH)
    return current


# ---------------------------------------------------------------- cost logging

def log_cost(cfg, kind, seconds, usage, ok, transport="exec", model=None,
             error=None, note=None):
    """Append-only spend trail with the real token counts exec reports. We do
    not cap the visitor, so this is the only way spend stays visible.

    `error` is recorded because omitting it hid a real defect for an entire test
    run: when claude-4-sonnet went to legacy state every COMPLETE turn began
    failing in ~0.5s, the exec fallback quietly absorbed it, and the log showed
    ok=False with no reason. A failure you cannot attribute is one you will not
    fix."""
    path = os.path.join(HERE, (cfg.get("coco") or {}).get("cost_log", "cost.jsonl"))
    u = usage or {}
    rec = {"at": time.time(), "kind": kind, "seconds": round(seconds, 2),
           "transport": transport, "model": model,
           "input_tokens": u.get("input_tokens"),
           "output_tokens": u.get("output_tokens"),
           "cache_read": u.get("cache_read_input_tokens"),
           "cache_write": u.get("cache_creation_input_tokens"),
           "ok": ok, "error": error}
    if note:
        rec["note"] = note
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------- the CoCo bridge

# Reasoning worth showing a visitor. Deliberately excludes assistant text,
# because that is the reply and it already has a home in the thought bubble.
def _readable(name):
    return {"sql_execute": "querying Snowflake", "web_search": "searching the web",
            "web_fetch": "reading a page", "read": "reading a file",
            "bash": "running a command", "python_repl": "working it out"}.get(
                name, name.replace("_", " "))


# Lines that break character or expose plumbing. A booth audience must never
# read "the user wants me to respond as CoCo the Snowflake penguin" - it tells
# them he was told to act like one - nor see internal tool names.
_TRAY_BLOCK = re.compile(
    r"(the user wants|the user is asking|the user said|i need to (keep|pick|respond)"
    r"|i should (keep|respond|reply)|no bullet points|plain english|short sentences"
    r"|max \d+ words|as coco|the coco|my instructions|the prompt|mcp__|create_draft"
    r"|tool search|toolset|google-workspace|google-calendar|gmail|skill\b"
    r"|verbatim|minified json|booth)", re.I)


def _sentences(text, limit=3):
    out = []
    for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()):
        s = s.strip().lstrip("-*\u2022 ").strip()
        if len(s) < 12 or _TRAY_BLOCK.search(s):
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def run_exec(cfg, prompt, kind, job_id=None, use_mcp=False, timeout=None,
             effort=None, tools=None, max_turns=None, allowed=None):
    """Run `cortex exec` and stream its real reasoning into state.reasoning.

    Uses --format json, which emits NDJSON events including genuine `thinking`
    blocks, tool calls, and token usage. The tray therefore shows what CoCo is
    actually doing rather than a decorative spinner.
    """
    c = cfg.get("coco") or {}
    cmd = [c.get("binary", "cortex"), "exec", prompt, "--format", "json"]
    conn = (cfg.get("snowflake") or {}).get("connection_name")
    if conn:
        cmd += ["--connection", conn]
    if use_mcp:
        # Without --bypass, exec auto-rejects the Gmail tool call and the send
        # silently does nothing. Proven: "Tool denied: headless mode requires..."
        cmd.append("--bypass")
    else:
        cmd.append("--no-mcp")
        if tools:
            # Let CoCo actually USE its built-in tools. exec auto-rejects tool
            # calls without --bypass, so without this the tray only ever shows
            # thinking. MCP stays OFF: we want the built-ins, not the MCP
            # server startup cost.
            cmd.append("--bypass")
    if max_turns:
        # Safety ceiling on the agentic loop once tools are in play.
        cmd += ["--max-turns", str(max_turns)]
    if allowed:
        # Optional allow-list, e.g. ["Bash(gh *)"]. Unused by default; the
        # blueprint is clamped server-side anyway (off-list features dropped,
        # guide choice falls back), so a wandering model cannot leak through.
        cmd += ["--allowed", ",".join(allowed) if isinstance(allowed, list)
                else str(allowed)]
    # Each turn is a fresh, stateless exec: --no-history skips the session save
    # and keeps one visitor's turns from bleeding into the next.
    cmd.append("--no-history")
    # Booth-tunable model + effort. Global config.coco.effort is the default; a
    # per-turn `effort` override (from the location) wins. Measured: default
    # (null) behaves like high effort; "low" cuts ~8s off simple turns and ~17s
    # off the workshop, and the {minimal,low,medium} spread is only ~3-4s.
    model = c.get("model")
    if model:
        cmd += ["-m", str(model)]
    eff = effort or c.get("effort")
    if eff:
        cmd += ["--effort", str(eff)]

    started = time.time()
    lines, final, usage, ok = [], "", {}, False

    def push(line):
        lines.append(line)
        write_state({"reasoning": lines[-40:]})

    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True,
                             bufsize=1, cwd=HERE)
        deadline = started + (timeout or c.get("timeout_seconds", 180))
        for raw in p.stdout:
            raw = raw.strip()
            if not raw:
                continue
            if job_id is not None:
                cur = read_state()
                if cur.get("job_id") not in (None, job_id):
                    p.kill()
                    return False, "", {"transport": "exec", "ok": False}  # superseded
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                push("Processing your request...")
            elif t == "assistant":
                for b in (ev.get("message", {}).get("content") or []):
                    bt = b.get("type")
                    if bt == "thinking":
                        for s in _sentences(b.get("thinking")):
                            push(s)
                    elif bt == "tool_use":
                        push("\u2192 " + _readable(b.get("name") or "a tool"))
            elif t == "result":
                final = (ev.get("result") or "").strip()
                usage = ev.get("usage") or {}
                ok = not ev.get("is_error")
            if time.time() > deadline:
                p.kill()
                push("That is taking a while. I will go with what I have.")
                break
        p.wait(timeout=10)
        if p.returncode == 0 and final:
            ok = True
    except FileNotFoundError:
        return False, "", {"transport": "exec", "ok": False,
                           "error": "cortex CLI not found"}
    except Exception as e:                                       # noqa: BLE001
        return False, "", {"transport": "exec", "ok": False,
                           "error": str(e)[:200]}

    log_cost(cfg, kind, time.time() - started, usage, ok, "exec", model)
    meta = {"seconds": round(time.time() - started, 1), "usage": usage,
            "ok": ok, "transport": "exec", "model": model}
    if not final:
        return False, "I lost my train of thought there. Try me again.", meta
    return ok, final, meta


# --------------------------------------------------- the fast path (no agent)

_conn = None
_conn_lock = threading.Lock()


def sf_conn(cfg):
    """One long-lived Snowflake connection, reused for Cortex COMPLETE calls and
    for row logging.

    Reusing a connection is the whole point: a fresh `snow sql` subprocess costs
    ~3.4s of CLI startup per statement, which is more than a COMPLETE call takes.
    """
    global _conn
    with _conn_lock:
        if _conn is not None:
            try:
                if not _conn.is_closed():
                    return _conn
            except Exception:                                    # noqa: BLE001
                pass
            _conn = None
        import snowflake.connector as sc                    # lazy: booth-only dep
        name = (cfg.get("snowflake") or {}).get("connection_name")
        # Keep-alive stops the session token expiring between visitors; without
        # it a server left up overnight fails every COMPLETE with 390114.
        _conn = sc.connect(client_session_keep_alive=True,
                           **({"connection_name": name} if name else {}))
        return _conn


def drop_conn():
    """Discard the cached connection so the next sf_conn() reconnects."""
    global _conn
    with _conn_lock:
        try:
            if _conn is not None:
                _conn.close()
        except Exception:                                        # noqa: BLE001
            pass
        _conn = None


# A SECOND long-lived connection for background work (bonus questions, the
# scoreboard), so it never queues behind a visitor's own COMPLETE turn on the
# shared session.
_bg_conn = None
_bg_lock = threading.Lock()


def bg_conn(cfg, fresh=False):
    global _bg_conn
    with _bg_lock:
        if fresh and _bg_conn is not None:
            try:
                _bg_conn.close()
            except Exception:                                    # noqa: BLE001
                pass
            _bg_conn = None
        if _bg_conn is not None:
            try:
                if not _bg_conn.is_closed():
                    return _bg_conn
            except Exception:                                    # noqa: BLE001
                pass
        import snowflake.connector as sc
        name = (cfg.get("snowflake") or {}).get("connection_name")
        _bg_conn = sc.connect(client_session_keep_alive=True,
                              **({"connection_name": name} if name else {}))
        return _bg_conn


ACCOUNT = {"name": ""}


def account_name(cfg):
    """The account this booth writes to, cached for the process lifetime.

    Resolved lazily rather than in the warm thread so there is no ordering
    dependency: if the first visitor arrives before warming finishes, the row is
    still attributed. One query, once.
    """
    if ACCOUNT["name"]:
        return ACCOUNT["name"]
    try:
        cur = sf_conn(cfg).cursor()
        cur.execute("SELECT CURRENT_ACCOUNT()")
        ACCOUNT["name"] = (cur.fetchone() or [""])[0] or ""
        cur.close()
    except Exception as e:                                        # noqa: BLE001
        print(f"[loco4coco] could not resolve account name: {str(e)[:100]}")
    return ACCOUNT["name"]
PREFLIGHT = {"checked": False, "model": None, "tried": [], "note": ""}


CONN_PREFLIGHT = {"checked": False, "ok": None, "name": "", "detail": ""}


def preflight_connection(cfg):
    """Prove the configured connection actually authenticates, at startup.

    The postbox stages the visitor's .docx with `snow stage copy -c <name>` and
    presigns it, all against snowflake.connection_name. If that name is stale,
    someone else's, or still the shipped placeholder, every Snowflake call fails
    - but the only visible symptom is an error at the postbox with a visitor
    watching. So we check once, here, and say so loudly. Warn only: the booth
    still starts, because a booth that refuses to open is worse than one whose
    delivery leg needs a fix before the doors do.

    Tested via the `snow` CLI (snow_sql), NOT the Python connector, because that
    is the exact path the postbox uses: a connection can authenticate via the CLI
    but not via sc.connect() when its connections.toml has no private_key_file,
    and testing the connector would then false-warn on a booth that delivers fine.
    """
    name = (cfg.get("snowflake") or {}).get("connection_name") or ""
    CONN_PREFLIGHT.update({"checked": True, "name": name})
    if not name or name == "MYBOOTH":
        CONN_PREFLIGHT.update({"ok": False,
            "detail": "connection_name is still the placeholder MYBOOTH; run "
                      "python3 deploy/bootstrap.py -c <your-connection>"})
        print("[loco4coco] *** snowflake.connection_name is not set for this "
              "laptop (still 'MYBOOTH'). The postbox cannot deliver. ***")
        print("[loco4coco] *** Fix: python3 deploy/bootstrap.py -c <your-connection> "
              "(or set snowflake.connection_name in game/config.json). ***")
        return False
    rows, err = snow_sql(cfg, "SELECT CURRENT_USER() AS U, CURRENT_ACCOUNT() AS A")
    if err or not rows:
        CONN_PREFLIGHT.update({"ok": False, "detail": (err or "no rows")[:160]})
        print(f"[loco4coco] *** connection '{name}' cannot authenticate: "
              f"{(err or 'no rows')[:120]} ***")
        print("[loco4coco] *** The postbox will fail to stage/deliver. Fix the "
              "connection (python3 deploy/bootstrap.py -c <your-connection>) "
              "before opening. ***")
        return False
    row = rows[0] if isinstance(rows, list) else rows
    user = row.get("U") or row.get("u") or ""
    acct = row.get("A") or row.get("a") or ""
    CONN_PREFLIGHT.update({"ok": True, "detail": f"{user} @ {acct}"})
    print(f"[loco4coco] connection preflight: {name} ok ({user} @ {acct})")
    return True


def booth_auth_method(cfg):
    """The CoCo-revert canary. Cortex Code continuously rewrites connections.toml
    for the connection it is pointed at, and can flip an in-place key-pair back to
    OAuth - which opens a browser mid-visit. The booth deliberately uses a SEPARATE
    connection CoCo does not manage, so this should read as key-pair. Returns
    (method, safe, detail); safe is True/False/None (None = cannot judge, e.g. the
    MYBOOTH placeholder before bootstrap). Reads the local toml only - no round trip."""
    name = coco_connection() or ""
    if not name:
        return ("", None, "no booth connection configured")
    try:
        import pathlib
        import tomllib
        p = pathlib.Path.home() / ".snowflake" / "connections.toml"
        conns = tomllib.loads(p.read_text()) if p.exists() else {}
    except Exception as e:                                        # noqa: BLE001
        return ("", None, f"could not read connections.toml ({e})")
    c = conns.get(name)
    if not isinstance(c, dict):
        return ("", None, f"{name} not found in connections.toml (placeholder?)")
    auth = str(c.get("authenticator") or "").lower()
    haskey = bool(c.get("private_key_file") or c.get("private_key_path"))
    if any(t in auth for t in ("oauth", "externalbrowser", "sso")):
        return ("browser/OAuth", False,
                f"{name} is {auth} - will prompt mid-event; re-run deploy/bootstrap.py")
    if "jwt" in auth or haskey:
        return ("key-pair", True,
                f"{name} on SNOWFLAKE_JWT" if haskey
                else f"{name} JWT but no private_key_file set")
    if c.get("password"):
        return ("password", True, f"{name} on password (no browser prompt)")
    return (auth or "unknown", None, f"{name} authenticator={auth or 'unset'}")


def preflight_complete_model(cfg):
    """Prove the configured COMPLETE model answers; swap to a fallback if not.

    A model name is a perishable dependency. claude-4-sonnet was measured,
    chosen, and written into config, and then went to legacy state - after which
    every COMPLETE call failed in ~0.5s, the exec fallback silently absorbed it,
    and the only visible symptom was the booth feeling slow. QA had no fallback
    at all, so it produced nothing for every visitor.

    So the booth now asks the account, once, at startup. If the configured model
    is dead it walks coco.complete_model_fallbacks in order and rewrites cfg in
    place, which is enough because run_complete re-reads cfg on every call. The
    result is surfaced in /admin so a degraded model is visible, not inferred.
    """
    c = cfg.get("coco") or {}
    want = c.get("complete_model") or "openai-gpt-5.4"
    chain = [want] + [m for m in (c.get("complete_model_fallbacks") or [])
                      if m != want]
    tried = []

    # Open the connection once, before touching any model. A connection that will
    # not open fails identically for every model, so probing the whole chain just
    # reprints the same error N times and makes one connection problem look like N
    # dead models. Report it once, in plain words. The signature case on a personal
    # connection: connections.toml has private_key_path set (the snow CLI's param)
    # but not private_key_file (what the Python connector needs), which surfaces as
    # the opaque "Expected bytes, RSAPrivateKey... got NoneType" from cryptography.
    try:
        conn = sf_conn(cfg)
    except Exception as e:                                        # noqa: BLE001
        msg = str(e).replace("\n", " ")
        name = (cfg.get("snowflake") or {}).get("connection_name") or "(unset)"
        if "Expected bytes" in msg and "NoneType" in msg:
            note = (f"connection '{name}' will not open the in-process (Python "
                    f"connector) path: its private key did not load. This connection "
                    f"most likely has private_key_path set but not private_key_file - "
                    f"the snow CLI reads the former, the connector needs the latter. "
                    f"The CLI paths (delivery, cortex exec) are unaffected, so the "
                    f"booth still answers via the slower exec path.")
        else:
            note = (f"connection '{name}' will not open the in-process path: "
                    f"{msg[:140]}")
        PREFLIGHT.update({"checked": True, "model": None, "tried": [], "note": note})
        print(f"[loco4coco] *** model preflight: {note} ***")
        print("[loco4coco] *** COMPLETE will use the cortex exec path "
              "(~20s/turn); the booth still works. ***")
        return None

    for mdl in chain:
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, 'ping')",
                            (mdl,))
                cur.fetchone()
            finally:
                cur.close()
        except Exception as e:                                   # noqa: BLE001
            msg = str(e)
            why = ("legacy" if "legacy" in msg else
                   "unknown model" if "unknown model" in msg else
                   msg[:80].replace("\n", " "))
            tried.append(f"{mdl}: {why}")
            print(f"[loco4coco] model preflight: {mdl} unusable ({why})")
            continue
        PREFLIGHT.update({"checked": True, "model": mdl, "tried": tried})
        if mdl != want:
            # Loud on purpose. A quiet swap is how the last one hid.
            PREFLIGHT["note"] = (f"configured model {want} is unusable; "
                                 f"running on {mdl}")
            print(f"[loco4coco] *** {PREFLIGHT['note']} ***")
            print("[loco4coco] *** update coco.complete_model and re-run "
                  "scripts/rebench_complete.py ***")
            (cfg.setdefault("coco", {}))["complete_model"] = mdl
            if (cfg.get("qa") or {}).get("model") == want:
                cfg["qa"]["model"] = mdl
        else:
            PREFLIGHT["note"] = f"{mdl} answered"
            print(f"[loco4coco] model preflight: {mdl} ok")
        return mdl
    PREFLIGHT.update({"checked": True, "model": None, "tried": tried,
                      "note": "NO usable COMPLETE model"})
    print("[loco4coco] *** NO usable COMPLETE model. Tried: "
          + "; ".join(tried) + " ***")
    print("[loco4coco] *** Reflection turns will fall back to cortex exec "
          "(~20s each) and QA will not run. Fix before opening. ***")
    return None


def run_complete(cfg, prompt, kind, job_id=None, model=None, timeout=None):
    """Call Cortex inference directly, in-process, with no agentic loop.

    This skips the ~22-25s fixed floor that `cortex exec` pays on every call
    (process spawn + connection + model attach), which is why the reflection
    turns land in ~1-2s instead of ~24-34s.

    There is genuinely no reasoning to stream here, so the tray is CLEARED
    rather than filled with a decorative spinner: the app only ever shows real
    thinking. On any failure this returns ok=False so the caller can fall back
    to exec (Claude was unavailable via COMPLETE on this region once already).
    """
    c = cfg.get("coco") or {}
    # The last-resort default must be a model that actually answers. This used to
    # read mistral-large2, which has since gone to legacy state and now returns a
    # 400 on every call - so a config missing complete_model would have failed
    # every COMPLETE turn while the slow exec fallback quietly carried the booth.
    mdl = model or c.get("complete_model") or "openai-gpt-5"
    started = time.time()
    write_state({"reasoning": []})
    reply, err, usage = "", "", {}
    for attempt in (1, 2):
        try:
            reply, usage = _complete_once(cfg, mdl, prompt)
            err = ""
            break
        except Exception as e:                                   # noqa: BLE001
            err = str(e)[:200].replace("\n", " ")
            # Expired or dropped session: reconnect once, then give up.
            if attempt == 1 and ("390114" in err or "expired" in err.lower()
                                 or "closed" in err.lower()):
                drop_conn()
                continue
            break

    secs = time.time() - started
    ok = bool(reply) and not err
    log_cost(cfg, kind, secs, usage, ok, "complete", mdl,
             error=err or (None if reply else "empty reply"))
    meta = {"seconds": round(secs, 1), "usage": usage, "ok": ok,
            "transport": "complete", "model": mdl}
    if err:
        meta["error"] = err
    if job_id is not None:
        cur_state = read_state()
        if cur_state.get("job_id") not in (None, job_id):
            return False, "", meta          # superseded by a newer message
    if not ok:
        return False, "", meta
    return True, reply, meta


def _complete_once(cfg, mdl, prompt, conn=None):
    reply, usage = "", {}
    cur = (conn or sf_conn(cfg)).cursor()
    try:
        # The three-argument form, not COMPLETE(model, prompt). The two-arg
        # version returns a bare string with no usage, which is why every
        # COMPLETE turn logged 0 input and 0 output tokens while the agentic
        # turn logged real ones - so "cost per visitor" was only ever
        # counting the Workshop. This form returns choices plus usage.
        cur.execute(
            "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, "
            "ARRAY_CONSTRUCT(OBJECT_CONSTRUCT('role','user',"
            "'content',%s)), OBJECT_CONSTRUCT()) ", (mdl, prompt))
        row = cur.fetchone()
        raw = row[0] if row else ""
        if isinstance(raw, str):
            raw = json.loads(raw) if raw.strip().startswith("{") else {}
        if isinstance(raw, dict):
            ch = (raw.get("choices") or [{}])[0]
            reply = str(ch.get("messages") or ch.get("message") or "").strip()
            u = raw.get("usage") or {}
            usage = {"input_tokens": int(u.get("prompt_tokens") or 0),
                     "output_tokens": int(u.get("completion_tokens") or 0)}
        else:
            reply = str(raw or "").strip()
    finally:
        cur.close()
    return reply, usage


def turn_budget(cfg, loc, timeout=None):
    """Wall-clock ceiling for one turn, in seconds.

    A ceiling is not a nicety. The Library stop has been measured at a 127.3s
    outlier against a 2.2s median, on a stop budgeted for a few seconds, inside a
    five-minute visit with a queue waiting. Past the ceiling the visitor is
    better served by the precomputed answer than by a better sentence.
    """
    coco = cfg.get("coco") or {}
    return int(timeout
               or loc.get("timeout")
               or coco.get("turn_timeout")
               or 60)


def run_turn(cfg, prompt, kind, loc=None, job_id=None, use_mcp=False,
             timeout=None):
    """Dispatch a turn, with four layers of fallback beneath it.

        1. warm agent   `cortex mcp serve`, held open between visitors. ~3.4s.
        2. cortex exec  a cold one-shot process. ~26s, of which ~18s is startup.
        3. COMPLETE     SNOWFLAKE.CORTEX.COMPLETE. Fast, non-agentic.
        4. precomputed  the archetype's own defaults, no model at all.

    Which layer leads depends on what the location asks for. A reflection turn
    wants COMPLETE and does not need judgement; the Workshop wants judgement and
    so leads with the warm agent. Every layer falls through, so a flat network, a
    suspended warehouse or a model outage degrades the visit rather than ending
    it - layer 4 needs nothing but this repo.

    Returns (ok, reply, meta). meta["transport"] records which layer answered,
    which is what makes the cost log worth reading afterwards.
    """
    loc = loc or {}
    budget = turn_budget(cfg, loc, timeout)
    coco = cfg.get("coco") or {}
    wants = (loc.get("transport") or "exec")
    started = time.time()
    meta = None

    def remaining():
        return max(4, budget - int(time.time() - started))

    if wants == "complete":
        ok, reply, meta = run_complete(cfg, prompt, kind, job_id=job_id,
                                       model=loc.get("complete_model"))
        if ok or meta.get("ok"):
            return ok, reply, meta
        if not meta.get("error"):
            return ok, reply, meta          # superseded, not a failure
        print(f"[loco4coco] COMPLETE failed for {kind}, falling back: "
              f"{meta['error']}")

    # Layer 1: the warm agent. Skipped when disabled in config, and skipped
    # quietly when the process will not start - nobody at the stand should ever
    # be able to tell which layer answered.
    elif coco.get("warm_agent", True):
        agent = agent_pool.get_agent(
            connection=coco_connection(), model=coco.get("model"),
            workdir=HERE, log=lambda m: print("[loco4coco] " + m),
            binary=coco.get("binary"))
        if agent.alive() or agent.start():
            ok, reply, meta = agent.ask(prompt, timeout=remaining())
            # log_cost lives inside run_exec and run_complete, so the warm path
            # has to record its own turn or the Workshop - the stop this whole
            # transport exists for - would vanish from the cost log entirely.
            log_cost(cfg, kind, meta.get("seconds") or 0, {}, ok, "warm",
                     coco.get("model"),
                     note=f"prompt_chars={len(prompt)} "
                          f"waited={meta.get('waited')}s call={meta.get('seconds')}s")
            print(f"[loco4coco] warm {kind}: prompt {len(prompt)} chars, "
                  f"waited {meta.get('waited')}s, call {meta.get('seconds')}s")
            if ok:
                return True, reply, meta
            print("[loco4coco] warm agent failed for %s (%s), falling back to "
                  "exec" % (kind, meta.get("error")))

    # Layer 2: a cold one-shot process. Needs ~20s just to start, so it is not
    # worth beginning if the budget cannot cover that.
    if remaining() > 20:
        ok, reply, meta = run_exec(
            cfg, prompt, kind, job_id=job_id, use_mcp=use_mcp,
            timeout=remaining(), effort=loc.get("effort"),
            tools=loc.get("tools"), max_turns=loc.get("max_turns"))
        if ok:
            return ok, reply, meta
        print("[loco4coco] exec failed for %s (%s)"
              % (kind, (meta or {}).get("error")))
    else:
        meta = {"transport": "skipped", "error": "no budget left for exec"}
        print("[loco4coco] skipping exec for %s: %ss of %ss budget used"
              % (kind, int(time.time() - started), budget))

    # Layer 3: COMPLETE, if it was not already the lead.
    if wants != "complete":
        ok3, reply3, meta3 = run_complete(cfg, prompt, kind, job_id=job_id,
                                          model=loc.get("complete_model"))
        if ok3:
            meta3["transport"] = "complete-fallback"
            return True, reply3, meta3

    # Layer 4 is the caller's business: it holds the archetype and its defaults,
    # so it can answer with no model at all. Returning not-ok is how it is asked.
    return False, "", (meta or {"transport": "none",
                                "error": "all layers failed"})


def base_context(cfg, state):
    vis = state.get("visitor") or {}
    # Prose, not the label: this lands in "... at Acme, in {ind}." and the
    # 'other' label would read "in Something else." Empty simply drops the clause.
    ind = industry_prose(cfg, vis.get("industry"))
    ctx = [
        f"You are CoCo, the Snowflake penguin, at the {event_label(cfg)} booth.",
        "This is a five-minute activation. Be warm, brief and concrete.",
        "Never use bullet points, headings, markdown or emoji - your words are "
        "shown in a small speech bubble.",
        "You are already mid-conversation: do not open with a greeting and do "
        "not start by saying their name.",
    ]
    if vis.get("first_name"):
        ctx.append(f"You are speaking to {vis['first_name']}"
                   + (f" from {vis['company']}" if vis.get("company") else "")
                   + (f", in {ind}." if ind else "."))
    return ctx


def event_label(cfg):
    """The event as visitors see it, e.g. 'Hayadata'. Falls back to the SWT
    naming when event.event_name is not set."""
    ev = cfg.get("event") or {}
    return (ev.get("event_name") or "").strip() or \
        f"Snowflake World Tour {ev.get('city', '')}".strip()


def fill(tmpl, cfg, state, **extra):
    vis = state.get("visitor") or {}
    vals = {
        "first_name": vis.get("first_name") or "there",
        "company": vis.get("company") or "your organisation",
        "email": vis.get("email") or "you",
        "industry": industry_prose(cfg, vis.get("industry")) or "your sector",
        "held": ", ".join(state.get("held") or []) or "nothing yet",
        "joined": ", ".join(state.get("joined") or []) or "nothing yet",
        # Captured at the letter stage, so it is already in hand by the time the
        # visitor reaches the library. Every location prompt can lean on it,
        # which is why the later questions can be shorter than they were.
        "problem": (vis.get("problem") or "").strip() or "not stated",
        "platforms": ", ".join(state.get("platforms") or []) or "not stated",
        # Home-stage sovereignty answers, so the reveal can reassure in-region.
        "company_country": (vis.get("company_country") or "").strip() or "not stated",
        "residency": {"country_only": "must stay in their country",
                      "eu": "must stay in the EU", "us_ok": "the US is acceptable too",
                      "unsure": "not yet decided"}.get(
                          _residency_key(vis), "not stated"),
        # Bonus answers collected on the map so far, as "question -> answer"
        # lines. Templates that reference {bonus} get them in the main brief.
        "bonus": bonus_block(state) or "none answered",
    }
    vals.update(extra)
    out = tmpl
    for k, v in vals.items():
        out = out.replace("{" + k + "}", str(v))
    return out


# ------------------------------------------------------------------- industries

def industry_name(cfg, key):
    if not key:
        return ""
    return ((cfg.get("industries") or {}).get(key) or {}).get("name", "")


def industry_prose(cfg, key):
    """The industry as it can appear in a SENTENCE.

    'other' is a legitimate pick whose display label is 'Something else'. That
    label is right on a button and wrong in prose: a visitor who picked it was
    handed a blueprint saying the POC "gives Something else a single trusted
    view", and CoCo offered to "build the most impactful thing for Something
    else". Returning empty here lets each caller fall back to its own natural
    phrase instead.

    Anything that goes into a model prompt or a rendered sentence wants this.
    industry_name() is still correct for the picker, the tiles and telemetry,
    where the label is the label.
    """
    if not key or key == "other":
        return ""
    return industry_name(cfg, key)


def infer_industry(cfg, company):
    """Keyword match on the company name. Cheap, instant, and always
    confirmed by the visitor with one click."""
    c = (company or "").lower()
    if not c:
        return "other"
    for key, hints in INDUSTRY_HINTS.items():
        for h in hints:
            if h in c:
                return key
    return "other"


def options_for(cfg, loc_id, state):
    """The checklist a location shows.

    The Marketplace reads the verified listing index rather than config, so
    the visitor is offered real listings with real providers - Tier 0
    agentic first if it finished in time, then Tier 1 live, then Tier 2
    curated. See listings_for().
    """
    loc = (cfg.get("locations") or {}).get(loc_id) or {}
    src = loc.get("source")
    if not src:
        return []
    ind = (state.get("visitor") or {}).get("industry") or "other"
    if src == "marketplace_index":
        return [{"id": r["global_name"], "label": r["title"],
                 "note": f"{r['provider']} \u00b7 {r['access']}",
                 "url": r["url"], "provider": r["provider"],
                 "access": r["access"]}
                for r in listings_for(cfg, ind, state.get("session_id"), state)]
    industries = cfg.get("industries") or {}
    block = industries.get(ind) or industries.get("other") or {}
    return block.get(src) or []


# ----------------------------------------------------------------- guides index

_guides_cache = None


def load_guides():
    """Parse guides-index.md into {archetype: (title, slug)} primary forks."""
    global _guides_cache
    if _guides_cache is not None:
        return _guides_cache
    try:
        rows = bctx.load(conn=coco_connection())[0].get("guides") or []
        prim = {}
        for r in rows:
            if r.get("is_primary") and r.get("archetype") not in prim:
                prim[r["archetype"]] = (r.get("title"), r.get("slug"))
        if prim:
            _guides_cache = prim
            return _guides_cache
    except Exception:
        pass
    out, current = {}, None
    try:
        with open(GUIDES_PATH, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^##\s+\d+\.\s+(\S+)", line)
                if m:
                    current = m.group(1).strip()
                    continue
                if current and line.startswith("|") and "`" in line:
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    if len(cells) < 2:
                        continue
                    slug = cells[1].strip("` ")
                    note = cells[2] if len(cells) > 2 else ""
                    if "Primary fork" in note and current not in out:
                        out[current] = (cells[0], slug)
    except OSError:
        pass
    _guides_cache = out
    return out


def guide_for(cfg, archetype):
    g = load_guides().get((archetype or "").strip())
    if not g:
        return "", ""
    base = (cfg.get("delivery") or {}).get(
        "guides_base", "https://www.snowflake.com/en/developers/guides/")
    return g[0], base + g[1] + "/"


# ------------------------------------------------- feature documentation links

_features_cache = None


def coco_connection():
    """The Snowflake connection name the booth is configured to use.

    Read from config rather than passed down: the context loaders are called
    from a dozen places, several of them before any request exists, and
    threading a connection through all of them buys nothing.
    """
    try:
        cfg = load_config()
        return ((cfg.get("coco") or {}).get("connection")
                or (cfg.get("snowflake") or {}).get("connection_name") or None)
    except Exception:
        return None


def load_features():
    """Parse feature-docs.md into {feature name: docs url}.

    This is a CLOSED list: the Workshop may only name features from here, which
    is what guarantees every feature in a blueprint carries a working link. A
    name outside the list is dropped rather than rendered bare - omitting one is
    better than shipping a guess in something a visitor keeps.
    """
    global _features_cache
    if _features_cache is not None:
        return _features_cache
    # Prefer the shared context (Snowflake, then the committed bundle) so a
    # laptop that never pulled this branch still gets the current closed list.
    # context.load() falls back to this same markdown, so the try below is the
    # last resort of a last resort rather than a duplicate path.
    try:
        rows = bctx.load(conn=coco_connection())[0].get("features") or []
        if rows:
            _features_cache = {r["name"]: r["docs_url"] for r in rows}
            return _features_cache
    except Exception:
        pass
    out = {}
    try:
        with open(FEATURES_PATH, encoding="utf-8") as f:
            for line in f:
                if not line.startswith("|") or "https://" not in line:
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 2 and cells[1].startswith("https://"):
                    out[cells[0]] = cells[1]
    except OSError:
        pass
    _features_cache = out
    return out


def link_features(names):
    """Return [(name, url)] for names in the closed list, preserving order."""
    docs = load_features()
    seen, out = set(), []
    for n in (names or []):
        key = str(n).strip()
        if key in docs and key not in seen:
            seen.add(key)
            out.append((key, docs[key]))
    return out


ARCHETYPES_PATH = os.path.join(HERE, "archetypes.md")
_arch_cache = None


def load_archetypes():
    """Parse archetypes.md into {key: {features, first_step, pool}}.

    This is the precompute that makes the forge fast. Everything here is
    available with zero inference, so the blocking model call only has to choose
    an archetype and speak, and the background call returns consideration
    INDICES instead of four sentences of prose.
    """
    global _arch_cache
    if _arch_cache is not None:
        return _arch_cache
    out, cur = {}, None
    try:
        with open(ARCHETYPES_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("## "):
                    cur = line[3:].strip()
                    out[cur] = {"features": [], "first_step": "", "pool": []}
                    continue
                if not cur:
                    continue
                m = re.match(r"^\|\s*features\s*\|\s*(.+?)\s*\|$", line)
                if m:
                    out[cur]["features"] = [x.strip() for x in
                                            m.group(1).split(",") if x.strip()]
                    continue
                m = re.match(r"^\|\s*first_step\s*\|\s*(.+?)\s*\|$", line)
                if m:
                    out[cur]["first_step"] = m.group(1).strip()
                    continue
                m = re.match(r"^\d+\.\s+(.+)$", line)
                if m:
                    out[cur]["pool"].append(m.group(1).strip())
    except OSError:
        pass
    _arch_cache = out
    return out


def archetype_defaults(cfg, arche):
    """Everything we can say about an archetype without asking a model."""
    cat = load_archetypes()
    a = cat.get(arche) or cat.get("talk-to-my-data") or {}
    return {
        "features": [n for n, _u in link_features(a.get("features") or [])],
        "first_step": a.get("first_step") or "",
        "pool": a.get("pool") or [],
    }


# ------------------------------------------------------- marketplace listings

_market_cache = None


def _row_dict(r):
    return {
        "title": r.get("title"), "provider": r.get("provider"),
        "access": r.get("access"), "url": r.get("url"),
        "global_name": r.get("global_name"),
        "regions": r.get("regions") or "",
        "reserve": bool(r.get("reserve")),
    }


def _group_by_profile(rows):
    """[listing rows] -> {profile: {industry: [dicts]}}, primary before reserve."""
    prof = {}
    for r in sorted(rows, key=lambda x: (x.get("market_profile") or "uk",
                                         x.get("industry") or "",
                                         1 if x.get("reserve") else 0,
                                         x.get("ordinal") or 0)):
        p = r.get("market_profile") or "uk"
        prof.setdefault(p, {}).setdefault(r.get("industry"), []).append(_row_dict(r))
    return prof


def _all_marketplace():
    """{profile: {industry: [listing dicts]}} from the table/bundle, or the
    marketplace.json source of truth as the offline fallback."""
    global _market_cache
    if _market_cache is not None:
        return _market_cache
    try:
        rows = bctx.load(conn=coco_connection())[0].get("listings") or []
        if rows:
            _market_cache = _group_by_profile(rows)
            return _market_cache
    except Exception:
        pass
    rows = []
    try:
        with open(MARKET_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for profile, inds in (data.get("profiles") or {}).items():
            for industry, block in (inds or {}).items():
                for kind, reserve in (("primary", False), ("reserve", True)):
                    for n, r in enumerate((block or {}).get(kind) or [], start=1):
                        rows.append({**r, "market_profile": profile,
                                     "industry": industry, "ordinal": n,
                                     "reserve": reserve})
    except OSError:
        pass
    _market_cache = _group_by_profile(rows)
    return _market_cache


def load_marketplace(profile="uk"):
    """{industry: [listing dicts]} for the active market profile.

    Only listings in marketplace.json may ever be named. Each industry leads
    with its primary picks and appends any reserves. When the active profile has
    no rows for an industry (fr/de not yet curated), that industry falls back to
    the uk profile, so Paris/Berlin serve the region-filtered uk set rather than
    an empty stall.
    """
    allp = _all_marketplace()
    active = allp.get(profile) or {}
    uk = allp.get("uk") or {}
    out = {}
    for ind in set(active) | set(uk):
        out[ind] = active.get(ind) or uk.get(ind) or []
    return out


_live_cache = {"at": 0, "rows": [], "error": ""}
_live_lock = threading.Lock()

# Tier 0: agentic marketplace search, fired once per visitor from _intake()
# (THE LETTER), racing THE LIBRARY. Single-visitor state, so a session_id
# guard is enough - no per-session dict needed. See listings_for().
_agentic_cache = {"session_id": None, "status": "idle", "rows": [], "at": 0}
_agentic_lock = threading.Lock()


def run_agentic_search(cfg, industry, problem):
    """Find Marketplace listings for what this visitor actually TYPED, restricted
    to the region their event is in.

    Uses `cortex search marketplace` with a cloudRegion filter rather than asking
    an agent in prose. That distinction is the whole reason this works:

      MEASURED, free-form `cortex exec` prompt asking an agent to honour a region:
        ~110-237s per call, and of 91 candidates collected that way, 78 were not
        available in the event's region. Making the region a stated hard
        requirement in the prompt changed nothing.
      MEASURED, the same search with --filter '{"cloudRegion":["..."]}':
        6.8-8.3s, every result already in region, provider names included.

    The region is a FILTER PARAMETER, not something to ask for politely. All three
    event regions were verified answering from this one AWS Frankfurt account,
    because the Marketplace catalogue is global - which is what lets a London
    booth recommend eu-west-2 data while writing its sessions to eu-central-1.

    Returns (rows, usage, ok, seconds). Never raises - every failure mode
    (CLI missing, timeout, bad JSON, empty result) comes back as ([], {}, False, t).
    """
    ac = (cfg.get("marketplace") or {}).get("agentic") or {}
    c = cfg.get("coco") or {}
    ind_name = industry_name(cfg, industry) or industry
    # Seeded from the EVENT LOCATION, never the booth account.
    # event.marketplace_region is the
    # EVENT's region (London = eu-west-2); the account's own region is whatever
    # the DataOps pool assigned and is irrelevant here. The
    # visitor never imports at the booth - they open the links later from their own
    # account - so what matters is availability where THEY are.
    ev = cfg.get("event") or {}
    locality = (cfg.get("marketplace") or {}).get("locality") or {}
    country = locality.get(ev.get("market_profile") or "uk") or ""
    region = region_short(cfg)

    query = f"{problem} for {ind_name}" + (f" in {country}" if country else "")
    cmd = [c.get("binary", "cortex"), "search", "marketplace", query,
           "--max-results", str(int(ac.get("max_results", 10)))]
    if region:
        cmd += ["--filter", json.dumps({"cloudRegion": [region]})]
    conn = ac.get("connection") or (cfg.get("snowflake") or {}).get("connection_name")
    if conn:
        cmd += ["-c", conn]
    timeout = float(ac.get("timeout_seconds", 45))
    started = time.time()
    rows, ok = [], False
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=HERE)
        payload = json.loads(p.stdout or "{}")
        if "error" not in payload:
            rows = _parse_marketplace_results(payload.get("results") or "")
            ok = True
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    except Exception:                                            # noqa: BLE001
        pass
    seconds = time.time() - started
    log_cost(cfg, "marketplace_agentic", seconds, {}, ok, "search")
    return rows, {}, ok, seconds


# "  3. Some Title (MARKETPLACE LISTING - DATA)" then indented "Key: value" lines.
_MP_ENTRY = re.compile(
    r"^\s*\d+\.\s+(?P<title>.+?)\s+\(MARKETPLACE LISTING - (?P<kind>[A-Z ]+)\)\s*$")
_MP_FIELD = re.compile(r"^\s+(?P<key>Provider|Subtitle|Global Name|URL):\s*(?P<val>.+?)\s*$")


def _parse_marketplace_results(text):
    """Turn the search's numbered text output into listing rows.

    APPLICATION listings are dropped: a visitor is being handed data to join to
    their own, and an app is not that. Anything without a global name is dropped
    too - a link we cannot build is worse than one fewer suggestion.
    """
    out, cur = [], None

    def flush(entry):
        if not entry or entry.get("kind") == "APPLICATION":
            return
        gn = (entry.get("global_name") or "").strip()
        if not gn or not entry.get("title"):
            return
        out.append({
            "title": entry["title"],
            "provider": (entry.get("provider") or "Snowflake Marketplace").strip(),
            "access": "Found for what you asked for",
            "url": (entry.get("url") or "").strip() or LISTING_URL + gn,
            "global_name": gn,
            "regions": "",
        })

    for line in text.splitlines():
        m = _MP_ENTRY.match(line)
        if m:
            flush(cur)
            cur = {"title": m.group("title").strip(), "kind": m.group("kind").strip()}
            continue
        if cur:
            f = _MP_FIELD.match(line)
            if f:
                cur[f.group("key").lower().replace(" ", "_")] = f.group("val")
    flush(cur)
    return out


def start_agentic_search(cfg, session_id, industry, problem):
    """Fire-and-forget: called from _intake() the moment THE LETTER is
    submitted. Runs on a daemon thread so the Letter response is never
    delayed. See run_agentic_search() and listings_agentic()."""
    ac = (cfg.get("marketplace") or {}).get("agentic") or {}
    if not ac.get("enabled") or not (problem or "").strip():
        return

    def job():
        with _agentic_lock:
            _agentic_cache.update(session_id=session_id, status="pending",
                                  rows=[], at=time.time())
        rows, _usage, ok, _s = run_agentic_search(cfg, industry, problem)
        with _agentic_lock:
            if _agentic_cache.get("session_id") != session_id:
                return              # a new visitor started while we ran
            _agentic_cache.update(
                status="ready" if (ok and rows) else "failed",
                rows=rows, at=time.time())

    threading.Thread(target=job, daemon=True).start()


def listings_agentic(cfg, session_id):
    """Tier 0 read path. Ready only if this visitor's call finished, matched
    the deny list Tier 1 already uses, and actually returned rows."""
    if not session_id:
        return []
    with _agentic_lock:
        if (_agentic_cache.get("session_id") != session_id
                or _agentic_cache.get("status") != "ready"):
            return []
        rows = list(_agentic_cache.get("rows") or [])
    mk = cfg.get("marketplace") or {}
    deny = [w.lower() for w in (mk.get("exclude") or [])]
    return [r for r in rows
            if not any(d in (r.get("title") or "").lower() for d in deny)]


def region_short(cfg):
    """The EVENT's region, short form. Never the account's - see resolve_venue."""
    ev = cfg.get("event") or {}
    raw = ev.get("marketplace_region") or ev.get("region") or ""
    return raw.strip().split(".")[-1]


def refresh_live_listings(cfg, force=False):
    """Pull the real Marketplace catalogue once, then serve every visitor from memory.

    No private preview needed: SHOW AVAILABLE LISTINGS piped through RESULT_SCAN
    is enough. Measured on a London-region account: 4327 listings, 799 available in
    AWS_EU_WEST_2, 188 of those importable. One query per server start rather
    than one per visitor, so the stall stays instant.

    Note SHOW ... LIKE does NOT filter on title, which is why the title match is
    done here in SQL and not in the SHOW.
    """
    mk = cfg.get("marketplace") or {}
    ttl = float(mk.get("cache_minutes", 120)) * 60
    with _live_lock:
        if not force and _live_cache["rows"] and time.time() - _live_cache["at"] < ttl:
            return _live_cache["rows"], ""
        region = region_short(cfg)
        if not region:
            _live_cache["error"] = "event.marketplace_region is not set"
            return [], _live_cache["error"]
        try:
            cur = sf_conn(cfg).cursor()
            try:
                cur.execute("SHOW AVAILABLE LISTINGS")
                cur.execute(
                    'SELECT "title", "global_name", "profile", "regions" '
                    "FROM TABLE(RESULT_SCAN(LAST_QUERY_ID(-1))) "
                    'WHERE "regions" LIKE %s AND "is_ready_for_import" = \'true\'',
                    (f"%{region}%",))
                rows = [{"title": t, "global_name": g, "profile": p,
                         "regions": r,
                         # is_ready_for_import was already filtered to true
                         "access": "Ready to import",
                         "url": LISTING_URL + g}
                        for (t, g, p, r) in cur.fetchall()]
            finally:
                cur.close()
        except Exception as e:                                   # noqa: BLE001
            _live_cache["error"] = str(e)[:200].replace("\n", " ")
            return [], _live_cache["error"]
        _live_cache.update({"at": time.time(), "rows": rows, "error": ""})
        return rows, ""


def listings_live(cfg, industry):
    """Match the live catalogue against this industry's keyword avenues.

    Pinned listings (verified by global_name) come first so a booth always leads
    with something known-good, then keyword matches fill the rest.
    """
    rows, err = refresh_live_listings(cfg)
    if not rows:
        return [], err or "no live listings"
    mk = cfg.get("marketplace") or {}
    words = [w.lower() for w in
             ((mk.get("industry_keywords") or {}).get(industry)
              or (mk.get("industry_keywords") or {}).get("other") or [])]
    pins = (mk.get("pinned") or {}).get(industry) or []
    deny = [w.lower() for w in (mk.get("exclude") or [])]

    def blocked(title):
        t = (title or "").lower()
        return any(d in t for d in deny)

    by_gn = {r["global_name"]: r for r in rows}
    names = mk.get("provider_names") or {}

    def named(r):
        # SHOW returns an opaque profile id, not a display name, so a known
        # provider is named from config and anything else is labelled honestly.
        r = dict(r)
        r["provider"] = names.get(r.get("profile") or "", "Snowflake Marketplace")
        return r

    # Geographic relevance. MEASURED: a public-sector visitor at a UK stand was
    # offered "India Economic Monitor" and "Weather Data - All International
    # Postal Codes" because keyword matches were taken in raw catalogue order
    # with no notion of where the event is. Config-driven so moving the event to
    # another country is a config change, not a code change.
    # Per market_profile (uk/fr/de): a Paris or Frankfurt event weights local
    # datasets up, not UK ones. uk falls back to the base "geo" block.
    prof = ((cfg.get("event") or {}).get("market_profile")) or "uk"
    geo = ((mk.get("geo_by_profile") or {}).get(prof)) or (mk.get("geo") or {})
    prefer = [w.lower() for w in (geo.get("prefer") or [])]
    demote = [w.lower() for w in (geo.get("demote") or [])]
    # Per-industry veto: "postcode" is a legitimate public-sector keyword but it
    # matched "Postcode Sector Weather Forecasts", which is how three of the six
    # public-sector picks ended up being Met Office weather products.
    avoid = [w.lower() for w in
             ((mk.get("industry_avoid") or {}).get(industry) or [])]

    def relevance(r):
        """Higher is better. (geo_score, keyword_hits) decides the order."""
        hay = ((r.get("title") or "") + " " +
               (names.get(r.get("profile") or "", ""))).lower()
        g = sum(2 for w in prefer if w in hay) - sum(3 for w in demote if w in hay)
        k = sum(1 for w in words if w in hay)
        return (g, k)

    out, seen = [], set()
    for gn in pins:
        r = by_gn.get(gn)
        if r:
            out.append(named(r))
            seen.add(gn)
    cands = []
    for r in rows:
        if r["global_name"] in seen:
            continue
        t = (r["title"] or "").lower()
        if not any(w in t for w in words) or blocked(t):
            continue
        if any(a in t for a in avoid):
            continue
        cands.append(r)
    # Deterministic: sort by relevance, then title, so the same catalogue always
    # produces the same stall. Ties used to fall out in whatever order SHOW
    # happened to return, which made the booth unpredictable between visitors.
    cands.sort(key=lambda r: (relevance(r), (r.get("title") or "").lower()),
               reverse=True)
    for r in cands:
        out.append(named(r))
        seen.add(r["global_name"])
    return out, ""


def _residency_key(vis):
    """The STRICTEST residency rule the visitor picked.

    The home gate is multi-select - a group can be tied to its home country for
    some data and relaxed about the rest - so this field is now a comma list like
    "country_only, us_ok". Every consumer here wants the binding constraint, and
    exact-matching the whole string silently returned "not stated" and dropped
    the sovereignty section from the blueprint.
    """
    got = (vis.get("residency") or "").lower()
    for k in ("country_only", "eu", "us_ok", "unsure"):
        if k in got:
            return k
    return ""


def listings_for(cfg, industry, session_id=None, state=None):
    """Tier chain: agentic first if it's ready, live catalogue next, curated
    index as the booth-safe net.

    Tier 0 agentic  - cortex exec calling the marketplace-search skill,
                     started from _intake() at THE LETTER and racing THE
                     LIBRARY. Personalised to what this visitor actually
                     typed, not just their industry bucket. Used only if it
                     finished in time - see listings_agentic().
    Tier 1 live     - real, current, region-filtered, importable.
    Tier 2 curated  - marketplace-index.md, used when live is cold, fails, or
                     is too thin to fill a stall. Offline safe and quality
                     checked.

    "Agentic search on the Snowflake Marketplace" (PrPr) was verified working
    on a London-region account on 2026-08-23 via both Snowsight's Discover tab AND
    `cortex exec` calling the same marketplace-search skill from the CLI -
    the latter is what Tier 0 actually calls. It never blocks a visitor: if
    it hasn't finished, errored, or `marketplace.agentic.enabled` is false,
    this falls straight through to Tier 1 then Tier 2 exactly as before.
    """
    loc = ((cfg.get("locations") or {}).get("marketplace") or {})
    mk = cfg.get("marketplace") or {}
    want = int(mk.get("min_live_results", 3))
    # The stall is ALWAYS the deterministic curated six (optionally the live
    # catalogue). Agentic results deliberately do NOT replace them any more:
    # they are the 7th option, merged into the takeaway document at the postbox
    # when the visitor typed something of their own. A stall that changed shape
    # depending on whether a background call had landed was unpredictable in
    # public, which is the one thing a branded stand cannot be.
    if (loc.get("discovery") or "manual") == "live":
        live, err = listings_live(cfg, industry)
        if len(live) >= want:
            _live_cache["tier"] = "live"
            return live[:max(want, 6)]
        print(f"[loco4coco] live marketplace thin for {industry} "
              f"({len(live)} hits{', ' + err if err else ''}), using curated index")
    _live_cache["tier"] = "curated"
    return listings_curated(cfg, industry, state)


def _problem_tokens(state):
    """Content words from what the visitor actually typed, for matching titles."""
    vis = ((state or {}).get("visitor") or {})
    txt = " ".join([str(vis.get("problem") or ""),
                     str(vis.get("company") or "")]).lower()
    stop = {"the", "and", "our", "for", "with", "from", "into", "that", "this",
            "have", "has", "are", "was", "we", "us", "to", "of", "in", "on",
            "it", "is", "a", "an", "by", "at", "be", "do", "not", "but",
            "data", "snowflake", "want", "need", "lot", "lots", "time",
            # Words that appear in LISTING TITLES as generic furniture rather
            # than as subject matter. Without these, a problem mentioning "a
            # system that flags urgent emails" scored a hit on "Industry
            # Classification Systems" and outranked the weather listings an
            # energy visitor was actually there for.
            "system", "systems", "sample", "feature", "features", "service",
            "services", "analytics", "insight", "insights", "bundle", "global",
            "dataset", "datasets", "information", "level", "would", "like",
            "hours", "spend", "identify", "immediate", "attention", "automatic",
            "automatically", "manually", "review", "reviewing", "critical"}
    out = set()
    for w in re.split(r"[^a-z0-9]+", txt):
        # Four characters or more: three-letter fragments match too much.
        if len(w) > 4 and w not in stop:
            out.add(w)
            if w.endswith("s"):
                out.add(w[:-1])
    return out


def _held_themes(cfg, state):
    """What the visitor's own shelves imply about the joins that would help.

    The industry bucket says what sector they are in; this says what SHAPE their
    data is. Someone holding clinical notes wants document-shaped joins; someone
    holding estates and rotas wants geography. Without this, both were offered
    the same six, which is the single biggest reason the stall felt repetitive.
    """
    mk = cfg.get("marketplace") or {}
    rules = mk.get("source_themes") or {}
    if not rules:
        return set()
    held = " ".join(str(x) for x in ((state or {}).get("held") or [])).lower()
    if not held.strip():
        return set()
    out = set()
    for theme, words in rules.items():
        for w in (words or []):
            if str(w).lower() in held:
                out.add(theme)
                break
    return out


def _bucket_only_for(cfg, profile):
    """Listings that may not be borrowed into another industry, for this profile.

    Keyed by market profile, because the protection is per-listing and a listing
    only exists in the profiles it was curated into. It was a flat list of London
    global names once, which meant Paris and Berlin ran with NO protection at all
    while the config read as though they were covered.

    A flat list is still accepted and treated as uk, so an older config keeps
    working rather than silently losing London's protection too.
    """
    raw = (cfg.get("marketplace") or {}).get("bucket_only") or []
    if isinstance(raw, dict):
        return set(raw.get(profile) or [])
    return set(raw) if profile == "uk" else set()


def listings_curated(cfg, industry, state=None):
    """Curated listings, ordered by what this visitor typed, filtered to region.

    Two visitors in the same industry used to get an IDENTICAL stall: this
    returned a fixed six in file order, and since `marketplace.agentic.enabled`
    is false and `locations.marketplace.discovery` is "manual", this is the only
    tier that ever runs. So the stall was a function of the industry bucket
    alone, and the same six appeared over and over.

    Two changes, neither of which invents a listing - every row here is a real,
    checked Marketplace entry and that discipline is not worth trading for
    variety:

      1. The pool is widened by BORROWING from other buckets. A visitor whose
         problem mentions weather, or property, or company registrations is
         better served by the real listing that matches it than by the sixth-best
         pick from their own sector. Their own bucket still leads, via a base
         score.
      2. Ties rotate on the session id, so consecutive visitors in one bucket do
         not see the same order.

    The region filter PREFERS in-region listings rather than deleting the rest -
    see the bottom of this function for why deleting made the stall sparse.
    """
    profile = ((cfg.get("event") or {}).get("market_profile")) or "uk"
    market = load_marketplace(profile)
    own = market.get(industry) or market.get("other") or []
    toks = _problem_tokens(state)
    themes = _held_themes(cfg, state)
    tagmap = (cfg.get("marketplace") or {}).get("listing_themes") or {}
    # Sector-specific reference data does not travel. Borrowing is for listings
    # that help anyone; a biomedical corpus is not one of them.
    own_gns = {r.get("global_name") for r in own}
    bucket_only = _bucket_only_for(cfg, profile)
    seen, scored = set(), []
    for bucket, rows in [(industry, own)] + sorted(market.items()):
        for r in rows:
            gn = r.get("global_name") or r.get("title")
            if gn in seen:
                continue
            if gn in bucket_only and gn not in own_gns:
                continue
            seen.add(gn)
            hay = (str(r.get("title") or "") + " "
                   + str(r.get("provider") or "")).lower()
            hits = sum(1 for t in toks if t in hay)
            # PRECEDENCE, not a nudge. `own` holds this bucket's primaries AND
            # its reserves, so a bonus of 2 for "in my bucket" gave a reserve the
            # same standing as a primary - and one theme overlap (worth 2) was
            # then enough to push a curated pick out of its own stall. Measured
            # before this change: energy and financial each served only 3 of their
            # 6 primaries, the rest displaced by reserves. The curated six are the
            # deliberate, reviewed, region-checked answer, so ranking may reorder
            # WITHIN them but must never drop one in favour of a reserve or a
            # borrowed listing. Reserves and borrows compete only for slots the
            # primaries do not fill.
            own_primary = (r in own) and not r.get("reserve")
            base = 1000 if own_primary else (2 if r in own else 0)
            # Theme overlap is weighted below a problem-word hit but above the
            # home-bucket bonus, so what they HOLD outranks what sector they are
            # in without ever outranking what they actually said.
            overlap = len(themes & set(tagmap.get(gn) or []))
            scored.append((base + hits * 3 + overlap * 2, hits, r))
    # Stable per-session shuffle for the ties, so the stall changes visitor to
    # visitor without becoming random - the same session always sees the same
    # stall, which matters when the panel is reopened.
    salt = str(((state or {}).get("session_id")) or "")
    def key(item):
        s, hits, r = item
        h = hashlib.md5((salt + str(r.get("global_name") or "")).encode()).hexdigest()
        return (-s, h)
    rows = [r for _, _, r in sorted(scored, key=key)]
    # Region PREFERS, it does not DELETE. This used to hard-drop anything not
    # offered in the event's region, and that is what made the stall look sparse:
    # MEASURED against the live catalogue, only 28 of the 48 curated UK picks are
    # importable in eu-west-2, so media arrived at the stall with ONE dataset.
    #
    # The visitor never imports anything at the booth. They tick datasets, those
    # become links in a document, and they open them later from their own account
    # - which may be in any region at all. So a listing that is relevant, real and
    # SE-reviewed belongs on the stall even if this particular region cannot
    # attach it; what it must never be is invented. In-region picks lead, the rest
    # fill the six, and the document is always six real datasets rather than one.
    region = region_short(cfg)
    if not region:
        return rows[:6]
    short = region

    def local(r):
        regs = (r.get("regions") or "").strip()
        # "ALL" means every region. MEASURED: this was being substring-matched
        # like a region list, so it matched nothing and silently dropped the
        # listing - which is how public sector shipped 5 picks instead of 6 and
        # lost Ordnance Survey Boundary Line, one of the best UK public-sector
        # datasets on the Marketplace. A truncated "+N" list means we cannot
        # prove absence, so it counts as local.
        return regs.upper() == "ALL" or short in regs or "+" in regs

    return ([r for r in rows if local(r)] + [r for r in rows if not local(r)])[:6]


# --------------------------------------------------------------- the CoCo prompt

def build_coco_prompt(cfg, state):
    """The thing they paste into CoCo on their own free trial.

    Six parts, built deterministically from what they
    actually told us: it must never invent table or column names, so it tells
    CoCo to inspect and ask instead.
    """
    vis = state.get("visitor") or {}
    poc = state.get("poc") or {}
    ind = industry_prose(cfg, vis.get("industry")) or "our sector"
    held = state.get("held") or []
    joined = state.get("joined") or []
    listings = state.get("joined_listings") or []

    lines = [
        f"You are helping me build a proof of concept in Snowflake. I work in "
        f"{ind}" + (f" at {vis.get('company')}" if vis.get("company") else "") + ".",
        "",
        "THE PROBLEM",
        (vis.get("problem") or "").strip()
        or "(I described this at the booth but did not write it down.)",
        "",
        "THE DATA",
        "I have not loaded anything yet. The data I hold is:",
    ]
    lines += [f"  - {h}" for h in (held or ["(to be confirmed)"])]
    plats = state.get("platforms") or []
    if plats:
        lines.append("It is sitting on: " + ", ".join(plats)
                     + ". Tell me the shortest route to get it into Snowflake "
                       "before you build anything.")
    if listings:
        lines.append("I also want to attach these Snowflake Marketplace listings:")
        for r in listings:
            lines.append(f"  - {r['title']} (provider: {r['provider']}, "
                         f"listing {r['global_name']})")
        extra = [j for j in joined if j not in {r["title"] for r in listings}]
        for j in extra:
            lines.append(f"  - {j} (I will need to find a suitable listing)")
    elif joined:
        lines.append("I also want to join these categories of Marketplace data:")
        lines += [f"  - {j}" for j in joined]
    lines += [
        "",
        "BUILD THIS",
        poc.get("poc_name") or "A working proof of concept",
    ]
    if poc.get("summary"):
        lines.append(poc["summary"])
    feats = [n for n, _u in link_features(poc.get("features"))]
    if feats:
        lines.append("Use these Snowflake features: " + ", ".join(feats) + ".")

    title, url = poc.get("guide_title"), poc.get("guide_url")
    if title and url:
        lines += ["", "START FROM", f"Follow this guide as the base: {title}", url]

    told = bonus_block(state)
    if told:
        lines += ["", "WHAT ELSE I TOLD YOU", told.replace("- ", "  - ")]
    lines += [
        "",
        "CONSTRAINTS",
        "Do not invent table or column names. Inspect what exists first, and "
        "ask me when something is ambiguous.",
        "Work in small steps and show me the SQL before you run anything that "
        "creates or changes objects.",
        "Keep everything in one database and schema so it is easy to drop afterwards.",
        "",
        "FIRST STEP",
        poc.get("first_step") or "Tell me what you need from me to get started, "
        "then set up the database and schema.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------- blueprint

# HTML output is gone entirely. The email body (blueprint_html) went with
# the outbox, and the on-screen page (blueprint_page) plus its /blueprint
# route went on 2026-08-27: the QR to the presigned .docx is the only
# delivery surface, so there is one artifact to review, not two.


INTEGRATION_PATHS = {
    "Microsoft / Azure": (
        "Openflow has a first-party connector for Azure Blob Storage and "
        "SQL Server. For Fabric or OneLake, register the Iceberg tables through "
        "a catalog integration and query them in place - no copy.",
        "https://docs.snowflake.com/en/user-guide/data-load-azure"),
    "AWS": (
        "Point an external stage at the S3 bucket with a storage integration, "
        "then Snowpipe for continuous load. If the data is already Iceberg in "
        "Glue, use a catalog integration and leave it where it is.",
        "https://docs.snowflake.com/en/user-guide/data-load-s3"),
    "Google Cloud": (
        "A storage integration over the GCS bucket plus an external stage. "
        "BigQuery data moves cleanly as Parquet exported to GCS, or through "
        "Openflow if you need it on a schedule.",
        "https://docs.snowflake.com/en/user-guide/data-load-gcs"),
    "Oracle": (
        "Openflow's Oracle connector does change data capture, so you get an "
        "ongoing replica rather than a nightly dump. Start with the handful of "
        "tables the proof of concept actually reads.",
        "https://other-docs.snowflake.com/en/connectors"),
    "SAP": (
        "Either the SAP connector for Snowflake, or SAP Business Data Cloud "
        "sharing the data as Iceberg that Snowflake reads without a copy. The "
        "second route is usually faster to stand up.",
        "https://docs.snowflake.com/en/user-guide/tables-iceberg"),
    "On-premise / our own servers": (
        "Openflow can run inside your network and push out, so nothing has to "
        "be exposed inbound. For a first proof of concept, a one-off bulk load "
        "of a representative extract is usually enough.",
        "https://docs.snowflake.com/en/user-guide/data-load-local-file-system"),
    "SaaS apps (Salesforce, Workday, etc.)": (
        "Openflow has connectors for the common SaaS sources, and the "
        "Marketplace carries some of them as ready-made shares. Check the "
        "Marketplace first - it is the cheaper answer when it exists.",
        "https://other-docs.snowflake.com/en/connectors"),
    "Already in Snowflake": (
        "Nothing to move. Point the proof of concept at the existing tables "
        "and spend the saved time on the model and the interface instead.",
        "https://docs.snowflake.com/en/guides-overview-queries"),
    "Not sure yet": (
        "Worth ten minutes with whoever owns the source before you build. The "
        "answer changes the effort more than any other decision here.",
        "https://docs.snowflake.com/en/guides-overview-loading-data"),
}

# "Already in Snowflake" is no longer an exclusive answer: an estate can be part
# in Snowflake and part in Oracle, and forcing one or the other made visitors
# misrepresent it. But the standalone route says "nothing to move", which
# contradicts the named-source routes printed beside it. So when it is combined,
# it prints THIS instead - the same fact, phrased as a starting point rather
# than a conclusion.
PLATFORM_COMBINED = {
    "Already in Snowflake": (
        "Some of it is already here, so the proof of concept starts on those "
        "tables today while the rest lands alongside them.",
        "https://docs.snowflake.com/en/guides-overview-queries"),
}


def normalise_platforms(cfg, plats):
    """Resolve a raw platform selection into a predictable, non-contradictory set.

    Of the possible pairs, some are logically contradictory and some print
    "Openflow" twice. "Not sure yet" plus AWS would say "go and ask whoever owns
    the source" next to a concrete S3 route. At a Snowflake-branded event the
    document has to be defensible, so the contradiction is resolved here rather
    than left to chance.

    "Already in Snowflake" is NOT exclusive. A real estate is often part in
    Snowflake and part elsewhere, so it survives alongside a named source and
    integration_paths() prints its combined phrasing instead.

    Rules, in order:
      1. Keep only chips the config actually offers, de-duplicated.
      2. Restore config order, so the same taps always produce the same document
         regardless of the order they were tapped in.
      3. An `exclusive` chip ("go and ask") survives only if NOTHING else was
         picked. A named source is actionable and wins.
      4. Cap at `max_routes` so the ingestion section stays a plan, not a list.

    The UI enforces 3 on tap as well; this is the backstop, because the document
    is the thing that leaves the stand.
    """
    cfgp = (cfg.get("platforms") or {})
    offered = list(cfgp.get("options") or [])
    excl = set(cfgp.get("exclusive") or [])
    cap = int(cfgp.get("max_routes") or 4)

    seen, kept = set(), []
    for p in (plats or []):
        if p in offered and p not in seen:
            seen.add(p)
            kept.append(p)
    kept.sort(key=offered.index)                  # deterministic output

    named = [p for p in kept if p not in excl]
    if named:
        kept = named                              # a real source beats "not sure"
    else:
        kept = kept[:1]                           # at most one exclusive answer
    return kept[:cap]


def sovereignty_lines(cfg, state):
    """The four in-region pillars, personalised to the visitor's residency.

    Precompute, not generation: the prose is ours (config.sovereignty.pillars),
    so it is consistent between visitors and cannot name a control that does not
    exist. Returns [] when we have nothing to personalise against, so a visitor
    who skipped the question does not get a generic lecture.
    """
    vis = state.get("visitor") or {}
    residency = _residency_key(vis)
    country = (vis.get("company_country") or "").strip()
    if not residency and not country and not state.get("platforms"):
        return []
    region = {"country_only": country or "your country",
              "eu": "the EU",
              "us_ok": "your chosen region",
              "unsure": "your chosen region"}.get(residency, "your chosen region")
    pillars = (cfg.get("sovereignty") or {}).get("pillars") or {}
    order = ["data", "models", "marketplace", "governance"]
    return [pillars[k].replace("{region}", region) for k in order if pillars.get(k)]


def integration_paths(state):
    """The route into Snowflake for each platform the visitor named.

    A platform with a combined phrasing uses it whenever it is not the only
    answer, so "already here" reads as a starting point beside the other routes
    rather than contradicting them.
    """
    out = []
    picked = normalise_platforms(load_config(), state.get("platforms"))
    for p in picked:
        hit = PLATFORM_COMBINED.get(p) if len(picked) > 1 else None
        hit = hit or INTEGRATION_PATHS.get(p)
        if hit:
            out.append((p, hit[0], hit[1]))
        else:
            out.append((p, "Check the connector catalogue for this source, or "
                        "land an extract on a stage as Parquet to begin with.",
                        "https://other-docs.snowflake.com/en/connectors"))
    return out


# The .docx is a good thing to forward to a colleague and a poor thing to read on
# a phone: Word on iOS wants a download and an app switch before anyone sees a
# word of it, and a QR code at a booth is scanned by a phone every single time.
# So the same state renders twice - a page for reading now, a document for
# keeping. Mobile-first is not a nicety here, it is the primary case.
BLUEPRINT_CSS = """
/* The blueprint is the one light surface in the game, and deliberately so:
   it is a printable handout, and #2C3444 across A4 is a ruined cartridge. The
   paper field (#F4F8FB) and the card white are therefore the only two tones
   here that are not palette values - every ink, rule, and accent below is. */
:root { color-scheme:light; }
* { box-sizing:border-box; }
body { margin:0; padding:0 18px 64px;
  font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,
       Arial,sans-serif;
  color:#191D27; background:#F4F8FB;
  -webkit-text-size-adjust:100%; }
.wrap { max-width:34rem; margin:0 auto; }
header { padding:26px 0 18px; }
.mark { display:block; margin-bottom:14px; }
h1 { font-size:1.55rem; line-height:1.25; margin:0 0 6px; color:#166184;
  letter-spacing:-.01em; }
.for { color:#465C6D; font-size:.95rem; margin:0; }
.lede { font-size:1.06rem; color:#2C3444; margin:16px 0 0; }
section { background:#fff; border:1px solid #DEE5EB; border-radius:12px;
  padding:16px 16px 18px; margin:14px 0; }
h2 { font-size:.78rem; letter-spacing:.10em; text-transform:uppercase;
  color:#29B5E8; margin:0 0 10px; }
ul { margin:0; padding-left:1.15rem; }
li { margin:0 0 9px; }
li b { color:#166184; }
.note { display:block; color:#465C6D; font-size:.88rem; margin-top:2px; }
a { color:#166184; overflow-wrap:anywhere; }
/* Tap targets, not links: a 14px underline on a phone is a coin toss. */
.btn { display:block; text-align:center; text-decoration:none;
  padding:15px 18px; border-radius:10px; font-weight:700; font-size:1rem;
  background:#29B5E8; color:#191D27; border:none; width:100%;
  cursor:pointer; font-family:inherit; }
.btn.alt { background:#fff; color:#166184; border:2px solid #29B5E8;
  margin-top:10px; }
pre { margin:0; padding:14px; background:#191D27; color:#DEE5EB;
  border-radius:10px; font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
  white-space:pre-wrap; overflow-wrap:anywhere; max-height:15rem;
  overflow-y:auto; }
ol { margin:0; padding-left:1.3rem; }
footer { text-align:center; color:#465C6D; font-size:.82rem; padding:22px 0 0; }
@media (prefers-color-scheme:dark) {
  body { background:#2C3444; color:#DEE5EB; }
  section { background:#35424C; border-color:#465C6D; }
  h1,li b { color:#29B5E8; } .for,.note { color:#8FA6B8; }
  .lede { color:#DEE5EB; } a { color:#67B5ED; }
  .btn.alt { background:transparent; color:#67B5ED; }
}
"""

SNOWFLAKE_SVG = (
    '<svg class="mark" width="34" height="34" viewBox="-50 -50 100 100" '
    'aria-hidden="true">'
    + "".join(
        '<g transform="rotate(%d)">'
        '<path d="M15 -4 L36 -4 L36 -15 L50 0 L36 15 L36 4 L15 4 Z" '
        'fill="#29B5E8"/></g>' % (i * 60) for i in range(6))
    + '<path d="M0 -15 L15 0 L0 15 L-15 0 Z" fill="#29B5E8"/>'
      '<path d="M0 -6 L6 0 L0 6 L-6 0 Z" fill="#F4F8FB"/></svg>')


def blueprint_docx(cfg, state):
    """Generate a real .docx with python-docx. Returns a path, or "" if the
    library is missing - a missing document must not block the email."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.oxml.ns import qn
    except ImportError:
        return ""
    vis = state.get("visitor") or {}
    poc = state.get("poc") or {}
    doc = Document()

    # Snowflake brand, not the python-docx default. The template ships Times New
    # Roman, which is the first thing a visitor notices about a document they
    # keep. Inter is the Snowflake typeface; the fallbacks cover a machine that
    # does not have it, and eastasia must be set too or Word substitutes a serif.
    BRAND_BLUE = RGBColor(0x29, 0xB5, 0xE8)
    HEADING_BLUE = RGBColor(0x11, 0x56, 0x7F)
    BODY_INK = RGBColor(0x1A, 0x24, 0x2E)

    def set_font(style_obj, name, size=None, color=None, bold=None):
        f = style_obj.font
        f.name = name
        if size is not None:
            f.size = size
        if color is not None:
            f.color.rgb = color
        if bold is not None:
            f.bold = bold
        rpr = style_obj.element.get_or_add_rPr()
        rfonts = rpr.get_or_add_rFonts()
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rfonts.set(qn(attr), name)

    FONT = "Inter"
    set_font(doc.styles["Normal"], FONT, Pt(10.5), BODY_INK)
    for name, size in (("Title", Pt(24)), ("Heading 1", Pt(15)),
                       ("Heading 2", Pt(12.5)), ("List Bullet", Pt(10.5))):
        try:
            st = doc.styles[name]
        except KeyError:
            continue
        colour = BRAND_BLUE if name == "Title" else (
            HEADING_BLUE if name.startswith("Heading") else BODY_INK)
        set_font(st, FONT, size, colour, True if name != "List Bullet" else None)
        st.paragraph_format.space_before = Pt(10 if name != "Title" else 0)
        st.paragraph_format.space_after = Pt(4)

    h = doc.add_heading(poc.get("poc_name") or "Your proof of concept", level=0)
    for run in h.runs:
        run.font.color.rgb = BRAND_BLUE
        run.font.name = FONT
    doc.add_paragraph(
        f"Prepared for {vis.get('first_name') or 'you'}"
        + (f" at {vis.get('company')}" if vis.get("company") else "")
        + f" - {event_label(cfg)}.")
    if poc.get("summary"):
        doc.add_paragraph(poc["summary"])

    def bullets(title, items):
        if not items:
            return
        doc.add_heading(title, level=2)
        for i in items:
            doc.add_paragraph(str(i), style="List Bullet")

    bullets("Data you already hold", state.get("held") or [])

    paths = integration_paths(state)
    if paths:
        doc.add_heading("Getting that data into Snowflake", level=2)
        for plat, how, url in paths:
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(plat).bold = True
            p.add_run(f"\n{how}\n{url}")

    bullets("Sovereignty and security", sovereignty_lines(cfg, state))

    problem = ((state.get("visitor") or {}).get("problem") or "").strip()
    if problem:
        doc.add_heading("The problem you described", level=2)
        doc.add_paragraph(problem)

    told = ((state.get("bonus") or {}).get("answered")) or []
    if told:
        doc.add_heading(((cfg.get("bonus") or {}).get("copy") or {}).get(
            "docx_heading") or "What you told CoCo", level=2)
        for a in told:
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(a["question"] + " ").bold = True
            p.add_run(a["answer"])

    listings = state.get("joined_listings") or []
    joined = state.get("joined") or []
    if listings or joined:
        doc.add_heading("To attach from the Snowflake Marketplace", level=2)
        named = set()
        for r in listings:
            named.add(r["title"])
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(r["title"]).bold = True
            p.add_run(f"\n{r['provider']} \u00b7 {r['access']}\n{r['url']}")
        for j in joined:
            if j not in named:
                doc.add_paragraph(
                    f"{j} - search the Marketplace for a provider",
                    style="List Bullet")

    feats = link_features((poc.get("features") or []))
    if feats:
        doc.add_heading("Snowflake features you will use", level=2)
        for name, docurl in feats:
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(name).bold = True
            p.add_run(f"\n{docurl}")

    signup = (cfg.get("delivery") or {}).get("signup_url", "")
    doc.add_heading("Before you start", level=2)
    doc.add_paragraph(
        "Start a free Snowflake trial" + (f" at {signup}" if signup else "")
        + ", then open Cortex Code.", style="List Number")
    doc.add_paragraph(
        "Copy the prompt in the next section and paste it in as your first "
        "message.", style="List Number")

    doc.add_heading("Paste this into Cortex Code to begin", level=2)
    p = doc.add_paragraph()
    run = p.add_run(build_coco_prompt(cfg, state))
    run.font.name = "Consolas"
    run.font.size = Pt(9)
    rpr = run._element.get_or_add_rPr()
    rf = rpr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        rf.set(qn(attr), "Consolas")

    if poc.get("guide_title"):
        doc.add_heading("Start from this guide", level=2)
        doc.add_paragraph(poc["guide_title"])
        if poc.get("guide_url"):
            doc.add_paragraph(poc["guide_url"])
    cons = poc.get("considerations") or []
    if cons:
        doc.add_heading("Considerations", level=2)
        doc.add_paragraph("Worth thinking about before you start:")
        for c in cons:
            doc.add_paragraph(str(c), style="List Bullet")

    safe = re.sub(r"[^A-Za-z0-9]+", "-", (vis.get("first_name") or "visitor")).strip("-")
    path = os.path.join(tempfile.gettempdir(),
                        f"loco4coco-{safe}-{int(time.time())}.docx")
    doc.save(path)
    return path


# ----------------------------------------------------------------- snow helpers

_SNOW_BIN = {"path": None}


def snow_bin(cfg):
    """Resolve the snow CLI, robustly, so a fresh laptop just works.

    `pip install snowflake-cli` drops `snow` next to the python that ran pip,
    which on macOS is the framework bin/ - usually NOT on the default PATH. An
    unconfigured booth calling bare "snow" then only fails at the postbox, mid
    visit. shutil.which alone does not help (it is PATH-only, and off-PATH is the
    whole problem). Resolution order, cached for the process:
      1. snowflake.snow_binary if it is an absolute path that exists
      2. PATH (shutil.which)
      3. the sibling of THIS python (os.path.dirname(sys.executable)/snow) - where
         pip put it - plus sysconfig's scripts dir and ~/.local/bin
      4. the bare name as a last resort (may still fail, but we warned at startup)
    """
    if _SNOW_BIN["path"]:
        return _SNOW_BIN["path"]
    configured = (cfg.get("snowflake") or {}).get("snow_binary")
    if configured and os.path.isabs(configured) and os.path.exists(configured):
        _SNOW_BIN["path"] = configured
        return configured
    name = configured or "snow"
    found = shutil.which(name)
    if not found:
        cands = [os.path.join(os.path.dirname(sys.executable), "snow")]
        try:
            import sysconfig
            sp = sysconfig.get_path("scripts")
            if sp:
                cands.append(os.path.join(sp, "snow"))
        except Exception:                                        # noqa: BLE001
            pass
        cands.append(os.path.expanduser("~/.local/bin/snow"))
        found = next((c for c in cands if c and os.path.exists(c)), None)
    _SNOW_BIN["path"] = found or name
    return _SNOW_BIN["path"]


def snow_findable(cfg):
    """True if snow_bin resolves to something actually runnable."""
    r = snow_bin(cfg)
    return (os.path.isabs(r) and os.path.exists(r)) or bool(shutil.which(r))


def snow_sql(cfg, sql):
    """Run one statement via the snow CLI. Avoids adding a connector
    dependency to what is otherwise a stdlib server."""
    conn = (cfg.get("snowflake") or {}).get("connection_name")
    binary = snow_bin(cfg)
    cmd = [binary, "sql", "-q", sql, "--format", "json"]
    if conn:
        cmd += ["-c", conn]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return None, (r.stderr or r.stdout or "").strip()[:300]
        return json.loads(r.stdout or "[]"), ""
    except FileNotFoundError:
        return None, "snow CLI not found on PATH"
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        return None, str(e)[:300]


def stage_and_presign(cfg, local_path):
    """Upload the .docx and return a presigned download URL."""
    d = cfg.get("delivery") or {}
    stage = d.get("stage") or ""
    if not stage or not local_path:
        return "", "no stage configured"
    conn = (cfg.get("snowflake") or {}).get("connection_name")
    binary = snow_bin(cfg)
    cmd = [binary, "stage", "copy", local_path, stage, "--overwrite"]
    if conn:
        cmd += ["-c", conn]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return "", (r.stderr or r.stdout or "").strip()[:300]
    except FileNotFoundError:
        return "", "snow CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return "", "stage upload timed out"

    name = os.path.basename(local_path)
    secs = int(d.get("presign_seconds", 604800))
    rows, err = snow_sql(
        cfg, f"SELECT GET_PRESIGNED_URL({stage}, '{name}', {secs}) AS URL")
    if not rows:
        return "", err or "presign returned nothing"
    row = rows[0] if isinstance(rows, list) else rows
    if isinstance(row, list):
        row = row[0] if row else {}
    return (row.get("URL") or row.get("url") or ""), ""


def log_session(cfg, state):
    """Write the visitor's row. Returns (ok, error).

    Escaping is done ONCE, by handing Snowflake a single JSON document and
    letting it do the typing. Building 27 quoted literals by hand is how the
    previous version ended up writing to columns that did not exist and failing
    silently four times in a row.
    """
    sf = cfg.get("snowflake") or {}
    tbl = f"{sf.get('database')}.{sf.get('schema')}.{sf.get('sessions_table', 'SESSIONS')}"
    vis = state.get("visitor") or {}
    poc = state.get("poc") or {}
    turns = state.get("turns") or []
    qa = state.get("qa") or {}

    started = state.get("started_at") or time.time()
    payload = {
        "SESSION_ID": state.get("session_id") or "",
        "EVENT_CITY": (cfg.get("event") or {}).get("city", ""),
        "LANGUAGE_CODE": (cfg.get("event") or {}).get("language", ""),
        "FIRST_NAME": vis.get("first_name", ""),
        "COMPANY": vis.get("company", ""),
        "INDUSTRY": industry_name(cfg, vis.get("industry")),
        # The label is what a human reads in a report; the key is what
        # groups reliably. Storing only the label meant "Something else"
        # and a renamed industry were indistinguishable after the fact.
        "INDUSTRY_KEY": vis.get("industry") or "",
        "DATA_HELD": state.get("held") or [],
        "MARKETPLACE_JOINED": state.get("joined") or [],
        "POC_ARCHETYPE": poc.get("archetype", ""),
        "POC_NAME": poc.get("poc_name", ""),
        "POC_SUMMARY": poc.get("summary", ""),
        "GUIDE_FORKED": poc.get("guide_title", ""),
        "GUIDE_URL": poc.get("guide_url", ""),
        "FEATURES": poc.get("features") or [],
        "CONSIDERATIONS": poc.get("considerations") or [],
        "FIRST_STEP": poc.get("first_step", ""),
        "DOCUMENT_URL": state.get("blueprint_url", ""),
        "DELIVERY_STATUS": delivery_status(state),
        "DURATION_SECONDS": int(time.time() - started),
        "COCO_SECONDS": int(state.get("coco_seconds") or 0),
        "INPUT_TOKENS": int(state.get("input_tokens") or 0),
        "OUTPUT_TOKENS": int(state.get("output_tokens") or 0),
        # Was a hand-typed "Name / Stand 2". Removed: nothing read it, and all
        # 52 rows written before this change had it empty, so in practice it was
        # never filled. The question it was meant to answer - which laptop
        # produced this row - is already answered by the account, because a
        # second laptop at an event gets its own pool account. So stamp the
        # account instead: same information, nothing to type, nothing to forget.
        "SE_OPERATOR": account_name(cfg),
        "NOTES": f"{len(turns)} turns",
        # The qualification payload. Everything above is a pick from a list we
        # wrote; these two are the visitor's own words and their real estate.
        "PROBLEM_STATEMENT": vis.get("problem", ""),
        "PLATFORMS": state.get("platforms") or [],
        # Home-stage sovereignty answers - the visitor's own words on where they
        # are and what their rules are, useful for follow-up grouping.
        "COMPANY_COUNTRY": vis.get("company_country", ""),
        "RESIDENCY": vis.get("residency", ""),
        # QA-bot verdict for this blueprint. The per-finding detail is in
        # QA_FINDINGS; these four are the summary. QA_PASSED/QA_RELEVANT may be
        # NULL (bot off, or relevance undetermined), which the ::BOOLEAN cast of
        # a JSON null preserves.
        "QA_PASSED": qa.get("passed"),
        "QA_REPAIRS": int(qa.get("repairs") or 0),
        "QA_RELEVANT": qa.get("relevant"),
        "QA_NOTE": qa.get("note", ""),
        # Bonus objectives: every question asked (answered or not), how many
        # were answered, and the server-side score the scoreboard reads.
        "BONUS_ANSWERED": len(((state.get("bonus") or {}).get("answered")) or []),
        "SCORE": compute_score(cfg, state, final=True),
        "BONUS_QA": {
            "answered": [{k: a.get(k) for k in ("question", "answer", "where", "points")}
                         for a in ((state.get("bonus") or {}).get("answered")) or []],
            "missed": [m.get("question") for m in
                       ((state.get("bonus") or {}).get("missed")) or []],
        },
    }
    bool_cols = ["QA_PASSED", "QA_RELEVANT"]
    arr_cols = ["DATA_HELD", "MARKETPLACE_JOINED", "FEATURES",
                "CONSIDERATIONS", "PLATFORMS"]
    var_cols = ["BONUS_QA"]
    cols = [k for k in payload
            if k not in arr_cols and k not in bool_cols and k not in var_cols]
    num = {"DURATION_SECONDS", "COCO_SECONDS",
           "INPUT_TOKENS", "OUTPUT_TOKENS", "QA_REPAIRS",
           "BONUS_ANSWERED", "SCORE"}
    sel = [f"p:{c}::{'NUMBER' if c in num else 'TEXT'}" for c in cols]
    sel += [f"p:{c}::ARRAY" for c in arr_cols]
    sel += [f"p:{c}::BOOLEAN" for c in bool_cols]
    sel += [f"p:{c}::VARIANT" for c in var_cols]

    sql = (f"INSERT INTO {tbl} (SESSION_TS, {', '.join(cols + arr_cols + bool_cols + var_cols)}) "
           f"SELECT CURRENT_TIMESTAMP(), {', '.join(sel)} "
           f"FROM (SELECT PARSE_JSON(%s) AS p)")
    return sf_exec(cfg, sql, (json.dumps(payload, ensure_ascii=False),))


def log_qa(cfg, session_id, findings):
    """Write one QA_FINDINGS row per check that fired. This is the audit trail
    behind the silent repairs: the visitor's document is fixed in place, but the
    change is recorded here with its before/after. Best-effort - a logging
    failure must never block a visitor's handover. Returns (ok, error)."""
    if not findings:
        return True, ""
    sf = cfg.get("snowflake") or {}
    tbl = f"{sf.get('database')}.{sf.get('schema')}.QA_FINDINGS"
    rows, ok_all, last_err = 0, True, ""
    for f in findings:
        payload = {
            "SESSION_ID": session_id or "",
            "CHECK_NAME": str(f.get("check") or "")[:120],
            "SEVERITY": str(f.get("severity") or "")[:20],
            "DETAIL": str(f.get("detail") or "")[:2000],
            "REPAIRED": bool(f.get("repaired")),
            "BEFORE_VAL": f.get("before"),
            "AFTER_VAL": f.get("after"),
        }
        sql = (f"INSERT INTO {tbl} (SESSION_ID, FINDING_TS, CHECK_NAME, SEVERITY, "
               f"DETAIL, REPAIRED, BEFORE_VAL, AFTER_VAL) SELECT p:SESSION_ID::TEXT, "
               f"CURRENT_TIMESTAMP(), p:CHECK_NAME::TEXT, p:SEVERITY::TEXT, "
               f"p:DETAIL::TEXT, p:REPAIRED::BOOLEAN, p:BEFORE_VAL::TEXT, "
               f"p:AFTER_VAL::TEXT FROM (SELECT PARSE_JSON(%s) AS p)")
        ok, err = sf_exec(cfg, sql, (json.dumps(payload, ensure_ascii=False),))
        if ok:
            rows += 1
        else:
            ok_all, last_err = False, err
    return ok_all, last_err


def sf_exec(cfg, sql, args=None):
    """Run one statement on the shared connection with real bind parameters.

    Binds are why the base64 dance is gone: the driver ships values out of band,
    so braces, quotes and ampersands in a visitor's text cannot reach the SQL
    parser at all. It is also faster, since there is no `snow sql` subprocess.
    """
    try:
        cur = sf_conn(cfg).cursor()
        try:
            cur.execute(sql, args or ())
        finally:
            cur.close()
        return True, ""
    except Exception as e:                                       # noqa: BLE001
        return False, str(e)[:300].replace("\n", " ")


def delivery_status(state):
    """Truthful by construction. The QR to the presigned .docx is the delivery,
    so a staged URL is DELIVERED; anything else has not reached the visitor."""
    if state.get("blueprint_url"):
        return "DELIVERED"
    return "FAILED"


def log_turn(cfg, session_id, loc_id, text, reply, meta):
    """One row per location per visitor, so the slowest and most expensive beat
    is answerable rather than guessed at."""
    sf = cfg.get("snowflake") or {}
    tbl = f"{sf.get('database')}.{sf.get('schema')}.TURNS"
    u = (meta or {}).get("usage") or {}
    payload = {
        "SESSION_ID": session_id or "",
        "LOCATION": loc_id,
        "VISITOR_INPUT": (text or "")[:2000],
        "REPLY": (reply or "")[:2000],
        "DURATION_SECONDS": int((meta or {}).get("seconds") or 0),
        "INPUT_TOKENS": int(u.get("input_tokens") or 0),
        "OUTPUT_TOKENS": int(u.get("output_tokens") or 0),
        "CACHE_READ_TOKENS": int(u.get("cache_read_input_tokens") or 0),
        "SUCCEEDED": bool((meta or {}).get("ok")),
    }
    sql = (f"INSERT INTO {tbl} (TURN_TS, SESSION_ID, LOCATION, VISITOR_INPUT, "
           f"REPLY, DURATION_SECONDS, INPUT_TOKENS, OUTPUT_TOKENS, "
           f"CACHE_READ_TOKENS, SUCCEEDED) SELECT CURRENT_TIMESTAMP(), "
           f"p:SESSION_ID::TEXT, p:LOCATION::TEXT, p:VISITOR_INPUT::TEXT, "
           f"p:REPLY::TEXT, p:DURATION_SECONDS::NUMBER, p:INPUT_TOKENS::NUMBER, "
           f"p:OUTPUT_TOKENS::NUMBER, p:CACHE_READ_TOKENS::NUMBER, "
           f"p:SUCCEEDED::BOOLEAN FROM (SELECT PARSE_JSON(%s) AS p)")
    return sf_exec(cfg, sql, (json.dumps(payload, ensure_ascii=False),))


# ------------------------------------------------------------------- transports

class QRDelivery:
    """The only delivery path: build the .docx, stage it, presign it. The QR the
    visitor scans points at that presigned URL, which expires in seven days.

    There is deliberately NO local record. Email was removed, so the old outbox
    that wrote a fully-composed message (name, company, problem) to disk on every
    visit had no purpose left - it was pure liability, PII accumulating on a
    shared booth laptop between visitors. The governed Snowflake SESSIONS/TURNS
    row is the one intended persistence; nothing else about a visitor is kept.
    """

    last_meta = None

    def deliver(self, cfg, state, job_id):
        started = time.time()
        self.last_meta = {"transport": "qr", "ok": False, "seconds": 0}
        docx = blueprint_docx(cfg, state)
        url, err = ("", "")
        if docx:
            url, err = stage_and_presign(cfg, docx)
        # Recorded so the postbox TURNS row reflects the real outcome and time.
        self.last_meta = {"transport": "qr", "ok": bool(url),
                          "seconds": round(time.time() - started, 1)}
        if err:
            self.last_meta["error"] = err
        if url:
            write_state({"blueprint_url": url})
        vis = state.get("visitor") or {}
        name = (vis.get("first_name") or "").strip()
        who = f", {name}" if name else ""
        if url:
            reply = (f"Wrapped and labelled{who}. Scan the code on screen and "
                     f"it is yours - the link works for seven days.")
            return True, reply
        # Presign failed: be honest and point at a person, do not claim delivery.
        return False, ("I could not wrap it up this time - grab a Snowflake "
                       "person and we will sort it." + (f" ({err})" if err else ""))


def get_transport(cfg):
    # One transport now. The setting is kept so a future hosted-delivery build
    # can register another, but the booth path is QR-to-presigned-stage only.
    return QRDelivery()


# ------------------------------------------------------------------ turn runner

def finish_turn(cfg, loc_id, label, reply, extra=None, meta=None):
    cur = read_state()
    turns = list(cur.get("turns") or [])
    turns.append({"location": loc_id, "text": label, "reply": reply,
                  "at": time.time()})
    patch = {"thinking": False, "reply": reply, "turns": turns}
    if meta:
        u = meta.get("usage") or {}
        patch["coco_seconds"] = int((cur.get("coco_seconds") or 0) + (meta.get("seconds") or 0))
        patch["input_tokens"] = int((cur.get("input_tokens") or 0) + (u.get("input_tokens") or 0))
        patch["output_tokens"] = int((cur.get("output_tokens") or 0) + (u.get("output_tokens") or 0))
    if extra:
        patch.update(extra)
    write_state(patch)
    # Always record the turn, even when no exec ran (e.g. the postbox no longer
    # spends an exec on a doomed draft attempt) - otherwise the location is
    # missing from TURNS and the per-beat timing table has a hole.
    ok, err = log_turn(cfg, cur.get("session_id"), loc_id, label, reply, meta)
    if not ok:
        print(f"[loco4coco] TURNS insert failed for {loc_id}: {err}")


def unlock_next(cfg, loc_id, state):
    order = cfg.get("unlock_order") or []
    if loc_id not in order:
        return state.get("unlocked") or []
    unlocked = list(state.get("unlocked") or [])
    i = order.index(loc_id)
    if i + 1 < len(order) and order[i + 1] not in unlocked:
        unlocked.append(order[i + 1])
    return unlocked


def run_checklist(cfg, loc_id, labels, job_id):
    state = read_state()
    key = "held" if loc_id == "library" else "joined"
    patch = {key: labels}
    if loc_id == "marketplace":
        # Resolve each pick back to its verified listing so the blueprint can
        # name the provider and link the listing. Anything typed into "Other"
        # stays a plain string - we will not guess a provider for it.
        ind = (state.get("visitor") or {}).get("industry") or "other"
        by_title = {r["title"]: r for r in listings_for(cfg, ind, state.get("session_id"), state)}
        patch["joined_listings"] = [by_title[l] for l in labels if l in by_title]
        # THE 7th OPTION. The six curated picks stay deterministic; anything the
        # visitor TYPES into "something else" is what agentic marketplace search
        # is for, seeded with this event's country and region. Fired in the
        # BACKGROUND on purpose: the call is 75-110s, and making a visitor watch
        # that at the stall would eat the five-minute clock. It enriches the
        # takeaway document instead (merged in run_send), and if it does not
        # finish in time nothing is lost - they still have what they picked.
        typed = [l for l in labels if l not in by_title and str(l).strip()]
        if typed:
            start_agentic_search(cfg, state.get("session_id"), ind, " ".join(typed))
    write_state(patch)
    state = read_state()
    loc = (cfg.get("locations") or {}).get(loc_id) or {}
    body = fill(loc.get("prompt", ""), cfg, state,
                selection=", ".join(labels) or "nothing")
    prompt = "\n".join(base_context(cfg, state)) + "\n\n" + body
    ok, reply, meta = run_turn(cfg, prompt, loc_id, loc=loc, job_id=job_id)
    if not ok and not reply:
        if read_state().get("job_id") not in (None, job_id):
            return                          # superseded by a newer message
        # Every route failed. End the turn anyway so the screen never sits on
        # "thinking" forever; the visit carries on with what they picked.
        reply = loc.get("fallback_reply") or (
            "Good choices - I have noted those. Let's keep moving.")
    finish_turn(cfg, loc_id, ", ".join(labels), reply,
                {"unlocked": unlock_next(cfg, loc_id, state)}, meta)
    start_bonus_generation(cfg, state.get("session_id"))


def run_workshop(cfg, text, job_id, bg_session=None):
    """`bg_session` set = background mode: the visitor has already been sent
    on to the postbox, so this must ALWAYS leave a POC behind (never return
    empty-handed) and must drop its result if the visitor was reset."""
    state = read_state()
    loc = (cfg.get("locations") or {}).get("workshop") or {}
    feature_list = ", ".join(sorted(load_features()))
    body = fill(loc.get("prompt", ""), cfg, state, input=text,
                feature_list=feature_list)
    # Deterministic shortlist appended to the prompt: the likely POC shape, the
    # listings for their industry, and the features that shape actually uses.
    #
    # This is how the closed lists reach `cortex exec` - as content, not as a
    # tool. exec takes no tools except through MCP, which is not guaranteed on a
    # borrowed booth laptop, and at ~220 tokens injecting the slice is cheaper
    # than any lookup would be. The full feature list above is deliberately kept
    # as well: narrowing it would risk excluding the right feature when the
    # archetype guess is wrong, and tokens are not the bottleneck here - the
    # ~18s process startup is.
    try:
        vis = (state.get("visitor") or {})
        hint_block = bctx.shortlist_block(
            " ".join([str(vis.get("problem") or ""), str(text or "")]),
            industry=vis.get("industry"), conn=coco_connection())
        if hint_block:
            body += ("\n\nFor reference, resolved server-side from their own "
                     "words:\n" + hint_block)
    except Exception:
        pass  # Guidance only. The prompt above already stands on its own.
    extra = bonus_block(state)
    if extra and "{bonus}" not in (loc.get("prompt") or ""):
        # Older templates without the {bonus} slot still get the answers.
        body += ("\n\nThey also answered these bonus questions. Use them to "
                 "choose and shape the proof of concept:\n" + extra)
    # Optional invitation to actually USE a tool. Measured: --bypass alone
    # produces NO tool calls, because the feature list is injected and guides
    # are resolved server-side, so CoCo has no reason to reach for one. Inviting
    # a docs lookup does fire a real tool but cost ~34s on its own, which would
    # undo the time the fast path just saved. Off by default; the Ask stop is
    # where tool use is genuinely warranted and budgeted for.
    hint = loc.get("tool_hint")
    if hint:
        body += "\n\n" + str(hint)
    prompt = "\n".join(base_context(cfg, state)) + "\n\n" + body
    ok, raw, meta = run_turn(cfg, prompt, "workshop", loc=loc, job_id=job_id)
    if not ok and not raw:
        if not bg_session:
            return
        raw = ""                          # fall through to the stub POC below
    if bg_session and read_state().get("session_id") != bg_session:
        return                              # reset while drafting: discard

    poc, reply = {}, raw
    m = re.search(r"\{.*\}", raw or "", re.S)
    if m:
        try:
            poc = json.loads(m.group(0))
        except json.JSONDecodeError:
            poc = {}
    if poc:
        reply = poc.get("reply") or "Right, I have what I need."
        arche = poc.get("archetype") or ""
        title, url = guide_for(cfg, arche)
        if not title:
            # Fall back on the most broadly useful fork rather than invent one.
            title, url = guide_for(cfg, "talk-to-my-data")
        poc["guide_title"], poc["guide_url"] = title, url
        # Drop anything outside the closed list, so every feature has a link.
        poc["features"] = [n for n, _u in link_features(poc.get("features"))]
        cons = poc.get("considerations")
        if isinstance(cons, str):
            cons = [cons]
        poc["considerations"] = [str(c).strip() for c in (cons or [])
                                if str(c).strip()][:4]
        # Stage 1 is deliberately thin - archetype, name, features, reply - which
        # is what took 84s and 1051 output tokens when it also had to write the
        # summary and four considerations. Fill the rest from the catalogue now
        # so the blueprint is already complete and correct, then improve it in
        # the background while the visitor walks to the postbox.
        d = archetype_defaults(cfg, arche)
        if not poc.get("features"):
            poc["features"] = d["features"]
        if not poc.get("first_step"):
            poc["first_step"] = d["first_step"]
        if not poc.get("summary"):
            poc["summary"] = ""
        if not poc.get("considerations"):
            poc["considerations"] = d["pool"][:3]
    else:
        poc = {"poc_name": text[:60], "summary": raw[:300], "features": [],
               "considerations": ["We ran out of time to work through the "
                                  "detail. Start by confirming what data you "
                                  "can actually get hold of."]}
        t, u = guide_for(cfg, "talk-to-my-data")
        poc["guide_title"], poc["guide_url"] = t, u
        if bg_session and not reply:
            reply = ((cfg.get("locations") or {}).get("workshop") or {}).get(
                "background_ready") or "Your blueprint is ready at the postbox."

    extra = {"poc": poc, "unlocked": unlock_next(cfg, "workshop", state),
             "poc_pending": bool(poc.get("archetype")),
             "workshop_pending": False}
    now = read_state()
    if bg_session and now.get("location") == "postbox" and now.get("thinking"):
        # The visitor is already at the postbox and its send is running: do not
        # clear its thinking flag or overwrite its reply with the bench's.
        extra["thinking"] = True
        extra["reply"] = now.get("reply") or ""     # TURNS still logs the real one
    finish_turn(cfg, "workshop", text, reply, extra, meta)
    if poc.get("archetype"):
        threading.Thread(target=refine_poc, args=(cfg, text), daemon=True).start()
    start_bonus_generation(cfg, state.get("session_id"))


def refine_poc(cfg, text):
    """Stage 2. Runs AFTER CoCo has spoken, on the fast transport, while the
    visitor reads the reply and walks to the postbox. Fills the summary and
    picks considerations from the precomputed pool by INDEX rather than writing
    them.

    Never blocks and never fails loudly: the blueprint is already complete from
    catalogue defaults, so a failure here costs tailoring, not the visit.
    """
    try:
        state = read_state()
        poc = dict(state.get("poc") or {})
        arche = poc.get("archetype") or ""
        d = archetype_defaults(cfg, arche)
        pool = d["pool"]
        if not pool:
            write_state({"poc_pending": False})
            return
        listed = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(pool))
        vis = state.get("visitor") or {}
        prompt = (
            "You are helping shape a Snowflake proof of concept. Be concrete and "
            "never generic.\n\n"
            f"Person: {vis.get('first_name') or 'they'} at "
            f"{vis.get('company') or 'their organisation'}, "
            f"{industry_prose(cfg, vis.get('industry')) or 'sector not stated'}.\n"
            f"They hold: {', '.join(state.get('held') or []) or 'unspecified'}.\n"
            f"They will attach: {', '.join(state.get('joined') or []) or 'nothing yet'}.\n"
            f"They asked the POC to: \"{text}\"\n"
            f"It is a '{arche}' build called '{poc.get('poc_name') or 'their POC'}'.\n"
            + (f"They also told us:\n{bonus_block(state)}\n" if bonus_block(state) else "")
            + "\nCandidate considerations:\n" + listed + "\n\n"
            "Return ONLY minified JSON, no prose, no code fence, with exactly "
            "these keys: {\"summary\": 2 sentences on what it does and why it "
            "matters to THEM, \"first_step\": one concrete first action, "
            "\"considerations\": array of exactly 3 integers, the numbers of the "
            "most relevant candidates above}\n\n"
            # Prevention, not just detection. This text is printed and handed to
            # a stranger at a public booth, and a real blueprint came back with
            # the word "die" in it. The QA bot repairs that as a backstop; this
            # is the instruction meant to stop it being written at all.
            "Word choice matters: this goes into a document handed to a visitor "
            "at a public booth. Never use the words die, dies, dying, kill, "
            "killed, killing, suicide, cripple or crippled, and never use "
            "violent or morbid metaphors for systems failing. Say fails, stops, "
            "degrades or goes down instead. Keep the register plain and calm."
        )
        loc = {"transport": "complete"}
        ok, raw, _meta = run_turn(cfg, prompt, "workshop_refine", loc=loc)
        if not ok and not raw:
            write_state({"poc_pending": False})
            return
        m = re.search(r"\{.*\}", raw or "", re.S)
        extra = {}
        if m:
            try:
                extra = json.loads(m.group(0))
            except json.JSONDecodeError:
                extra = {}
        # Re-read: the visitor may have moved on and state may have changed.
        poc = dict((read_state().get("poc") or {}))
        if extra.get("summary"):
            poc["summary"] = str(extra["summary"])[:600]
        if extra.get("first_step"):
            poc["first_step"] = str(extra["first_step"])[:300]
        idx = extra.get("considerations")
        if isinstance(idx, list):
            picked = []
            for i in idx:
                try:
                    n = int(i)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(pool) and pool[n - 1] not in picked:
                    picked.append(pool[n - 1])
            if picked:
                poc["considerations"] = picked[:4]
        write_state({"poc": poc, "poc_pending": False,
                     "poc_refined_at": time.time()})
    except Exception:                                             # noqa: BLE001
        # A background refinement must never take the booth down.
        write_state({"poc_pending": False})


def run_ask(cfg, text, job_id):
    """The one unscripted moment: the visitor asks CoCo anything about their POC.

    This is where real CoCo genuinely earns it. Unlike the other turns there is
    no injected answer to reflect back, so CoCo has an actual reason to reach
    for its tools, and the tray shows real work. Always exec, never the fast
    path. Optional and skippable so it cannot blow the five minutes.
    """
    state = read_state()
    a = cfg.get("ask") or {}
    poc = state.get("poc") or {}
    body = fill(a.get("prompt", ""), cfg, state, input=text,
                poc_name=poc.get("poc_name") or "their POC",
                poc_summary=poc.get("summary") or "")
    hint = a.get("tool_hint")
    if hint:
        body += "\n\n" + str(hint)
    prompt = "\n".join(base_context(cfg, state)) + "\n\n" + body
    ok, reply, meta = run_turn(cfg, prompt, "ask", loc=a, job_id=job_id)
    if not ok and not reply:
        return
    finish_turn(cfg, "ask", text, reply, {"ask_used": True}, meta)


def qa_review(cfg, state, poc):
    """Review the finished blueprint against the request, repair what is safely
    fixable from the closed lists, and record every change.

    Returns (poc, findings, summary). Repairs are applied to a copy of `poc`;
    the visitor never sees a failure. Findings and the summary are written to
    Snowflake for audit (see run_send) - the "log that a change was made" rule.

    Deterministic checks run always; a model relevance turn runs when
    qa.model_review is on and fails open (never blocks the handover).
    """
    poc = dict(poc or {})
    vis = state.get("visitor") or {}
    findings = []

    def note(check, severity, detail, repaired=False, before=None, after=None):
        findings.append({"check": check, "severity": severity, "detail": detail,
                         "repaired": bool(repaired),
                         "before": None if before is None else str(before)[:200],
                         "after": None if after is None else str(after)[:200]})

    # 1. Every feature must resolve to a doc link, or it is dropped.
    before_feats = list(poc.get("features") or [])
    kept = [n for n, _u in link_features(before_feats)]
    if kept != before_feats:
        dropped = [f for f in before_feats if f not in kept]
        note("features_in_closed_list", "repair",
             "dropped feature(s) with no doc link: " + ", ".join(dropped),
             repaired=True, before=before_feats, after=kept)
        poc["features"] = kept

    # 2. The guide must resolve; if it fell back, say so.
    arche = poc.get("archetype") or ""
    title, url = guide_for(cfg, arche)
    if not title:
        title, url = guide_for(cfg, "talk-to-my-data")
        note("guide_resolves", "repair",
             "no fork for archetype %r; fell back to talk-to-my-data" % arche,
             repaired=True, before=arche, after="talk-to-my-data")
    if poc.get("guide_title") != title:
        poc["guide_title"], poc["guide_url"] = title, url

    # 3. Does the archetype match the visitor's own words? Flag, do not override -
    # the model saw the whole sentence and the resolver only scores keywords.
    problem = " ".join([str(vis.get("problem") or ""),
                        str(poc.get("_workshop_input") or "")]).strip()
    if arche and problem:
        try:
            hit, _ranked = bctx.resolve_archetype(problem, conn=coco_connection())
            if hit and hit.get("id") and hit["id"] != arche:
                note("archetype_matches_pain", "flag",
                     "model chose %r; pain text resolves to %r"
                     % (arche, hit["id"]))
        except Exception:
            pass

    # 4. Platform selection is normalised at ingest; re-apply the canonical rule
    # and flag if anything was still off - a cheap guard against a hand-edited
    # state or a future client sending raw values.
    plats = state.get("platforms") or []
    norm = normalise_platforms(cfg, plats)
    if list(norm) != list(plats):
        note("platforms_normalised", "flag",
             "platform set re-normalised for the blueprint",
             before=plats, after=norm)

    # 5. The visitor must leave with a name and a summary.
    if not str(poc.get("poc_name") or "").strip():
        note("poc_name_present", "flag", "no POC name produced")
    if not str(poc.get("summary") or "").strip():
        note("summary_present", "flag", "no summary produced")

    # 6. If they stated a residency rule, the sovereignty section must be there.
    residency = _residency_key(vis)
    if residency in ("country_only", "eu") and not sovereignty_lines(cfg, state):
        note("sovereignty_consistent", "flag",
             "residency is %r but no sovereignty lines were produced" % residency)

    # 7. Model relevance turn - fail open.
    qacfg = cfg.get("qa") or {}
    relevant = None
    if qacfg.get("model_review") and problem:
        relevant = _qa_relevance(cfg, problem, poc)
        if relevant is False:
            note("relevance", "flag",
                 "model judged the POC does not address the stated problem")
        elif relevant is None:
            note("relevance_skipped", "info",
                 "relevance check did not complete; delivery proceeded")

    # 8. Banned words. A booth document goes home with a stranger, so a few
    # words are simply not acceptable in it however apt the model thought they
    # were - "die" turned up in a real blueprint. Prevention lives in the exec
    # prompts (see locations.workshop.prompt); this is the backstop that catches
    # it when the model ignores them. Repaired in place, and logged, because
    # silently shipping it is the one outcome we cannot have.
    for field, replacement in (("die", "fail"), ("dies", "fails"),
                               ("dying", "failing"), ("kill", "stop"),
                               ("killed", "stopped"), ("killing", "stopping"),
                               ("suicide", "self-inflicted outage"),
                               ("cripple", "degrade"), ("crippled", "degraded")):
        pat = re.compile(r"\b" + re.escape(field) + r"\b", re.I)

        def _sub(m, rep=replacement):
            # Match the case we found, so a sentence-initial "Kill" does not
            # become a lower-case "stop" and leave the prose looking broken.
            got = m.group(0)
            if got.isupper():
                return rep.upper()
            if got[:1].isupper():
                return rep[:1].upper() + rep[1:]
            return rep

        for key in ("poc_name", "summary", "why_now", "first_step"):
            val = poc.get(key)
            if not isinstance(val, str) or not pat.search(val):
                continue
            fixed = pat.sub(_sub, val)
            note("banned_word", "flag",
                 f"{field!r} removed from {key}", True, val, fixed)
            poc[key] = fixed
        steps = poc.get("steps")
        if isinstance(steps, list):
            for i, s in enumerate(steps):
                if isinstance(s, str) and pat.search(s):
                    fixed = pat.sub(_sub, s)
                    note("banned_word", "flag",
                         f"{field!r} removed from steps[{i}]", True, s, fixed)
                    steps[i] = fixed

    repairs = sum(1 for f in findings if f["repaired"])
    flags = sum(1 for f in findings if f["severity"] == "flag")
    summary = {"passed": flags == 0 and relevant is not False,
               "repairs": repairs, "relevant": relevant,
               "note": "; ".join(f["check"] for f in findings)[:400]}
    return poc, findings, summary


def _qa_relevance(cfg, problem, poc):
    """One fast COMPLETE call: does the POC address the stated problem? Returns
    True / False / None (None = could not tell, treated as non-blocking)."""
    qacfg = cfg.get("qa") or {}
    model = qacfg.get("model") or (cfg.get("coco") or {}).get("complete_model")
    prompt = (
        "You are a strict reviewer. A booth built this proof-of-concept plan for "
        "a visitor. Judge ONLY whether it addresses the problem they stated.\n\n"
        "Their problem: \"" + problem[:600] + "\"\n\n"
        "The POC: name=\"" + str(poc.get("poc_name") or "")[:120] + "\"; "
        "builds=\"" + str(poc.get("summary") or "")[:400] + "\"; "
        "features=" + ", ".join(poc.get("features") or []) + "\n\n"
        "Reply with ONLY minified JSON, nothing else: "
        '{"relevant": true or false, "issue": "at most 12 words, empty if relevant"}')
    loc = {"transport": "complete", "complete_model": model,
           "timeout": int(qacfg.get("timeout_seconds") or 12)}
    try:
        ok, raw, _meta = run_turn(cfg, prompt, "qa", loc=loc,
                                  timeout=loc["timeout"])
    except Exception:
        return None
    if not ok or not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return bool(v.get("relevant")) if "relevant" in v else None


def run_send(cfg, job_id):
    # Background workshop still drafting (the visitor beat it to the postbox):
    # wait for it, bounded by its own budget, so the document has a real POC.
    wloc = (cfg.get("locations") or {}).get("workshop") or {}
    limit = float(wloc.get("timeout") or 60) + 30
    t0 = time.time()
    if read_state().get("workshop_pending"):
        write_state({"reasoning": [wloc.get("finishing") or "Finishing your blueprint…"]})
    while read_state().get("workshop_pending") and time.time() - t0 < limit:
        time.sleep(0.5)
    while read_state().get("poc_pending") and time.time() - t0 < limit + 8:
        time.sleep(0.25)
    state = read_state()

    # Merge anything the 7th option found. The agentic search was fired in the
    # background when the visitor typed into "something else" at the stall; by the
    # time they reach the postbox it has usually finished. Real listings only, and
    # never a duplicate of something they already picked. If it did not finish,
    # this is a no-op and the document is exactly what they chose.
    try:
        extra = listings_agentic(cfg, state.get("session_id"))
        if extra:
            have = {(r.get("global_name") or r.get("title")) for r
                    in (state.get("joined_listings") or [])}
            add = [r for r in extra
                   if (r.get("global_name") or r.get("title")) not in have]
            if add:
                write_state({"joined_listings":
                             (state.get("joined_listings") or []) + add})
                state = read_state()
                print(f"[loco4coco] 7th option added {len(add)} agentic listing(s)")
    except Exception as e:                                           # noqa: BLE001
        print(f"[loco4coco] agentic merge skipped (non-blocking): {e}")

    # Bonus answers given after the POC was last refined (typically the
    # postbox catch-up) are folded in now, so they reach the document. One
    # fast COMPLETE call; refine_poc never raises.
    poc0 = state.get("poc") or {}
    told = ((state.get("bonus") or {}).get("answered")) or []
    refined = state.get("poc_refined_at") or 0
    if poc0.get("archetype") and any(a.get("at", 0) > refined for a in told):
        wtext = next((t.get("text") for t in reversed(state.get("turns") or [])
                      if t.get("location") == "workshop"), "") or ""
        refine_poc(cfg, wtext)
        state = read_state()

    # QA bot runs BEFORE delivery so its repairs land in the document the visitor
    # actually scans. It reviews the finished POC against their stated problem,
    # fixes what is safely fixable from the closed lists, and records every change.
    # Config-gated; if it errors it must never block the handover.
    findings = []
    if (cfg.get("qa") or {}).get("enabled", True):
        try:
            poc2, findings, summary = qa_review(cfg, state, state.get("poc") or {})
            write_state({"poc": poc2, "qa": summary})
            state = read_state()
        except Exception as e:                                   # noqa: BLE001
            print(f"[loco4coco] QA review error (non-blocking): {e}")

    transport = get_transport(cfg)
    # deliver() reports failure by return value, but an unexpected raise here -
    # a malformed POC reaching the docx builder, say - killed this daemon thread
    # with `thinking` still set, and the postbox then span forever with no error
    # and no way out. Whatever happens, the turn gets finished.
    try:
        delivered, reply = transport.deliver(cfg, state, job_id)
    except Exception as e:                                       # noqa: BLE001
        print(f"[loco4coco] delivery raised: {e}")
        delivered, reply = False, ("I could not wrap it up this time - grab a "
                                   "Snowflake person and we will sort it.")
    st = read_state()
    write_state({"queued": bool(delivered), "email_sent": False})

    # Log BEFORE finish_turn. finish_turn clears `thinking`, which is the signal
    # everything else waits on, so logging after it means the row lands after
    # the visitor has already been told the flow is finished - and a failure
    # would go unnoticed until someone counted rows.
    ok, err = False, ""
    try:
        ok, err = log_session(cfg, read_state())
    except Exception as e:                                       # noqa: BLE001
        err = str(e)[:300]                    # never block the handover on audit
    if ok:
        write_state({"logged": True, "log_error": ""})
        invalidate_scoreboard()
    else:
        write_state({"logged": False, "log_error": err or "unknown"})
        print(f"[loco4coco] SESSIONS insert FAILED: {err}")

    # Audit trail for the silent repairs. Best-effort - a logging failure here
    # must not surface to the visitor, but it is printed so the operator sees it.
    qok, qerr = log_qa(cfg, read_state().get("session_id") or "", findings)
    if not qok:
        print(f"[loco4coco] QA_FINDINGS insert FAILED: {qerr}")

    finish_turn(cfg, "postbox", "send the blueprint", reply, {"stage": "done"},
                getattr(transport, "last_meta", None))


# ------------------------------------------------------------ bonus objectives
#
# Optional questions that spawn on the map while the visitor walks. Generated
# by COMPLETE in the background from everything known so far, so the next one
# is normally ready before the visitor is back on the map. Nothing in the main
# flow ever waits on this: if generation fails, no bonus spawns.

_bonus_lock = threading.Lock()
_BONUS_ASKS_FOR_PII = re.compile(
    r"\b(e-?mail|phone|mobile|your name|surname|home address|date of birth|"
    r"passport|salary)\b", re.I)
_BONUS_BANNED = re.compile(
    r"\b(die|dies|dying|kill|killed|killing|suicide|cripple|crippled)\b", re.I)


def _bonus_cfg(cfg):
    return cfg.get("bonus") or {}


def _bonus_mutate(fn):
    """Read-modify-write the bonus sub-state atomically. `fn(b, state)` edits
    `b` in place and returns a value. Only bonus code writes this key."""
    with _bonus_lock:
        st = read_state()
        b = json.loads(json.dumps(st.get("bonus") or BLANK_STATE["bonus"]))
        out = fn(b, st)
        write_state({"bonus": b})
        return out


def bonus_block(state):
    """The visitor's bonus answers as prompt lines, or "" if there are none."""
    ans = ((state.get("bonus") or {}).get("answered")) or []
    return "\n".join(f"- {a['question']} -> {a['answer']}" for a in ans)


def _bonus_known(cfg, state):
    vis = state.get("visitor") or {}
    poc = state.get("poc") or {}
    b = state.get("bonus") or {}
    lines = [
        f"Industry: {industry_prose(cfg, vis.get('industry')) or 'not stated'}",
        f"Their problem, in their words: {vis.get('problem') or 'not stated'}",
        f"Where the company is based: {vis.get('company_country') or 'not stated'}",
        f"Data residency rule: {vis.get('residency') or 'not stated'}",
        f"Where their data sits today: {', '.join(state.get('platforms') or []) or 'not stated'}",
        f"Data they hold: {', '.join(state.get('held') or []) or 'not chosen yet'}",
        f"Marketplace data they will join: {', '.join(state.get('joined') or []) or 'not chosen yet'}",
    ]
    if poc.get("poc_name"):
        lines.append(f"Proof of concept so far: {poc.get('poc_name')} - "
                     f"{poc.get('summary') or poc.get('archetype') or ''}")
    asked = [f"- {a['question']} -> {a['answer']}" for a in b.get("answered") or []]
    asked += [f"- {m['question']} -> (asked, not answered yet)"
              for m in b.get("missed") or []]
    if asked:
        lines.append("Already asked:\n" + "\n".join(asked))
    topics = [x.get("topic") for x in (b.get("answered") or []) + (b.get("missed") or [])
              if x.get("topic")]
    if topics:
        lines.append("Topics already covered (do NOT ask about these again, "
                     "in any wording): " + "; ".join(topics))
    return "\n".join(lines)


def _tokens(s):
    return set(re.findall(r"[a-z0-9]{3,}", (s or "").lower()))


def _jaccard(a, b):
    a, b = _tokens(a), _tokens(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def _validate_bonus(raw, previous):
    """Shape and safety checks, plus a same-topic check. `previous` is a list
    of earlier items ({question, topic, options}). Returns a clean item or None.

    Word overlap on the question alone let paraphrases through ("which goal
    matters most" vs "what would make this most valuable first"), so a repeat
    is caught on the model's own topic label or on largely shared options."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    q = str(d.get("question") or "").strip()
    opts = d.get("options")
    if not q or len(q) > 110 or not isinstance(opts, list):
        return None
    clean = []
    for o in opts:
        o = str(o or "").strip()
        if o and len(o) <= 40 and o.lower() not in {c.lower() for c in clean}:
            clean.append(o)
    if not 2 <= len(clean) <= 4:
        return None
    text = q + " " + " ".join(clean)
    if _BONUS_ASKS_FOR_PII.search(text) or _BONUS_BANNED.search(text):
        return None
    topic = str(d.get("topic") or "").strip()[:60]
    if not topic:
        return None
    for p in previous:
        if _jaccard(q, p.get("question")) >= 0.6:
            return None
        if p.get("topic") and (topic.lower() == p["topic"].lower()
                               or _jaccard(topic, p["topic"]) >= 0.5):
            return None
        if _jaccard(" ".join(clean), " ".join(p.get("options") or [])) >= 0.45:
            return None
    return {"id": uuid.uuid4().hex[:8], "question": q, "options": clean,
            "topic": topic, "why": str(d.get("why") or "")[:200]}


def start_bonus_generation(cfg, session_id):
    """Queue the next bonus question in the background, if one is due."""
    bc = _bonus_cfg(cfg)
    if not bc.get("enabled") or not session_id:
        return
    stale = float(bc.get("generate_timeout") or 8) * 3

    def claim(b, st):
        if st.get("session_id") != session_id or st.get("queued"):
            return None
        if st.get("stage") in ("attract", "done"):
            return None
        if b.get("next"):
            return None
        since = b.get("generating_since")
        if since and time.time() - since < stale:
            return None
        if len(b.get("answered") or []) >= int(bc.get("max_answered") or 5):
            return None
        if int(b.get("offered") or 0) >= int(bc.get("max_offered") or 7):
            return None
        b["gen"] = int(b.get("gen") or 0) + 1
        b["generating_since"] = time.time()
        return b["gen"]

    gen = _bonus_mutate(claim)
    if gen:
        threading.Thread(target=_generate_bonus, args=(cfg, session_id, gen),
                         daemon=True).start()


def _generate_bonus(cfg, session_id, gen):
    bc = _bonus_cfg(cfg)
    started = time.time()
    st = read_state()
    b = st.get("bonus") or {}
    previous = [{"question": x.get("question"), "topic": x.get("topic"),
                 "options": x.get("options")}
                for x in (b.get("answered") or []) + (b.get("missed") or [])]
    prompt = str(bc.get("prompt") or "").replace("{known}", _bonus_known(cfg, st))
    mdl = (cfg.get("coco") or {}).get("complete_model") or "openai-gpt-5"
    raw, usage, err = "", {}, ""
    # Direct call, not run_complete: run_complete clears the reasoning tray,
    # which would wipe what the visitor is watching at a stop.
    for attempt in (1, 2):
        try:
            raw, usage = _complete_once(cfg, mdl, prompt,
                                        conn=bg_conn(cfg, fresh=attempt == 2))
            err = ""
            break
        except Exception as e:                                   # noqa: BLE001
            err = str(e)[:200].replace("\n", " ")
            if attempt == 1 and ("390114" in err or "expired" in err.lower()
                                 or "closed" in err.lower()):
                continue
            break
    item = None if err else _validate_bonus(raw, previous)
    secs = time.time() - started
    late = secs > float(bc.get("generate_timeout") or 8) * 2
    log_cost(cfg, "bonus_question", secs, usage, bool(item), "complete", mdl,
             error=err or (None if item else "invalid or late"),
             note=(item or {}).get("why") or (raw or "")[:200])

    def land(b, st):
        b["generating_since"] = None
        if st.get("session_id") != session_id or int(b.get("gen") or 0) != gen:
            return                          # a reset or newer request won
        if not item or late or st.get("queued"):
            b["failed"] = int(b.get("failed") or 0) + 1
            return
        b["next"] = item
        b["offered"] = int(b.get("offered") or 0) + 1
    _bonus_mutate(land)


def compute_score(cfg, state, final=False):
    """Server-side score. Never decreases within a visit. `final` counts the
    postbox as done, for the SESSIONS row written just before that turn ends."""
    sc = _bonus_cfg(cfg).get("scoring") or {}
    b = state.get("bonus") or {}
    ans = b.get("answered") or []
    pts = sum(int(a.get("points") or 0) for a in ans)
    stops = {t.get("location") for t in state.get("turns") or []}
    if final:
        stops.add("postbox")
    pts += int(sc.get("stop") or 0) * len(
        stops & {"library", "marketplace", "workshop", "postbox"})
    limit = (cfg.get("event") or {}).get("time_limit_seconds")
    started = state.get("started_at")
    if (limit and started and "postbox" in stops
            and (time.time() if final else (state.get("updated_at") or time.time()))
            - started <= limit):
        pts += int(sc.get("under_time") or 0)
    if len(ans) >= int(_bonus_cfg(cfg).get("max_answered") or 5):
        pts += int(sc.get("all_answered") or 0)
    return pts


def map_points(cfg, left_ms):
    """Points for a bonus answered on the map. Never below scoring.map (100);
    the faster the visitor reached it, the bigger the multiplier, up to
    scoring.map_max_multiplier at the instant it spawned. Rounded to tens."""
    bc = _bonus_cfg(cfg)
    sc = bc.get("scoring") or {}
    base = int(sc.get("map") or 100)
    mx = float(sc.get("map_max_multiplier") or 1)
    total = float(bc.get("spawn_seconds") or 7) * 1000
    try:
        frac = min(1.0, max(0.0, float(left_ms) / total))
    except (TypeError, ValueError):
        frac = 0.0
    return int(round(base * (1 + (mx - 1) * frac) / 10.0) * 10)


def bonus_answer(cfg, item_id, choice, where, left_ms=0):
    """Record an answer. Returns (ok, error, points)."""
    sc = _bonus_cfg(cfg).get("scoring") or {}

    def apply(b, st):
        if st.get("queued"):
            return False, "blueprint already sent", 0
        if len(b.get("answered") or []) >= int(_bonus_cfg(cfg).get("max_answered") or 5):
            return False, "bonus limit reached", 0
        nxt = b.get("next") or {}
        missed = b.get("missed") or []
        if where == "map" and nxt.get("id") == item_id:
            item = nxt
            # The client reports how much of the countdown was left; it is
            # clamped to the configured window, so the most it can claim is
            # the top multiplier.
            pts = map_points(cfg, left_ms)
        elif where == "postbox" and any(m["id"] == item_id for m in missed):
            item = next(m for m in missed if m["id"] == item_id)
            pts = int(sc.get("postbox") or 0)
        else:
            return False, "unknown bonus", 0
        # Validate BEFORE touching state: a rejected tap must not consume it.
        if choice not in item.get("options", []):
            return False, "not one of the options", 0
        if where == "map":
            b["next"] = None
        else:
            b["missed"] = [m for m in missed if m["id"] != item_id]
        b.setdefault("answered", []).append(
            {"id": item_id, "question": item["question"], "answer": choice,
             "topic": item.get("topic"), "options": item.get("options"),
             "where": where, "points": pts, "at": time.time()})
        return True, "", pts
    return _bonus_mutate(apply)


def bonus_miss(cfg, item_id):
    def apply(b, st):
        nxt = b.get("next") or {}
        if nxt.get("id") != item_id:
            return False
        b["next"] = None
        b.setdefault("missed", []).append(
            {k: nxt.get(k) for k in ("id", "question", "options", "topic")})
        return True
    return _bonus_mutate(apply)


_board_cache = {"at": 0.0, "rows": None}


def invalidate_scoreboard():
    _board_cache["at"] = 0.0


def scoreboard(cfg, session_id, limit=10):
    """Today's board for THIS account, read from SESSIONS. Nothing local."""
    if time.time() - _board_cache["at"] > 15 or _board_cache["rows"] is None:
        sf = cfg.get("snowflake") or {}
        tbl = (f"{sf.get('database')}.{sf.get('schema')}."
               f"{sf.get('sessions_table', 'SESSIONS')}")
        sql = (f"SELECT FIRST_NAME, SCORE, SESSION_ID FROM {tbl} "
               "WHERE SE_OPERATOR = CURRENT_ACCOUNT() "
               "AND SESSION_TS >= DATE_TRUNC('day', CURRENT_TIMESTAMP()) "
               "AND SCORE IS NOT NULL ORDER BY SCORE DESC, SESSION_TS ASC "
               "LIMIT 500")
        rows = None
        for attempt in (1, 2):
            try:
                cur = bg_conn(cfg, fresh=attempt == 2).cursor()
                try:
                    cur.execute(sql)
                    rows = cur.fetchall()
                finally:
                    cur.close()
                break
            except Exception as e:                               # noqa: BLE001
                msg = str(e)
                if attempt == 1 and ("390114" in msg or "expired" in msg.lower()
                                     or "closed" in msg.lower()):
                    continue
                print(f"[loco4coco] scoreboard query failed: {msg[:150]}")
                return None
        _board_cache.update(at=time.time(), rows=rows or [])
    rows = _board_cache["rows"]
    rank = next((i + 1 for i, r in enumerate(rows) if r[2] == session_id), None)
    return {"top": [{"name": r[0] or "Penguin", "score": int(r[1] or 0),
                     "you": r[2] == session_id} for r in rows[:limit]],
            "rank": rank, "total": len(rows)}


# ------------------------------------------------------------------ HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "Loco4CoCo"
    # Timestamp of the last HTTP request. The idle watchdog uses it to stop a
    # forgotten server so it can never linger (and never burns anything: the
    # server is idle when no browser is polling).
    last_request = time.time()

    def log_message(self, fmt, *args):
        if "/api/state" not in (self.path or ""):
            super().log_message(fmt, *args)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        Handler.last_request = time.time()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"), MIME[".json"])

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            cfg, st = load_config(), read_state()
            # Self-heal: a draft flag older than the workshop budget (+ margin)
            # has no thread behind it any more (server restarted, or a crash),
            # so clear it rather than leave the postbox "preparing" forever.
            wl = (cfg.get("locations") or {}).get("workshop") or {}
            if (st.get("workshop_pending") or st.get("poc_pending")) and \
                    st.get("workshop_started") and \
                    time.time() - st["workshop_started"] > \
                    float(wl.get("timeout") or 60) + 90:
                st = write_state({"workshop_pending": False, "poc_pending": False})
            b = dict(st.get("bonus") or {})
            if b.get("next"):
                b["next"] = {k: v for k, v in b["next"].items() if k != "why"}
            st["bonus"] = b
            st["score"] = compute_score(cfg, st)
            return self._json(st)
        if path == "/api/scoreboard":
            cfg, st = load_config(), read_state()
            board = scoreboard(cfg, st.get("session_id"))
            if board is None:
                return self._json({"ok": False}, 503)
            board.update(ok=True, score=compute_score(cfg, st))
            return self._json(board)
        if path == "/api/config":
            return self._json(load_config())
        if path == "/api/options":
            cfg, st = load_config(), read_state()
            return self._json({loc: options_for(cfg, loc, st)
                               for loc in (cfg.get("locations") or {})})
        if path == "/api/blueprint":
            # The visitor must leave with something even if the email never
            # arrives, so the finished blueprint is readable on screen.
            cfg, st = load_config(), read_state()
            poc = st.get("poc") or {}
            # weakest_point and reply are internal; strip before rendering to
            # the visitor.
            safe_poc = {k: v for k, v in poc.items()
                        if k not in ("weakest_point", "reply")}
            return self._json({
                "poc": safe_poc,
                "held": st.get("held") or [],
                "joined": st.get("joined") or [],
                "listings": st.get("joined_listings") or [],
                "features": [{"name": n, "url": u}
                             for n, u in link_features(poc.get("features"))],
                "considerations": poc.get("considerations") or [],
                "prompt": build_coco_prompt(cfg, st) if st.get("poc") else "",
                "document_url": st.get("blueprint_url") or "",
                "signup_url": (cfg.get("delivery") or {}).get("signup_url", ""),
                "email": (st.get("visitor") or {}).get("email", ""),
                "queued": bool(st.get("queued")),
                "draft_created": bool(st.get("draft_created")),
            })
        if path == "/api/qr":
            # Tier 2 of delivery: the presigned URL as a scannable SVG so the
            # visitor leaves with the document even if no email ever goes out.
            # Rendered server-side with segno (pure Python) rather than a
            # vendored JS encoder, so what they scan comes from a library we
            # can verify rather than hand-rolled bit placement.
            st = read_state()
            url = (parse_qs(urlparse(self.path).query).get("u") or
                   [st.get("blueprint_url") or ""])[0]
            if not url:
                return self._send(404, b"no document url yet")
            try:
                import io
                import segno
                buf = io.BytesIO()
                segno.make(url, error="M").save(
                    buf, kind="svg", scale=5, border=2,
                    dark="#0A121A", light="#FFFFFF")
                return self._send(200, buf.getvalue(), MIME[".svg"])
            except ImportError:
                return self._send(501, b"segno not installed")
            except Exception as e:                                # noqa: BLE001
                return self._send(500, str(e)[:200].encode("utf-8"))
        if path == "/api/delivery/check":
            return self._delivery_check()
        if path == "/api/admin/status":
            return self._admin_status()
        if path == "/admin":
            return self._file("admin.html")
        if path in ("/", "/index.html"):
            return self._file("index.html")
        safe = posixpath.normpath(path).lstrip("/")
        if not safe or ".." in safe:
            return self._send(404, b"not found")
        return self._file(safe)

    def do_POST(self):
        path = urlparse(self.path).path
        routes = {
            "/api/intake": self._intake,
            "/api/suggest": self._suggest,
            "/api/home": self._home,
            "/api/select": self._select,
            "/api/compose": self._compose,
            "/api/ask": self._ask,
            "/api/send": self._post_it,
            "/api/reset": self._reset,
            "/api/admin/apply": self._admin_apply,
            "/api/admin/restart": self._admin_restart,
            "/api/state": self._patch,
            "/api/card": self._card,
            "/api/bonus/answer": self._bonus_answer,
            "/api/bonus/miss": self._bonus_miss,
        }
        fn = routes.get(path)
        if not fn:
            return self._send(404, b"not found")
        return fn()

    def _card(self):
        """Stage the share card and presign it, so it can leave on a phone.

        The card is drawn client-side on a canvas, and the browser's own
        `a.download` was the original handover - which cannot work: the booth
        machine is a shared demo laptop the visitor never logs into and walks
        away from (see CONSTRAINTS.md). The bytes therefore come back here as a
        data URL, get written as a real PNG, staged and presigned, and go home as
        a QR code. A presigned .png serves as octet-stream like everything else,
        which for an image on a phone is exactly right: it saves to Files or
        Photos and is theirs.
        """
        body = self._body() or {}
        raw = (body.get("png") or "").strip()
        if "," in raw:
            raw = raw.split(",", 1)[1]
        if not raw:
            return self._json({"error": "no image"}, 400)
        st = read_state()
        if st.get("card_url"):                     # idempotent, like POST IT
            return self._json({"url": st["card_url"]})
        try:
            png = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error):
            return self._json({"error": "not base64"}, 400)
        if len(png) > 4_000_000 or png[:8] != b"\x89PNG\r\n\x1a\n":
            return self._json({"error": "not a png"}, 400)
        sid = st.get("session_id") or "visitor"
        name = "card-%s.png" % re.sub(r"[^A-Za-z0-9_-]", "", sid)[:40]
        path = os.path.join(tempfile.gettempdir(), name)
        with open(path, "wb") as fh:
            fh.write(png)
        url, err = stage_and_presign(load_config(), path)
        if not url:
            print("[loco] card presign failed:", err or "?")
            return self._json({"error": err or "presign failed"}, 502)
        write_state({"card_url": url})
        return self._json({"url": url})

    def _admin_status(self):
        """What the operator needs to see at a glance, and nothing a visitor
        would care about. Cheap: no Snowflake round trip, so it can be polled."""
        cfg = load_config()
        ev = cfg.get("event") or {}
        coco = cfg.get("coco") or {}
        try:
            stall = len(load_marketplace(ev.get("market_profile") or "uk") or {})
        except Exception:                                        # noqa: BLE001
            stall = None
        # WHERE the closed lists came from, and how many rows this event's profile
        # actually has. This is here because the failure it catches is silent: the
        # Snowflake read falls back to the committed bundle on any error, and a
        # stale bundle serves LONDON's datasets at a Paris booth with a full stall,
        # eight industries and nothing in the log. Measured once for real.
        source, prof_rows = "unknown", None
        try:
            data, source = bctx.load(conn=coco_connection())
            want = ev.get("market_profile") or "uk"
            prof_rows = sum(1 for r in (data.get("listings") or [])
                            if r.get("market_profile") == want)
        except Exception:                                        # noqa: BLE001
            pass
        warm = None
        if coco.get("warm_agent", True):
            try:
                warm = bool(agent_pool.get_agent().alive())
            except Exception:                                    # noqa: BLE001
                warm = False
        st = read_state()
        return self._json({
            "venue": ev.get("venue") or "",
            "venues": [k for k in (cfg.get("venues") or {})
                       if not k.startswith("_")],
            "city": ev.get("city") or "",
            "language": ev.get("language") or "",
            "marketplace_region": ev.get("marketplace_region") or "",
            "market_profile": ev.get("market_profile") or "",
            "complete_model": coco.get("complete_model") or "",
            "qa_model": (cfg.get("qa") or {}).get("model") or "",
            # Whether the model was actually proven to answer, not just
            # configured. A configured-but-dead model is the failure that hid
            # behind the exec fallback for a whole test run.
            "model_checked": PREFLIGHT.get("checked"),
            "model_verified": PREFLIGHT.get("model") or "",
            "model_note": PREFLIGHT.get("note") or "",
            "model_rejected": "; ".join(PREFLIGHT.get("tried") or []),
            # Whether the configured connection actually authenticated at
            # startup. A stale or placeholder connection_name is the postbox
            # failure: it only shows at delivery, with a visitor watching.
            "connection_checked": CONN_PREFLIGHT.get("checked"),
            "connection_ok": CONN_PREFLIGHT.get("ok"),
            "connection_note": CONN_PREFLIGHT.get("detail") or "",
            "warm_agent": warm,
            "industries_on_stall": stall,
            "context_source": source,
            "listings_for_profile": prof_rows,
            "connection": coco_connection() or "",
            # A visitor counts as present only if they have told us something or
            # moved off the attract loop. `bool(st.get("visitor"))` was always
            # true, because the default state carries a visitor dict of empty
            # strings, so the panel could never report an idle booth and "Clear
            # visitor" appeared to do nothing.
            "visitor_active": bool(
                (st.get("stage") or "") not in ("", "attract")
                or any((st.get("visitor") or {}).get(k)
                       for k in ("first_name", "company", "industry", "problem"))
            ),
            "stage": st.get("stage") or "",
            "bonus": {
                "enabled": bool((cfg.get("bonus") or {}).get("enabled")),
                "offered": int((st.get("bonus") or {}).get("offered") or 0),
                "answered": len((st.get("bonus") or {}).get("answered") or []),
                "missed": len((st.get("bonus") or {}).get("missed") or []),
                "failed": int((st.get("bonus") or {}).get("failed") or 0),
                "score": compute_score(cfg, st),
            },
        })

    def _admin_apply(self):
        """Change the venue without restarting anything.

        config.json is re-read by load_config() on every request, so the only
        thing that would go stale is the per-profile marketplace cache - flush it
        and the next visitor sees the new city's stall. The venue must be one the
        config actually defines: an unknown venue silently falls through to the
        hand-configured event block, which is how you end up running Paris with
        London's datasets.
        """
        body = self._body() or {}
        venue = str(body.get("venue") or "").strip().lower()
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        known = [k for k in (raw.get("venues") or {}) if not k.startswith("_")]
        if venue and venue not in known:
            return self._json({"error": "unknown venue %r; known: %s"
                                        % (venue, ", ".join(known))}, 400)
        ev = raw.setdefault("event", {})
        if venue:
            ev["venue"] = venue
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
        global _market_cache
        _market_cache = None
        return self._admin_status()

    def _admin_restart(self):
        """Re-exec this process in place.

        Config and venue changes do NOT need this - they are picked up on the
        next request. This exists for the two cases that genuinely need a new
        process: server.py was edited, and the warm agent pool wedged (a
        cancelled in-flight call can leave it unable to answer, and no amount of
        config reloading fixes that).

        Reply first, then re-exec on a short timer, or the operator's browser
        gets a dead socket instead of a confirmation.
        """
        self._json({"restarting": True})

        def go():
            time.sleep(0.4)
            try:
                agent_pool.get_agent().stop()
            except Exception:                                    # noqa: BLE001
                pass
            os.execv(sys.executable, [sys.executable] + sys.argv)

        threading.Thread(target=go, daemon=True).start()

    def _reset(self):
        # replace=True, so nothing from the previous visitor survives.
        st = write_state(dict(BLANK_STATE), replace=True)
        # Clear the visitor-named blueprint files from the OS temp dir too.
        purge_temp_artifacts()
        # Recycle the warm agent too. It probed stateless between calls, but a
        # shared booth must not merely trust that: restarting the process on
        # reset makes it impossible for one visitor's words to reach the next,
        # even if a future CLI build changed the agent's memory behaviour. Only
        # if it is actually running - do not spawn one when warming is off. Done
        # off-thread so reset stays instant; it re-warms on the next turn.
        try:
            ag = agent_pool.get_agent()
            if ag.alive():
                threading.Thread(target=ag.restart, daemon=True).start()
        except Exception:                                        # noqa: BLE001
            pass
        return self._json(st)

    def _patch(self):
        patch = self._body()
        if not isinstance(patch, dict):
            return self._json({"error": "expected an object"}, 400)
        # The bonus record drives the server-side score; the client never
        # writes it directly.
        patch.pop("bonus", None)
        return self._json(write_state(patch))

    def _bonus_answer(self):
        b = self._body()
        if not isinstance(b, dict):
            return self._json({"error": "invalid json"}, 400)
        cfg = load_config()
        where = "postbox" if b.get("where") == "postbox" else "map"
        ok, err, pts = bonus_answer(cfg, str(b.get("id") or "")[:16],
                                    str(b.get("choice") or "")[:60], where,
                                    b.get("left_ms") or 0)
        st = read_state()
        if ok and where == "map":
            start_bonus_generation(cfg, st.get("session_id"))
        return self._json({"ok": ok, "error": err, "points": pts,
                           "score": compute_score(cfg, st)}, 200 if ok else 409)

    def _bonus_miss(self):
        b = self._body()
        if not isinstance(b, dict):
            return self._json({"error": "invalid json"}, 400)
        cfg = load_config()
        ok = bonus_miss(cfg, str(b.get("id") or "")[:16])
        start_bonus_generation(cfg, read_state().get("session_id"))
        return self._json({"ok": bool(ok)}, 200 if ok else 409)

    def _suggest(self):
        """Write a starting problem statement FOR the visitor, on request.

        The problem field is the highest-value thing on the letter and the
        hardest to fill in cold at a noisy stand. This is the "CoCo choose"
        path: one fast completion, grounded in material that already exists and
        is closed - the industry's own data_sources labels, and the nine
        VISITOR_PAIN lines, which are already written in a visitor's voice.

        So the model is rephrasing our own catalogue into one sentence, not
        inventing a use case. It lands in an editable field: the visitor is
        expected to correct it, and everything downstream keys off what they
        finally submit rather than off this.
        """
        b = self._body()
        if not isinstance(b, dict):
            return self._json({"error": "invalid json"}, 400)
        cfg = load_config()
        industry = b.get("industry") or ""
        inds = cfg.get("industries") or {}
        if industry not in inds:
            industry = "other"
        ind = inds.get(industry) or {}
        ind_name = ind.get("name") or "their sector"
        company = (b.get("company") or "").strip()[:120]

        held = []
        for d in (ind.get("data_sources") or []):
            lab = d.get("label") if isinstance(d, dict) else d
            if lab:
                held.append(str(lab))

        pains = []
        try:
            import context
            bundle = context.load(prefer_snowflake=False)
            for a in (bundle.get("archetypes") or []):
                p = str(a.get("pain") or "").strip()
                if p:
                    pains.append(f"- {a.get('friendly')}: {p}")
        except Exception:                                        # noqa: BLE001
            pains = []

        who = f" at {company}" if company else ""
        prompt = (
            f"A visitor{who} works in {ind_name}. Write ONE problem statement in "
            f"their voice, as they would type it into a form, describing "
            f"something worth building with data and AI.\n\n"
            f"Data an organisation like theirs typically holds:\n"
            + "\n".join(f"- {h}" for h in held) + "\n\n"
            f"The kinds of problem this booth can actually build for, with the "
            f"complaint people usually make:\n" + "\n".join(pains) + "\n\n"
            "Rules. Two sentences, maximum 32 words total. First sentence is the "
            "problem as it is felt day to day; second is what good would look "
            "like. Plain first-person plural, no jargon, no product names, no "
            "buzzwords, no em dashes, and never mention Snowflake or AI by name. "
            "Be specific to their sector rather than generic. Return ONLY the "
            "two sentences, with no quotes, no preamble and no explanation."
        )
        loc = {"transport": "complete"}
        ok, raw, _meta = run_turn(cfg, prompt, "letter_suggest", loc=loc)
        text = re.sub(r"\s+", " ", str(raw or "")).strip().strip('"').strip()
        # A refusal, an empty answer or an essay are all failures. Falling back
        # to the industry's own placeholder means the button always does
        # something useful, and never leaves a wall of prose in the field.
        if not ok or not text or len(text) > 320:
            text = str(ind.get("problem_placeholder") or "").strip()
            text = re.sub(r"^e\.g\.\s*", "", text)
            return self._json({"ok": True, "problem": text, "source": "fallback"})
        return self._json({"ok": True, "problem": text, "source": "model"})

    def _intake(self):
        b = self._body()
        if not isinstance(b, dict):
            return self._json({"error": "invalid json"}, 400)
        cfg = load_config()
        first = (b.get("first_name") or "").strip()[:80]
        company = (b.get("company") or "").strip()[:120]
        email = (b.get("email") or "").strip()[:160]
        # Two sentences, generously bounded. This is the single highest-value
        # field on the form: it is the only place the visitor says WHY, and it
        # arrives before they have walked anywhere.
        problem = (b.get("problem") or "").strip()[:400]
        # Email is optional and no longer collected by the letter. Keep
        # accepting it so an operator-run intake can still supply one.
        if email and "@" not in email:
            email = ""
        if not first:
            return self._json({"error": "need a first name"}, 400)
        industry = b.get("industry") or infer_industry(cfg, company)
        if industry not in (cfg.get("industries") or {}):
            industry = "other"
        st = write_state({
            "visitor": {"first_name": first, "company": company,
                        "email": email, "industry": industry,
                        "problem": problem},
            "stage": "library", "unlocked": ["library"], "reasoning": [],
            "session_id": uuid.uuid4().hex,
            # A new visitor never inherits the last one's draft flags or POC.
            "workshop_pending": False, "poc_pending": False,
            "workshop_started": None,
        })
        start_bonus_generation(cfg, st.get("session_id"))
        # Tier 0 is NO LONGER fired here. Racing the Library with a 75-110s call
        # never worked, and the curated six must be deterministic at the stall.
        # Agentic search is now the 7th option: triggered only when the visitor
        # TYPES into "something else" at the marketplace - see run_checklist().
        return self._json({"ok": True, "industry": industry,
                           "industry_name": industry_name(cfg, industry),
                           "state": st})

    def _home(self):
        """Home-stage answers: where the data lives, where the company is based,
        and the residency rule. Captured before the Library now (moved off the
        Library's second question 2026-08-25). Enriches the blueprint; never
        gates correctness, so a failure here is not fatal to the visit.
        """
        b = self._body()
        if not isinstance(b, dict):
            return self._json({"error": "invalid json"}, 400)
        plats = [str(x)[:60] for x in (b.get("platforms") or []) if str(x).strip()]
        country = (b.get("company_country") or "").strip()[:120]
        # A closed vocabulary of TOKENS, so the blueprint's region logic can
        # still rely on it while the gate stays multi-select. Validating the
        # whole string collapsed "country_only, us_ok" to "unsure" and threw the
        # answer away.
        allowed = ("country_only", "eu", "us_ok", "unsure")
        got = (b.get("residency") or "").strip().lower()
        picks = [k for k in allowed if k in got]
        residency = ", ".join(picks) if picks else "unsure"
        cur = read_state()
        vis = dict(cur.get("visitor") or {})
        vis["company_country"] = country
        vis["residency"] = residency
        # The house is a location now, so its exchanges belong in the history
        # the visitor can scroll back through. The client sends the wording it
        # actually displayed, so the recalled reply matches what they saw. Only
        # the region and residency questions are logged - the identity answers
        # (company, persona name) are deliberately not recallable.
        turns = list(cur.get("turns") or [])
        for ex in (b.get("exchanges") or [])[:4]:
            if not isinstance(ex, dict):
                continue
            txt = str(ex.get("text") or "").strip()[:240]
            rep = str(ex.get("reply") or "").strip()[:1200]
            if txt and rep:
                turns.append({"location": "house", "text": txt,
                              "reply": rep, "at": time.time()})
        patch = {"platforms": plats[:8], "visitor": vis}
        if len(turns) != len(cur.get("turns") or []):
            patch["turns"] = turns
        st = write_state(patch)
        return self._json({"ok": True, "state": st})

    def _select(self):
        b = self._body()
        if not isinstance(b, dict):
            return self._json({"error": "invalid json"}, 400)
        cfg = load_config()
        loc_id = b.get("location") or ""
        if loc_id not in (cfg.get("locations") or {}):
            return self._json({"error": f"unknown location {loc_id!r}"}, 400)
        labels = [str(x)[:120] for x in (b.get("labels") or []) if str(x).strip()]
        # Platforms are captured on the home stage now (see _home), not here.
        if not labels:
            return self._json({"error": "nothing selected"}, 400)
        job = uuid.uuid4().hex
        loc = (cfg.get("locations") or {}).get(loc_id) or {}
        write_state({"thinking": True, "reply": "", "reasoning": [],
                     "transport": loc.get("transport") or "exec",
                     "job_id": job, "location": loc_id, "stage": loc_id})
        threading.Thread(target=run_checklist,
                         args=(cfg, loc_id, labels, job), daemon=True).start()
        return self._json({"accepted": True, "job_id": job})

    def _compose(self):
        b = self._body()
        if not isinstance(b, dict):
            return self._json({"error": "invalid json"}, 400)
        text = (b.get("text") or "").strip()[:500]
        if not text:
            return self._json({"error": "empty message"}, 400)
        cfg = load_config()
        job = uuid.uuid4().hex
        wloc = (cfg.get("locations") or {}).get("workshop") or {}
        if wloc.get("background"):
            st0 = read_state()
            # One draft per visitor: a second submit would race the first.
            if st0.get("workshop_pending") or (st0.get("poc") or {}).get("poc_name"):
                return self._json({"error": "workshop already submitted"}, 409)
            # Do not hold the visitor at the bench. CoCo acknowledges at once,
            # the postbox unlocks, and the agent drafts the POC while they walk
            # (and collect bonuses). Its real reply lands in the bubble when it
            # is ready, and the postbox waits for it only if they get there
            # first. job_id is not passed to the worker: the postbox sets a new
            # one, which must not count as superseding this draft.
            st0 = read_state()
            write_state({"thinking": False, "reasoning": [],
                         "reply": wloc.get("background_ack") or "On it.",
                         "transport": wloc.get("transport") or "exec",
                         "job_id": job, "location": "workshop", "stage": "workshop",
                         "workshop_pending": True, "poc_pending": True,
                         "workshop_started": time.time(),
                         "unlocked": unlock_next(cfg, "workshop", st0)})
            threading.Thread(target=run_workshop,
                             args=(cfg, text, None, st0.get("session_id")),
                             daemon=True).start()
            start_bonus_generation(cfg, st0.get("session_id"))
            return self._json({"accepted": True, "job_id": job, "background": True})
        write_state({"thinking": True, "reply": "", "reasoning": [],
                     "transport": wloc.get("transport") or "exec",
                     "job_id": job, "location": "workshop", "stage": "workshop"})
        threading.Thread(target=run_workshop, args=(cfg, text, job),
                         daemon=True).start()
        return self._json({"accepted": True, "job_id": job})

    def _ask(self):
        b = self._body()
        if not isinstance(b, dict):
            return self._json({"error": "invalid json"}, 400)
        text = (b.get("text") or "").strip()[:300]
        if not text:
            return self._json({"error": "empty question"}, 400)
        cfg = load_config()
        if not (cfg.get("ask") or {}).get("enabled", True):
            return self._json({"error": "ask is disabled"}, 400)
        st = read_state()
        if not st.get("poc"):
            return self._json({"error": "no POC to ask about yet"}, 400)
        job = uuid.uuid4().hex
        write_state({"thinking": True, "reply": "", "reasoning": [],
                     "transport": (cfg.get("ask") or {}).get("transport", "exec"),
                     "job_id": job, "location": "ask", "stage": "ask"})
        threading.Thread(target=run_ask, args=(cfg, text, job),
                         daemon=True).start()
        return self._json({"accepted": True, "job_id": job})

    def _post_it(self):
        cfg = load_config()
        st = read_state()
        # No email gate. The letter no longer asks for one: the QR handover is
        # the delivery that cannot fail, and the outbox draft is a bonus an
        # operator drains when there IS an address. Requiring one here is what
        # made SEND MY POC dead-end with a 400 on every press.
        if not st.get("poc") and not st.get("workshop_pending"):
            return self._json({"error": "no POC to send yet"}, 400)
        # Already delivered for this visitor: hand back the same result rather
        # than re-staging the document and logging a duplicate SESSIONS row.
        if st.get("queued") and st.get("blueprint_url"):
            return self._json({"accepted": True, "already": True,
                               "blueprint_url": st.get("blueprint_url")})
        # Stage 2 may still be running if the visitor sprinted here. Give it a
        # moment, then go with what we have: the blueprint is already complete
        # from catalogue defaults, so this is a quality wait, not a hard one.
        waited = 0.0
        while read_state().get("poc_pending") and waited < 8.0:
            time.sleep(0.25)
            waited += 0.25
        job = uuid.uuid4().hex
        write_state({"thinking": True, "reply": "", "reasoning": [],
                     "transport": "none",
                     "job_id": job, "location": "postbox", "stage": "postbox"})
        threading.Thread(target=run_send, args=(cfg, job), daemon=True).start()
        return self._json({"accepted": True, "job_id": job})

    def _delivery_check(self):
        """Per-stand preflight. Names the leg that is broken rather than failing
        silently at the postbox with a visitor watching."""
        cfg = load_config()
        d = cfg.get("delivery") or {}
        checks = []

        def add(name, ok, detail=""):
            checks.append({"check": name, "ok": bool(ok), "detail": detail})

        try:
            import docx                                          # noqa: F401
            add("word_library", True, "python-docx importable")
        except Exception as e:                                    # noqa: BLE001
            add("word_library", False, f"pip install python-docx ({e})")

        resolved = snow_bin(cfg)
        add("snow_cli", snow_findable(cfg),
            resolved if snow_findable(cfg) else
            f"{resolved!r} not found - pip install snowflake-cli, or set "
            f"snowflake.snow_binary in config.json to the absolute path")

        # CoCo-revert canary: the booth connection must stay key-pair so no browser
        # or keychain prompt interrupts a visitor. Unknown (placeholder) does not
        # block; only an actively browsery connection is a fail.
        method, safe, detail = booth_auth_method(cfg)
        add("booth_auth", safe is not False, detail or method)

        stage = d.get("stage") or ""
        add("stage_configured", bool(stage), stage or "delivery.stage is empty")
        if stage:
            rows, err = snow_sql(cfg, f"LIST {stage}")
            add("stage_reachable", err == "", err or f"{len(rows or [])} objects")

        # End-to-end proof: stage a tiny file and presign it. This is the exact
        # path a visitor's document takes, so a green here means Tier 2 works.
        try:
            probe = os.path.join(tempfile.gettempdir(), "loco4coco-preflight.txt")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("loco4coco preflight\n")
            url, err = stage_and_presign(cfg, probe)
            add("presign_works", bool(url), err or "presigned URL returned")
        except Exception as e:                                    # noqa: BLE001
            add("presign_works", False, str(e)[:160])

        add("delivery_transport", True,
            "QR to the presigned .docx is the handover; no email, no local record")

        ok = all(c["ok"] for c in checks
                 if c["check"] != "delivery_transport")
        return self._json({"ok": ok, "checks": checks,
                           "stage": stage,
                           "transport": d.get("transport")})

    def _file(self, rel):
        full = os.path.join(HERE, rel)
        if not os.path.isfile(full):
            return self._send(404, b"not found")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=cfg["server"]["host"])
    ap.add_argument("--port", type=int, default=cfg["server"]["port"])
    args = ap.parse_args()

    if not os.path.exists(STATE_PATH):
        write_state(dict(BLANK_STATE))

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    # Nothing can be drafting at startup: a pending flag left in state.json by
    # a previous process (killed mid-draft) would otherwise strand the visitor.
    try:
        if os.path.exists(STATE_PATH):
            write_state({"workshop_pending": False, "poc_pending": False})
    except Exception:                                            # noqa: BLE001
        pass
    print(f"Loco4CoCo companion running at http://{args.host}:{args.port}/")
    print(f"  event     : {cfg['event']['city']} ({cfg['event']['language']})")
    print(f"  account   : {cfg['snowflake']['connection_name']}")
    print(f"  transport : {(cfg.get('delivery') or {}).get('transport')}")
    print(f"  guides    : {len(load_guides())} primary forks loaded")
    if snow_findable(cfg):
        print(f"  snow CLI  : {snow_bin(cfg)}")
    else:
        print(f"  snow CLI  : *** NOT FOUND ({snow_bin(cfg)!r}) ***")
        print(f"              Blueprint delivery will fail at the end of every visit.")
        print(f"              Fix: pip install snowflake-cli, or set snowflake.snow_binary")
        print(f"              in config.json to the absolute path.")
    # No stand field to check any more. Rows are attributed by account, which is
    # resolved in the warm thread and needs nothing from the operator.
    # Warm the model so the first real visitor does not pay cold-start latency.
    #
    # This used to spawn a throwaway `cortex exec`, which warmed almost nothing:
    # exec has no --resume or --session, so the next call was a cold process
    # again regardless. Now it starts the persistent `cortex mcp serve` process
    # and takes one cheap turn on it, because the first call on a fresh process
    # is measurably slower than the rest (~9.4s against ~6.0s, and ~3.4s once
    # settled). That cost belongs before doors open, not on visitor one.
    if (cfg.get("coco") or {}).get("warm_up", True):
        def _warm():
            c = cfg.get("coco") or {}
            if c.get("warm_agent", True):
                agent = agent_pool.get_agent(
                    connection=coco_connection(), model=c.get("model"),
                    workdir=HERE, log=lambda m: print("[loco4coco] " + m),
                    binary=c.get("binary"))
                ok, note = agent.warm()
                print("  warm-up   : warm agent %s (%s)"
                      % ("ready" if ok else "FAILED", note))
                if ok:
                    return
                # Fall through: if the pool will not start, the booth is about to
                # run on `cortex exec`, so warm whatever that can benefit from.
            cmd = [c.get("binary", "cortex"), "exec",
                   "Reply with the single word: ready.", "--no-mcp"]
            conn = (cfg.get("snowflake") or {}).get("connection_name")
            if conn:
                cmd += ["--connection", conn]
            if c.get("model"):
                cmd += ["-m", str(c["model"])]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, cwd=HERE, timeout=90)
            except Exception:                                    # noqa: BLE001
                pass
        threading.Thread(target=_warm, daemon=True).start()
        print("  warm-up   : dispatched")
    # Open the Snowflake connection now so the first visitor's fast-path turn
    # pays ~2s, not the ~5s that includes connection setup. Then prove the
    # configured model actually answers, because a model name is a perishable
    # dependency: claude-4-sonnet went to legacy state mid-project and every
    # COMPLETE turn failed silently for a whole test run while the slow exec
    # fallback covered for it. Better to find out here than in front of someone.
    def _warm_conn():
        try:
            preflight_connection(cfg)
        except Exception as e:                                    # noqa: BLE001
            print(f"[loco4coco] connection preflight skipped: {str(e)[:120]}")
        try:
            preflight_complete_model(cfg)
        except Exception as e:                                    # noqa: BLE001
            print(f"[loco4coco] model preflight skipped: {str(e)[:120]}")
    threading.Thread(target=_warm_conn, daemon=True).start()
    # Idle watchdog: OFF by default (0). A booth must never stop itself - a quiet
    # lunchtime is not a reason to be dead when the next visitor walks up, and the
    # warehouse auto-suspends on its own so an idle server costs nothing. The
    # shipped config sets this to 0; keeping the CODE default 0 too means a
    # stripped or hand-edited config can never silently reintroduce a mid-event
    # self-shutdown. Set server.idle_shutdown_minutes to a number only if you want
    # an unattended laptop to stop itself.
    idle_min = (cfg.get("server") or {}).get("idle_shutdown_minutes", 0)
    if idle_min:
        def _watchdog():
            while True:
                time.sleep(30)
                if time.time() - Handler.last_request > idle_min * 60:
                    print(f"\nidle for {idle_min} min - shutting down.")
                    srv.shutdown()
                    return
        threading.Thread(target=_watchdog, daemon=True).start()
        print(f"  idle stop : after {idle_min} min with no requests")
    print("Ctrl-C to stop.")
    # A plain `kill` (SIGTERM) used to bypass the KeyboardInterrupt path, so the
    # warm agent's `cortex mcp serve` child outlived every restart. Treat
    # SIGTERM like Ctrl-C and always stop the agent on the way out.
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        try:
            if agent_pool._agent is not None:
                agent_pool._agent.stop()
        except Exception:                                        # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
