#!/usr/bin/env python3
"""
CheckFire Stock Availability - NetSuite web query -> docs/data.json

Pulls the NetSuite webquery (.iqy) endpoints, parses the returned HTML tables,
filters to the target locations, and computes:

    Available = On Hand - Committed

Config comes from environment variables (GitHub Secrets in CI):

    NS_URL_1   full NetSuite webquery URL (cr=2252)
    NS_URL_2   full NetSuite webquery URL (cr=2253)
    NS_EMAIL   the email address the .iqy prompts for (optional if already in the URL)

Run `python fetch_stock.py --debug` to dump the tables and detected columns
without writing data.json - use this the first time to confirm the mapping.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

import requests
import pandas as pd

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data.json")

# Display name -> list of lowercase substrings that identify the location in NetSuite.
# NetSuite often prefixes locations (e.g. "CheckFire : Unit 19"), so we match on
# fragments rather than exact strings. Add aliases here if a site is missed.
LOCATIONS = {
    "Checkfire Unit 19": ["unit 19", "unit19"],
    "Northern Depot": ["northern depot", "northen depot", "northern dep"],
    "PJ Fire Main Warehouse": ["pj fire main", "pjfire main", "main warehouse"],
}

# Header matching. First pattern that hits wins.
FIELD_PATTERNS = {
    "location": [r"^location$", r"location"],
    "item": [r"^item$", r"item.*(name|number|id)", r"^name$", r"^sku$", r"item"],
    "description": [r"description", r"display\s*name", r"^memo$"],
    "on_hand": [r"on\s*hand(?!.*value)", r"quantity\s*on\s*hand", r"^qty\s*on\s*hand"],
    "committed": [r"committed", r"quantity\s*committed", r"allocated"],
}

TIMEOUT = 120


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip()


def with_email(url: str, email: str | None) -> str:
    """NetSuite .iqy URLs carry an Excel prompt placeholder for email:
        email=["emailaddress","Please enter your email address:"]
    Replace it with the real address (or add it if missing)."""
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
    if "login" in resp.url.lower() or "<form" in resp.text[:2000].lower():
        raise RuntimeError(
            "NetSuite returned a login page - check the hash and email in the URL secret."
        )
    return resp.text


def header_score(values) -> int:
    """How many of our known fields does this row look like it names?"""
    cells = [norm(v).lower() for v in values]
    hits = 0
    for patterns in FIELD_PATTERNS.values():
        if any(re.search(patterns[0], c) or re.search(patterns[-1], c) for c in cells):
            hits += 1
    return hits


def promote_header(df: pd.DataFrame) -> pd.DataFrame:
    """NetSuite renders its header row as <td>, not <th>, so pandas labels the
    columns 0..n and leaves the real names in the first data row. Detect that
    and promote the best-looking row to be the header."""
    labels = [norm(c).lower() for c in df.columns]
    looks_unlabelled = sum(
        1 for c in labels if c.isdigit() or c.startswith("unnamed")
    ) >= max(1, len(labels) * 0.6)
    if not looks_unlabelled:
        return df

    best_i, best = 0, -1
    for i in range(min(6, len(df))):
        s = header_score(df.iloc[i])
        if s > best:
            best_i, best = i, s

    df.columns = [norm(v) for v in df.iloc[best_i]]
    out = df.iloc[best_i + 1:].reset_index(drop=True)
    # drop any repeated header rows further down the grid
    return out[out.iloc[:, 0].map(lambda v: norm(v) != df.columns[0])].reset_index(drop=True)


def pick_table(html: str) -> pd.DataFrame:
    """Return the widest/longest table in the response - NetSuite wraps the
    results grid in layout tables, so take the one with the most cells."""
    tables = pd.read_html(io.StringIO(html))
    if not tables:
        raise RuntimeError("No tables found in the NetSuite response.")
    return promote_header(max(tables, key=lambda t: t.shape[0] * t.shape[1]))


def map_columns(df: pd.DataFrame) -> dict[str, str]:
    """Map our canonical field names onto the dataframe's actual column names."""
    cols = {norm(c).lower(): c for c in df.columns}
    mapping: dict[str, str] = {}
    for field, patterns in FIELD_PATTERNS.items():
        for pattern in patterns:
            hit = next((orig for low, orig in cols.items() if re.search(pattern, low)), None)
            if hit and hit not in mapping.values():
                mapping[field] = hit
                break
    return mapping


