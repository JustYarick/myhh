import random
import re
from datetime import datetime, timedelta, timezone
from .. import database as db
import logging

logger = logging.getLogger(__name__)

async def calculate_next_wait_seconds(interval_key: str, default_interval: str, jitter_key: str, default_jitter: str, min_interval_mins: int = 0) -> int:
    """Computes total wait time in seconds based on DB settings for interval and jitter."""
    interval_mins = int(await db.get_setting(interval_key, default_interval))
    if min_interval_mins > 0:
        interval_mins = max(interval_mins, min_interval_mins)
        
    jitter = int(await db.get_setting(jitter_key, default_jitter))
    jitter_secs = random.randint(0, jitter * 60) if jitter > 0 else 0
    return (interval_mins * 60) + jitter_secs

async def is_within_prime_time(prime_time_key: str, default_prime_time: str, tz_key: str = "monitoring_timezone_offset") -> bool:
    """Checks if current time is within the specified prime time window."""
    prime_time = await db.get_setting(prime_time_key, default_prime_time)
    if prime_time == "24/7":
        return True
        
    tz_offset = int(await db.get_setting(tz_key, "3"))
    user_time = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
    current_hour = user_time.hour

    match = re.search(r"(\d{2}):\d{2}\s*-\s*(\d{2}):\d{2}", prime_time)
    if match:
        start_h, end_h = int(match.group(1)), int(match.group(2))
        inside = (
            start_h <= current_hour < end_h
            if start_h <= end_h
            else current_hour >= start_h or current_hour < end_h
        )
        if not inside:
            logger.info(
                f"Hour {current_hour:02d}:00 outside prime time ({prime_time})"
            )
            return False
    return True

async def get_resumed_sleep_time(db_key: str) -> float | None:
    """Returns the amount of seconds left to sleep based on the next_run DB setting, or None if expired/missing."""
    next_run_str = await db.get_setting(db_key, "")
    if next_run_str:
        try:
            next_run_dt = datetime.fromisoformat(next_run_str)
            now = datetime.now(timezone.utc)
            if next_run_dt > now:
                return (next_run_dt - now).total_seconds()
        except Exception as e:
            logger.warning(f"Failed to parse {db_key}: {e}")
    return None
