#!/usr/bin/env python3
"""Audit every curated Marketplace pick against the live catalogue, per EVENT region.

Each profile is judged against ITS OWN EVENT's region, never the booth account's.
The visitor never imports anything at the booth - they open the links later from
their own account - so what matters is whether the dataset is available where THEY
are. London is judged on eu-west-2 even though every booth account is Frankfurt.
That is why one Frankfurt account can audit all three events.

This VERIFIES; it does not recurate. curate_locality.py replaces a whole stall and
will re-add a listing an earlier review deliberately deleted, so a blanket re-run
would silently undo curation decisions. Run this first, then fix only what it
names.

Four things can be wrong with a pick, and all four are judged on signals that are
GLOBAL rather than local to the account running the audit:

  GONE      no longer in the catalogue at all - a visitor gets a dead link
  REGION    exists, but not offered in that event's region - the link works and
            the import does not
  REQUEST   is_by_request - the visitor must ask the provider and wait, so the
            "click this and it is yours" promise does not hold
  DISCOVER  discover_only - visible in the catalogue but not obtainable
  PAID      the curated access label says Paid, and nothing Paid ships

TWO SIGNALS ARE DELIBERATELY NOT USED, because they are local to whichever account
runs the audit and reporting them as defects is how good picks get binned.

`is_ready_for_import` reports whether THIS account can import the listing. Run from
a Frankfurt booth account it is false for London picks that a London visitor can
import without trouble - measured: every London pick it flagged was present in
eu-west-2 and absent from eu-central-1. Judging an event's picks on it inverts the
whole point of auditing per event region.

`is_monetized` marks the listing as a paid product, which does not mean a visitor
cannot start on it for free: several curated picks are monetized listings carrying
a 7, 14, 30 or 60 day free trial, and the curated `access` string already records
exactly that. The trial is not always reflected in `is_limited_trial`, so
monetized-plus-trial is normal rather than a defect. The curated label is the
claim worth checking, and that is what PAID checks.

    python3 scripts/audit_marketplace_regions.py -c <connection>
    python3 scripts/audit_marketplace_regions.py --catalogue /tmp/cat.json
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "game"))
from context import _snow_binary  # noqa: E402  # resolve snow even when off PATH
JSON_PATH = os.path.join(ROOT, "skills", "loco4coco", "references", "marketplace.json")

# The EVENT's region per profile - see the module docstring.
PROFILE_REGION = {
    "uk": ("AWS_EU_WEST_2", "London"),
    "fr": ("AWS_EU_WEST_3", "Paris"),
    "de": ("AWS_EU_CENTRAL_1", "Berlin"),
    "il": ("AWS_EU_CENTRAL_1", "Tel Aviv"),
}


def pull_catalogue(conn):
    """SHOW AVAILABLE LISTINGS, as json. The regions column is global, so this is
    readable from any account and describes every event's region at once."""
    sql = ('SHOW AVAILABLE LISTINGS; SELECT "global_name", "title", "regions", '
           '"is_ready_for_import", "is_monetized", "is_by_request", '
           '"discover_only", "is_limited_trial" '
           'FROM TABLE(RESULT_SCAN(LAST_QUERY_ID(-1)))')
    cmd = [_snow_binary(), "sql", "--connection", conn, "--enable-templating", "NONE",
           "--format", "json", "-q", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        sys.exit("SHOW AVAILABLE LISTINGS failed: %s" % (r.stderr or r.stdout)[:400])
    payload = json.loads(r.stdout)
    # snow returns a list of result sets when several statements are sent.
    rows = payload[-1] if payload and isinstance(payload[0], list) else payload
    return rows


def index(rows):
    cat = {}
    for r in rows:
        gn = r.get("global_name")
        if gn:
            cat[gn] = r
    return cat


def in_region(regions, region):
    if not regions:
        return False
    if regions.strip().upper() == "ALL":
        return True
    return region in regions


def flag(row, name):
    return str(row.get(name)).strip().lower() in ("true", "1")


def verdict(row, region, pick):
    """None when the pick is sound, else a (code, detail) pair.

    Only globally-true signals are consulted - see the module docstring for why
    is_ready_for_import and is_monetized are excluded."""
    if row is None:
        return ("GONE", "not in the catalogue")
    if not in_region(row.get("regions"), region):
        return ("REGION", "not offered in %s" % region)
    if flag(row, "is_by_request"):
        return ("REQUEST", "is_by_request - the visitor must ask and wait")
    if flag(row, "discover_only"):
        return ("DISCOVER", "discover_only - visible but not obtainable")
    if (pick.get("access") or "").strip().lower() == "paid":
        return ("PAID", "curated access label says Paid")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection", "-c")
    ap.add_argument("--catalogue", help="cached SHOW AVAILABLE LISTINGS json")
    ap.add_argument("--primary-only", action="store_true",
                    help="ignore reserves - only the six a visitor sees")
    a = ap.parse_args()

    if a.catalogue:
        rows = json.load(open(a.catalogue))
    elif a.connection:
        rows = pull_catalogue(a.connection)
        json.dump(rows, open("/tmp/marketplace_catalogue.json", "w"))
        print("catalogue cached to /tmp/marketplace_catalogue.json")
    else:
        sys.exit("need --connection or --catalogue")

    cat = index(rows)
    print("catalogue: %d listings\n" % len(cat))

    market = json.load(open(JSON_PATH))
    problems = []
    checked = 0
    for profile, block in sorted(market["profiles"].items()):
        region, city = PROFILE_REGION[profile]
        tiers = ["primary"] if a.primary_only else ["primary", "reserve"]
        bad_here = 0
        for industry, stall in sorted(block.items()):
            for tier in tiers:
                for pos, pick in enumerate(stall.get(tier) or [], 1):
                    checked += 1
                    v = verdict(cat.get(pick["global_name"]), region, pick)
                    if v:
                        bad_here += 1
                        problems.append({
                            "profile": profile, "city": city, "region": region,
                            "industry": industry, "tier": tier, "position": pos,
                            "global_name": pick["global_name"],
                            "title": pick.get("title"),
                            "code": v[0], "detail": v[1],
                        })
        print("%-6s %-22s %-8s %d problem(s)" % (profile, city, region, bad_here))

    print("\nchecked %d picks, %d problem(s)" % (checked, len(problems)))
    if problems:
        print()
        for p in sorted(problems, key=lambda x: (x["code"], x["profile"], x["industry"])):
            seen = "%s/%s" % (p["profile"], p["industry"])
            print("  %-8s %-22s %-8s #%d %s" % (
                p["code"], seen, p["tier"], p["position"], (p["title"] or "")[:52]))
            print("           %s  (%s)" % (p["global_name"], p["detail"]))

    out = os.path.join(ROOT, "audit", "marketplace-region-audit.json")
    json.dump({"checked": checked, "problems": problems}, open(out, "w"), indent=2)
    print("\nwrote %s" % out)

    # EVERY pick is visitor-facing, reserves included. A stall borrows across
    # industries within the profile, so a reserve is not a quiet backup: measured,
    # a Paris energy visitor was offered two listings that existed only in
    # manufacturing/reserve. Grading reserves as lower severity is what let two
    # by-request listings reach a stall, so the tier is reported for context and
    # not used to soften the verdict.
    prim = [p for p in problems if p["tier"] == "primary"]
    if not problems:
        print("PASS - every curated pick is sound in its own event region")
    else:
        print("FAIL - %d problem(s): %d in a primary slot, %d in reserve. Reserves "
              "count: any stall can borrow them." % (len(problems), len(prim),
                                                    len(problems) - len(prim)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
