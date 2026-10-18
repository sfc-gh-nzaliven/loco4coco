-- Loco4CoCo booth objects.
--
-- Only DEFINE, GRANT and ATTACH statements are permitted in a DCM definition
-- file, and every object must be fully qualified. DEFINE behaves as
-- CREATE OR ALTER, so this file is the desired end state: removing a statement
-- DROPS the object on the next deploy.
--
-- Resource monitors are NOT a supported DEFINE entity. This booth deliberately
-- binds NONE: see hooks/post_hook.sql, which only UNSETs any monitor a previous
-- deploy may have left on the warehouse and proves resource_monitor reads null.
-- Nothing here can suspend a visit.

-- ---------------------------------------------------------------- containers

DEFINE DATABASE {{db}}
  COMMENT = 'Loco4CoCo booth activation. Managed by the LOCO4COCO_BOOTH DCM project.';

DEFINE SCHEMA {{db}}.{{schema}}
  COMMENT = 'Visitor sessions, per-turn telemetry and generated blueprints.';

-- ---------------------------------------------------------------- compute
-- X-Small. Auto-suspend is set from manifest.yml (3600s, to hold the warehouse
-- warm through the quiet spells a booth has between visitors so no visitor pays a
-- ~56s cold start). INITIALLY_SUSPENDED is immutable in DCM, which
-- suits the default-posture-off cost rule: the warehouse only runs when
-- something actually needs it.
--
-- Generation is deliberately NOT pinned. The London warehouse was created as
-- STANDARD_GEN_2 (which bills ~1.35 credits/hour rather than 1.0), and
-- RESOURCE_CONSTRAINT cannot be used to change it - Snowflake rejects it and
-- directs you to a GENERATION property whose accepted syntax did not work here
-- either. At one INSERT per visitor the difference is a rounding error, so this
-- is left as-is on purpose rather than chased. A NEW account gets whatever the
-- default generation is, which is fine.

DEFINE WAREHOUSE {{wh}}
  WITH
    warehouse_size = '{{wh_size}}'
    auto_suspend = {{auto_suspend}}
    auto_resume = TRUE
    initially_suspended = TRUE
    comment = 'Loco4CoCo booth. Tiny workload; kept small and fast to suspend.';

-- ---------------------------------------------------------------- stage
-- ENCRYPTION type is IMMUTABLE in DCM, and SNOWFLAKE_SSE is what makes
-- GET_PRESIGNED_URL work. Getting this wrong at creation cannot be patched
-- later, so it is pinned here rather than left to a default.

DEFINE STAGE {{db}}.{{schema}}.BLUEPRINTS
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  DIRECTORY = (ENABLE = TRUE)
  COMMENT = 'Generated .docx blueprints. Presigned for download, 7 day max.';

-- ---------------------------------------------------------------- sessions
-- One row per visitor. Shaped around what the game actually knows: the v1 table
-- carried PAIN_CHOSEN / DREAM_CHOSEN / AUDIENCE / ROLE_STATED, which the game
-- can never populate, and lacked company, industry, Marketplace picks and cost.

