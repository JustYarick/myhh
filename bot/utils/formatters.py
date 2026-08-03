import datetime

def format_next_run_text(daemon_instance, is_enabled: bool) -> str:
    """Formats the time remaining until the next run for a given daemon."""
    if not is_enabled or not getattr(daemon_instance, 'next_run_time', None):
        return ""
        
    now = datetime.datetime.now(datetime.timezone.utc)
    diff = daemon_instance.next_run_time - now
    if diff.total_seconds() > 0:
        mins, secs = divmod(int(diff.total_seconds()), 60)
        return f"Следующий запуск через: <b>{mins}м {secs}с</b>\n"
    else:
        return f"Следующий запуск: <b>сейчас</b>\n"
