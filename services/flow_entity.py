import json
import logging
from typing import Optional

import aiosqlite

from ..config import get_settings
from ..models.flow import FlowConfig, FlowEntity

logger = logging.getLogger(__name__)

DB_PATH = None


def _get_db_path() -> str:
    global DB_PATH
    if DB_PATH is None:
        DB_PATH = str(get_settings().db_file)
    return DB_PATH


async def init_flow_table() -> None:
    async with aiosqlite.connect(_get_db_path()) as db:
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
        await db.commit()
    logger.info("Flow tables initialized")


async def create_flow(name: str, config: FlowConfig) -> FlowEntity:
    async with aiosqlite.connect(_get_db_path()) as db:
        cursor = await db.execute(
            "INSERT INTO flow_entities (name, config) VALUES (?, ?)",
            (name, json.dumps(config.to_dict())),
        )
        await db.commit()
        flow_id = cursor.lastrowid
    return FlowEntity(id=flow_id, name=name, config=config)


async def get_flow(flow_id: int) -> Optional[FlowEntity]:
    async with aiosqlite.connect(_get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM flow_entities WHERE id = ?", (flow_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return FlowEntity(
            id=row["id"],
            name=row["name"],
            config=FlowConfig.from_dict(json.loads(row["config"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


async def list_flows() -> list[FlowEntity]:
    async with aiosqlite.connect(_get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM flow_entities ORDER BY id"
        )
        rows = await cursor.fetchall()
        return [
            FlowEntity(
                id=r["id"],
                name=r["name"],
                config=FlowConfig.from_dict(json.loads(r["config"])),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]


async def update_flow(flow_id: int, name: Optional[str] = None, config: Optional[FlowConfig] = None) -> bool:
    async with aiosqlite.connect(_get_db_path()) as db:
        if name is not None:
            await db.execute(
                "UPDATE flow_entities SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (name, flow_id),
            )
        if config is not None:
            await db.execute(
                "UPDATE flow_entities SET config = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(config.to_dict()), flow_id),
            )
        await db.commit()
        return db.total_changes > 0


async def delete_flow(flow_id: int) -> bool:
    async with aiosqlite.connect(_get_db_path()) as db:
        await db.execute("DELETE FROM flow_entities WHERE id = ?", (flow_id,))
        await db.commit()
        return db.total_changes > 0


async def set_active_flow(flow_id: Optional[int]) -> None:
    async with aiosqlite.connect(_get_db_path()) as db:
        if flow_id is None:
            await db.execute("DELETE FROM bot_settings WHERE key = 'active_flow_id'")
        else:
            await db.execute(
                """INSERT INTO bot_settings (key, value, updated_at)
                   VALUES ('active_flow_id', ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = CURRENT_TIMESTAMP""",
                (str(flow_id),),
            )
        await db.commit()


async def get_active_flow_id() -> Optional[int]:
    async with aiosqlite.connect(_get_db_path()) as db:
        cursor = await db.execute(
            "SELECT value FROM bot_settings WHERE key = 'active_flow_id'"
        )
        row = await cursor.fetchone()
        if row:
            try:
                return int(row[0])
            except (ValueError, TypeError):
                return None
        return None


async def get_active_flow() -> Optional[FlowEntity]:
    flow_id = await get_active_flow_id()
    if flow_id is None:
        return None
    return await get_flow(flow_id)


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(_get_db_path()) as db:
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
    async with aiosqlite.connect(_get_db_path()) as db:
        cursor = await db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def get_all_settings() -> dict[str, str]:
    async with aiosqlite.connect(_get_db_path()) as db:
        cursor = await db.execute("SELECT key, value FROM bot_settings")
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}