def to_number(value) -> float:
    if pd.isna(value):
        return 0.0
    text = re.sub(r"[^\d.\-]", "", str(value))
    if text in ("", "-", "."):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def match_location(raw: str) -> str | None:
    low = norm(raw).lower()
    for display, fragments in LOCATIONS.items():
        if any(f in low for f in fragments):
            return display
    return None


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

def extract(html: str, source: str, debug: bool = False) -> dict[tuple[str, str], dict]:
    """Parse one web query response into {(item, location): record}."""
    df = pick_table(html)
    mapping = map_columns(df)

    if debug:
        print(f"\n--- {source} ---")
        print(f"shape: {df.shape}")
        print(f"columns: {list(df.columns)}")
        print(f"detected mapping: {mapping}")
        print(df.head(5).to_string())

    if "item" not in mapping or "location" not in mapping:
        raise RuntimeError(
            f"{source}: could not find item and location columns.\n"
            f"  columns found : {list(df.columns)}\n"
            f"  mapped so far : {mapping}\n"
            f"  first rows:\n{df.head(4).to_string()}\n"
            f"Add a pattern to FIELD_PATTERNS for whichever column is missing."
        )

    records: dict[tuple[str, str], dict] = {}
    for _, row in df.iterrows():
        location = match_location(row[mapping["location"]])
        if not location:
            continue
        item = norm(row[mapping["item"]])
        if not item or item.lower() in ("nan", "total", "-"):
            continue

        key = (item, location)
        rec = records.setdefault(
            key,
            {
                "item": item,
                "description": "",
                "location": location,
                "on_hand": None,
                "committed": None,
            },
        )
        if "description" in mapping:
            desc = norm(row[mapping["description"]])
            if desc and desc.lower() != "nan":
                rec["description"] = desc
        if "on_hand" in mapping:
            rec["on_hand"] = to_number(row[mapping["on_hand"]])
        if "committed" in mapping:
            rec["committed"] = to_number(row[mapping["committed"]])
    return records


def merge(*sources: dict) -> list[dict]:
    """Merge records from both web queries. Handles the case where one search
    carries On Hand and the other carries Committed."""
    merged: dict[tuple[str, str], dict] = {}
    for src in sources:
        for key, rec in src.items():
            target = merged.setdefault(key, dict(rec))
            for field in ("on_hand", "committed"):
                if target.get(field) is None and rec.get(field) is not None:
                    target[field] = rec[field]
            if not target.get("description") and rec.get("description"):
                target["description"] = rec["description"]

    rows = []
    for rec in merged.values():
        on_hand = rec.get("on_hand") or 0.0
        committed = rec.get("committed") or 0.0
        rows.append(
            {
                "item": rec["item"],
                "description": rec["description"],
                "location": rec["location"],
                "onHand": round(on_hand, 2),
                "committed": round(committed, 2),
                "available": round(on_hand - committed, 2),
            }
        )
    rows.sort(key=lambda r: (r["item"].lower(), r["location"]))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="dump tables, do not write output")
    args = parser.parse_args()

    email = os.environ.get("NS_EMAIL")
    urls = [
        ("cr=2252", os.environ.get("NS_URL_1")),
        ("cr=2253", os.environ.get("NS_URL_2")),
    ]
    if not any(u for _, u in urls):
        print("ERROR: set NS_URL_1 (and optionally NS_URL_2).", file=sys.stderr)
        return 1

    sources = []
    for label, url in urls:
        if not url:
            continue
        print(f"Fetching {label} ...")
        sources.append(extract(fetch(url, email), label, debug=args.debug))

    rows = merge(*sources)
    if args.debug:
        print(f"\nmerged rows: {len(rows)}")
        for r in rows[:10]:
            print(r)
        return 0

    if not rows:
        print("ERROR: no rows matched the target locations - refusing to overwrite data.json.",
              file=sys.stderr)
        return 1

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "locations": list(LOCATIONS.keys()),
        "rows": rows,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    print(f"Wrote {len(rows)} rows to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
