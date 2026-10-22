#!/usr/bin/env python3
"""Deploy the SE operator console as LOCO4COCO.BOOTH.LOCO4COCO_SE_CONSOLE.

    python3 operator_app/deploy.py -c <connection>

Resolves the account's Streamlit compute pool at deploy time (it differs per
account, so it is not committed), writes snowflake.yml, deploys with the
container runtime and checks the object exists. No pyproject.toml on purpose:
the app uses only pre-installed packages, so no PyPI integration is needed.
"""
import argparse, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "game"))
from context import _snow_binary  # noqa: E402

NAME, DB, SCHEMA, WH = "LOCO4COCO_SE_CONSOLE", "LOCO4COCO", "BOOTH", "LOCO4COCO_WH"


def snow(conn, *args, capture=True):
    cmd = [_snow_binary(), *args, "-c", conn]
    r = subprocess.run(cmd, cwd=HERE, text=True,
                       capture_output=capture)
    if r.returncode:
        sys.exit(f"failed: {' '.join(args[:3])}\n{(r.stderr or r.stdout or '')[-1500:]}")
    return r.stdout


def sql_json(conn, q):
    return json.loads(snow(conn, "sql", "--format", "json", "-q", q) or "[]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--connection", required=True)
    ap.add_argument("--compute-pool", help="override the account default")
    a = ap.parse_args()

    pool = a.compute_pool
    if not pool:
        p = sql_json(a.connection,
                     "SHOW PARAMETERS LIKE 'DEFAULT_STREAMLIT_COMPUTE_POOL' IN ACCOUNT")
        pool = (p[0].get("value") or p[0].get("default")) if p else None
    pools = [r.get("name") for r in sql_json(a.connection, "SHOW COMPUTE POOLS")]
    if not pool or pool.upper() not in [x.upper() for x in pools if x]:
        sys.exit(f"No usable default Streamlit compute pool (got {pool!r}). "
                 f"Pick one and pass --compute-pool: {pools}")

    with open(os.path.join(HERE, "snowflake.yml"), "w") as f:
        f.write(f"""definition_version: 2
entities:
  se_console:
    type: streamlit
    identifier:
      name: {NAME}
      database: {DB}
      schema: {SCHEMA}
    query_warehouse: {WH}
    runtime_name: SYSTEM$ST_CONTAINER_RUNTIME_PY3_11
    compute_pool: {pool}
    main_file: streamlit_app.py
    title: "Loco for CoCo - SE console"
    artifacts:
      - streamlit_app.py
""")
    snow(a.connection, "streamlit", "deploy", "se_console", "--replace", "--prune", capture=False)
    rows = sql_json(a.connection, f"SHOW STREAMLITS LIKE '{NAME}' IN SCHEMA {DB}.{SCHEMA}")
    if not rows:
        sys.exit("Deploy returned OK but the STREAMLIT object is not there.")
    print(f"OK: {DB}.{SCHEMA}.{NAME} on compute pool {pool}")
    print(f"https://app.snowflake.com/#/streamlit-apps/{DB}.{SCHEMA}.{NAME}")


if __name__ == "__main__":
    main()
