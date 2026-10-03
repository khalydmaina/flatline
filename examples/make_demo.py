#!/usr/bin/env python3
"""Write demo.csv: a bot that bought every few hours, then went quiet 5 days ago
while still logging skips every hour. Run flatline against it to see an alert."""

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

random.seed(7)
now = datetime.now(timezone.utc).replace(microsecond=0)
quiet_since = now - timedelta(days=5)

rows = []
when = now - timedelta(days=14)
while when < now:
    buying = when < quiet_since and random.random() < 0.15
    rows.append([when.isoformat(), "buy" if buying else "skip"])
    when += timedelta(hours=1)

path = Path(__file__).with_name("demo.csv")
with open(path, "w", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["timestamp", "action"])
    writer.writerows(rows)
print(f"wrote {len(rows)} rows to {path}")