DEFINE TABLE {{db}}.{{schema}}.SESSIONS (
  SESSION_ID          TEXT          COMMENT 'Joins to TURNS.SESSION_ID',
  SESSION_TS          TIMESTAMP_LTZ COMMENT 'When the visitor finished',
  EVENT_CITY          TEXT,
  LANGUAGE_CODE       TEXT,

  FIRST_NAME          TEXT,
  COMPANY             TEXT,
  INDUSTRY            TEXT          COMMENT 'Inferred from company, confirmable by the visitor',

  DATA_HELD           ARRAY         COMMENT 'Library picks - data they already hold',
  MARKETPLACE_JOINED  ARRAY         COMMENT 'Marketplace picks - data to enhance it',

  POC_ARCHETYPE       TEXT          COMMENT 'One of the nine archetypes; drives the guide',
  POC_NAME            TEXT,
  POC_SUMMARY         TEXT,
  GUIDE_FORKED        TEXT,
  GUIDE_URL           TEXT,
  FEATURES            ARRAY,
  FIRST_STEP          TEXT,

  DOCUMENT_URL        TEXT          COMMENT 'Presigned .docx link, expires after 7 days',
  DELIVERY_STATUS     TEXT          COMMENT 'QUEUED | DRAFTED | SENT | FAILED - set when the QR document is staged',

  DURATION_SECONDS    NUMBER(6,0)   COMMENT 'Wall clock from intake to postbox',
  COCO_SECONDS        NUMBER(6,0)   COMMENT 'Of which spent waiting on Cortex Code',
  INPUT_TOKENS        NUMBER(12,0),
  OUTPUT_TOKENS       NUMBER(12,0),

  SE_OPERATOR         TEXT,
  NOTES               TEXT,

  -- Appended, not inserted: CREATE OR ALTER TABLE (which DEFINE runs) rejects
  -- adding a column anywhere but the end. Replaces the old WEAKEST_POINT.
  CONSIDERATIONS      ARRAY         COMMENT 'What the visitor is told to think about, in place of a score',

  -- Also appended. These two are the qualification payload: the problem in the
  -- visitor's OWN WORDS, and the platforms their data sits on. Everything else
  -- in this table is a pick from a list we wrote; these are the only columns an
  -- SDR can quote back to them.
  PROBLEM_STATEMENT   TEXT          COMMENT 'Two sentences, typed by the visitor at the letter stage',
  PLATFORMS           ARRAY         COMMENT 'Where the data lives today - decides the integration path',
  -- Appended, because DCM's CREATE OR ALTER cannot add a column before the end
  -- of the list. INDUSTRY holds the human label; this holds the config key,
  -- which is what groups reliably when a label is reworded.
  INDUSTRY_KEY        TEXT          COMMENT 'config.industries key, e.g. public | healthcare | other',
  -- Home-stage sovereignty answers, appended for the same CREATE OR ALTER reason.
  COMPANY_COUNTRY     TEXT          COMMENT 'Where the visitor says their company is based',
  RESIDENCY           TEXT          COMMENT 'Data/model residency rule: country_only | eu | us_ok | unsure',
  -- QA-bot verdict, appended for the same CREATE OR ALTER reason. The per-finding
  -- detail lives in QA_FINDINGS; these are the at-a-glance summary.
  QA_PASSED           BOOLEAN       COMMENT 'TRUE when the QA bot found no flags and relevance was not judged false',
  QA_REPAIRS          NUMBER(4,0)   COMMENT 'How many fixes the QA bot applied to this blueprint',
  QA_RELEVANT         BOOLEAN       COMMENT 'Model relevance verdict; NULL when the check was off or did not complete',
  QA_NOTE             TEXT          COMMENT 'Short list of the QA checks that fired',
  -- Appended for bonus objectives (CREATE OR ALTER only allows appends).
  BONUS_QA            VARIANT       COMMENT 'Bonus questions answered (question, answer, where, points) and missed',
  BONUS_ANSWERED      NUMBER        COMMENT 'How many bonus questions were answered',
  SCORE               NUMBER        COMMENT 'Server-side game score; the scoreboard reads this',
  -- SE qualification, appended (CREATE OR ALTER only allows appends). Never
  -- shown to the visitor; they only learn whether they qualified.
  SE_SCORE_NOW        NUMBER(1,0)   COMMENT 'SE score 1-5: can the visitor work on it in the near term (their own capacity answer)',
  SE_SCORE_SOLVABLE   NUMBER(1,0)   COMMENT 'SE score 1-5: Snowflake-solvable, and a quick win CoCo can build',
  SE_SCORE_CONSUMPTION NUMBER(1,0)  COMMENT 'SE score 1-5: estimated Snowflake consumption of the use case',
  SE_SCORE_AVG        NUMBER(3,1)   COMMENT 'Average of the three SE scores',
  SE_QUALIFIED        BOOLEAN       COMMENT 'TRUE when SE_SCORE_AVG >= the pass threshold (3.0)',
  SE_SCORING          VARIANT       COMMENT 'Capacity answer, per-score reasons, consumption estimate, threshold, model'
)
  COMMENT = 'One row per booth visitor.';

