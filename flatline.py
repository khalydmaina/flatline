#!/usr/bin/env python3
"""flatline: fail loudly when a bot stops producing output.

A scheduled bot can exit 0 on every run while doing nothing at all. This
counts the events it actually recorded (rows in a CSV, JSONL or SQLite file)
inside a time window, and exits non-zero when there are too few.

Exit codes: 0 healthy, 1 flatline, 2 the check itself could not run.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

EXIT_OK, EXIT_FLATLINE, EXIT_ERROR = 0, 1, 2

FORMATS = {
    ".csv": "csv",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".db": "sqlite",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
}


class CheckError(Exception):
    """The check could not run: bad config, missing file, unreadable data."""


class Filter(NamedTuple):
    field: str
    negate: bool
    value: str

    def matches(self, row: dict) -> bool:
        return (cell_text(row.get(self.field)) == self.value) != self.negate

    def __str__(self) -> str:
        return f"{self.field}{'!=' if self.negate else '='}{self.value}"


@dataclass
class Report:
    matched: int = 0
    window_rows: int = 0
    unreadable: int = 0
    last_event: datetime | None = None
    daily: list[int] = field(default_factory=list)
    breakdown: dict[str, Counter] = field(default_factory=dict)
    median_gap: timedelta | None = None
    longest_gap: timedelta | None = None


def env(name: str) -> str | None:
    # Unset action inputs arrive as empty strings, so treat those as missing.
    return os.environ.get(name) or None


def cell_text(value) -> str:
    return "" if value is None else str(value)


def parse_window(text: str) -> timedelta:
    text = text.strip().lower()
    unit = {"m": "minutes", "h": "hours", "d": "days"}.get(text[-1:])
    try:
        amount = float(text[:-1])
    except ValueError:
        amount = 0
    if not unit or not math.isfinite(amount) or amount <= 0:
        raise CheckError(f"window must look like 30m, 48h or 7d, got {text!r}")
    return timedelta(**{unit: amount})


def parse_filter(text: str) -> Filter:
    name, sep, value = text.partition("=")
    negate = name.endswith("!")
    name = name.rstrip("!").strip()
    if not sep or not name:
        raise CheckError(f"filter must look like field=value or field!=value, got {text!r}")
    return Filter(name, negate, value.strip())


def parse_time(value) -> datetime | None:
    """Read ISO 8601 text or a unix timestamp (seconds or milliseconds) as UTC."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = None
    try:
        if number is not None:
            if not math.isfinite(number):
                return None
            if abs(number) > 1e11:
                number /= 1000
            return datetime.fromtimestamp(number, timezone.utc)
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        when = datetime.fromisoformat(text)
    except (ValueError, OverflowError, OSError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def format_duration(span: timedelta) -> str:
    minutes = max(0, int(span.total_seconds() // 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise CheckError(f"{path} line {number} is not valid JSON") from None
            if not isinstance(row, dict):
                raise CheckError(f"{path} line {number} is not a JSON object")
            rows.append(row)
    return rows


def load_sqlite(path: Path, table: str | None, query: str | None) -> list[dict]:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not query:
            tables = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master"
                    " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            if not table:
                if len(tables) != 1:
                    raise CheckError(
                        f"{path} has {len(tables)} tables ({', '.join(tables)});"
                        " pick one with --table or pass --query"
                    )
                table = tables[0]
            elif table not in tables:
                raise CheckError(f"no table {table!r} in {path}; found: {', '.join(tables)}")
            query = 'SELECT * FROM "{}"'.format(table.replace('"', '""'))
        return [dict(row) for row in db.execute(query)]
    except sqlite3.Error as error:
        raise CheckError(f"sqlite error in {path}: {error}") from None
    finally:
        db.close()


def load_rows(path: Path, fmt: str | None, table: str | None, query: str | None) -> list[dict]:
    if not path.is_file():
        raise CheckError(f"source file not found: {path}")
    fmt = fmt or FORMATS.get(path.suffix.lower())
    try:
        if fmt == "csv":
            return load_csv(path)
        if fmt == "jsonl":
            return load_jsonl(path)
        if fmt == "sqlite":
            return load_sqlite(path, table, query)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise CheckError(f"could not read {path}: {error}") from None
    raise CheckError(f"cannot tell the format of {path}; pass --format csv, jsonl or sqlite")


def analyse(
    rows: list[dict],
    time_field: str,
    filters: list[Filter],
    window: timedelta,
    now: datetime,
    history_days: int,
) -> Report:
    columns = set()
    for row in rows:
        columns.update(row)
    for name in [time_field] + [f.field for f in filters]:
        if rows and name not in columns:
            raise CheckError(f"no column {name!r} in source; found: {', '.join(sorted(columns))}")

    report = Report(breakdown={f.field: Counter() for f in filters})
    cutoff = now - window
    events = []
    bad_example = None
    for row in rows:
        when = parse_time(row.get(time_field))
        if when is None:
            report.unreadable += 1
            bad_example = row.get(time_field)
            continue
        if when >= cutoff:
            report.window_rows += 1
            for name, counter in report.breakdown.items():
                counter[cell_text(row.get(name))] += 1
        if all(f.matches(row) for f in filters):
            events.append(when)

    if rows and report.unreadable == len(rows):
        raise CheckError(
            f"none of the {len(rows)} rows has a readable time in {time_field!r}"
            f" (example value: {bad_example!r})"
        )

    events.sort()
    report.matched = sum(1 for when in events if when >= cutoff)
    if events:
        report.last_event = events[-1]
    per_day = Counter(when.date() for when in events)
    report.daily = [
        per_day[now.date() - timedelta(days=back)] for back in range(history_days - 1, -1, -1)
    ]
    gaps = sorted(later - earlier for earlier, later in zip(events, events[1:]))
    if len(gaps) >= 2:
        report.median_gap = gaps[len(gaps) // 2]
        report.longest_gap = gaps[-1]
    return report


def render(
    report: Report,
    name: str,
    window_text: str,
    minimum: int,
    filters: list[Filter],
    now: datetime,
) -> str:
    healthy = report.matched >= minimum
    lines = [
        f"{'OK' if healthy else 'FLATLINE'}: {name}",
        f"{report.matched} event(s) in the last {window_text}, need at least {minimum}",
    ]
    if report.last_event:
        ago = format_duration(now - report.last_event)
        lines.append(f"Last event: {report.last_event:%Y-%m-%d %H:%M} UTC ({ago} ago)")
    else:
        lines.append("Last event: none on record")
    if filters:
        wanted = " and ".join(str(f) for f in filters)
        lines.append(
            f"Rows in window: {report.window_rows} total, {report.matched} matching {wanted}"
        )
        for column, counter in report.breakdown.items():
            top = ", ".join(f"{value or '(empty)'} x{count}" for value, count in counter.most_common(5))
            if top:
                lines.append(f"{column} in window: {top}")
        if not healthy and report.window_rows == 0:
            lines.append("No rows at all in the window: the bot looks stopped.")
        elif not healthy:
            lines.append(
                "Rows are still being written, so the bot is running;"
                " it is just not producing matching events."
            )
    if report.longest_gap is not None:
        lines.append(
            f"Usual gap between events: median {format_duration(report.median_gap)},"
            f" longest {format_duration(report.longest_gap)}"
        )
    if report.daily:
        counts = " ".join(str(count) for count in report.daily)
        lines.append(f"Per day, last {len(report.daily)} days (oldest first): {counts}")
    if report.unreadable:
        lines.append(f"Skipped {report.unreadable} row(s) with no readable time")
    return "\n".join(lines)


def report_to_github(text: str, failed: bool) -> None:
    summary = env("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"```\n{text}\n```\n")
    if failed and env("GITHUB_ACTIONS"):
        print("::error title=flatline::" + " | ".join(text.splitlines()[:2]))


def send_telegram(text: str) -> None:
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token and not chat:
        return
    if not (token and chat):
        print("warning: need both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to alert", file=sys.stderr)
        return
    if env("GITHUB_RUN_ID") and env("GITHUB_REPOSITORY"):
        server = env("GITHUB_SERVER_URL") or "https://github.com"
        text += f"\nRun: {server}/{env('GITHUB_REPOSITORY')}/actions/runs/{env('GITHUB_RUN_ID')}"
    body = urllib.parse.urlencode(
        {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}
    ).encode()
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body, timeout=20
        ):
            pass
    except OSError as error:
        # Only the reason is printed: the request URL carries the bot token.
        print(f"warning: Telegram alert failed: {getattr(error, 'reason', error)}", file=sys.stderr)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="flatline",
        description="Fail when a bot has recorded too few events in a time window.",
        epilog="Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to get a message on failure.",
    )
    parser.add_argument(
        "source", nargs="?", default=env("FLATLINE_SOURCE"),
        help="CSV, JSONL or SQLite file the bot writes its events to",
    )
    parser.add_argument(
        "--time-field", default=env("FLATLINE_TIME_FIELD") or "timestamp",
        help="column holding the event time: ISO 8601 or unix seconds/ms (default: timestamp)",
    )
    parser.add_argument(
        "--where", action="append", default=[], metavar="FIELD=VALUE",
        help="only count rows where FIELD=VALUE or FIELD!=VALUE; repeat to combine",
    )
    parser.add_argument(
        "--window", default=env("FLATLINE_WINDOW") or "24h",
        help="how far back to look, like 30m, 48h or 7d (default: 24h)",
    )
    parser.add_argument(
        "--min", dest="minimum", type=int, default=env("FLATLINE_MIN") or 1,
        help="fewest events that still counts as healthy (default: 1)",
    )
    parser.add_argument(
        "--name", default=env("FLATLINE_NAME"),
        help="label for this check in the output (default: the file name)",
    )
    parser.add_argument(
        "--format", default=env("FLATLINE_FORMAT"),
        help="csv, jsonl or sqlite (default: guessed from the file extension)",
    )
    parser.add_argument(
        "--table", default=env("FLATLINE_TABLE"),
        help="SQLite table to read (default: the only table)",
    )
    parser.add_argument(
        "--query", default=env("FLATLINE_QUERY"),
        help="SQLite query to read rows from, instead of a whole table",
    )
    parser.add_argument(
        "--history", type=int, default=env("FLATLINE_HISTORY") or 14,
        help="days of per-day counts to show (default: 14)",
    )
    parser.add_argument("--now", default=env("FLATLINE_NOW"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.where += [line.strip() for line in (env("FLATLINE_WHERE") or "").splitlines() if line.strip()]
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    name = args.name or (Path(args.source).name if args.source else "flatline")
    try:
        if not args.source:
            raise CheckError("no source file given")
        window = parse_window(args.window)
        filters = [parse_filter(text) for text in args.where]
        now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
        if now is None:
            raise CheckError(f"--now is not a readable time: {args.now!r}")
        rows = load_rows(Path(args.source), args.format, args.table, args.query)
        report = analyse(rows, args.time_field, filters, window, now, args.history)
        text = render(report, name, args.window.strip(), args.minimum, filters, now)
        code = EXIT_OK if report.matched >= args.minimum else EXIT_FLATLINE
    except CheckError as error:
        # A check that cannot run is as silent as the bot it watches, so alert on it too.
        text = f"ERROR: {name}\nThe flatline check could not run: {error}"
        code = EXIT_ERROR

    print(text)
    report_to_github(text, failed=code != EXIT_OK)
    if code != EXIT_OK:
        send_telegram(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
