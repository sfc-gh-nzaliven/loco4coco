#!/usr/bin/env python3
"""Curate the per-city Marketplace picks with region-filtered Marketplace search.

Uses `cortex search marketplace` with an explicit cloudRegion filter, which is
what makes this correct AND fast:

    cortex search marketplace "<use case> for <country>" \
        --filter '{"cloudRegion":["AWS_EU_WEST_3"]}'

WHY THIS AND NOT A FREE-FORM AGENT PROMPT. The first attempt asked an agent, in
prose, to find listings for a country and to honour a region. MEASURED: 91
candidates, 0 hallucinated, but 78 unavailable in the event's region, and
strengthening the prompt to make the region a hard requirement changed nothing -
because the region is a FILTER PARAMETER on the search, not something to ask for
politely. Supplying it directly returns region-correct results in ~7 seconds
instead of ~237, and it returns provider names, which SHOW AVAILABLE LISTINGS
does not expose.

The region searched is the EVENT LOCATION's, never the booth account's. Every
event runs on an AWS Frankfurt account, but the visitor never imports anything at
the booth - they leave with links and open them later from their own account - so
what matters is that a dataset is relevant and available where THEY are.

    python3 scripts/curate_locality.py --profile fr de -c <your-connection>
    python3 scripts/curate_locality.py --profile fr --dry-run      # look first

Writes profiles.<profile> in references/marketplace.json (6 primary + reserves
per industry) and leaves every other profile untouched.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFS = os.path.join(ROOT, "skills", "loco4coco", "references")
JSON_PATH = os.path.join(REFS, "marketplace.json")

# The EVENT LOCATION's region and country - see the module docstring.
PROFILE = {
    "uk": ("AWS_EU_WEST_2", "the United Kingdom"),
    "fr": ("AWS_EU_WEST_3", "France"),
    "de": ("AWS_EU_CENTRAL_1", "Germany"),
    # Tel Aviv (Hayadata). Snowflake has no Israel region; Israeli accounts
    # most often sit in AWS Frankfurt, so that is the region judged against.
    "il": ("AWS_EU_CENTRAL_1", "Israel"),
}

INDUSTRIES = ["healthcare", "financial", "retail", "public",
              "manufacturing", "energy", "media", "other"]

# Several angles per industry so one weak query cannot leave a stall short, and
# so the six are not six versions of the same dataset.
USE_CASES = {
    "healthcare": ["population health and demographics",
                   "hospitals and health facilities",
                   "air quality and environmental health",
                   "disease prevalence and mortality",
                   "deprivation and health inequality",
                   "pharmaceutical and clinical research data"],
    "financial": ["macroeconomic and inflation indicators",
                  "company and business registries",
                  "ESG and sustainability risk",
                  "consumer spend and card transactions"],
    "retail": ["household income and geodemographic segmentation",
               "footfall and mobility",
               "weather driving retail demand",
               "product and price benchmarks"],
    "public": ["census and administrative boundaries",
               "addresses and property data",
               "transport networks and road data",
               "public tenders and government spending"],
    "manufacturing": ["supply chain and logistics",
                      "commodity and raw materials prices",
                      "shipping, ports and freight",
                      "company and supplier registries",
                      "industrial production and output statistics",
                      "energy and input costs for industry",
                      "trade, imports and exports"],
    "energy": ["renewable generation and grid data",
               "weather and severe weather warnings",
               "gas and oil market prices",
               "emissions and net-zero indicators",
               "electricity consumption and demand",
               "solar irradiance and wind resource",
               "EV charging and utility customer data"],
    "media": ["audience segmentation and demographics",
              "broadband and telecoms coverage",
              "social media and sentiment",
              "advertising and viewership",
              "mobile network performance",
              "events, venues and attendance"],
    "other": ["geospatial boundaries and geocoding",
              "company registries",
              "economic indicators",
              "weather forecasts"],
}

# Does a pick actually belong in this industry's stall? The picker fills six slots
# per industry, and where a city genuinely lacks sector-specific data the generic
# population/geography datasets win every slot. MEASURED on the first pass: MBI
# Sociodemographic Data for Germany led SEVEN of the eight German industries, and
# French manufacturing was handed road traffic casualties and health facilities.
# A pick with no thematic hit is demoted to reserve rather than deleted - it is
# still real, checked, local data, just not a headline pick for that sector.
THEME = {
    "healthcare": ["health", "hospital", "clinic", "patient", "medical", "disease",
                   "care", "air qual", "pharma", "mortal", "demograph",
                   "population", "age", "deprivation"],
    "financial": ["financ", "bank", "econom", "inflation", "macro", "compan",
                  "registr", "esg", "carbon", "spend", "card", "credit",
                  "market", "tax", "equity", "exchange", "purchasing power",
                  "income", "risk", "insur"],
    "retail": ["retail", "income", "consumer", "spend", "footfall", "mobility",
               "price", "product", "store", "shop", "basket", "segment",
               "purchasing power", "demograph", "weather", "catchment"],
    "public": ["census", "boundar", "address", "propert", "transport", "road",
               "tender", "government", "public", "deprivation", "planning",
               "admin", "postcode", "iris", "population", "geospatial"],
    "manufacturing": ["supply", "logistic", "commodit", "material", "shipping",
                      "port", "vessel", "supplier", "manufact", "industr",
                      "freight", "trade", "compan", "steel", "energy price",
                      "production"],
    "energy": ["energ", "renewab", "grid", "generation", "wind", "solar",
               "electric", "gas", "oil", "emission", "carbon", "net-zero",
               "weather", "power", "utilit", "charg", "irradiance",
               "consumption", "fuel", "climate"],
    "media": ["audience", "media", "social", "sentiment", "broadband", "telecom",
              "telco", "network", "advertis", "viewership", "content", "mobile",
              "coverage", "fiber", "fibre", "segment", "demograph", "event"],
    "other": ["boundar", "geocod", "geospatial", "compan", "registr", "econom",
              "weather", "address", "population", "h3", "travel", "census"],
}

# Content from the wrong country is a misfit even when it is available in region:
# the first pass put American Community Survey in a London media stall.
FOREIGN = {
    "uk": ["american", " usa", " u.s.", "united states", "canada", "australia",
           "germany", "german ", "france", "french"],
    "fr": ["american", " usa", " u.s.", "united states", "canada", "australia",
           "germany", "german ", "united kingdom", " uk", "britain"],
    "de": ["american", " usa", " u.s.", "united states", "canada", "australia",
           "france", "french", "united kingdom", " uk", "britain"],
    # Almost nothing on the Marketplace is Israel-specific, so global datasets
    # are the right answer here; only single-country content elsewhere is out.
    "il": ["american", " usa", " u.s.", "united states", "canada", "australia",
           "germany", "german ", "france", "french", "united kingdom", " uk",
           "britain", "england", "london", "paris", "munich", "japan",
           "india", "brazil", "european ", "pan european", "eu taxonomy"],
}

# How many industries one listing may LEAD in, per city. Without this the same
# generic dataset heads every stall and the six stop feeling chosen.
MAX_PRIMARY_APPEARANCES = 3

# A listing that cannot be attached is a dead end in a takeaway document, and an
# application is not a dataset a visitor can join to their own data.
SKIP_KINDS = ("APPLICATION",)

ENTRY = re.compile(
    r"^\s*\d+\.\s+(?P<title>.+?)\s+\(MARKETPLACE LISTING - (?P<kind>[A-Z ]+)\)\s*$")
FIELD = re.compile(r"^\s+(?P<key>Provider|Subtitle|Global Name|URL):\s*(?P<val>.+?)\s*$")


def on_theme(row, industry):
    hay = (row.get("title", "") + " " + row.get("subtitle", "")).lower()
    return any(k in hay for k in THEME.get(industry, []))


def foreign(row, profile):
    hay = (row.get("title", "") + " " + row.get("subtitle", "")).lower()
    return any(k in hay for k in FOREIGN.get(profile, []))


def search(query, region, conn, max_results, timeout):
    """One region-filtered Marketplace search. Returns a list of listing dicts."""
    cmd = ["cortex", "search", "marketplace", query,
           "--filter", json.dumps({"cloudRegion": [region]}),
           "--max-results", str(max_results)]
    if conn:
        cmd += ["-c", conn]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], "timeout"
    try:
        payload = json.loads(p.stdout)
    except json.JSONDecodeError:
        return [], (p.stdout or p.stderr or "unparseable")[:120]
    if "error" in payload:
        return [], str(payload["error"])[:120]

    out, cur = [], None
    for line in (payload.get("results") or "").splitlines():
        m = ENTRY.match(line)
        if m:
            if cur:
                out.append(cur)
            cur = {"title": m.group("title").strip(),
                   "kind": m.group("kind").strip()}
            continue
        if cur:
            f = FIELD.match(line)
            if f:
                cur[f.group("key").lower().replace(" ", "_")] = f.group("val")
    if cur:
        out.append(cur)
    return out, None


def load_catalogue(path):
    """global_name -> {regions, ready} so we can prove importability, not assume."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    cols = d.get("columns") or d["metadata"]["columns"]
    i = {c: n for n, c in enumerate(cols)}
    out = {}
    for r in d["rows"]:
        gn = r[i["global_name"]]
        if gn:
            out[gn.strip().upper()] = {
                "regions": r[i["regions"]] or "",
                "ready": str(r[i["is_ready_for_import"]]).lower() == "true",
                "by_request": str(r[i["is_by_request"]]).lower() == "true",
                "monetized": str(r[i["is_monetized"]]).lower() == "true",
                "discover_only": str(r[i["discover_only"]]).lower() == "true",
            }
    return out


