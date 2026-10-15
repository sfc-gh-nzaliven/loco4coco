#!/usr/bin/env python3
"""Build marketplace.json (the source of truth) and regenerate the human-readable
marketplace-index.md mirror from it.

marketplace.json shape:

    {
      "_generated": {...},
      "profiles": {
        "uk": { "<industry>": { "primary": [listing, ...], "reserve": [listing, ...] }, ... },
        "fr": { ... },
        "de": { ... }
      }
    }

listing = {global_name, title, provider, access, url, regions, ready}

`regions` is stored as the subset of the SWT venue cloud regions the listing is
available in (AWS_EU_WEST_2 / AWS_EU_WEST_3 / AWS_EU_CENTRAL_1), or "ALL". That
is exact for the booth's region filter, which only ever filters to a venue
region. `ready` is is_ready_for_import.

    python3 scripts/build_marketplace_json.py --seed-uk   # migrate current md -> json (uk primary)
    python3 scripts/build_marketplace_json.py --mirror     # regenerate marketplace-index.md from json

The fr/de profiles and the reserve lists are populated by the agentic
precompute (scripts/precompute_marketplace.py); this script only seeds uk from
the existing curated md and keeps the md mirror in sync.
"""

import argparse
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REFS = os.path.join(ROOT, "skills", "loco4coco", "references")
JSON_PATH = os.path.join(REFS, "marketplace.json")
MD_PATH = os.path.join(REFS, "marketplace-index.md")

SWT_REGIONS = ["AWS_EU_WEST_2", "AWS_EU_WEST_3", "AWS_EU_CENTRAL_1"]
INDUSTRY_ORDER = ["healthcare", "financial", "retail", "public",
                  "manufacturing", "energy", "media", "other"]

# Authoritative availability, measured 2026-09-16 from SHOW AVAILABLE LISTINGS
# (regions is global, so readable from any account). Tuple: (london eu-west-2,
# paris eu-west-3, frankfurt/berlin eu-central-1, is_ready_for_import). "ALL"
# listings are marked with regions "ALL" below rather than the triple.
AVAIL = {
    "GZ1MOZBWYYT": (1, 1, 1, True, "ALL"),
    "GZ2FRZQNY1":  (1, 0, 1, True, None),
    "GZ2FSZH8URW": (1, 0, 1, True, None),
    "GZSNZ4PHA6":  (1, 1, 1, True, "ALL"),
    "GZSOZ71OJH":  (1, 1, 1, True, None),
    "GZSTZ67BY9OQW": (1, 0, 1, True, None),
    "GZSTZLT2II6": (1, 1, 1, True, None),
    "GZSVZ1K7UA":  (1, 1, 1, True, None),
    "GZSVZ1K7UQ":  (1, 1, 1, True, None),
    "GZSVZ1K7UU":  (1, 1, 1, True, None),
    "GZSVZ1K7VF":  (1, 0, 1, True, None),
    "GZSVZ8MX1I":  (1, 1, 1, True, "ALL"),
    "GZSVZAJO3":   (1, 0, 0, False, None),
    "GZSYZSRWU5":  (1, 1, 1, True, "ALL"),
    "GZT0Z4CM1E9KJ": (1, 0, 1, True, None),
    "GZT0Z4CM1E9L4": (1, 0, 0, False, None),
    "GZT0Z4CM1E9NA": (1, 1, 1, True, None),
    "GZT0ZGCQ51RQ": (1, 1, 1, True, "ALL"),
    "GZT0ZI0XJ6Q": (1, 0, 1, True, None),
    "GZT0ZKUCHKL": (1, 0, 0, False, None),
    "GZT0ZPWB4J7": (1, 1, 1, True, "ALL"),
    "GZT1ZFQ0JE5": (1, 1, 1, False, None),
    "GZTDZ1PNFO":  (1, 1, 1, False, None),
    "GZTDZ7DJU9":  (1, 1, 1, True, None),
    "GZTDZJKVCM":  (1, 0, 1, True, None),
    "GZTDZJKVCU":  (1, 1, 1, True, None),
    "GZTDZJKVCY":  (1, 0, 1, True, None),
    "GZTDZJKVH3":  (1, 0, 0, False, None),
    "GZTSZ290BVCAO": (1, 1, 1, True, None),
    "GZTSZRC7HQ3": (1, 1, 1, True, None),
    "GZTYZAPS3FP": (1, 1, 1, True, None),
}


