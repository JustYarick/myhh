import datetime

async def format_next_run_text(db_key: str, is_enabled: bool) -> str:
    """Formats the time remaining until the next run based on the DB timestamp."""
    if not is_enabled:
        return ""
        
    from .. import database as db
    next_run_str = await db.get_setting(db_key, "")
    if not next_run_str:
        return ""
        
    try:
        next_run_dt = datetime.datetime.fromisoformat(next_run_str)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = next_run_dt - now
        if diff.total_seconds() > 0:
            mins, secs = divmod(int(diff.total_seconds()), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                return f"Следующий запуск через: <b>{hours}ч {mins}м {secs}с</b>\n"
            return f"Следующий запуск через: <b>{mins}м {secs}с</b>\n"
        else:
            return f"Следующий запуск: <b>сейчас</b>\n"
    except Exception:
        return ""