def access_of(cat):
    if not cat:
        return "Free"
    if cat.get("by_request"):
        return "By request"
    if cat.get("monetized"):
        return "Paid"
    return "Free"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", nargs="+", required=True, choices=list(PROFILE))
    ap.add_argument("--industry", nargs="+", default=INDUSTRIES, choices=INDUSTRIES)
    ap.add_argument("--connection", "-c")
    ap.add_argument("--catalogue", help="cached SHOW AVAILABLE LISTINGS json, to "
                                       "confirm is_ready_for_import")
    ap.add_argument("--max-results", type=int, default=12)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--primary", type=int, default=6)
    ap.add_argument("--reserves", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written, change nothing")
    ap.add_argument("--reserves-only", action="store_true",
                    help="keep the existing primary six untouched and only fill "
                         "reserves. Use for uk: London's six are SE-reviewed and "
                         "deliberately not re-curated.")
    a = ap.parse_args()

    cat = load_catalogue(a.catalogue)
    if a.catalogue and not cat:
        print(f"warning: no catalogue at {a.catalogue}; access/ready unverified",
              file=sys.stderr)

    with open(JSON_PATH, encoding="utf-8") as f:
        market = json.load(f)
    market.setdefault("profiles", {})

    for profile in a.profile:
        region, country = PROFILE[profile]
        print(f"\n=== {profile}  region={region}  country={country}")
        leads = {}          # global_name -> how many industries it already leads
        for industry in a.industry:
            found, order = {}, 0
            for uc in USE_CASES[industry]:
                q = f"{uc} for {country}"
                t0 = time.time()
                rows, err = search(q, region, a.connection, a.max_results, a.timeout)
                kept = 0
                for r in rows:
                    gn = (r.get("global_name") or "").strip()
                    if not gn or r.get("kind") in SKIP_KINDS:
                        continue
                    c = cat.get(gn.upper())
                    # Reject on globally-true signals only.
                    #
                    # is_by_request means the visitor has to ask the provider and
                    # wait, so "tick it and it is yours" is not true of it. One of
                    # these reached a Paris primary slot before this check existed.
                    #
                    # is_ready_for_import is NOT used to reject. It reports whether
                    # THIS account can import, and every booth account is Frankfurt
                    # while the picks are judged for London, Paris or Berlin -
                    # measured, it marks London listings unavailable that a London
                    # visitor imports without trouble. The region string is the
                    # signal that travels, and it is checked above.
                    if c and c.get("by_request"):
                        continue
                    if c and c.get("discover_only"):
                        continue
                    if gn in found:
                        found[gn]["use_cases"].append(uc)
                        continue
                    order += 1
                    found[gn] = {
                        "global_name": gn,
                        "title": r.get("title") or "",
                        "provider": r.get("provider") or "",
                        "access": access_of(c),
                        "url": r.get("url")
                               or f"https://app.snowflake.com/marketplace/listing/{gn}",
                        "regions": (c or {}).get("regions") or region,
                        "ready": bool((c or {}).get("ready", True)),
                        "subtitle": r.get("subtitle") or "",
                        "use_cases": [uc],
                        "_rank": order,
                    }
                    kept += 1
                print(f"  [{industry}/{uc[:34]:34s}] {len(rows):2d} hits "
                      f"{kept:2d} new {time.time()-t0:4.0f}s"
                      f"{'  ' + err if err else ''}")

            # Breadth first: one pick per use case before a second from any of
            # them, so a stall of six covers six angles rather than six takes on
            # whichever query happened to return most.
            rows = sorted(found.values(), key=lambda r: r["_rank"])
            picked, seen_uc = [], set()
            for r in rows:
                uc = r["use_cases"][0]
                if uc not in seen_uc:
                    seen_uc.add(uc)
                    picked.append(r)
            picked += [r for r in rows if r not in picked]

            # Now decide what may LEAD. A pick has to be on theme for this
            # industry, be about this country rather than another, and not already
            # be heading three other stalls. Everything rejected stays available as
            # a reserve - it is real, checked, local data, just not a headline.
            lead, bench = [], []
            for r in picked:
                why = None
                if not on_theme(r, industry):
                    why = "off-theme"
                elif foreign(r, profile):
                    why = "other country"
                elif leads.get(r["global_name"], 0) >= MAX_PRIMARY_APPEARANCES:
                    why = "already leads 3 stalls"
                if why or len(lead) >= a.primary:
                    r["demoted"] = why or "beyond the six"
                    bench.append(r)
                else:
                    lead.append(r)
                    leads[r["global_name"]] = leads.get(r["global_name"], 0) + 1
            for r in lead + bench:
                r.pop("_rank", None)
            picked = lead + bench

            block = {"primary": picked[:a.primary],
                     "reserve": picked[a.primary:a.primary + a.reserves]}
            if a.reserves_only:
                # Keep the reviewed primary exactly as it is, and make sure a
                # reserve never duplicates one of them.
                existing = ((market["profiles"].get(profile) or {})
                            .get(industry) or {}).get("primary") or []
                have = {(r.get("global_name") or r.get("title")) for r in existing}
                block = {"primary": existing,
                         "reserve": [r for r in picked
                                     if r["global_name"] not in have][:a.reserves]}
            print(f"  -> {industry}: {len(block['primary'])} primary, "
                  f"{len(block['reserve'])} reserve"
                  f"{' (primary preserved)' if a.reserves_only else ''}"
                  f"{'' if a.reserves_only or len(lead) >= a.primary else '  on-theme only ' + str(len(lead))}")
            if not a.dry_run:
                market["profiles"].setdefault(profile, {})[industry] = block

    if a.dry_run:
        print("\n(dry run - nothing written)")
        return 0
    market.setdefault("_generated", {})["curate_locality"] = {
        "at": time.strftime("%Y-%m-%d %H:%M"),
        "profiles": a.profile,
        "how": "cortex search marketplace with a cloudRegion filter",
    }
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(market, f, indent=2, ensure_ascii=False)
    print(f"\nwrote {JSON_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
