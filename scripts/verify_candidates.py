#!/usr/bin/env python3
"""Verify agentic marketplace candidates against the live global catalogue.

The agentic search proposes listings; it does not prove they exist, and it cannot
see the flags that decide whether a visitor can actually use one. This closes that
gap: it matches every candidate against SHOW AVAILABLE LISTINGS and records the
provider, the access route and the regions from the catalogue itself.

A candidate is judged against ITS OWN EVENT'S REGION, never the booth account's.
The visitor never imports anything at the booth - they open the links later from
their own account - so what matters is that the dataset is available where THEY
are. London is judged on eu-west-2 even though every booth account is Frankfurt.

    SHOW AVAILABLE LISTINGS;                       -- in the app, then:
    python3 scripts/verify_candidates.py --catalogue <cached-query.json>

Writes marketplace-candidates-verified.json and prints a per-industry summary.
"""
import argparse
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFS = os.path.join(ROOT, "skills", "loco4coco", "references")
CAND = os.path.join(REFS, "marketplace-candidates.json")
OUT = os.path.join(REFS, "marketplace-candidates-verified.json")

# The EVENT LOCATION's region - what a recommendation is judged on.
PROFILE_REGION = {"uk": "AWS_EU_WEST_2", "fr": "AWS_EU_WEST_3", "de": "AWS_EU_CENTRAL_1",
                  "il": "AWS_EU_CENTRAL_1"}


def load_catalogue(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    cols = d["columns"] if "columns" in d else d["metadata"]["columns"]
    rows = d["rows"]
    idx = {c: i for i, c in enumerate(cols)}
    by_gn, by_title = {}, {}
    for r in rows:
        rec = {
            "global_name": r[idx["global_name"]],
            "title": r[idx["title"]],
            "provider": r[idx.get("organization_profile_name", idx["profile"])]
                        or r[idx["profile"]] or "",
            "regions": r[idx["regions"]] or "",
            "ready": str(r[idx["is_ready_for_import"]]).lower() == "true",
            "by_request": str(r[idx["is_by_request"]]).lower() == "true",
            "trial": str(r[idx["is_limited_trial"]]).lower() == "true",
            "monetized": str(r[idx["is_monetized"]]).lower() == "true",
            "discover_only": str(r[idx["discover_only"]]).lower() == "true",
        }
        if rec["global_name"]:
            by_gn[rec["global_name"].strip().upper()] = rec
        if rec["title"]:
            by_title.setdefault(rec["title"].strip().lower(), rec)
    return by_gn, by_title


def access_of(rec):
    """How a visitor gets it - the thing that decides if a link is a dead end."""
    if rec["discover_only"]:
        return "Discover only (cannot import)"
    if rec["by_request"]:
        return "By request"
    if rec["trial"]:
        return "Limited trial"
    if rec["monetized"]:
        return "Paid"
    return "Free"


def walk(cand):
    """Yield (profile, industry, entry) for every candidate in the file."""
    for profile, inds in (cand.get("profiles") or {}).items():
        for industry, block in (inds or {}).items():
            items = block if isinstance(block, list) else \
                (block or {}).get("candidates") or []
            for e in items:
                if isinstance(e, dict):
                    yield profile, industry, e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalogue", required=True,
                    help="cached JSON of SHOW AVAILABLE LISTINGS")
    a = ap.parse_args()

    by_gn, by_title = load_catalogue(a.catalogue)
    print(f"catalogue: {len(by_gn)} listings with a global name\n")

    with open(CAND, encoding="utf-8") as f:
        cand = json.load(f)

    out, stats = {}, {}
    for profile, industry, e in walk(cand):
        region = PROFILE_REGION.get(profile, "AWS_EU_CENTRAL_1")
        gn = (e.get("global_name") or "").strip().upper()
        title = (e.get("title") or "").strip()
        rec = by_gn.get(gn) or by_title.get(title.lower())

        v = {"claimed_title": title, "claimed_global_name": gn or None,
             "event_region": region}
        if not rec:
            v.update(verdict="NOT FOUND", note="no such listing in the catalogue")
        else:
            local = region in (rec["regions"] or "")
            v.update(global_name=rec["global_name"], title=rec["title"],
                     provider=rec["provider"], access=access_of(rec),
                     regions=rec["regions"], ready=rec["ready"], local=local,
                     url=f"https://app.snowflake.com/marketplace/listing/{rec['global_name']}")
            if rec["discover_only"]:
                v["verdict"] = "REJECT"
                v["note"] = "discover-only: the link is a dead end for a visitor"
            elif not local:
                v["verdict"] = "REJECT"
                v["note"] = f"not offered in {region}"
            elif not rec["ready"]:
                v["verdict"] = "RESERVE"
                v["note"] = "in region but needs a request or trial first"
            else:
                v["verdict"] = "OK"
                v["note"] = "available and importable in the event's region"

        out.setdefault(profile, {}).setdefault(industry, []).append(v)
        k = (profile, industry)
        stats.setdefault(k, {"OK": 0, "RESERVE": 0, "REJECT": 0, "NOT FOUND": 0})
        stats[k][v["verdict"]] += 1

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"_generated": {"by": "scripts/verify_candidates.py",
                                  "catalogue": os.path.basename(a.catalogue)},
                   "profiles": out}, f, indent=2, ensure_ascii=False)

    print(f"{'profile/industry':28s} {'OK':>3} {'RES':>4} {'REJ':>4} {'MISS':>5}")
    print("-" * 48)
    tot = {"OK": 0, "RESERVE": 0, "REJECT": 0, "NOT FOUND": 0}
    for (p, i), s in sorted(stats.items()):
        print(f"{p+'/'+i:28s} {s['OK']:>3} {s['RESERVE']:>4} "
              f"{s['REJECT']:>4} {s['NOT FOUND']:>5}")
        for k in tot:
            tot[k] += s[k]
    print("-" * 48)
    print(f"{'TOTAL':28s} {tot['OK']:>3} {tot['RESERVE']:>4} "
          f"{tot['REJECT']:>4} {tot['NOT FOUND']:>5}")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
