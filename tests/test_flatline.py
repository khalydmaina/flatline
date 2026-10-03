import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flatline

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def hours_ago(hours):
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FlatlineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("FLATLINE_", "GITHUB_", "TELEGRAM_"))}
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_csv(self, rows, header="timestamp,action"):
        path = self.dir / "signals.csv"
        path.write_text(header + "\n" + "".join(f"{when},{action}\n" for when, action in rows))
        return str(path)

    def run_check(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = flatline.main([*argv, "--now", NOW.isoformat()])
        return code, out.getvalue()

    def test_recent_events_pass(self):
        source = self.write_csv([(hours_ago(30), "buy"), (hours_ago(2), "buy")])
        code, text = self.run_check(source, "--window", "24h")
        self.assertEqual(code, flatline.EXIT_OK)
        self.assertIn("OK: signals.csv", text)
        self.assertIn("1 event(s) in the last 24h", text)
        self.assertIn("(2h 0m ago)", text)

    def test_old_events_flatline(self):
        source = self.write_csv([(hours_ago(400), "buy"), (hours_ago(340), "buy")])
        code, text = self.run_check(source, "--window", "48h", "--name", "memebot")
        self.assertEqual(code, flatline.EXIT_FLATLINE)
        self.assertIn("FLATLINE: memebot", text)
        self.assertIn("(14d 4h ago)", text)

    def test_min_threshold(self):
        source = self.write_csv([(hours_ago(1), "buy"), (hours_ago(2), "buy")])
        self.assertEqual(self.run_check(source, "--min", "2")[0], flatline.EXIT_OK)
        self.assertEqual(self.run_check(source, "--min", "3")[0], flatline.EXIT_FLATLINE)

    def test_running_but_idle_is_diagnosed(self):
        rows = [(hours_ago(100), "buy")] + [(hours_ago(h), "skip") for h in range(1, 20)]
        code, text = self.run_check(self.write_csv(rows), "--where", "action=buy")
        self.assertEqual(code, flatline.EXIT_FLATLINE)
        self.assertIn("Rows in window: 19 total, 0 matching action=buy", text)
        self.assertIn("action in window: skip x19", text)
        self.assertIn("the bot is running", text)

    def test_stopped_bot_is_diagnosed(self):
        source = self.write_csv([(hours_ago(100), "buy"), (hours_ago(90), "skip")])
        code, text = self.run_check(source, "--where", "action=buy")
        self.assertEqual(code, flatline.EXIT_FLATLINE)
        self.assertIn("the bot looks stopped", text)

    def test_negated_filter(self):
        rows = [(hours_ago(3), "NO_TRADE"), (hours_ago(2), "LONG"), (hours_ago(1), "NO_TRADE")]
        code, text = self.run_check(self.write_csv(rows), "--where", "action!=NO_TRADE")
        self.assertEqual(code, flatline.EXIT_OK)
        self.assertIn("3 total, 1 matching action!=NO_TRADE", text)

    def test_daily_counts_and_gaps(self):
        rows = [(hours_ago(h), "buy") for h in (80, 60, 50, 30, 1)]
        code, text = self.run_check(self.write_csv(rows), "--history", "4")
        self.assertIn("Per day, last 4 days (oldest first): 1 2 1 1", text)
        self.assertIn("Usual gap between events: median 20h 0m, longest 1d 5h", text)

    def test_jsonl_with_millisecond_times(self):
        path = self.dir / "events.jsonl"
        recent = int((NOW - timedelta(hours=1)).timestamp() * 1000)
        path.write_text(json.dumps({"ts": recent, "kind": "post"}) + "\n\n")
        code, text = self.run_check(str(path), "--time-field", "ts")
        self.assertEqual(code, flatline.EXIT_OK)
        self.assertIn("(1h 0m ago)", text)

    def test_sqlite_table_and_query(self):
        path = self.dir / "bot.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE decisions (ts INTEGER, action TEXT)")
        stamp = int((NOW - timedelta(hours=5)).timestamp())
        db.executemany("INSERT INTO decisions VALUES (?, ?)", [(stamp, "buy"), (stamp, "skip")])
        db.commit()
        db.close()
        code, text = self.run_check(str(path), "--time-field", "ts", "--where", "action=buy")
        self.assertEqual(code, flatline.EXIT_OK)
        self.assertIn("2 total, 1 matching", text)
        query = "SELECT ts FROM decisions WHERE action = 'sell'"
        code, text = self.run_check(str(path), "--time-field", "ts", "--query", query)
        self.assertEqual(code, flatline.EXIT_FLATLINE)
        self.assertIn("none on record", text)

    def test_broken_checks_are_errors_not_flatlines(self):
        source = self.write_csv([(hours_ago(1), "buy")])
        cases = [
            [str(self.dir / "missing.csv")],
            [source, "--time-field", "created_at"],
            [source, "--where", "side=buy"],
            [source, "--window", "soon"],
            [source, "--format", "xml"],
            [self.write_csv([("yesterday", "buy")])],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, text = self.run_check(*argv)
                self.assertEqual(code, flatline.EXIT_ERROR)
                self.assertIn("could not run", text)

    def test_empty_file_is_a_flatline(self):
        code, text = self.run_check(self.write_csv([]))
        self.assertEqual(code, flatline.EXIT_FLATLINE)
        self.assertIn("none on record", text)

    def test_environment_configures_the_check(self):
        rows = [(hours_ago(1), "skip"), (hours_ago(2), "buy")]
        os.environ.update(
            FLATLINE_SOURCE=self.write_csv(rows),
            FLATLINE_WHERE="action=buy\n\n",
            FLATLINE_WINDOW="90m",
            FLATLINE_TABLE="",
        )
        code, text = self.run_check()
        self.assertEqual(code, flatline.EXIT_FLATLINE)
        self.assertIn("0 event(s) in the last 90m", text)

    def test_telegram_alert_only_on_failure(self):
        os.environ.update(TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CHAT_ID="42")
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.run_check(self.write_csv([(hours_ago(1), "buy")]))
            urlopen.assert_not_called()
            self.run_check(self.write_csv([(hours_ago(99), "buy")]))
            url = urlopen.call_args.args[0]
            body = urlopen.call_args.kwargs["data"].decode()
        self.assertEqual(url, "https://api.telegram.org/bot123:abc/sendMessage")
        self.assertIn("chat_id=42", body)
        self.assertIn("FLATLINE", body)

    def test_telegram_failure_keeps_exit_code_and_hides_token(self):
        os.environ.update(TELEGRAM_BOT_TOKEN="123:secret", TELEGRAM_CHAT_ID="42")
        source = self.write_csv([(hours_ago(99), "buy")])
        err = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("network down")):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = flatline.main([source, "--now", NOW.isoformat()])
        self.assertEqual(code, flatline.EXIT_FLATLINE)
        self.assertIn("network down", err.getvalue())
        self.assertNotIn("secret", err.getvalue())

    def test_github_summary_and_annotation(self):
        summary = self.dir / "summary.md"
        os.environ.update(GITHUB_STEP_SUMMARY=str(summary), GITHUB_ACTIONS="true")
        code, text = self.run_check(self.write_csv([(hours_ago(99), "buy")]))
        self.assertIn("::error title=flatline::FLATLINE: signals.csv | 0 event(s)", text)
        self.assertIn("FLATLINE: signals.csv", summary.read_text())


