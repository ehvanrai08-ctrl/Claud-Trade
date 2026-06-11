"""
Persistent market-hours scheduler.
Sleeps until 9:30 AM ET, runs market_monitor every 60s until 4:00 PM ET,
then sleeps until next trading day. Runs indefinitely.
"""

import time
import subprocess
import requests
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import dotenv_values

BASE_DIR = "/home/user/Claud-Trade"
config = dotenv_values(f"{BASE_DIR}/.env")
ET = ZoneInfo("America/New_York")

HEADERS = {
    "APCA-API-KEY-ID": config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}

logging.basicConfig(
    filename=f"{BASE_DIR}/monitor.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


def get_clock():
    r = requests.get(
        f"{config['ALPACA_BASE_URL']}/clock",
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.json()


def seconds_until(dt_str):
    """Seconds from now until an ISO datetime string."""
    from datetime import timezone
    target = datetime.fromisoformat(dt_str)
    now = datetime.now(timezone.utc).astimezone(target.tzinfo)
    delta = (target - now).total_seconds()
    return max(delta, 0)


def run_monitor():
    result = subprocess.run(
        ["python3", f"{BASE_DIR}/market_monitor.py"],
        capture_output=True, text=True,
    )
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0 and result.stderr:
        log.error(f"monitor error: {result.stderr.strip()}")


print("=== Claud-Trade scheduler started ===")
log.info("Scheduler started")

while True:
    clock = get_clock()

    if clock["is_open"]:
        # Market is open — run monitor then sleep 60s
        run_monitor()
        time.sleep(60)
    else:
        # Sleep until next open
        next_open = clock["next_open"]
        wait = seconds_until(next_open)
        next_close = clock["next_close"]
        print(f"Market closed. Sleeping {wait/3600:.1f}h until {next_open}")
        log.info(f"Market closed. Next open: {next_open} | Next close: {next_close}")

        # Wake up a minute early to be ready
        time.sleep(max(wait - 60, 30))
