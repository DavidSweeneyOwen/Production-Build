#!/usr/bin/env python3
"""
CheckFire Stock Availability - NetSuite web queries -> docs/data.json

Three NetSuite saved reports, one per location. The location is NOT a column in
the export - it comes from which report the row arrived in:

    NS_URL_1  cr=2252  "Production Build Count V3 CF"        -> Checkfire Unit 19
    NS_URL_3  cr=2254  "Production Build Count V3 Northern"  -> Northern Depot
    NS_URL_2  cr=2253  "Production Build Count V3 PJ"        -> PJ Fire Main Warehouse
    NS_EMAIL           the address the .iqy prompts for in Excel

For each item at each location:  Available = On Hand - Committed

Because location is inferred from the source, the script verifies that
assumption: in a correctly filtered single-location report each item appears
once (twice if a sublocation is included). An item appearing three or more
times means the report still holds every location, and the run aborts rather
than reporting a group total as one depot's stock.

Some reports also carry a Location column (and a blank "Location: Name
(Grouped)" grouping column). Those are NOT keyed off directly - they are used
to cross-check the source mapping, and a disagreement aborts the run, which is
what catches a URL sitting in the wrong secret.

Run with --debug (or tick "debug" on Run workflow) to dump the detected layout
without writing data.json.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

import requests
import pandas as pd

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data.json")

# (env var, location this report covers, human label for the logs)
SOURCES = [
    ("NS_URL_1", "Checkfire Unit 19", "cr=2252 V3 CF"),
    ("NS_URL_3", "Northern Depot", "cr=2254 V3 Northern"),
    ("NS_URL_2", "PJ Fire Main Warehouse", "cr=2253 V3 PJ"),
]

# Fragments used only when a report DOES expose location names.
LOCATIONS = {
    "Checkfire Unit 19": ["checkfire unit 19", "unit 19"],
    "Northern Depot": ["northern depot", "northen depot"],
    "PJ Fire Main Warehouse": ["pj fire", "pjfire"],
}

FIELD_PATTERNS = {
    "location": [r"location"],
    "item": [r"^item$", r"item\s*(name|number|id)$", r"^name$", r"^sku$"],
    "description": [r"descrip", r"display\s*name"],
    "on_hand": [r"on\s*hand(?!.*value)"],
    "committed": [r"committed"],
}

ON_HAND_RE = re.compile(r"on\s*hand(?!.*value)", re.I)
COMMITTED_RE = re.compile(r"committed", re.I)
ITEM_RE = re.compile(r"^item$|item\s*(name|number|id)?$", re.I)
DESC_RE = re.compile(r"descrip", re.I)

JUNK_ITEM_RE = re.compile(
    r"^(total\b|assembly/bill of materials$|inventory item$|non-?inventory item$|"
    r"kit/package$|service$|other charge$|group$)",
    re.I,
)

# An item appearing this many times in a single-location report means the
# report is not actually filtered to one location.
MAX_ROWS_PER_ITEM = 3

TIMEOUT = 120


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def norm(s) -> str:
    if s is None:
        return ""
    text = re.sub(r"\s+", " ", str(s)).strip()
    return "" if text.lower() in ("nan", "none") else text


def with_email(url: str, email: str | None) -> str:
    if not email:
        return url
    parts = urlparse(url)
    qs = dict(parse_qsl(parts.query, keep_blank_values=True))
    qs["email"] = email
    return urlunparse(parts._replace(query=urlencode(qs)))


def fetch(url: str, email: str | None) -> str:
    resp = requests.get(
        with_email(url, email),
        timeout=TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; CheckFireStockBoard/1.0)"},
    )
    resp.raise_for_status()
    if "login" in resp.url.lower():
        raise RuntimeError("NetSuite returned a login page - check the hash and email.")
    return resp.text


def to_number(value) -> float:
    """NetSuite's web query prefixes figures with '=' (Excel formula escaping)."""
    text = re.sub(r"[^\d.\-]", "", str(value))
    if text in ("", "-", "."):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def match_location(raw: str) -> str | None:
    low = norm(raw).lower()
    if not low or low.startswith("total"):
        return None
    for display, fragments in LOCATIONS.items():
        if any(f in low for f in fragments):
            return display
    return None


def biggest(tables):
    return max(tables, key=lambda t: t.shape[0] * t.shape[1])


# --------------------------------------------------------------------------
# Table reading
# --------------------------------------------------------------------------