def regions_for(gn):
    a = AVAIL.get(gn)
    if not a:
        return "ALL"          # unknown: don't hide it, let the region filter keep it
    london, paris, frankfurt, _ready, allflag = a
    if allflag == "ALL":
        return "ALL"
    present = [r for r, flag in zip(SWT_REGIONS, (london, paris, frankfurt)) if flag]
    return ",".join(present) if present else "AWS_EU_WEST_2"


def ready_for(gn):
    a = AVAIL.get(gn)
    return bool(a[3]) if a else True


def parse_md():
    """Current marketplace-index.md -> {industry: [listing dict, ...]} (uk)."""
    with open(MD_PATH, encoding="utf-8") as f:
        text = f.read()
    row_re = re.compile(r"^\|\s*\[(?P<title>.+?)\]\((?P<url>[^)]+)\)\s*\|"
                        r"\s*(?P<prov>[^|]+?)\s*\|\s*(?P<acc>[^|]+?)\s*\|"
                        r"\s*`(?P<gname>[^`]+)`\s*\|\s*(?P<regs>[^|]*)\|")
    out, current = {}, None
    for line in text.splitlines():
        h = re.match(r"^##\s+([a-z_]+)\s*$", line.strip())
        if h:
            current = h.group(1)
            out.setdefault(current, [])
            continue
        if not current:
            continue
        m = row_re.match(line.strip())
        if m:
            gn = m.group("gname").strip()
            out[current].append({
                "global_name": gn,
                "title": m.group("title").strip(),
                "provider": m.group("prov").strip(),
                "access": m.group("acc").strip(),
                "url": m.group("url").strip(),
                "regions": regions_for(gn),
                "ready": ready_for(gn),
            })
    return out


def seed_uk():
    uk = parse_md()
    profiles = {"uk": {}, "fr": {}, "de": {}}
    for ind in INDUSTRY_ORDER:
        profiles["uk"][ind] = {"primary": uk.get(ind, []), "reserve": []}
        profiles["fr"][ind] = {"primary": [], "reserve": []}
        profiles["de"][ind] = {"primary": [], "reserve": []}
    payload = {
        "_generated": {
            "note": "GENERATED source of truth for the booth marketplace. Edit via "
                    "scripts/precompute_marketplace.py (agentic) or by hand, then run "
                    "--mirror to refresh marketplace-index.md. uk seeded from the "
                    "original curated md; fr/de/reserve filled by the precompute.",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "availability_measured": "2026-09-16",
        },
        "profiles": profiles,
    }
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    counts = {p: sum(len(profiles[p][i]["primary"]) + len(profiles[p][i]["reserve"])
                     for i in profiles[p]) for p in profiles}
    print("wrote", JSON_PATH, "counts:", counts)


def write_mirror():
    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    profiles = data["profiles"]
    lines = [
        "---",
        "name: marketplace-index",
        "description: \"Human-readable mirror of marketplace.json (the source of "
        "truth). Curated Marketplace listings offered to booth visitors, per "
        "market profile (uk/fr/de) and industry, with primary and reserve picks. "
        "GENERATED - edit marketplace.json, then run scripts/build_marketplace_json.py --mirror.\"",
        "---",
        "",
        "# Curated Marketplace listings (mirror of marketplace.json)",
        "",
        "Profiles: **uk** (London eu-west-2), **fr** (Paris eu-west-3), "
        "**de** (Frankfurt & Berlin eu-central-1), **il** (Tel Aviv, judged "
        "on eu-central-1). `regions` is the subset of the "
        "three SWT venue regions each listing is available in, or ALL.",
        "",
    ]
    for prof in ("uk", "fr", "de", "il"):
        lines.append("# profile: %s" % prof)
        lines.append("")
        for ind in INDUSTRY_ORDER:
            block = (profiles.get(prof) or {}).get(ind) or {}
            for kind in ("primary", "reserve"):
                rows = block.get(kind) or []
                if not rows:
                    continue
                lines.append("## %s / %s / %s" % (prof, ind, kind))
                lines.append("")
                lines.append("| Listing | Provider | Access | Global name | Regions |")
                lines.append("|---|---|---|---|---|")
                for r in rows:
                    lines.append("| [%s](%s) | %s | %s | `%s` | %s |" % (
                        r["title"], r["url"], r["provider"], r["access"],
                        r["global_name"], r["regions"]))
                lines.append("")
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("wrote mirror", MD_PATH)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-uk", action="store_true")
    ap.add_argument("--mirror", action="store_true")
    a = ap.parse_args()
    if a.seed_uk:
        seed_uk()
    if a.mirror:
        write_mirror()
    if not (a.seed_uk or a.mirror):
        ap.print_help()
