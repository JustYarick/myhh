import json
import logging
from typing import Optional

import aiosqlite

from ..config import get_settings
from ..models.flow import FlowConfig, FlowEntity
from ..database import get_setting, set_setting, get_all_settings

logger = logging.getLogger(__name__)

DB_PATH = None


def _get_db_path() -> str:
    global DB_PATH
    if DB_PATH is None:
        DB_PATH = str(get_settings().db_file)
    return DB_PATH


async def init_flow_table() -> None:
    try:
        await migrate_existing_flows_prompts()
    except Exception as e:
        logger.warning(f"Failed to migrate flow prompts: {e}")


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


# Settings helpers are imported from database module for backwards compatibility


async def migrate_existing_flows_prompts() -> None:
    flows = await list_flows()
    for flow in flows:
        updated = False
        if "Generate a short cover letter" in flow.config.cover_letter_prompt or "professional but casual, Russian" in flow.config.cover_letter_prompt:
            flow.config.cover_letter_prompt = FlowConfig().cover_letter_prompt
            updated = True
        if "Rate relevance of vacancy" in flow.config.analysis_prompt:
            flow.config.analysis_prompt = FlowConfig().analysis_prompt
            updated = True
        if (
            "Кандидат Junior/Intern, а вакансия Senior/Lead" in flow.config.analysis_prompt
            and "совпадения СТЕКА И РОЛИ" not in flow.config.analysis_prompt
        ):
            # Обновляем устаревшую жёсткую версию промпта (авто-1-3 при недоборе грейда)
            flow.config.analysis_prompt = FlowConfig().analysis_prompt
            updated = True
        
        if updated:
            await update_flow(flow.id, config=flow.config)
            logger.info(f"Migrated default prompts to Russian for flow ID={flow.id} ({flow.name})")