class ParsingTest(unittest.TestCase):
    def test_parse_time(self):
        expected = datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc)
        for value in (
            "2026-08-08T10:00:00Z",
            "2026-08-08 10:00:00",
            "2026-08-08T12:00:00+02:00",
            1786183200,
            "1786183200",
            1786183200000,
            1786183200.0,
        ):
            with self.subTest(value=value):
                self.assertEqual(flatline.parse_time(value), expected)
        for value in (None, "", "yesterday", True, "nan"):
            with self.subTest(value=value):
                self.assertIsNone(flatline.parse_time(value))

    def test_parse_window(self):
        self.assertEqual(flatline.parse_window("30m"), timedelta(minutes=30))
        self.assertEqual(flatline.parse_window(" 1.5H "), timedelta(minutes=90))
        self.assertEqual(flatline.parse_window("7d"), timedelta(days=7))
        for bad in ("", "h", "0h", "-2d", "10", "2w"):
            with self.subTest(bad=bad), self.assertRaises(flatline.CheckError):
                flatline.parse_window(bad)

    def test_parse_filter(self):
        self.assertEqual(flatline.parse_filter("a=b"), flatline.Filter("a", False, "b"))
        self.assertEqual(flatline.parse_filter("a != b=c"), flatline.Filter("a", True, "b=c"))
        self.assertEqual(flatline.parse_filter("note="), flatline.Filter("note", False, ""))
        for bad in ("action", "=buy", "!=buy"):
            with self.subTest(bad=bad), self.assertRaises(flatline.CheckError):
                flatline.parse_filter(bad)


if __name__ == "__main__":
    unittest.main()