def promote_header(df: pd.DataFrame) -> pd.DataFrame:
    """NetSuite renders header cells as <td>, so pandas labels the columns 0..n
    and leaves the real names in a data row. Find that row and promote it."""
    labels = [norm(c).lower() for c in df.columns]
    unlabelled = sum(1 for c in labels if c.isdigit() or c.startswith("unnamed"))
    if unlabelled < max(1, len(labels) * 0.6):
        return df
    best_i, best = 0, -1
    for i in range(min(6, len(df))):
        cells = [norm(v).lower() for v in df.iloc[i]]
        score = sum(1 for pats in FIELD_PATTERNS.values()
                    if any(re.search(pats[0], c) for c in cells))
        if score > best:
            best_i, best = i, score
    df.columns = [norm(v) for v in df.iloc[best_i]]
    out = df.iloc[best_i + 1:].reset_index(drop=True)
    first = df.columns[0]
    return out[out.iloc[:, 0].map(lambda v: norm(v) != first)].reset_index(drop=True)


def read_flat(html: str) -> pd.DataFrame | None:
    try:
        tables = pd.read_html(io.StringIO(html))
    except Exception:
        return None
    return promote_header(biggest(tables)) if tables else None


def read_pivot(html: str) -> pd.DataFrame | None:
    try:
        tables = pd.read_html(io.StringIO(html), header=[0, 1])
    except Exception:
        return None
    tables = [t for t in tables if isinstance(t.columns, pd.MultiIndex) and t.shape[1] > 5]
    return biggest(tables) if tables else None


def map_columns(df: pd.DataFrame) -> dict[str, str]:
    cols = {norm(c).lower(): c for c in df.columns}
    mapping: dict[str, str] = {}
    for field, patterns in FIELD_PATTERNS.items():
        for pattern in patterns:
            hit = next((orig for low, orig in cols.items() if re.search(pattern, low)), None)
            if hit and hit not in mapping.values():
                mapping[field] = hit
                break
    return mapping


def map_pivot_columns(df: pd.DataFrame):
    item_col = desc_col = None
    groups: dict[str, dict[str, list]] = {}
    for col in df.columns:
        top, sub = norm(col[0]), norm(col[1])
        top_l, sub_l = top.lower(), sub.lower()
        if top_l.startswith("unnamed") or top_l == sub_l:
            label = sub if not sub_l.startswith("unnamed") else top
            if item_col is None and ITEM_RE.search(label):
                item_col = col
            elif desc_col is None and DESC_RE.search(label):
                desc_col = col
            continue
        loc = match_location(top)
        if not loc:
            continue
        if ON_HAND_RE.search(sub):
            groups.setdefault(loc, {}).setdefault("on_hand", []).append(col)
        elif COMMITTED_RE.search(sub):
            groups.setdefault(loc, {}).setdefault("committed", []).append(col)
    return item_col, desc_col, groups


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def blank(item: str, desc: str, loc: str) -> dict:
    return {"item": item, "description": desc, "location": loc,
            "on_hand": 0.0, "committed": 0.0}


