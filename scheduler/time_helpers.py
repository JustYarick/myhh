import random
import re
from datetime import datetime, timedelta, timezone
from .. import database as db
import logging

logger = logging.getLogger(__name__)

async def calculate_next_wait_seconds(
    interval_key: str, 
    default_interval: str, 
    jitter_key: str, 
    default_jitter: str, 
    min_interval_mins: int = 0,
    prime_time_key: str = "",
    default_prime_time: str = "24/7"
) -> int:
    """Computes total wait time in seconds based on DB settings for interval, jitter, and prime time."""
    interval_mins = int(await db.get_setting(interval_key, default_interval))
    if min_interval_mins > 0:
        interval_mins = max(interval_mins, min_interval_mins)
        
    jitter = int(await db.get_setting(jitter_key, default_jitter))
    jitter_secs = random.randint(0, jitter * 60) if jitter > 0 else 0
    total_wait = (interval_mins * 60) + jitter_secs
    
    if not prime_time_key:
        return total_wait
        
    prime_time = await db.get_setting(prime_time_key, default_prime_time)
    if prime_time == "24/7":
        return total_wait
        
    tz_offset = int(await db.get_setting("monitoring_timezone_offset", "3"))
    now_utc = datetime.now(timezone.utc)
    standard_next_run_utc = now_utc + timedelta(seconds=total_wait)
    
    match = re.search(r"(\d{2}):\d{2}\s*-\s*(\d{2}):\d{2}", prime_time)
    if not match:
        return total_wait
        
    start_h, end_h = int(match.group(1)), int(match.group(2))
    user_next = standard_next_run_utc + timedelta(hours=tz_offset)
    
    def is_hour_inside(h: int) -> bool:
        if start_h <= end_h:
            return start_h <= h < end_h
        else:
            return h >= start_h or h < end_h
            
    if not is_hour_inside(user_next.hour):
        user_now = now_utc + timedelta(hours=tz_offset)
        candidate = user_now.replace(hour=start_h, minute=0, second=0, microsecond=0)
        if candidate <= user_now:
            candidate += timedelta(days=1)
        actual_next_run_utc = candidate - timedelta(hours=tz_offset)
        
        return int((actual_next_run_utc - now_utc).total_seconds())
        
    return total_wait

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
