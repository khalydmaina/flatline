# flatline

Catch bots that run green but do nothing.

A scheduled bot can exit 0 on every run while producing zero output. The
Actions history stays solid green, so nothing tells you it died. I lost more
than two weeks of output from two trading bots this way: one had a score
threshold that nothing could reach any more, the other had talked itself into
answering "no trade" forever. Both "ran fine" the whole time.

`flatline` ignores the run status and looks at what the bot actually recorded.
It counts events (rows in a CSV, JSONL or SQLite file) inside a time window,
fails the run when there are too few, and can message you on Telegram.

It is one Python file with no dependencies, plus a GitHub Action wrapper.

## What an alert looks like

```
FLATLINE: memebot buys
0 event(s) in the last 48h, need at least 1
Last event: 2026-09-28 00:21 UTC (5d 16h ago)
Rows in window: 47 total, 0 matching action=buy
action in window: skip x47
Rows are still being written, so the bot is running; it is just not producing matching events.
Usual gap between events: median 3h 0m, longest 22h 0m
Per day, last 14 days (oldest first): 7 3 3 4 5 8 3 2 1 0 0 0 0 0
```

The alert carries the numbers that point at the cause. Here the bot is alive
and logging 47 rows, all of them skips, and the daily counts show the day it
went quiet. If there were no rows at all, it would say the bot looks stopped.

## Use it as a GitHub Action

Add a workflow to the repo that holds your bot's data file:

```yaml
name: flatline
on:
  schedule:
    - cron: "0 8 * * *"
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: khalydmaina/flatline@v1
        with:
          source: data/signals.csv
          where: action=buy
          window: 48h
          name: memebot buys
          telegram-token: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          telegram-chat: ${{ secrets.TELEGRAM_CHAT_ID }}
```

The step fails when the bot has flatlined, so you also get GitHub's normal
failed-workflow email. The Telegram inputs are optional.

The check is stateless: it alerts on every run while the bot is still quiet.
Schedule it as often as you want to be reminded. Once a day is a good start.

## Use it from the command line

```bash
python3 flatline.py signals.csv --where action=buy --window 48h
python3 flatline.py bot.db --table decisions --time-field ts --where action=buy
python3 flatline.py events.jsonl --time-field created_at --window 7d --min 5
```

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment to get the
Telegram message. Exit codes: `0` healthy, `1` flatline, `2` the check could
not run (missing file, missing column, unreadable times). A broken check also
alerts, because a check that cannot run is as silent as the bot it watches.

To see it fire on sample data:

```bash
python3 examples/make_demo.py
python3 flatline.py examples/demo.csv --where action=buy --window 48h
```

## Options

| Action input | CLI flag | Default | Meaning |
|---|---|---|---|
| `source` | first argument | required | CSV, JSONL or SQLite file the bot writes to |
| `time-field` | `--time-field` | `timestamp` | Column holding the event time |
| `where` | `--where` | none | Only count rows matching `field=value` or `field!=value` |
| `window` | `--window` | `24h` | How far back to look: `30m`, `48h`, `7d` |
| `min` | `--min` | `1` | Fewest events that still counts as healthy |
| `name` | `--name` | file name | Label shown in the output |
| `format` | `--format` | from extension | `csv`, `jsonl` or `sqlite` |
| `table` | `--table` | the only table | SQLite table to read |
| `query` | `--query` | none | SQLite query to read rows from, instead of a table |
| `history` | `--history` | `14` | Days of per-day counts to show |
| `telegram-token` | env `TELEGRAM_BOT_TOKEN` | none | Telegram bot token |
| `telegram-chat` | env `TELEGRAM_CHAT_ID` | none | Telegram chat ID |

Times can be ISO 8601 (`2026-08-08T10:00:00Z`, `2026-08-08 10:00:00`) or unix
timestamps in seconds or milliseconds. Times without a zone are read as UTC.

Several filters are combined with "and". In the Action, put one per line:

```yaml
          where: |
            action=buy
            mode!=paper
```

## Picking what to watch and for how long

Two things I learned the hard way:

1. **Watch the last step the bot still controls, not the final result.** If a
   bot only trades 28 times a year, an alarm on "no trade this week" fires all
   the time and you mute it. Alarm on something frequent that stops when the
   bot is broken, like "the model proposed any direction at all".
2. **Set the window from the data, not from a guess.** Run the check once and
   read the `Usual gap between events` line. A window of about twice the
   longest normal gap catches real silence without crying wolf.

## Limits

- The data file has to be reachable from the workflow. If your bot commits its
  data to another repo or branch, point `actions/checkout` at that with its
  `repository` or `ref` options.
- GitHub turns off scheduled workflows in public repos after 60 days without
  repo activity, so the checker can go quiet too. If the repo is otherwise
  idle, re-enable the workflow from the Actions tab when that happens.
- The Action runs `python3`, which GitHub's Linux and macOS runners have.
- Telegram is the only built-in alert channel.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT
