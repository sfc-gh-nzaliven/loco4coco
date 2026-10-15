#!/usr/bin/env python3
"""Agentic marketplace precompute: fill the per-region/per-industry gaps.

For each (market profile x industry x creative use-case) it asks the
marketplace-search skill (PrPr agentic discovery) via `cortex exec` for free,
importable, region-appropriate Snowflake Marketplace listings, then VERIFIES
every candidate against SHOW AVAILABLE LISTINGS (region membership +
is_ready_for_import) and writes them to a candidates file for SE review.

It never edits marketplace.json directly - curation is judgement, and provider
names cannot be scraped. The SE reviews marketplace-candidates.json and promotes
the good ones into marketplace.json (primary or reserve).

    python3 scripts/precompute_marketplace.py --profile uk --industry media -c PG_LONDON
    python3 scripts/precompute_marketplace.py --profile de --industry media energy -c <your-connection>
    python3 scripts/precompute_marketplace.py --all -c <your-connection>          # everything (slow)

Latency: each cortex exec is ~30-110s. Runs are serialised. Use --industry to
scope. Offline, so latency does not matter the way it does at the booth.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REFS = os.path.join(ROOT, "skills", "loco4coco", "references")
OUT = os.path.join(REFS, "marketplace-candidates.json")

# The region a candidate is judged against is the EVENT LOCATION's region, NOT the
# booth account's. Every event account is in AWS Frankfurt, but a visitor never
# imports anything at the booth - they leave with links and open them later from
# their own account, wherever they are. So a London recommendation is judged on
# AWS_EU_WEST_2 availability even though the booth writes to eu-central-1.
PROFILE_REGION = {"uk": "AWS_EU_WEST_2", "fr": "AWS_EU_WEST_3", "de": "AWS_EU_CENTRAL_1",
                  "il": "AWS_EU_CENTRAL_1"}
PROFILE_COUNTRY = {"uk": "the United Kingdom", "fr": "France", "de": "Germany",
                   "il": "Israel"}

INDUSTRIES = ["healthcare", "financial", "retail", "public",
              "manufacturing", "energy", "media", "other"]

# Creative use-cases per industry - deliberately broader than the sector bucket,
# so the search surfaces joins a visitor would not think to ask for. Each becomes
# a separate agentic query, widening the candidate pool.
USE_CASES = {
    "healthcare": ["population health and demographics", "clinical research and drug data",
                   "environmental and air-quality data affecting health"],
    "financial": ["macroeconomic and inflation indicators", "company and ownership registries",
                  "ESG and sustainability risk", "consumer spend and card data"],
    "retail": ["household income and geodemographic segmentation", "footfall and mobility",
               "weather driving demand", "product and price benchmarks"],
    "public": ["census and administrative boundaries", "addresses and property (UPRN-style)",
               "transport networks and road data", "deprivation and planning"],
    "manufacturing": ["supply chain and logistics", "commodity and materials prices",
                      "shipping and port activity", "company and supplier data"],
    "energy": ["weather and severe-weather warnings", "grid, generation and wind/solar forecasts",
               "gas and oil market data", "emissions and net-zero indicators"],
    "media": ["audience segmentation and demographics", "social and sentiment conversations",
              "broadband/telco coverage and network", "advertising, content and viewership"],
    "other": ["geospatial boundaries and geocoding", "company registries",
              "economic indicators", "weather"],
}


def cortex_exec(prompt, conn, timeout):
    cmd = ["cortex", "exec", prompt, "--format", "json", "--bypass", "--no-history"]
    if conn:
        cmd += ["--connection", conn]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return "", False
    final, ok = "", False
    for raw in (p.stdout or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            final = (ev.get("result") or "").strip()
            ok = not ev.get("is_error")
    return final, ok


def parse_candidates(text):
    """Pull a JSON array of {title, provider, global_name} out of the reply."""
    m = re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for o in arr:
        if isinstance(o, dict) and (o.get("global_name") or o.get("title")):
            out.append({"global_name": (o.get("global_name") or "").strip(),
                        "title": (o.get("title") or "").strip(),
                        "provider": (o.get("provider") or "").strip()})
    return out


def verify(global_names, region, conn):
    """SHOW AVAILABLE LISTINGS -> {global_name: {title, ready, in_region, regions}}."""
    gns = [g for g in global_names if g]
    if not gns:
        return {}
    inlist = ",".join("'" + g.replace("'", "") + "'" for g in gns)
    sql = (
        "SHOW AVAILABLE LISTINGS; "
        "SELECT \"global_name\" gn, \"title\" title, "
        "\"is_ready_for_import\" ready, "
        "(CONTAINS(\"regions\",'%s') OR \"regions\"='ALL') in_region "
        "FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE \"global_name\" IN (%s)"
        % (region, inlist))
    cmd = ["snow", "sql", "-q", sql, "--format", "json", "--enable-templating", "NONE"]
    if conn:
        cmd += ["-c", conn]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        sys.stderr.write((p.stderr or p.stdout or "")[-500:] + "\n")
        return {}
    try:
        rows = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return {r["GN"]: {"title": r.get("TITLE"),
                      "ready": str(r.get("READY")).lower() == "true",
                      "in_region": str(r.get("IN_REGION")).lower() == "true"}
            for r in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", nargs="*", default=["uk"], choices=list(PROFILE_REGION))
    ap.add_argument("--industry", nargs="*", default=None)
    ap.add_argument("--all", action="store_true", help="all profiles x all industries")
    ap.add_argument("--connection", "-c", default=None)
    ap.add_argument("--timeout", type=float, default=150)
    ap.add_argument("--use-cases", type=int, default=0,
                    help="Cap the creative use-cases per industry (0 = all). Each "
                         "one is a separate ~75s agentic call, so 1 keeps a full "
                         "3-event sweep inside about 20 minutes.")
    ap.add_argument("--no-verify", action="store_true",
                    help="Collect candidates only, skip the SHOW AVAILABLE LISTINGS "
                         "check. Use when the connection needs an interactive OAuth "
                         "redirect (a background snow sql cannot complete one and the "
                         "timeout is paid on every industry). Verify afterwards in one "
                         "batch from a live session.")
    a = ap.parse_args()

    profiles = list(PROFILE_REGION) if a.all else a.profile
    industries = a.industry or (INDUSTRIES if (a.all or not a.industry) else a.industry)

    result = {}
    if os.path.exists(OUT):
        try:
            result = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            result = {}
    result.setdefault("profiles", {})

    for profile in profiles:
        region, country = PROFILE_REGION[profile], PROFILE_COUNTRY[profile]
        result["profiles"].setdefault(profile, {})
        for industry in industries:
            found = {}
            ucs = USE_CASES.get(industry, [])
            if a.use_cases > 0:
                ucs = ucs[:a.use_cases]
            for uc in ucs:
                # The region MUST be in the prompt. It was not, originally - the
                # prompt asked only "for {country}" and the region was applied
                # afterwards as a rejection filter. MEASURED result: 71 of 91
                # candidates failed that filter, and only 3 of them existed in any
                # EU region at all. We were not being refused, we were not asking.
                #
                # This works from a Frankfurt account because SHOW AVAILABLE
                # LISTINGS returns the GLOBAL catalogue: the "regions" column
                # lists every region a listing is published to, so eu-west-3
                # availability is readable from eu-central-1. The agent is told
                # how to check rather than merely asked to prefer, because a soft
                # "prefer" is what produced US-only results.
                prompt = (
                    f"Find Snowflake Marketplace listings relevant to {country} "
                    f"about: {uc}.\n\n"
                    f"HARD REQUIREMENT - the listing must be available in the "
                    f"{region} region. Verify this yourself, do not assume: run "
                    f"SHOW AVAILABLE LISTINGS and keep a row ONLY if its "
                    f'"regions" column contains {region} (or equals ALL). '
                    f"Discard everything else, even if it looks like a perfect "
                    f"subject match. Most Marketplace data is US-only, so expect "
                    f"to reject many candidates - returning three verified "
                    f"listings is better than ten unverified ones.\n"
                    f"Note: the account you are connected to is in "
                    f"AWS_EU_CENTRAL_1. That is irrelevant - filter for "
                    f"{region}, which is where this event's visitors are.\n\n"
                    f"Also require: free to import, and is_ready_for_import = "
                    f"true (not by-request, not discover-only).\n\n"
                    f"Return ONLY a JSON array (no prose, no code fence) of "
                    f"objects with keys title, provider, global_name (the GZ... "
                    f'id), and regions (the "regions" value you verified).')
                t0 = time.time()
                text, ok = cortex_exec(prompt, a.connection, a.timeout)
                cands = parse_candidates(text)
                print(f"[{profile}/{industry}] {uc[:40]:40s} {len(cands):2d} cands "
                      f"{time.time()-t0:.0f}s {'ok' if ok else 'FAIL'}")
                for c in cands:
                    key = c["global_name"] or c["title"]
                    found.setdefault(key, {**c, "use_cases": []})
                    found[key]["use_cases"].append(uc)
            ver = {} if a.no_verify else verify(
                [c.get("global_name") for c in found.values()], region, a.connection)
            rows = []
            for c in found.values():
                v = ver.get(c["global_name"], {})
                rows.append({**c,
                             "verified_title": v.get("title"),
                             "ready": v.get("ready"),
                             "in_region": v.get("in_region"),
                             "region_checked": region})
            # importable in-region first
            rows.sort(key=lambda r: (not (r.get("ready") and r.get("in_region")),
                                     r.get("global_name") or ""))
            result["profiles"][profile][industry] = rows
            keep = sum(1 for r in rows if r.get("ready") and r.get("in_region"))
            print(f"  -> {len(rows)} unique, {keep} importable in {region}")
            json.dump(result, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    result["_generated"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "note": "Agentic candidates for SE review. Promote good ones "
                                    "into marketplace.json (primary/reserve). NOT shipped as-is."}
    json.dump(result, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print("\nwrote", OUT)


if __name__ == "__main__":
    sys.exit(main())
