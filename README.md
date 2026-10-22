# Loco for CoCo

**If something breaks:** [FAQ — what could break](FAQ-what-could-break.md), written
from defects actually found. SQL for leads, health checks and session recovery is in
[sql_statements/](sql_statements/README.md).

Setup: [TL;DR (one page)](https://docs.google.com/document/d/1TwRR5uurF8NHGTbQBiBQBs0fHV9lqqUZ7357WxY5e_E/edit) | [SETUP.md (full)](SETUP.md)

A five-minute Cortex Code activation for **Snowflake World Tour**. A visitor drives a penguin
round an arctic map, answers a few light questions, and leaves with the scaffolding for a POC:
their idea in their own words, the Snowflake developer guide to fork, the
features with doc links, and a kick-off prompt to paste into Cortex Code
on a free trial. They take it away by **scanning a QR code** with their own phone.

> **Read [CONSTRAINTS.md](CONSTRAINTS.md) before designing or changing any feature.**
> It states what the event is, who the visitor is, and the six venue properties
> every change is checked against. They are properties of the stand, not
> preferences, and they have been broken before.

It uses **real** Cortex Code and real Snowflake Marketplace listings. It runs on a laptop:
a Python standard-library server plus a browser canvas. No npm, no build step, nothing hosted.

## Setting it up

The whole setup is three things, and you only do two of them:

1. **Get an account** — register at <https://go.dataops.live/emea-swt/register>, once per
   event. The name is random and you never need it.
2. **Connect** — `snow connection add`, then confirm
   `cortex exec "Reply with the single word: ready." --no-mcp -c <conn>` answers.
3. **Let Cortex Code do the rest** — paste the prompt in
   **[SETUP.md](SETUP.md#let-cortex-code-do-it)**. It clones this repo, deploys, loads the
   datasets, sets your venue and name, starts the server and proves it with a smoke test.

Full longhand, and the operational side — the pre-flight before doors open, the cost
guardrails, resetting between visitors, and the SDR export you must run before the
account is torn down — is all in **[SETUP.md](SETUP.md)**.

Already set up:

```bash
cd game
python3 server.py          # visitor: http://127.0.0.1:4747/
                           # operator: http://127.0.0.1:4747/admin
```

## Running an event

## How it is put together

| Path | What it is |
|---|---|
| `game/server.py` | The booth server. Prompts CoCo, streams reasoning, logs to Snowflake. |
| `game/index.html` | The whole front end: procedural pixel-art canvas and the panels. |
| `game/config.json` | Everything tunable: copy, locations, transports, models, guardrails. |
| `game/smoke_test.py` | End-to-end proof. Run it after any change. |
| `game/bench.py`, `game/bench2.py` | Latency benchmarks (agentic path, and direct inference). |
| `deploy/verify_context.py` | **Pre-flight gate.** Closed lists, doc links, live listings. Exits with the failure count. |
| `deploy/load_context.py` | Loads the reference markdown into the shared Snowflake context tables. |
| `game/context.py` | The closed lists, their three-layer fallback, and deterministic keyword retrieval. |
| `game/agent_pool.py` | The warm Cortex Code agent. One in-flight call, always. |
| `audit/` | Chromium persona suite (`npx playwright test`), 35 stories over 8 industries. |
| `game/state_cli.py` | Reset between visitors, purge rows. |
| `deploy/` | DCM project that builds the Snowflake objects on any account. |
| `skills/loco4coco/references/` | The closed lists the game may name - listings, guides, features, archetypes - plus their committed offline bundle. |

## Two speeds, on purpose

Turns that only reflect back what the visitor just said (Library, Marketplace) call Cortex
inference directly and return in ~2s. Turns where CoCo must exercise judgement or actually
do something (Workshop, and the optional "Ask CoCo one thing") run real agentic Cortex Code
with tools, and take 35 to 40s.

The reasoning tray appears **only** for the agentic turns, because only they have real
reasoning to show. Making the cheap turns instant is what earns attention for the one
moment where CoCo genuinely thinks.

Total CoCo wait is about 58s of the 300s budget, down from 144.5s before tuning.

## A few hard-won notes

- **Python does not hot-reload.** Restart the server after editing `server.py` or
  `config.json`. HTML and CSS are served from disk.
- **One toggle per event: `event.venue`** (`london` / `paris` / `berlin`).
  It resolves city, language, marketplace region and dataset profile from the `venues`
  map in `config.json`. Set it in **`/admin`** at the booth — venue and operator changes
  take effect on the next visitor with no restart.
  **Two regions exist and they are not the same thing.** `event.marketplace_region`
  (London `AWS_EU_WEST_2`, Paris `AWS_EU_WEST_3`, Berlin `AWS_EU_CENTRAL_1`) decides which
  datasets are *recommended*, because that is where the visitors are. The **account's**
  region is whatever the DataOps pool assigned, decides only where rows are written, and
  is never needed. Each event runs on its own account, so `LOCO4COCO.BOOTH` in that
  account is the per-event separation and there are no per-event schemas.
- **`deploy/load_context.py` is not optional.** It puts the city's datasets in the
  account. Skip it and the game silently falls back to a committed copy, which can serve
  another city's data with no visible symptom. `/admin` shows **datasets loaded** (expect
  88–100 — 48 offered picks plus a fallback pool whose size differs per city) and
  **lists read from** (expect `snowflake`) so you can see it.
- **Models are region-specific in availability *and* speed.** The fast/QA path defaults to
  **`openai-gpt-5.4`**, with `openai-gpt-5`, `claude-sonnet-5` and `claude-opus-5`
  behind it, all measured at 1.4–2.1s on an eu-central-1 event account, against
  **69.5s median (33.7–111.4s)** for `llama3.3-70b`. The earlier defaults
  `claude-4-sonnet` and `mistral-large2` are both in legacy state now and return a
  400 on every call, which is why the model is proven at startup. Re-measure if
  you move region rather than assuming; several Claude, Llama-4 and OpenAI models are not
  offered in eu-central-1 at all. It falls back to the agentic path automatically if
  inference fails.
- **Delivery is a QR to a presigned stage document, not an email.** The booth keeps
  nothing on the laptop and sends no email; the visitor scans the QR on screen and the
  document lands on their own phone.
- **Nothing can stop the booth mid-event.** No resource monitor, no credit cap, and no
  idle shutdown - each of those ends the activation in public rather than saving money
  worth having. Measured: the booth warehouse used 0.78 credits in 14 days against 64
  for Cortex Code Desktop. Cost is observed via `game/cost.jsonl`, not enforced.

## Solution Engineer console

`operator_app/` is a Streamlit in Snowflake app over `LOCO4COCO.BOOTH.SESSIONS`:
visitors appear live as they finish, with their SE brief, scores and blueprint,
and an SE can correct the scores, brief, notes and visitor inputs (the tier is
recomputed with the game's thresholds and the edit is stamped). Deploy it with

```bash
python3 operator_app/deploy.py -c <connection>
```

## Running it at Hayadata (Tel Aviv)

The defaults are the SWT events. For Hayadata, set these in `game/config.json`:

- `event.venue`: `"tel_aviv"` and `event.event_name`: `"Hayadata"`. The venue
  serves the Israel dataset profile (`il`), judged on AWS Frankfurt because
  Snowflake has no Israel region.
- `intake.fields[company].placeholder`: `"e.g. Clalit, Bank Leumi, Shufersal"`
- `locations.marketplace.other_placeholder`: `"e.g. Israeli address data, company registry data"`
- `sourcing.external`: replace with

```json
[
  {
    "name": "data.gov.il",
    "url": "https://data.gov.il/",
    "note": "Israeli government open data. Not on the Snowflake Marketplace."
  },
  {
    "name": "Central Bureau of Statistics",
    "url": "https://www.cbs.gov.il/en",
    "note": "Israeli population, localities, prices and economic statistics."
  },
  {
    "name": "GovMap",
    "url": "https://www.govmap.gov.il/",
    "note": "Israeli national GIS: parcels, addresses, boundaries."
  },
  {
    "name": "Israel Meteorological Service",
    "url": "https://ims.gov.il/en",
    "note": "Israeli weather observations and forecasts."
  }
]
```

## Authors

- Paddy and Sabrina - original authors
- Nikita Zaliven (nikita.zaliven@snowflake.com) - Hayadata (Tel Aviv) venue,
  bonus questions, Solution Engineer scoring and console, shorter onboarding
