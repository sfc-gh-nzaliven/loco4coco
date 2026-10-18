# SQL statements

Everything you might need to ask the booth's tables, split by the job you are doing.
Each file is runnable as-is:

```bash
snow sql --connection <your-connection> --enable-templating NONE -f sql_statements/01-lead-list.sql
```

`--enable-templating NONE` matters: several queries contain `&` and `$`, which the
CLI otherwise treats as template syntax.

| File | Use it when |
| --- | --- |
| `01-lead-list.sql` | Handing leads to an SDR after the event |
| `02-listing-demand.sql` | Deciding what to curate for the next event |
| `03-event-health.sql` | Checking, during or after the event, that the booth behaved |
| `04-recover-a-session.sql` | A visitor left with nothing and you need their blueprint back |
| `05-export-before-teardown.sql` | The event account is about to be destroyed |
| `06-se-priority.sql` | Deciding which visitors' use cases an SE should follow up first |

**Run these before the account is torn down.** Event accounts are assigned per event
from a pool and destroyed afterwards. Nothing in them survives, so anything you want
to keep has to leave first — see `05-export-before-teardown.sql`.

The two tables behind all of this:

- `LOCO4COCO.BOOTH.SESSIONS` — one row per visitor, written at the postbox. Their
  details, the problem they described, the blueprint they left with, timings, and the
  quality flags.
- `LOCO4COCO.BOOTH.TURNS` — one row per exchange, with `VISITOR_INPUT` and `REPLY`.
  This is where you look when you need to know what was actually said.
