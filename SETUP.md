# Loco for CoCo: Setup Guide

**In a hurry?** There is a one-page TL;DR version of this guide: [Loco4CoCo — TL;DR setup](https://docs.google.com/document/d/1TwRR5uurF8NHGTbQBiBQBs0fHV9lqqUZ7357WxY5e_E/edit). This document is the long form, with the reasoning behind each choice.

For someone who has never run this, on a fresh laptop, against a brand new Snowflake account.

Last updated 2026-09-21.

> **Before you change anything, read [CONSTRAINTS.md](CONSTRAINTS.md).** It states
> what the event is (Snowflake World Tour, a booth stand, five minutes, one
> visitor at a time, who is not a developer) and the six venue properties every
> change is checked against. Setting the thing up does not require it; changing
> it does.

> **Hayadata (Tel Aviv) branch — setup decisions are already made. Do not ask
> the operator about them.**
>
> - **Venue:** `tel_aviv` (already set in `game/config.json`). It serves the
>   Israel dataset profile (`il`), judged on AWS Frankfurt because Snowflake has
>   no Israel region. Never offer london/paris/berlin or ask for a city.
> - **Connection:** use the operator's default `snow` connection (the one
>   marked default in `snow connection list`; if none is marked, the only one).
>   Ask only if there are several and none is the default.
> - **Existing install:** if `~/.snowflake/cortex/plugins/loco4coco` already
>   holds this branch, reuse it — do not re-clone, back up or discard it. Pull
>   the latest with `git pull` if the working tree is clean.
> - **Repo:** `https://github.com/sfc-gh-nzaliven/loco4coco`, branch
>   `hayadata-bonus-objectives`.

## TL;DR — the whole setup is three things

You do two of them. Cortex Code does the third.

**1. Get an account.** Register at <https://go.dataops.live/emea-swt/register>. Once per
event — London, Paris and Berlin each get their own. The account name is random and you
never need it.

**2. Connect to it.**

```bash
snow connection add                  # name it e.g. MYBOOTH
snow connection test -c MYBOOTH
cortex exec "Reply with the single word: ready." --no-mcp -c MYBOOTH
```

All three must answer. If the last one says `No models available`, Cortex Code is not
authenticated — fix that before going on.

**3. Paste the prompt in the next section into Cortex Code.** It clones the repo, deploys,
loads the datasets, sets your venue and name, starts the server and proves it with a smoke
test. Then it tells you the four numbers you need to check.

That is it. Nothing to translate, no region to set, no deploy target to add, no marketplace
to re-verify, no account identifier to paste anywhere.

If you would rather drive it yourself, Steps 1–8 below are the same steps in longhand.

## Let Cortex Code do it

This is the expected route and the repo is written for it. Open Cortex Code in a terminal,
replace the three bracketed values, and paste:

```
Set up the Loco for CoCo booth game on this laptop.

Clone branch hayadata-bonus-objectives of https://github.com/sfc-gh-nzaliven/loco4coco
into ~/.snowflake/cortex/plugins/loco4coco (reuse it if it is already there).
Then read SETUP.md in that repo and follow it end to end for Hayadata, Tel Aviv
(venue tel_aviv - already set, do not ask me for a city).

Use my default snow connection; do not ask me which one unless there are
several and none is marked default.

Do all of it: install the Python packages, run deploy/bootstrap.py, run
deploy/load_context.py, run deploy/verify_context.py --all, confirm event.venue
is tel_aviv,
start game/server.py, and run game/smoke_test.py against it.

Then tell me four things: the venue, how many datasets loaded for that city,
whether the lists are being read from snowflake, and the smoke test result. Stop
and tell me if any step fails rather than working around it.
```

It will ask before anything destructive.

**One prompt you may see, and one you should never see:**

