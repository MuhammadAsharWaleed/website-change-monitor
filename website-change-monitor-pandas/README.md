# Website Change Monitor

Watches web pages for changes and keeps a full history of every check, not
just the last snapshot. Most "change detector" scripts just tell you
something changed. This one logs every check to SQLite, so you can actually
pull a report out of it later - how often a page changes, whether its
content size is growing or shrinking, whether it's gotten slower to load.
SQLite handles the storage, pandas does the querying, numpy does the stats,
matplotlib draws the charts.

Started out as a basic fetch/hash/diff script. Added the pandas/numpy/
matplotlib reporting side later, mainly because "the page changed" isn't
usually the whole answer a client wants - they usually want to see the
pattern over time.

## What it does

- Fetches a page, strips out scripts/styles, and optionally narrows down
  to one part of it with a CSS selector (so ad rotations or timestamps
  don't trigger false positives)
- Diffs new content against the last snapshot, shows what changed
- Logs every check to SQLite - timestamp, content length, changed or not,
  response time
- `report` loads that history into pandas and saves 3 charts: changes per
  site, content size over time, and avg response time with std-dev error
  bars (numpy)
- `stats` prints quick numpy numbers - mean/median/std response time,
  overall change rate, content-size volatility, most-changing site
- Optional email alerts on change

## What it won't do

Fetches raw HTML with `requests`, so no JS execution. If a site renders
content client-side you'll mostly get an empty shell back. Swapping the
fetch function for Playwright would fix this without touching storage or
reporting - haven't needed to yet, but it's a straightforward change if a
target site needs it.

## Setup

```bash
git clone <your repo url>
cd website-change-monitor
pip install -r requirements.txt
```

For email alerts, copy the example config and fill in your SMTP details:

```bash
cp config.example.yaml config.yaml
```

`config.yaml` is git-ignored so your credentials won't end up in the repo.

## Usage

Track a page:

```bash
python monitor.py add --url https://example.com/careers --name "Careers Page"
```

Narrow it to one part of the page:

```bash
python monitor.py add --url https://example.com/pricing --name "Pricing" --selector "#pricing-table"
```

Run a check pass:

```bash
python monitor.py check --force
```

See what's tracked:

```bash
python monitor.py list
```

Run continuously:

```bash
python monitor.py watch --interval 5
```

### Analysis (pandas/numpy/matplotlib)

Generate a report from the last 30 days of check history:

```bash
python monitor.py report --days 30
```

Prints a per-site summary and saves three PNGs into `reports/`: changes
detected per site, content size over time, and average response times with
error bars.

Filter to one site:

```bash
python monitor.py report --days 30 --url https://example.com/careers
```

Just the numbers, no charts:

```bash
python monitor.py stats --days 30
```

## Sample output

```
Monitoring summary (last 30 days):
  Job Board                   31 checks,   6 change(s) (19.4%), avg response 172 ms
  Competitor Pricing          29 checks,   1 change(s) (3.4%), avg response 335 ms

Saved charts:
  reports/changes_per_site_all_sites.png
  reports/content_size_over_time_all_sites.png
  reports/response_times_all_sites.png
```

## Running it in the background

Cron, every 15 minutes:

```
*/15 * * * * cd /path/to/website-change-monitor && /usr/bin/python3 monitor.py check >> monitor.log 2>&1
```

Or set it up as a systemd service running `watch` - check the comments in
`monitor.py` for the fields it expects.

## Ideas for later

- Playwright instead of requests/BeautifulSoup, for JS-heavy sites
- Rolling z-score on content length to flag "unusually large" changes
  instead of treating every diff the same
- Slack/Discord webhook alerts
- Streamlit front-end over the same SQLite db (the pandas queries here
  would mostly just drop in)

## License

MIT - use it, extend it, build on it.
