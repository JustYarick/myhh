import aiosqlite
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from .config import get_settings

logger = logging.getLogger(__name__)

DB_PATH: Optional[Path] = None


def _get_db_path() -> Path:
    global DB_PATH
    if DB_PATH is None:
        DB_PATH = get_settings().db_file
    return DB_PATH


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

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_vacancy_url ON applied_vacancies(vacancy_url)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_vacancy_created ON applied_vacancies(created_at)"
        )

        await db.commit()
    logger.info(f"Database initialized: {db_path}")


async def was_vacancy_applied(vacancy_url: str) -> bool:
    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT 1 FROM applied_vacancies WHERE vacancy_url = ? AND status != 'error' LIMIT 1",
            (vacancy_url,)
        )
        return await cursor.fetchone() is not None


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
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """INSERT INTO applied_vacancies
               (vacancy_url, title, employer, description, cover_letter,
                ai_relevance, ai_analysis, status, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vacancy_url, title, employer, description[:2000], cover_letter,
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
                1 if status == "skipped" else 0,
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
               AND status IN ('success', 'analyzed_skip')""",
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
