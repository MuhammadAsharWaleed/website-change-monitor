#!/usr/bin/env python3
"""
Website Change Monitor
-----------------------
Watches web pages for changes and keeps a full history of every check
in a SQLite database - not just the latest snapshot. That history is
what makes the reporting side useful: pandas pulls it into a
DataFrame, numpy does the stats, matplotlib turns it into charts you
can actually show someone (how often a page changes, how its content
size trends over time, how slow it is to fetch).

Quick tour:
    python monitor.py add --url https://example.com --name "Example"
    python monitor.py check --force
    python monitor.py report --days 30
    python monitor.py stats

Data lives in data/monitor.db. Charts get saved into reports/.
"""

import argparse
import difflib
import hashlib
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless, no display needed
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "monitor.db"
CONFIG_PATH = BASE_DIR / "config.yaml"
REPORTS_DIR = BASE_DIR / "reports"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def get_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            selector TEXT,
            interval_minutes INTEGER DEFAULT 30,
            last_checked TEXT,
            last_changed TEXT,
            last_hash TEXT,
            last_content TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            content_length INTEGER,
            changed INTEGER NOT NULL DEFAULT 0,
            response_time_ms REAL,
            FOREIGN KEY (site_id) REFERENCES sites(id)
        )
        """
    )
    conn.commit()
    return conn


def load_checks_df(conn, days=30, url=None):
    """Pull check history into a DataFrame, joined against site info.

    This is the table pandas actually gets to work with - every check
    that's ever run, not just the latest state. Filtering by date and
    by site happens in SQL so we're not hauling the whole history into
    memory for a report that only cares about the last month.
    """
    query = """
        SELECT c.checked_at, c.content_length, c.changed, c.response_time_ms,
               s.name, s.url
        FROM checks c
        JOIN sites s ON s.id = c.site_id
        WHERE c.checked_at >= ?
    """
    params = [(datetime.now() - timedelta(days=days)).isoformat()]

    if url:
        query += " AND s.url = ?"
        params.append(url)

    query += " ORDER BY c.checked_at"
    df = pd.read_sql_query(query, conn, params=params, parse_dates=["checked_at"])
    return df


# ---------------------------------------------------------------------------
# fetching + comparing
# ---------------------------------------------------------------------------

def fetch_content(url, selector=None, timeout=15):
    """Grab the page and pull out the text that's actually worth comparing.

    Stripping scripts/styles and optionally narrowing to a CSS selector
    cuts down on false positives from ads, timestamps, and other noise
    that changes on every request regardless of the content you're
    actually watching. Returns the text plus how long the request took,
    since that timing is worth tracking too.
    """
    start = time.perf_counter()
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    elapsed_ms = (time.perf_counter() - start) * 1000
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    if selector:
        found = soup.select(selector)
        if not found:
            raise ValueError(f"Selector '{selector}' matched nothing on {url}")
        text = "\n".join(el.get_text(separator="\n", strip=True) for el in found)
    else:
        text = soup.get_text(separator="\n", strip=True)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines), elapsed_ms


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_diff(old_text, new_text, context=2):
    diff = difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(), lineterm="", n=context,
        fromfile="before", tofile="after",
    )
    return "\n".join(diff)


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------

def load_config():
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def send_email_alert(site_name, url, diff_text, config):
    email_cfg = (config.get("notifications") or {}).get("email") or {}
    if not email_cfg.get("enabled"):
        return

    recipients = email_cfg.get("recipients") or []
    if not recipients:
        print("  (email is enabled but no recipients are set, skipping)")
        return

    body = (
        f"'{site_name}' changed.\n\n"
        f"URL: {url}\n"
        f"Checked: {datetime.now().isoformat(timespec='seconds')}\n\n"
        f"--- what changed ---\n{diff_text[:4000]}"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"[Website Monitor] Change detected: {site_name}"
    msg["From"] = email_cfg["sender"]
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(email_cfg["smtp_server"], email_cfg.get("smtp_port", 587)) as server:
            server.starttls()
            server.login(email_cfg["sender"], email_cfg["password"])
            server.sendmail(email_cfg["sender"], recipients, msg.as_string())
        print(f"  email alert sent to {', '.join(recipients)}")
    except Exception as exc:
        print(f"  could not send email alert: {exc}")


# ---------------------------------------------------------------------------
# core actions
# ---------------------------------------------------------------------------

def add_site(args):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO sites (name, url, selector, interval_minutes) VALUES (?, ?, ?, ?)",
            (args.name or args.url, args.url, args.selector, args.interval),
        )
        conn.commit()
        print(f"Added '{args.name or args.url}' ({args.url})")
    except sqlite3.IntegrityError:
        print(f"That URL is already being tracked: {args.url}")
    conn.close()


def remove_site(args):
    conn = get_db()
    cur = conn.execute("DELETE FROM sites WHERE url = ?", (args.url,))
    conn.commit()
    conn.close()
    print(f"Removed {args.url}" if cur.rowcount else f"No tracked site matches {args.url}")


def list_sites(args):
    conn = get_db()
    df = pd.read_sql_query(
        "SELECT name, url, interval_minutes, last_checked, last_changed FROM sites", conn
    )
    conn.close()

    if df.empty:
        print("Nothing tracked yet. Add a site with `python monitor.py add --url ...`")
        return

    df["last_checked"] = df["last_checked"].fillna("never")
    df["last_changed"] = df["last_changed"].fillna("never")
    print(df.to_string(index=False))


def check_one(conn, row, config, force=False):
    site_id, name, url, selector, interval, last_checked, last_changed, last_hash, last_content = row

    if not force and last_checked:
        elapsed_min = (datetime.now() - datetime.fromisoformat(last_checked)).total_seconds() / 60
        if elapsed_min < interval:
            return

    print(f"checking {name} ({url})...")
    try:
        new_content, response_ms = fetch_content(url, selector)
    except Exception as exc:
        print(f"  failed to fetch: {exc}")
        return

    new_hash = content_hash(new_content)
    now = datetime.now().isoformat(timespec="seconds")
    changed = 0

    if last_hash is None:
        conn.execute(
            "UPDATE sites SET last_checked=?, last_hash=?, last_content=? WHERE id=?",
            (now, new_hash, new_content, site_id),
        )
        print("  first check, saved a baseline snapshot")
    elif new_hash != last_hash:
        changed = 1
        diff_text = make_diff(last_content or "", new_content)
        print(f"  CHANGE DETECTED at {now}")
        print(diff_text[:1500])
        conn.execute(
            "UPDATE sites SET last_checked=?, last_changed=?, last_hash=?, last_content=? WHERE id=?",
            (now, now, new_hash, new_content, site_id),
        )
        send_email_alert(name, url, diff_text, config)
    else:
        conn.execute("UPDATE sites SET last_checked=? WHERE id=?", (now, site_id))
        print("  no change")

    conn.execute(
        "INSERT INTO checks (site_id, checked_at, content_length, changed, response_time_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (site_id, now, len(new_content), changed, response_ms),
    )
    conn.commit()


def check_all(args):
    conn = get_db()
    config = load_config()
    rows = conn.execute(
        "SELECT id, name, url, selector, interval_minutes, last_checked, last_changed, last_hash, last_content FROM sites"
    ).fetchall()

    if not rows:
        print("Nothing tracked yet. Add a site with `python monitor.py add --url ...`")
        conn.close()
        return

    for row in rows:
        check_one(conn, row, config, force=args.force)
    conn.close()


def watch(args):
    print(f"Watching all tracked sites, checking every {args.interval} minute(s). Ctrl+C to stop.")
    try:
        while True:
            check_all(argparse.Namespace(force=False))
            time.sleep(args.interval * 60)
    except KeyboardInterrupt:
        print("\nStopped.")


def show_diff(args):
    conn = get_db()
    row = conn.execute("SELECT last_content FROM sites WHERE url = ?", (args.url,)).fetchone()
    conn.close()
    if not row or not row[0]:
        print("No saved snapshot for that URL yet.")
        return
    print(row[0])


# ---------------------------------------------------------------------------
# reporting: pandas + numpy + matplotlib
# ---------------------------------------------------------------------------

def generate_report(args):
    conn = get_db()
    df = load_checks_df(conn, days=args.days, url=args.url)
    conn.close()

    if df.empty:
        print("No check history in that window yet. Run `check` a few times first.")
        return

    REPORTS_DIR.mkdir(exist_ok=True)
    label = f"last {args.days} days" if not args.url else f"{args.url}, last {args.days} days"

    # --- per-site summary ---
    summary = df.groupby("name").agg(
        checks=("changed", "count"),
        changes=("changed", "sum"),
        avg_response_ms=("response_time_ms", "mean"),
        avg_content_length=("content_length", "mean"),
    )
    summary["change_rate_pct"] = (summary["changes"] / summary["checks"] * 100).round(1)

    print(f"\nMonitoring summary ({label}):")
    for site_name, row in summary.iterrows():
        print(
            f"  {site_name:<25} {int(row['checks']):>4} checks, "
            f"{int(row['changes']):>3} change(s) ({row['change_rate_pct']}%), "
            f"avg response {row['avg_response_ms']:.0f} ms"
        )

    # --- bar chart: changes detected per site ---
    fig, ax = plt.subplots(figsize=(8, 5))
    summary["changes"].sort_values(ascending=False).plot(kind="bar", ax=ax, color="#C44E52")
    ax.set_title(f"Changes Detected per Site ({label})")
    ax.set_ylabel("Number of changes")
    ax.set_xlabel("")
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    safe_label = args.url.replace("/", "_").replace(":", "") if args.url else "all_sites"
    changes_path = REPORTS_DIR / f"changes_per_site_{safe_label}.png"
    plt.savefig(changes_path, dpi=150)
    plt.close(fig)

    # --- line chart: content length over time ---
    fig, ax = plt.subplots(figsize=(8, 5))
    for site_name, group in df.groupby("name"):
        ax.plot(group["checked_at"], group["content_length"], marker="o", markersize=3, label=site_name)
    ax.set_title(f"Page Content Size Over Time ({label})")
    ax.set_ylabel("Content length (characters)")
    ax.legend(fontsize=8)
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    size_path = REPORTS_DIR / f"content_size_over_time_{safe_label}.png"
    plt.savefig(size_path, dpi=150)
    plt.close(fig)

    # --- bar chart: avg response time with std-dev error bars (numpy) ---
    response_stats = df.groupby("name")["response_time_ms"].agg(["mean", lambda s: np.std(s.to_numpy())])
    response_stats.columns = ["mean", "std"]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(response_stats.index, response_stats["mean"], yerr=response_stats["std"], capsize=5, color="#55A868")
    ax.set_title(f"Average Response Time ({label})")
    ax.set_ylabel("Response time (ms)")
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    response_path = REPORTS_DIR / f"response_times_{safe_label}.png"
    plt.savefig(response_path, dpi=150)
    plt.close(fig)

    print(f"\nSaved charts:\n  {changes_path}\n  {size_path}\n  {response_path}")


def show_stats(args):
    conn = get_db()
    df = load_checks_df(conn, days=args.days, url=args.url)
    conn.close()

    if df.empty:
        print("No check history in that window yet. Run `check` a few times first.")
        return

    response_times = df["response_time_ms"].dropna().to_numpy()
    content_lengths = df["content_length"].dropna().to_numpy()

    print(f"Total checks:        {len(df)}")
    print(f"Total changes:       {int(df['changed'].sum())}")
    print(f"Overall change rate: {df['changed'].mean() * 100:.1f}%")

    if len(response_times) > 0:
        print(f"\nResponse time (ms):")
        print(f"  mean:   {np.mean(response_times):.0f}")
        print(f"  median: {np.median(response_times):.0f}")
        print(f"  std:    {np.std(response_times):.0f}")
        print(f"  min:    {np.min(response_times):.0f}")
        print(f"  max:    {np.max(response_times):.0f}")

    if len(content_lengths) > 1:
        volatility = np.std(content_lengths) / np.mean(content_lengths) * 100
        print(f"\nContent size volatility: {volatility:.1f}% (std dev relative to mean)")

    most_volatile = df.groupby("name")["changed"].mean().sort_values(ascending=False)
    if not most_volatile.empty and most_volatile.iloc[0] > 0:
        print(f"\nMost frequently changing site: {most_volatile.index[0]} "
              f"({most_volatile.iloc[0] * 100:.1f}% of checks resulted in a change)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Track web pages, get notified when they change, and analyze the history.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="start tracking a URL")
    p_add.add_argument("--url", required=True)
    p_add.add_argument("--name", help="friendly name (defaults to the URL)")
    p_add.add_argument("--selector", help="CSS selector to narrow down what's checked, e.g. '#price'")
    p_add.add_argument("--interval", type=int, default=30, help="minutes between checks (default 30)")
    p_add.set_defaults(func=add_site)

    p_remove = sub.add_parser("remove", help="stop tracking a URL")
    p_remove.add_argument("--url", required=True)
    p_remove.set_defaults(func=remove_site)

    p_list = sub.add_parser("list", help="show everything being tracked")
    p_list.set_defaults(func=list_sites)

    p_check = sub.add_parser("check", help="run one check pass over all due sites")
    p_check.add_argument("--force", action="store_true", help="check every site regardless of its interval")
    p_check.set_defaults(func=check_all)

    p_watch = sub.add_parser("watch", help="run continuously in the foreground")
    p_watch.add_argument("--interval", type=int, default=5, help="how often to run a check pass, in minutes")
    p_watch.set_defaults(func=watch)

    p_show = sub.add_parser("show", help="print the last saved snapshot for a URL")
    p_show.add_argument("--url", required=True)
    p_show.set_defaults(func=show_diff)

    p_report = sub.add_parser("report", help="analyze check history and save charts (pandas/numpy/matplotlib)")
    p_report.add_argument("--days", type=int, default=30, help="how many days of history to include (default 30)")
    p_report.add_argument("--url", help="limit the report to one site")
    p_report.set_defaults(func=generate_report)

    p_stats = sub.add_parser("stats", help="quick numpy-powered stats on check history")
    p_stats.add_argument("--days", type=int, default=30)
    p_stats.add_argument("--url", help="limit to one site")
    p_stats.set_defaults(func=show_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
