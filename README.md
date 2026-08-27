# CheckFire Stock Availability Board

A single-page stock board for production planning. Pulls two NetSuite saved searches
via their web query (`.iqy`) endpoints, filters to three locations, and shows:

```
On Hand − Committed = Available
```

Locations: **Checkfire Unit 19**, **Northern Depot**, **PJ Fire Main Warehouse**.

Same pattern as the `Gecko-` repo — GitHub Actions does the pulling, GitHub Pages
serves the page. Lee just bookmarks a URL. No NetSuite login, no Excel, any laptop.

---

## Setup (about 10 minutes)

**1. Create the repo and push these files.**

```
fetch_stock.py            pulls + parses + writes docs/data.json
requirements.txt
.github/workflows/refresh.yml
docs/index.html           the board
docs/data.json            currently SAMPLE data - overwritten on first real run
```

**2. Add the secrets** — Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `NS_URL_1` | the full URL from `report454.iqy` (cr=2252) |
| `NS_URL_2` | the full URL from `report730.iqy` (cr=2253) |
| `NS_EMAIL` | the email address the `.iqy` prompts for in Excel |

Paste the URLs exactly as they appear on line 3 of each `.iqy`, including the
`hash=` parameter. Leave the `email=["emailaddress",...]` placeholder in — the
script replaces it with `NS_EMAIL` at runtime.

**3. Enable Pages** — Settings → Pages → Source: *Deploy from a branch*, branch
`main`, folder `/docs`. The board lands at
`https://<you>.github.io/<repo>/`.

**4. Run it once** — Actions → *Refresh stock data* → Run workflow. The sample
banner disappears once real data lands.

---

## First run: check the column mapping

The two saved searches almost certainly don't use the exact headers the parser
expects, so confirm the mapping before trusting the numbers:

```bash
pip install -r requirements.txt
export NS_URL_1='...' NS_URL_2='...' NS_EMAIL='...'
python fetch_stock.py --debug
```

That prints each table's shape, its real column headers, what the script mapped
them to, and the first ten merged rows — without writing `data.json`.

Two things to fix if it looks wrong, both near the top of `fetch_stock.py`:

- **`LOCATIONS`** — the fragments used to match location names. NetSuite often
  prefixes them (`CheckFire : Unit 19`), so matching is on substrings. If a site
  is missing from the board, its name fragment needs adding here.
- **`FIELD_PATTERNS`** — regexes that find the Item / Description / Location /
  On Hand / Committed columns. Add a pattern if a column isn't being picked up.

The script handles the case where one search carries On Hand and the other
carries Committed — it merges them on item + location. It also refuses to write
an empty `data.json`, so a bad run leaves yesterday's numbers on screen rather
than blanking the board.

---

## Refresh

Scheduled twice each working day at `30 5,11 * * 1-5` (UTC) — 06:30 and 12:30
during BST, 05:30 and 11:30 in winter, since GitHub cron doesn't do DST.

The **Refresh** button on the page re-pulls `data.json` and updates the
timestamp. To be straight about what it does: it picks up the latest *published*
data, it doesn't force a fresh pull from NetSuite. The page also re-checks
quietly every 5 minutes, so it stays current if left open.

If Lee wants a real on-demand pull, three options:

1. Give him read/write access and he uses Actions → Run workflow (2 clicks).
2. Bump the schedule — there's a commented-out half-hourly cron in
   `refresh.yml`, one line to swap. Actions minutes are free on public repos.
3. Point cron-job.org at the `repository_dispatch` endpoint, same trick that
   fixed the Gecko- scheduler.

---

## Security — worth a decision before you push

The `hash=` in those URLs is a bearer credential. Anyone holding it can pull
that saved search's data without logging in. Keep it in Actions Secrets only —
never commit it, never put it in the HTML, and don't paste the raw `.iqy` into a
public issue.

Separately: **on a free GitHub account, Pages sites are public.** The HTML and
`docs/data.json` would be readable by anyone with the URL — item codes,
descriptions and stock quantities for three sites. The URL isn't advertised
anywhere, but it isn't protected either. Your call whether that's acceptable for
stock levels. If not:

- GitHub Pro/Team lets you keep the repo private (Pages still public on Pro; use
  Team/Enterprise for a genuinely private site), or
- host the same `docs/` folder somewhere behind your SSO instead — the fetch
  script and the page don't care where they're served from.

---

## The board

- Three location cards, each showing lines tracked, **short** (available ≤ 0),
  **tight** (1–10) and clear. Click a card to filter to that site.
- Search box (`/` focuses it, `Esc` clears) and location tabs.
- Every column sorts. Available is colour-coded and boxed when it's a problem.
- Light and dark, remembered per browser. Prints cleanly if he wants it on paper.

The tight threshold is `TIGHT = 10` at the top of the `<script>` in
`docs/index.html` — change it if 10 is the wrong line for your build quantities.
