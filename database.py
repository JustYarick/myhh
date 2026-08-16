import aiosqlite
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from .config import get_settings
import re

logger = logging.getLogger(__name__)

DB_PATH: Optional[Path] = None


def _get_db_path() -> Path:
    global DB_PATH
    if DB_PATH is None:
        DB_PATH = get_settings().db_file
    return DB_PATH


def extract_vacancy_id(url: str) -> str:
    if not url:
        return ""
    match = re.search(r"/vacancy/(\d+)", url)
    if match:
        return match.group(1)
    if url.isdigit():
        return url
    return url


async def init_db() -> None:
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS applied_vacancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vacancy_url TEXT NOT NULL,
                title TEXT NOT NULL,
                employer TEXT NOT NULL,
                description TEXT DEFAULT '',
                cover_letter TEXT DEFAULT '',
                ai_relevance INTEGER DEFAULT 0,
                ai_analysis TEXT DEFAULT '',
                status TEXT NOT NULL,
                error_message TEXT DEFAULT '',
                shown INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_applied INTEGER DEFAULT 0,
                successful INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                analyzed_skip INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS vacancy_cache (
                vacancy_url TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                employer TEXT DEFAULT '',
                description TEXT DEFAULT '',
                ai_relevance INTEGER DEFAULT 0,
                ai_summary TEXT DEFAULT '',
                result TEXT NOT NULL,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Check if description column exists in vacancy_cache, if not, add it
        try:
            async with db.execute("PRAGMA table_info(vacancy_cache)") as cursor:
                columns = [row[1] for row in await cursor.fetchall()]
                if "description" not in columns:
                    await db.execute("ALTER TABLE vacancy_cache ADD COLUMN description TEXT DEFAULT ''")
                    logger.info("Database migration: added description column to vacancy_cache")
        except Exception as e:
            logger.warning(f"Failed to perform database migration: {e}")

        # Check if shown column exists in applied_vacancies, if not, add it
        try:
            async with db.execute("PRAGMA table_info(applied_vacancies)") as cursor:
                columns = [row[1] for row in await cursor.fetchall()]
                if "shown" not in columns:
                    await db.execute("ALTER TABLE applied_vacancies ADD COLUMN shown INTEGER DEFAULT 0")
                    logger.info("Database migration: added shown column to applied_vacancies")
        except Exception as e:
            logger.warning(f"Failed to perform database migration: {e}")

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_processed ON vacancy_cache(processed_at)"
        )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_vacancy_url ON applied_vacancies(vacancy_url)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_vacancy_created ON applied_vacancies(created_at)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS flow_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                config TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migrate vacancy_cache to use numeric IDs instead of URLs
        try:
            async with db.execute("SELECT vacancy_url FROM vacancy_cache") as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                old_url = row[0]
                v_id = extract_vacancy_id(old_url)
                if v_id and v_id != old_url:
                    try:
                        await db.execute(
                            "UPDATE vacancy_cache SET vacancy_url = ? WHERE vacancy_url = ?",
                            (v_id, old_url)
                        )
                    except Exception:
                        await db.execute("DELETE FROM vacancy_cache WHERE vacancy_url = ?", (old_url,))
            
            async with db.execute("SELECT id, vacancy_url FROM applied_vacancies") as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                row_id, old_url = row[0], row[1]
                v_id = extract_vacancy_id(old_url)
                if v_id and v_id != old_url:
                    await db.execute(
                        "UPDATE applied_vacancies SET vacancy_url = ? WHERE id = ?",
                        (v_id, row_id)
                    )
            logger.info("Database migration completed: URL keys migrated to numeric vacancy IDs")
        except Exception as migration_err:
            logger.warning(f"Vacancy ID database migration skipped or failed: {migration_err}")

        await db.commit()
    logger.info(f"Database initialized: {db_path}")


async def was_vacancy_applied(vacancy_url: str) -> bool:
    db_path = _get_db_path()
    v_id = extract_vacancy_id(vacancy_url)
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT 1 FROM applied_vacancies WHERE vacancy_url = ? AND status != 'error' LIMIT 1",
            (v_id,)
        )
        return await cursor.fetchone() is not None


async def is_vacancy_cached(vacancy_url: str, ttl_days: int = 30) -> bool:
    db_path = _get_db_path()
    v_id = extract_vacancy_id(vacancy_url)
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            """SELECT 1 FROM vacancy_cache
               WHERE vacancy_url = ?
               AND result != 'parsed'
               AND processed_at >= datetime('now', ?) LIMIT 1""",
            (v_id, f"-{ttl_days} days"),
        )
        return await cursor.fetchone() is not None