- Cortex Code asking permission to run a command. Normal; approve it.
- **macOS asking for your keychain password ("Python wants to access key
  `com.snowflake.connector.python`").** If you see this, stop and run the key-pair step in
  Step 2. A DataOps event account uses OAuth by default, which caches its token in the
  keychain and raises that dialog once per process — **312 times over a 100-visitor day**,
  including mid-visit. `python3 scripts/setup_keypair.py --connection <your-connection>`
  removes it permanently. "Always Allow" does not.


## What you end up with

A local booth game. A visitor drives a penguin round an arctic map, answers a few light
questions in under five minutes, and leaves with a POC blueprint: their idea in
their own words, the Snowflake developer guide to fork, the features with doc links, and
a kick-off prompt to paste into Cortex Code on a free trial.

It runs entirely on the laptop (a small Python server plus a browser canvas). Nothing is
hosted. The only thing it reaches out to is your Snowflake account: Cortex inference,
session logging, and the presigned stage document the visitor scans at the end.

**Budget:** about 58s of the five minutes is CoCo thinking, leaving roughly four minutes
for the human. The optional "Ask CoCo one thing" stop adds about 35s.

## Step 1: install the tools

macOS or Linux, **Python 3.11+**, plus the Snowflake CLI and Cortex Code. Then:

```bash
pip3 install snowflake-connector-python python-docx PyYAML segno
```

Check all three answer, and you are done with prerequisites:

```bash
python3 --version && snow --version && cortex --version
```

Everything else is Python standard library. No npm, no build step. After you clone
(Step 3) you can reinstall from the pinned `requirements.txt` instead, which is the
more reliable route.

`cortex` is optional but strongly recommended: it powers the Workshop and the 7th
marketplace option. Without it both fall back silently and the game still runs.

**Any version will do, and the version is not pinned.** The booth uses three
things from the CLI — `cortex exec --format json`, `cortex search marketplace` and
`cortex mcp serve` — and the third is the one that varies between releases. All
four ways it can fail (binary absent, exits immediately, serves without a
`cortex_code_agent` tool, or serves and never replies) were tested with stub
binaries, and each degrades in bounded time to `cortex exec`, so a laptop with an
older or newer CLI gets a slower booth rather than a stuck one. If the warm agent
is unavailable the startup log says so in plain words. Verified end to end on
v1.1.91.

If the CLI is installed somewhere that is not on `PATH`, set `coco.binary` in
`game/config.json` to the absolute path — all three call sites read that setting.

## Step 2: get your event account, then create a connection

**Register at <https://go.dataops.live/emea-swt/register>.** You will be assigned a
Snowflake account from a pool. **Do this once per event** - London, Paris and Berlin
each get their own account.

The account name is random (`NDL_HACKATHON_WEHBMG` and similar) and you will not know
it in advance. That is fine: **nothing in this setup asks you for the account's name or
its region.** Whichever account you are given, the steps below are identical.

The game never stores credentials. It refers to a **named connection** in your own
`~/.snowflake/connections.toml`, and Snowflake's tooling resolves it.

```bash
snow connection add                     # name it e.g. MYBOOTH
snow connection test -c MYBOOTH
cortex exec "Reply with the single word: ready." --no-mcp -c MYBOOTH
```

All three must succeed before you go further. If that last command returns
`No models available`, Cortex Code is not authenticated for this connection. Fix it
now - without it the Workshop and the 7th marketplace option quietly fall back to
COMPLETE, and you will not find out until a visitor is standing in front of you.

### Then swap to key-pair auth, as soon as you have cloned the repo

`snow connection add` gives you OAuth against a DataOps account, which caches its token in
the macOS keychain and raises a password dialog **once per process** — 10 during setup, then
3 per visitor, 312 over a 100-visitor day. One of those can land mid-visit and the stand
stops until somebody types a password. "Always Allow" does not fix it: the grant is per
binary and is lost when the token refreshes.

Key-pair auth has no token to cache, so it never touches the keychain at all. The script
lives in the repo, so run this **immediately after Step 3**:

```bash
python3 scripts/setup_keypair.py --connection MYBOOTH
```

Use the same connection name you created in Step 0. It generates an RSA key under
`~/.snowflake/keys/` (0600), registers the public half on your event user, and creates a
**separate `LOCO4COCO_BOOTH` connection** on `SNOWFLAKE_JWT` — leaving your original
connection untouched. `deploy/bootstrap.py` runs this same step automatically (on by
default, and it skips itself if `LOCO4COCO_BOOTH` already exists) and writes
`LOCO4COCO_BOOTH` into `game/config.json`, so the booth uses it for `snow` (`-c`),
`cortex exec` (`-c`) and the warm agent (which passes the connection per request).

Creating a separate connection rather than converting in place is deliberate. Cortex Code
continuously **manages the connection it is pointed at** — it rewrites `connections.toml`
and reverts `SNOWFLAKE_JWT` back to OAuth within seconds. A connection Cortex Code did not
create is left alone, so `LOCO4COCO_BOOTH` stays on key-pair permanently. Your original
connection keeps working for chatting with Cortex Code; the booth never uses it.

You may still see two or three prompts before the swap. That is expected and harmless —
what matters is that none of them can happen once the doors open.


## Step 3: get the code

It is a Cortex Code plugin, so it lives in the plugins directory:

```bash
git clone -b hayadata-bonus-objectives https://github.com/sfc-gh-nzaliven/loco4coco.git ~/.snowflake/cortex/plugins/loco4coco
cd ~/.snowflake/cortex/plugins/loco4coco
```

That is the simplest arrangement and the one the rest of this guide assumes.
If you would rather keep the checkout somewhere you actually look at, such as
`~/Desktop/Loco4CoCo`, clone it there and symlink the plugin path at it
instead. Cortex Code follows the symlink, so the plugin still loads and you
only have one copy of the code:

```bash
git clone -b hayadata-bonus-objectives https://github.com/sfc-gh-nzaliven/loco4coco.git ~/Desktop/Loco4CoCo
ln -s ~/Desktop/Loco4CoCo ~/.snowflake/cortex/plugins/loco4coco
```

Whichever you pick, keep exactly one checkout. Two copies drift, and the
`git status` you are reading will not be the code that is running.

## Step 4: there is no deploy target to add

**Skip this - there is nothing to edit.** The default `EVENT` target in
`deploy/manifest.yml` deliberately pins no account and deploys wherever your
connection points. Pasting a pool-assigned account ID into a tracked file would be
pure friction, and invites committing someone else's account identifier.

## Step 5: deploy the Snowflake objects

One command builds the database, schema, `SESSIONS` and `TURNS` tables, stage and
warehouse. Look before you leap:

```bash
python3 deploy/bootstrap.py -c MYBOOTH --plan-only
python3 deploy/bootstrap.py -c MYBOOTH
```

It also patches `game/config.json` to point at your account and runs the smoke test, so
the account is proven rather than assumed.

**There is deliberately no resource monitor, and nothing that can stop the game.**
The post-hook's job is the opposite of creating one: it detaches any monitor left by an
earlier deploy and reads the warehouse back to prove nothing is bound.

> **Why there is no cap.** The booth runs all day with a queue in front of it. A credit
> limit that trips does not save money in any meaningful sense — it ends the activation,
> in public, mid-visit, with a stranger watching. That is a far worse outcome than the
> compute it would have saved.
>
> And the compute is not the problem. Measured on an event account, the booth warehouse
> used **0.78 credits in 14 days**, against **64 credits for Cortex Code Desktop** over
> the same period. The line that moves is Cortex Code during setup, not the booth.
>
> This was got wrong twice, in opposite directions, so it is worth stating plainly. A
> monitor was first created with notify-only triggers — but a pool account's user
> usually has no email address, `NOTIFY_USERS` fails outright with "User ... does not
> have an associated email address", and **Snowflake silently drops notify triggers
> when there are no notify users**. So it reported a quota, fired nothing, warned
> nobody, and looked like a guardrail. A suspend trigger was then added to make it
> real, which made it genuinely dangerous. Neither is wanted.
>
> Cost is observed, not enforced: `game/cost.jsonl` per model call,
> `sql_statements/03-event-health.sql` per visit and per turn, and
> `SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY` for credits. If you genuinely
> need a cap on a shared account, put it on a **different** warehouse and leave
> `LOCO4COCO_WH` alone.

## Step 6: load this city's datasets

**Do not skip this. It is the step whose absence is invisible.**

```bash
python3 deploy/load_context.py --connection MYBOOTH
python3 deploy/verify_context.py --all --connection MYBOOTH
```

`verify_context.py` **exits 0 on a healthy booth**. Only `FAIL` lines gate
anything. The twelve `note` lines saying a listing is "live but not importable by
this account" are expected and harmless — the booth *shows* listings and never
imports one, so import-readiness is a property of your event account, not of the
stand. Do not go hunting those on the morning of an event.

This pushes the curated dataset lists — all three cities — into the account. Until it
runs, the account has nothing to serve, and the game silently falls back to a copy
committed in the repo. If that copy is older than the last curation, **a Paris booth
serves London's datasets with a full stall, eight industries and nothing in the log.**
That has happened. It is why Step 8 has you check a number.

You only re-run it if the curation changes. If it does, three artefacts have to be
refreshed in order, because each feeds the next:

```bash
python3 scripts/build_marketplace_json.py --mirror   # json  -> marketplace-index.md
python3 game/context.py --bundle                    # md    -> context-bundle.json
python3 deploy/load_context.py --connection MYBOOTH  # md    -> Snowflake tables
```

Skipping the middle line is survivable but not silent: `verify_context.py`'s
parity check compares all three layers and fails when they disagree. It has
already caught exactly that — a committed bundle carrying nine duplicate rows and
missing three listings while the markdown was correct. If parity fails, re-run
`python3 game/context.py --bundle` and commit the result.

## Step 7: point the game at your event

Start the server (Step 8), then open **<http://127.0.0.1:4747/admin>**. Pick your venue
and press Apply. That is the whole configuration.

| Venue | City | Language | Marketplace region (recommendations) | Dataset profile |
|---|---|---|---|---|
| `london` | London | en | AWS_EU_WEST_2 | uk |
| `paris` | Paris | fr | AWS_EU_WEST_3 | fr |
| `berlin` | Berlin | de | AWS_EU_CENTRAL_1 | de |

Those three and no others. Frankfurt is the **account's** region, not an event, and
is deliberately not a venue: `resolve_venue()` answers an unknown venue by quietly
defaulting the dataset profile to `uk`, so inventing one would stand up a booth
serving London's datasets with no error anywhere.

**Applying a venue does not need a restart.** The config is re-read on every request,
and Apply flushes the dataset cache, so the change lands on the next visitor.

**Press Apply *after* `load_context.py`, not before.** Apply is the only thing that
flushes the dataset cache. If you load a city's datasets while the server is
already running and do not press Apply afterwards, the server can keep serving the
datasets it read earlier for the rest of its life. Applying last, or using the
restart button, avoids the question entirely.

**Operator** is not something you type. `SESSIONS.SE_OPERATOR` is stamped with
`CURRENT_ACCOUNT()` for every visitor, so "which laptop produced this row" stays
answerable with nothing to fill in: a second laptop at an event is given its own
pool account. There was once a hand-typed `event.operator` field; no logic read
it and every row written before its removal had it empty.

You can still set `event.venue` by hand in `game/config.json` if you prefer;
`/admin` writes the same key. Do not set `event.marketplace_region` yourself — it
is resolved from the venue, and it is the *event's* region, never the account's.

### When you *do* need the restart button

`/admin` has one, and it is not for config changes. Use it when **replies stop coming
back** — a cancelled in-flight call can wedge the warm agent pool, and no amount of
config reloading fixes that — or when you have edited `server.py`, which Python does not
hot-reload. It re-execs the process, takes a couple of seconds, and the first reply
afterwards is slow again.

Between visitors use **Clear visitor** instead, which wipes the session and recycles the
agent without dropping the process.

### Two regions, and they are not the same thing

This is the one thing worth reading twice, because getting it backwards used to empty
the marketplace stall.

- **The event's region** is in the table above. It decides which Marketplace datasets
  are *preferred* in the recommendations, because that is where your visitors live.
  It is set for you by `event.venue`.
- **The account's region** is whatever the DataOps pool assigned you. It decides only
  where `SESSIONS`, `TURNS` and blueprints are written. **It must never filter
  recommendations, and you never need to know what it is.**

Keeping these apart is exactly what makes a random pool assignment safe. All three
event regions were verified answering correctly from a single AWS Frankfurt account,
because the Marketplace catalogue is global.

### One account per event

**Each event runs on its own account**, so `LOCO4COCO.BOOTH` in that account *is* the
per-event separation and there are deliberately no per-event schemas. Sessions still
carry `EVENT_CITY` so the rows are labelled.

> **Do not re-verify the curated listings against your own account, and do not drop a
> listing because your account cannot import it.** The visitor never imports anything
> at the booth - they leave with links and open them later from their own account. A
> pick is judged on whether it is relevant to the event's city, not on what the booth
> can attach. Measured: only 28 of the 48 curated UK picks are importable in
> `AWS_EU_WEST_2`, and deleting the other 20 would throw away four of the best UK
> datasets on the list.

### Several people setting up their own laptops

The app holds no account details of its own, so this is the whole flow per person:

```bash
snow connection add                  # their own account
python3 deploy/bootstrap.py -c <THEIRS>
# then set event.venue in game/config.json for the event they are running
```

`bootstrap.py` patches `game/config.json` to point at their connection. Everything else
(models, prompts, marketplace profiles) is identical from the clone.

Other keys you may still set directly:

| Key | What it does |
|---|---|
| `event.venue` | **Primary toggle** (above). Overwrites city/language/region and sets the marketplace profile. Leave unset to hand-configure the three fields below. |
| `event.city`, `event.language`, `event.marketplace_region` | Only used directly if `event.venue` is unset or unknown. A venue overwrites them. |
| `snowflake.connection_name` | Your connection. Ships as the placeholder `MYBOOTH`; bootstrap rewrites it to yours. A stale value here is the classic postbox failure - keep your real value local (bootstrap sets it) and never commit it. |

**Note the models are region-specific, in availability and in speed.** The default
`coco.complete_model` and `qa.model` are both **`openai-gpt-5.4`**, with
`openai-gpt-5`, `claude-sonnet-5` and `claude-opus-5` behind them as fallbacks, all
three verified at 1.4-2.1s on an eu-central-1 event account. The previous default
`claude-4-sonnet` went to **legacy state** mid-project and now returns a 400 on
every call, which is why the booth proves its model at startup instead of
discovering the problem in front of a visitor. `mistral-large2` is also legacy now.
`llama3.3-70b` is available but measured a **69.5s median, ranging 33.7-111.4s**,
enough to eat a third of the visit on one stall. The **first** COMPLETE call on a
cold warehouse takes around 56s and later ones around 4s, so run one throwaway
visitor through before doors open.

**How the marketplace stall is filled.** The six picks per industry are
precomputed per city and committed in
`skills/loco4coco/references/marketplace.json`. Choosing the venue selects the
right set. There is nothing to re-run on the day, and no live catalogue call on
the visitor's critical path.

**The 7th option.** Anything the visitor *types* into the "something else" box
triggers agentic Marketplace discovery via `cortex search marketplace`, filtered
to the event's region. It runs in the **background** and its results are merged
into the takeaway document at the postbox, so a slow call can never hold up the
stall and the six curated picks are never waited on.

This replaced an earlier free-form agent prompt, which was measured and
abandoned: it took 110-237s and returned out-of-region results 78 times out of
91. The filtered search is **6.2-6.4s measured** (timed against both eu-west-2 and
eu-central-1 filters on an event account) and is in-region by construction.

There is deliberately no call cap. Watch spend in `game/cost.jsonl`
(`kind: marketplace_agentic`) and set `marketplace.agentic.enabled: false` to
turn it off. Spend is watched **per account** - `game/cost.jsonl` plus the
`cost.jsonl` on that laptop - and there is deliberately no cross-booth rollup:
combine the per-account figures by hand if you want an event-wide total.

## Step 8: run it and prove it

```bash
cd game
python3 server.py
```

Open <http://127.0.0.1:4747/>. Then in a second terminal, against the running server:

```bash
python3 smoke_test.py
```

You want `PASS, all invariants hold`, **one session row and four turn rows**. That means
the account is wired end to end.

### Before doors open, every day, at every stand

Open <http://127.0.0.1:4747/admin> and read six lines:

| Line | Must say |
|---|---|
| Venue / City | the event you are actually at |
| Datasets loaded | **88–100**, of which 48 are the six-per-stall a visitor sees; the rest are the fallback pool and its size differs per city (London 92, Paris 97, Berlin 91). `0` means Step 6 never ran and you will serve the wrong city's data |
| Lists read from | **snowflake**. `bundle` or `markdown` means the account read failed and you are on a committed copy |
| Connection authenticated | **yes — user @ account**, in green. The booth tests the connection at startup, so a stale, wrong, or placeholder (`MYBOOTH`) one shows here rather than failing at the postbox with a visitor watching. A brief `checking…` at boot is normal — the test runs in the background |
| Model proven | **yes, followed by a model name**, in green. Amber `FELL BACK to …` is fine — something answered. Red `NO USABLE MODEL` is the only version of this line that should stop you |
| Warm agent | `ready` is ideal. `off` or `not running (falls back, still works)` is **also fine**, and shows in grey rather than red on purpose: the booth drops to a slower path and the visitor sees no difference |

Then press **Setup check** in the admin panel: it proves the delivery chain end to
end in one click — the snow CLI resolves, the stage is reachable, a document stages
and presigns, and the booth connection is still on key-pair so no browser or keychain
prompt interrupts a visit. Every row should be green before the doors open; a red
`booth_auth` row means the connection has been reverted off key-pair (fix it with the
key-pair step above). It is read-only — the only side effect is a tiny probe file
staged and presigned.

Then run **one throwaway visitor all the way through**. The first inference call on a cold
warehouse takes around 56s against roughly 4s afterwards, and you do not want a stranger
to be the one who discovers that.

While you are in there: a long reply **pages itself**. The speech bubble holds four
lines, and anything longer advances on its own after a readable pause, showing
`1 / 2` while it does. Nobody has to press anything. The bubble is a fixed-height
box in every state, so a short reply leaves some empty space inside its border —
that is deliberate. It is what stops the board and the interior sliding up and
down under a bubble that changes size, which visitors found disorienting.

## Running four stands

The game holds **one visitor at a time** (a single state file), so four simultaneous
stands means **four independent servers**. Simplest and cheapest: one laptop per stand.

Give each stand its **own account and connection** if you can. Not for cost (tokens cost
the same either way) but so four stands' requests do not queue behind one another, and one
stand cannot affect another. Two accounts with two stands each is fine.

**Do not deploy this to SPCS to make it faster.** Model inference already runs server-side
inside Snowflake, so adding compute behind the game does not speed up replies. It adds
cost, and the single-session design would still need one service per stand.

**Pre-warm each stand** before doors open: start the server and click through one throwaway
visitor. The first Cortex call in a fresh process takes 5 to 9s (it includes connection
setup); after that it settles to 2 to 3.5s.

## Cost: observed, never enforced

**Nothing can stop the booth.** There is no resource monitor, no credit cap, and no idle
shutdown. That is deliberate: every one of those ends the activation in public rather
than saving money worth having.

- **No resource monitor.** The post-hook asserts that none is bound to `LOCO4COCO_WH` and
  reads the warehouse back to prove it. If you need a cap on a shared account, put it on a
  different warehouse.
- **No idle shutdown.** `server.idle_shutdown_minutes` is `0`. It was 45, and 45 minutes of
  quiet at a stand is an ordinary lunchtime — the failure mode was a dead stand when the
  next visitor walked up, with nobody watching the terminal to notice.
- **The spend is not the problem.** Measured on an event account: the booth warehouse used
  **0.78 credits in 14 days**, against **64 credits for Cortex Code Desktop** over the same
  period. The line that moves is Cortex Code during setup, not the booth.
- **What to watch instead.** `game/cost.jsonl` records every model call locally — turn,
  transport, model, duration and token counts — which is how you see spend per visitor.
  `sql_statements/03-event-health.sql` gives it per visit and per turn, and
  `SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY` has the credits.
- The warehouse is X-Small with 60s auto-suspend and auto-resume, so it parks itself
  between visitors and comes back on its own. That is a latency question — the first call
  after a park is slow — not a cap.

## Delivery: how the visitor actually gets their blueprint

There are two independent routes, so a failure in one does not send a visitor away
empty-handed. **Email was removed: nothing is sent, and no visitor content is kept on
the laptop.**

**The QR code on screen - the primary handover.**
When the visitor presses SEND, the game builds a real Word document, uploads it to
`@LOCO4COCO.BOOTH.BLUEPRINTS` and presigns it for seven days. The confirmation card
shows that link as a QR code plus a tappable link. They scan it and leave with the
document. This path needs no email, no MCP, and no operator - only the Snowflake
connection the game already has.

**The durable record, for after the event.**
The document sits in the Snowflake stage and the row in `BOOTH.SESSIONS` carries
`DOCUMENT_URL` and `DELIVERY_STATUS`, so leads can be reconciled and re-presented in
bulk later even if the laptop is wiped. Note the presigned URL expires after seven days;
re-presign from the stage if you need it after that.

### Preflight: run this at every stand before doors open

```bash
curl -s http://127.0.0.1:4747/api/delivery/check | python3 -m json.tool
```

It checks, in order: `python-docx` importable, the stage configured, the stage
reachable, and - the one that matters - a real file staged and
presigned end to end. `"ok": true` means the QR handover works, which means no visitor
can leave with nothing. If `presign_works` fails, the stand is not ready: check the
connection name in `game/config.json` and that the deploy created the stage.

The most common cause is a stale `snowflake.connection_name`. The repo ships it as
the placeholder `MYBOOTH`; `bootstrap.py` rewrites it to your connection. If it is
left as `MYBOOTH`, or points at an old/expired account, every Snowflake call - and
so the postbox - fails (you will see `JWT token is invalid` or `connection ... not
found`). The server now tests the connection at startup and prints a loud
`connection preflight` line, and the admin panel shows a connection row, so a wrong
connection surfaces before the doors open rather than at the postbox.

If `segno` is not installed the QR degrades to a plain link rather than breaking, but
install it - a link nobody can type is not a handover.

## Between visitors, and end of day

```bash
python3 state_cli.py reset --level all --yes
```

Add `--purge-rows` to also clear the Snowflake rows. Use that when **testing**, not at a
live event, or you will delete your leads.

## SDR handover: export the leads before the account is torn down

**This is not optional and there is no second chance.** The account you were assigned
from the pool is temporary. Every lead the booth captured lives in `SESSIONS` on *that*
account, and because there is one account per event this has to be done three times -
once per city, on the last day of that event.

Run the handover query and save the result off the laptop:

```sql
SELECT SESSION_TS, EVENT_CITY, SE_OPERATOR,
       FIRST_NAME, COMPANY, INDUSTRY,
       PROBLEM_STATEMENT,
       POC_NAME, POC_SUMMARY, GUIDE_FORKED, FIRST_STEP,
       PLATFORMS, DATA_HELD, MARKETPLACE_JOINED,
       COMPANY_COUNTRY, RESIDENCY,
       DELIVERY_STATUS, DOCUMENT_URL
FROM LOCO4COCO.BOOTH.SESSIONS
WHERE PROBLEM_STATEMENT IS NOT NULL AND PROBLEM_STATEMENT <> ''
ORDER BY SESSION_TS DESC;
```

`PROBLEM_STATEMENT` is the one to quote back - it is the visitor's own words.

**The document links expire after 7 days**, independently of the account. If you export
later than that the links are dead, and you re-mint them from the stage:

```sql
LIST @LOCO4COCO.BOOTH.BLUEPRINTS;
SELECT GET_PRESIGNED_URL(@LOCO4COCO.BOOTH.BLUEPRINTS, '<filename>', 604800);
```

**The same person may visit more than one stand.** Deduplicate on first name and
company before handing anything over, or one visitor reads as several unrelated leads.

**Do not circulate the raw table.** It holds a first name, an employer and free text the
visitor typed. For anything going wider than the SDR doing the follow-up, drop
`FIRST_NAME` and `COMPANY` and keep industry, problem statement, POC and features.

An aggregated per-event view, a durable export and an `--anonymise` mode are scoped but
not built. Until they exist, the query above *is* the handover.

## Troubleshooting: what actually goes wrong

1. **Edited `config.json` and nothing changed.** Config *is* re-read on every request, so
   venue changes land immediately — use `/admin` and press Apply. Only
   `server.py` edits need the restart button. HTML and CSS are served fresh from disk.
2. **A Library or Marketplace reply is slow.** Those use direct Cortex inference and
   **models are region-specific, in availability and in speed.** The default is
   `openai-gpt-5.4`. Measured on an eu-central-1 event account: the primary and the
   three fallbacks `openai-gpt-5`, `claude-sonnet-5` and `claude-opus-5` all answer
   in 1.4–2.1s, while `llama3.3-70b` measured a **69.5s median, ranging 33.7–111.4s**
   and is a booth hazard. `claude-4-sonnet` and `mistral-large2` are both in **legacy
   state** and now return a 400 on every call, so do not set either. Also unavailable
   in eu-central-1: `claude-4-5-sonnet`, `claude-3-7-sonnet`, `llama4-maverick`,
   `llama4-scout`, `openai-gpt-4.1`, `deepseek-r1`. If you move region, re-measure
   before the event
   with a one-line `SELECT SNOWFLAKE.CORTEX.COMPLETE(...)` — do not assume.
3. **Marketplace stall shows the wrong city's datasets, or `/admin` says 0 loaded.**
   `deploy/load_context.py` has not been run against this account (Step 6), so the game
   is falling back to the copy committed in the repo. Check `/admin`: **lists read from**
   should say `snowflake`, not `bundle` or `markdown`, and **datasets loaded** should be
   88–100 — London 92, Paris 97, Berlin 91, of which 48 are always the picks a
   visitor is offered. This failure is otherwise completely silent.
4. **Marketplace stall is genuinely empty.** `event.marketplace_region` is unset — pick a
   venue in `/admin`.
5. **First visitor felt slow.** Cold warehouse: the first inference call takes around 56s
   against roughly 4s afterwards. Pre-warm with a throwaway visitor.
6. **`No models available`.** Cortex Code is not authenticated for that connection (Step 2).
7. **macOS keeps asking for your keychain password: "Python wants to access key
   `com.snowflake.connector.python`".** Fix this properly in Step 2; do not try to live
   with it.

   The cause is the auth mode, not the booth. A DataOps event account is handed out with
   `authenticator = oauth_authorization_code` and `client_store_temporary_credential = true`.
   That caches the OAuth token in the macOS keychain, and macOS asks permission once per
   *process* that reads it. The booth is process-heavy by design, so the count is:

   | when | authenticating processes |
   | --- | --- |
   | setup, once per event account | 10 |
   | server start | 2 |
   | **per visitor** | **3** (stage copy, presign, LIST) |
   | a 100-visitor day | **312** |

   Run `python3 scripts/count_auth_calls.py` to reproduce that count. The per-visitor number
   is the one that matters: a modal password dialog can land in the middle of someone's
   visit, and the stand stops until you type a password.

   **"Always Allow" is not a reliable fix.** The grant is scoped to one binary path, so
   `snow`, `cortex` and the framework Python each need their own, and the grant is lost
   whenever the token is rewritten on refresh. It also does nothing about the token expiring
   during a long day, where the recovery path is a browser window opening mid-visit.

   **The fix is key-pair auth, which has no token to cache and so never touches the keychain
   at all.** One command, once per event account:

   ```bash
   python3 scripts/setup_keypair.py --connection <your-connection>
   ```

   That generates an unencrypted RSA key under `~/.snowflake/keys/` (0600), registers the
   public half on your event user with `ALTER USER`, and creates a separate
   `LOCO4COCO_BOOTH` connection on `SNOWFLAKE_JWT` (leaving your original alone, because
   Cortex Code would otherwise revert an in-place conversion back to OAuth). The key is
   unencrypted deliberately — a passphrase would just reintroduce a prompt, and the booth
   has to run unattended.

   The connection keeps its name, so nothing downstream changes: the same `-c` works, the
   Cortex Code picker keeps its selection, and `game/config.json` already holds that name.
   Restart Cortex Code afterwards so it rereads the file. Verified: five consecutive fresh
   `snow sql` subprocesses, zero prompts, and 1.4–2.2s each against ~3.4s under OAuth.

   Do **not** instead set `client_store_temporary_credential = false`. That stops the caching,
   so the connector falls back to the full OAuth browser flow — a browser window opening
   mid-visitor is worse than the dialog you were trying to remove.

6. **The server vanished.** It idled out after 45 minutes — which is why
   `server.idle_shutdown_minutes` is now `0` (see "No idle shutdown" above). If the
   server disappears today it was closed, the laptop slept, or it crashed; the
   booth no longer times itself out during a quiet spell.
7. **A visit was cut off, or the stand was dead when someone walked up.** Something is
   capping or stopping the booth, and nothing in this project should be. Check that no
   resource monitor is bound - `SHOW WAREHOUSES LIKE 'LOCO4COCO_WH'` must report
   `resource_monitor` as `null` - and that `server.idle_shutdown_minutes` is `0`. Do
   **not** add a `DO SUSPEND` trigger to get a hard cap: a booth that suspends mid-visit
   is a worse failure than any overspend, and the booth warehouse measured 0.78 credits
   in 14 days. If a shared account genuinely needs a ceiling, put it on a different
   warehouse.

## If you change the fast model

The two fast turns use `openai-gpt-5.4` by default. Open-weight models need more prompt
discipline: `mistral-large2` (a previous default, now legacy) greeted the visitor by name on
*every* turn and named vague capabilities such as "the ability to handle large datasets".
Both are fixed in the prompts. If you change `coco.complete_model`, read a couple of Library
and Marketplace replies and check for exactly those two failures before trusting it at a booth.

And check the **feature it recommends is real and appropriate**, not merely plausible. On
the manufacturing test prompt `mistral-large2` recommended Time Travel for predicting
supplier delays and both llama models recommended Materialized Views, where the right
answer was Dynamic Tables. A fast wrong answer is worse at a booth than a slow right one.

## How the two speeds work

Turns that only reflect back what the visitor just told you (Library, Marketplace) call
Cortex inference directly and return in about 2s. Turns where CoCo must exercise judgement
or actually do something (the Workshop, and the optional Ask CoCo stop) run real Cortex
Code with tools and take 35 to 40s.

The reasoning tray is shown **only** for the agentic turns, because only they have real
reasoning to show. That is deliberate: making the cheap turns instant is what earns the
attention for the one moment where CoCo genuinely thinks.