-- ---------------------------------------------------------------- turns
-- One row per location per visitor. This is what makes "which beat is slowest
-- and most expensive" answerable - a live question, since the postbox is a ~50s
-- wait and it lands last in the five-minute budget.

DEFINE TABLE {{db}}.{{schema}}.TURNS (
  SESSION_ID        TEXT,
  TURN_TS           TIMESTAMP_LTZ,
  LOCATION          TEXT          COMMENT 'library | marketplace | workshop | postbox',
  VISITOR_INPUT     TEXT          COMMENT 'Their selection or sentence',
  REPLY             TEXT,
  DURATION_SECONDS  NUMBER(6,0),
  INPUT_TOKENS      NUMBER(12,0),
  OUTPUT_TOKENS     NUMBER(12,0),
  CACHE_READ_TOKENS NUMBER(12,0),
  SUCCEEDED         BOOLEAN
)
  COMMENT = 'Per-turn latency and token cost for each visitor.';

-- ---------------------------------------------------------------- qa findings
-- One row per QA-bot check that fired for a visitor's blueprint. The "log that a
-- change was made" audit trail: repairs are applied silently to the document the
-- visitor sees, but every one is recorded here with its before/after so the
-- change is answerable after the fact. SESSIONS carries the summary.
DEFINE TABLE {{db}}.{{schema}}.QA_FINDINGS (
  SESSION_ID   TEXT,
  FINDING_TS   TIMESTAMP_LTZ,
  CHECK_NAME   TEXT          COMMENT 'e.g. features_in_closed_list | guide_resolves | relevance',
  SEVERITY     TEXT          COMMENT 'repair | flag | info',
  DETAIL       TEXT          COMMENT 'Human-readable description of what was found',
  REPAIRED     BOOLEAN       COMMENT 'TRUE when the bot changed the blueprint to fix it',
  BEFORE_VAL   TEXT          COMMENT 'Value before a repair (NULL for flags)',
  AFTER_VAL    TEXT          COMMENT 'Value after a repair (NULL for flags)'
)
  COMMENT = 'Audit trail of QA-bot checks and silent repairs, one row per finding.';

-- ------------------------------------------------------- shared booth context
-- The five tables below are the *shared* half of the activation: the closed
-- lists that decide what a blueprint may name. The repo markdown under
-- skills/loco4coco/references/ stays the source of truth and the offline
-- fallback; these tables are how that content reaches another SE's laptop, or
-- Paris, without asking them to pull a git branch.
--
-- They are deliberately NOT an MCP server. MCP is not guaranteed on a borrowed
-- booth laptop (managed settings can disable user and plugin servers), whereas
-- a Snowflake connection is already a hard requirement. Sharing these is then
-- an ordinary secure share or listing.
--
-- Loaded by deploy/load_context.py, which parses the markdown and MERGEs. The
-- server reads them at boot and falls back to the markdown on any failure, so
-- an empty table is degraded, never broken.