def extract(html: str, label: str, location: str, debug: bool = False) -> dict:
    """Returns {(item, location): record}. `location` is the site this report
    is filtered to, used only when the export carries no location itself."""
    if debug:
        print(f"\n{'=' * 78}\n--- {label}  ->  {location} ---\n{'=' * 78}")

    # 1. Pivoted layout, locations as column groups (older report style).
    pivot = read_pivot(html)
    if pivot is not None:
        item_col, desc_col, groups = map_pivot_columns(pivot)
        if item_col is not None and groups:
            if debug:
                print(f"  layout   : PIVOT - locations read from column groups {list(groups)}")
            records = {}
            for _, row in pivot.iterrows():
                item = norm(row[item_col])
                desc = norm(row[desc_col]) if desc_col is not None else ""
                if not item or JUNK_ITEM_RE.match(item) or not desc:
                    continue
                for loc, cols in groups.items():
                    rec = blank(item, desc, loc)
                    rec["on_hand"] = sum(to_number(row[c]) for c in cols.get("on_hand", []))
                    rec["committed"] = sum(to_number(row[c]) for c in cols.get("committed", []))
                    records[(item, loc)] = rec
            if debug:
                print(f"  kept {len(records)} records")
            return records

    # 2. Flat table.
    df = read_flat(html)
    if df is None:
        raise RuntimeError(f"{label}: no table found in the response.")
    mapping = map_columns(df)

    if "item" not in mapping:
        raise RuntimeError(
            f"{label}: no Item column found.\n"
            f"  columns: {list(df.columns)}\n"
            f"  first rows:\n{df.head(4).to_string()}"
        )

    # Location is assigned from WHICH REPORT the row came from. Any location
    # column present is used only to cross-check that assignment - some of
    # these reports carry a grouping column that is blank on data rows, so it
    # is not safe to key off directly.
    loc_candidates = [c for c in df.columns if re.search(r"location", norm(c), re.I)]

    if debug:
        print(f"  layout   : FLAT ({df.shape[0]} rows x {df.shape[1]} cols)")
        print(f"  columns  : {list(df.columns)}")
        print(f"  mapping  : {mapping}")
        print(f"  location : from source = {location}")
        for c in loc_candidates:
            vals = sorted({norm(v) for v in df[c] if norm(v)})
            hits = sum(1 for v in df[c] if match_location(v))
            print(f"  xcheck   : {c!r} -> {hits}/{len(df)} rows resolve; values {vals[:6]}")

    records: dict[tuple[str, str], dict] = {}
    counts: Counter = Counter()
    mismatches: Counter = Counter()
    skipped = 0

    for _, row in df.iterrows():
        item = norm(row[mapping["item"]])
        desc = norm(row[mapping["description"]]) if "description" in mapping else ""
        if not item or JUNK_ITEM_RE.match(item) or (("description" in mapping) and not desc):
            skipped += 1
            continue

        for c in loc_candidates:
            seen = match_location(row[c])
            if seen and seen != location:
                mismatches[seen] += 1

        counts[item] += 1
        rec = records.setdefault((item, location), blank(item, desc, location))
        if desc and not rec["description"]:
            rec["description"] = desc
        if "on_hand" in mapping:
            rec["on_hand"] += to_number(row[mapping["on_hand"]])
        if "committed" in mapping:
            rec["committed"] += to_number(row[mapping["committed"]])

    if mismatches:
        raise RuntimeError(
            f"{label}: LOCATION MISMATCH - this report is mapped to {location},\n"
            f"  but its own location column says {dict(mismatches)}.\n"
            f"  Either the URL is in the wrong secret, or the report's filter changed.\n"
            f"  Check the cr= number in each of NS_URL_1/2/3 against the table in the README."
        )

    per_row_location = False

    # Verify the one-report-per-location assumption.
    if not per_row_location and counts:
        worst_item, worst = counts.most_common(1)[0]
        repeated = {i: c for i, c in counts.items() if c > 1}
        if worst >= MAX_ROWS_PER_ITEM:
            raise RuntimeError(
                f"{label}: THIS REPORT IS NOT FILTERED TO A SINGLE LOCATION.\n"
                f"  '{worst_item}' appears {worst} times, and there is no Location column,\n"
                f"  so those rows are almost certainly the different sites. Summing them\n"
                f"  would report the group total as {location}'s stock.\n"
                f"  Fix the report's location filter in NetSuite, then re-run.\n"
                f"  Items appearing more than once: {len(repeated)} of {len(counts)}."
            )
        if repeated and debug:
            print(f"  note     : {len(repeated)} item(s) had 2 rows (sublocation) - summed")

    if debug:
        print(f"  kept {len(records)} records ({skipped} furniture/unmatched rows skipped)")
        for r in list(records.values())[:10]:
            print(f"    {r['item']:<20} on hand {r['on_hand']:>8.0f}  "
                  f"committed {r['committed']:>8.0f}  available {r['on_hand']-r['committed']:>8.0f}")

    return records


def merge(*sources: dict) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for src in sources:
        for key, rec in src.items():
            if key not in merged:
                merged[key] = dict(rec)
            elif not merged[key].get("description"):
                merged[key]["description"] = rec.get("description", "")

    rows = []
    for rec in merged.values():
        oh, cm = rec["on_hand"], rec["committed"]
        rows.append({
            "item": rec["item"],
            "description": rec["description"],
            "location": rec["location"],
            "onHand": round(oh, 2),
            "committed": round(cm, 2),
            "available": round(oh - cm, 2),
        })
    rows.sort(key=lambda r: (r["item"].lower(), r["location"]))
    return rows


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="dump layout, do not write output")
    args = parser.parse_args()

    email = os.environ.get("NS_EMAIL")
    configured = [(os.environ.get(env), loc, label) for env, loc, label in SOURCES]
    if not any(u for u, _, _ in configured):
        print("ERROR: set NS_URL_1 / NS_URL_2 / NS_URL_3.", file=sys.stderr)
        return 1

    sources, failures = [], []
    for url, loc, label in configured:
        if not url:
            print(f"({label} not configured - {loc} will be missing)")
            continue
        print(f"Fetching {label} -> {loc} ...")
        try:
            sources.append(extract(fetch(url, email), label, loc, debug=args.debug))
        except Exception as exc:
            if not args.debug:
                raise
            print(f"\n!!! {label} failed: {type(exc).__name__}: {exc}\n")
            failures.append(label)

    if args.debug:
        print(f"\nDebug run complete - data.json was not written."
              f"{' Failures: ' + ', '.join(failures) if failures else ''}")
        return 0

    rows = merge(*sources)
    if not rows:
        print("ERROR: no rows extracted - refusing to overwrite data.json.", file=sys.stderr)
        return 1

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "locations": [loc for _, loc, _ in SOURCES],
        "rows": rows,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    by_loc = Counter(r["location"] for r in rows)
    short = sum(1 for r in rows if r["available"] <= 0)
    print(f"Wrote {len(rows)} rows to {OUT_PATH} ({short} short)")
    for loc, n in by_loc.items():
        print(f"  {loc}: {n} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