async def get_cached_vacancy_result(vacancy_url: str) -> dict | None:
    db_path = _get_db_path()
    v_id = extract_vacancy_id(vacancy_url)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM vacancy_cache WHERE vacancy_url = ?", (v_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def cache_vacancy_result(
    vacancy_url: str,
    title: str,
    employer: str,
    ai_relevance: int,
    ai_summary: str,
    result: str,
) -> None:
    db_path = _get_db_path()
    v_id = extract_vacancy_id(vacancy_url)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """INSERT INTO vacancy_cache
               (vacancy_url, title, employer, ai_relevance, ai_summary, result, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(vacancy_url) DO UPDATE SET
                   title = excluded.title,
                   employer = excluded.employer,
                   ai_relevance = excluded.ai_relevance,
                   ai_summary = excluded.ai_summary,
                   result = excluded.result,
                   processed_at = CURRENT_TIMESTAMP""",
            (v_id, title, employer, ai_relevance, ai_summary, result),
        )
        await db.commit()


async def get_cached_vacancy_description(vacancy_url: str) -> Optional[str]:
    db_path = _get_db_path()
    v_id = extract_vacancy_id(vacancy_url)
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT description FROM vacancy_cache WHERE vacancy_url = ? LIMIT 1",
            (v_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None


async def save_vacancy_description_to_cache(
    vacancy_url: str,
    title: str,
    employer: str,
    description: str,
) -> None:
    db_path = _get_db_path()
    v_id = extract_vacancy_id(vacancy_url)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """INSERT INTO vacancy_cache (vacancy_url, title, employer, description, result)
               VALUES (?, ?, ?, ?, 'parsed')
               ON CONFLICT(vacancy_url) DO UPDATE SET
                   title = excluded.title,
                   employer = excluded.employer,
                   description = excluded.description""",
            (v_id, title, employer, description),
        )
        await db.commit()


async def clear_vacancy_cache() -> None:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("DELETE FROM vacancy_cache")
        await db.commit()


async def save_application(
    vacancy_url: str,
    title: str,
    employer: str,
    description: str,
    cover_letter: str,
    ai_relevance: int,
    ai_analysis: str,
    status: str,
    error_message: str = "",
) -> None:
    db_path = _get_db_path()
    v_id = extract_vacancy_id(vacancy_url)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """INSERT INTO applied_vacancies
               (vacancy_url, title, employer, description, cover_letter,
                ai_relevance, ai_analysis, status, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (v_id, title, employer, description[:2000], cover_letter,
             ai_relevance, ai_analysis, status, error_message),
        )
        await db.commit()

    await _update_daily_stats(status)


async def _update_daily_stats(status: str) -> None:
    today = date.today().isoformat()
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """INSERT INTO daily_stats (date, total_applied, successful, errors, skipped, analyzed_skip)
               VALUES (?, 1, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                   total_applied = total_applied + 1,
                   successful = successful + excluded.successful,
                   errors = errors + excluded.errors,
                   skipped = skipped + excluded.skipped,
                   analyzed_skip = analyzed_skip + excluded.analyzed_skip""",
            (
                today,
                1 if status == "success" else 0,
                1 if status == "error" else 0,
                1 if status in ("skipped", "skipped_questions") else 0,
                1 if status == "analyzed_skip" else 0,
            ),
        )
        await db.commit()


async def get_today_stats() -> dict:
    today = date.today().isoformat()
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (today,)
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return {
            "date": today,
            "total_applied": 0,
            "successful": 0,
            "errors": 0,
            "skipped": 0,
            "analyzed_skip": 0,
        }


async def get_stats_range(days: int = 7) -> list[dict]:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM daily_stats ORDER BY date DESC LIMIT ?", (days,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_recent_applications(limit: int = 20) -> list[dict]:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, vacancy_url, title, employer, status, ai_relevance,
                      error_message, created_at
               FROM applied_vacancies
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_hourly_applications_count() -> int:
    db_path = _get_db_path()
    one_hour_ago = datetime.utcnow().isoformat(timespec="minutes")
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            """SELECT COUNT(*) FROM applied_vacancies
               WHERE created_at >= datetime(?, '-1 hour')
               AND status = 'success'""",
            (one_hour_ago,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_consecutive_errors() -> int:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            """SELECT status FROM applied_vacancies
               ORDER BY created_at DESC LIMIT 5"""
        )
        rows = await cursor.fetchall()
        count = 0
        for row in rows:
            if row[0] == "error":
                count += 1
            else:
                break
        return count


async def set_setting(key: str, value: str) -> None:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """INSERT INTO bot_settings (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = CURRENT_TIMESTAMP""",
            (key, value),
        )
        await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """INSERT INTO bot_settings (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (key, value),
        )
        await db.commit()


async def get_all_settings() -> dict[str, str]:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute("SELECT key, value FROM bot_settings")
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}


async def reset_today_limits() -> None:
    today = date.today().isoformat()
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("DELETE FROM daily_stats WHERE date = ?", (today,))
        await db.execute("DELETE FROM applied_vacancies WHERE created_at >= date('now', 'start of day')")
        await db.commit()


async def get_vacancies_with_questions(limit: int = 15) -> list[dict]:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, title, employer, vacancy_url, created_at 
               FROM applied_vacancies 
               WHERE status = 'skipped_questions' AND shown = 0
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return []
        
        # Mark as shown
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE applied_vacancies SET shown = 1 WHERE id IN ({placeholders})",
            ids
        )
        await db.commit()
        return [dict(r) for r in rows]