DEFINE TABLE {{db}}.{{schema}}.LISTINGS (
  INDUSTRY     TEXT    COMMENT 'Config industry key: public | healthcare | retail | ...',
  ORDINAL      NUMBER(4,0) COMMENT 'Display order within the profile+industry+kind, 1-based',
  TITLE        TEXT,
  PROVIDER     TEXT    COMMENT 'Recorded constant - SQL exposes a provider for only a minority of listings',
  ACCESS       TEXT    COMMENT 'Every curated listing is free to acquire, but the value varies: "Free" is perpetual, "Free 14-day trial" and friends expire. Nothing Paid ships.',
  GLOBAL_NAME  TEXT    COMMENT 'Marketplace global name, e.g. GZSVZAJO3',
  REGIONS      TEXT    COMMENT 'Subset of the SWT venue regions the listing is in (AWS_EU_WEST_2/3, AWS_EU_CENTRAL_1), or ALL',
  URL          TEXT,
  -- Appended (DCM CREATE OR ALTER only adds columns at the end).
  MARKET_PROFILE TEXT  COMMENT 'uk | fr | de - which venue profile this listing belongs to. Selected by event.market_profile, with uk fallback.',
  RESERVE      BOOLEAN COMMENT 'FALSE = one of the primary 6; TRUE = a reserve/backup pick used to fill the stall when a primary is region-filtered out'
)
  COMMENT = 'Closed list of Marketplace listings the booth may name, by profile and industry. Mirror of marketplace.json.';

DEFINE TABLE {{db}}.{{schema}}.GUIDES (
  ARCHETYPE    TEXT    COMMENT 'POC archetype key, e.g. talk-to-my-data',
  TITLE        TEXT,
  SLUG         TEXT    COMMENT 'Appended to delivery.guides_base to form the URL',
  IS_PRIMARY   BOOLEAN COMMENT 'TRUE for the primary fork; alternates are FALSE'
)
  COMMENT = 'Snowflake developer guides a visitor may be sent to fork. Mirror of guides-index.md.';

DEFINE TABLE {{db}}.{{schema}}.FEATURES (
  NAME         TEXT    COMMENT 'Exact spelling the model must copy',
  DOCS_URL     TEXT
)
  COMMENT = 'CLOSED list of Snowflake features a blueprint may name. A feature absent here is dropped rather than rendered without a link. Mirror of feature-docs.md.';

DEFINE TABLE {{db}}.{{schema}}.ARCHETYPES (
  ID               TEXT   COMMENT 'e.g. talk-to-my-data',
  ORDINAL          NUMBER(4,0),
  FRIENDLY         TEXT   COMMENT 'Shown to the visitor. The ID never is',
  WHAT_GETS_BUILT  TEXT,
  FEATURES         TEXT   COMMENT 'Comma-separated, all of which must exist in FEATURES',
  FORK_SLUG        TEXT,
  NEEDS_FROM_THEM  TEXT,
  PROMPT_SKELETON  TEXT,
  -- Appended rather than placed next to FRIENDLY where it reads better: DCM's
  -- CREATE OR ALTER cannot add a column before the end of the list. Physical
  -- order does not matter, because the loader names its columns.
  VISITOR_PAIN     TEXT   COMMENT 'The complaint in the visitor''s own language. This is what keyword retrieval matches against - "we retype invoices all day" shares no words with AI_EXTRACT, but plenty with this.'
)
  COMMENT = 'The nine POC shapes a visitor can be routed to. Mirror of poc-archetypes.md.';

DEFINE TABLE {{db}}.{{schema}}.ROUTES (
  PLATFORM     TEXT   COMMENT 'Platform chip label, e.g. AWS, Microsoft / Azure',
  GUIDANCE     TEXT   COMMENT 'How their existing stack reaches Snowflake',
  DOCS_URL     TEXT
)
  COMMENT = 'Integration guidance per platform the visitor says they are on. Mirror of INTEGRATION_PATHS in game/server.py.';

DEFINE TABLE {{db}}.{{schema}}.CONTEXT_MANIFEST (
  SOURCE_FILE  TEXT   COMMENT 'Repo-relative path the rows were parsed from',
  TARGET_TABLE TEXT,
  CONTENT_SHA  TEXT   COMMENT 'SHA-256 of the source file, so a laptop can prove parity',
  ROW_COUNT    NUMBER(8,0),
  LOADED_AT    TIMESTAMP_LTZ,
  LOADED_BY    TEXT,
  GIT_REF      TEXT   COMMENT 'Commit the load was made from, when available'
)
  COMMENT = 'Provenance for the shared context tables. Lets any laptop check its markdown matches the account without diffing content.';
