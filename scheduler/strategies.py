import asyncio
import logging
from abc import ABC, abstractmethod

from .. import database as db
from ..database import extract_vacancy_id
from ..services.hh_search import search_service
from .runner import _CaptchaPause

logger = logging.getLogger(__name__)

class RunStrategy(ABC):
    @abstractmethod
    async def execute(
        self, scheduler, config, anti_fraud, resume_text: str, flow_id: str,
        session_success_count: int, session_skipped_count: int,
        session_error_count: int, session_total_processed: int
    ) -> str | None:
        """
        Executes the strategy. Returns a new boundary URL if applicable (Monitoring).
        """
        pass

class ManualRunStrategy(RunStrategy):
    async def execute(
        self, scheduler, config, anti_fraud, resume_text: str, flow_id: str,
        session_success_count: int, session_skipped_count: int,
        session_error_count: int, session_total_processed: int
    ) -> str | None:
        max_search_pages = 40
        for page_num in range(max_search_pages):
            if scheduler._check_stop():
                break
            await scheduler._check_pause()
            if scheduler._check_stop():
                break

            allowed, reason = await anti_fraud.check_rate_limits()
            if not allowed:
                await scheduler._notify(f"🛑 Stopping: {reason}", notify_type="error")
                break

            await scheduler._notify(f"🔍 Searching page <b>{page_num + 1}/{max_search_pages}</b>...")
            scheduler.state.current_page = page_num + 1

            try:
                _, cards, _ = await scheduler._run_interruptible(
                    search_service.search_cards(anti_fraud=anti_fraud, page_num=page_num, url=config.search_url)
                )
            except Exception as e:
                logger.error(f"Search failed on page {page_num + 1}: {e}", exc_info=True)
                await scheduler._notify(f"⚠️ Search error on page {page_num + 1}, skipping: {e}", notify_type="error")
                continue

            if not cards:
                await scheduler._notify(f"📭 No vacancies found on page {page_num + 1}")
                break

            await scheduler._notify(f"📋 Found <b>{len(cards)}</b> vacancies on page {page_num + 1}")

            target_reached = False
            for i, card in enumerate(cards):
                if scheduler._check_stop():
                    break
                await scheduler._check_pause()
                if scheduler._check_stop():
                    break

                is_applied = await db.was_vacancy_applied(card.get("url", ""))
                if is_applied:
                    logger.debug(f"Already applied, skipping: {card.get('url')}")
                    continue

                allowed, reason = await anti_fraud.check_rate_limits()
                if not allowed:
                    await scheduler._notify(f"🛑 Stopping: {reason}", notify_type="error")
                    scheduler._stop_event.set()
                    break

                try:
                    applied = await asyncio.wait_for(
                        scheduler._process_card(card, None, anti_fraud, resume_text, config, i, len(cards)),
                        timeout=180.0
                    )
                    session_total_processed += 1
                    if applied:
                        session_success_count += 1
                        if session_success_count >= config.target_applies:
                            await scheduler._notify(
                                f"🎯 <b>Целевой лимит достигнут!</b> Отправлено <b>{session_success_count}</b> откликов за этот запуск."
                            )
                            scheduler._stop_event.set()
                            target_reached = True
                            break
                    else:
                        session_skipped_count += 1
                except _CaptchaPause:
                    break
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    session_total_processed += 1
                    session_error_count += 1
                    logger.error(f"Timeout processing vacancy '{card.get('title', '?')}' ({card.get('url', '?')})")
                    await scheduler._save_error_record(card, "Превышен лимит ожидания обработки вакансии (3 минуты)")
                    await scheduler._notify(
                        f"⚠️ <b>Пропуск (Превышен таймаут):</b> <a href=\"{card.get('url', '')}\">{card.get('title', 'Без названия')}</a> @ {card.get('employer', 'Неизвестно')}\n"
                        f"<i>Процесс обработки вакансии завис и был принудительно остановлен через 3 минуты.</i>",
                        notify_type="error"
                    )
                    continue
                except Exception as e:
                    session_total_processed += 1
                    session_error_count += 1
                    logger.error(f"Failed processing vacancy '{card.get('title', '?')}' ({card.get('url', '?')}): {e}", exc_info=True)
                    await scheduler._save_error_record(card, str(e))
                    await scheduler._notify(
                        f"❌ <b>Ошибка обработки вакансии:</b> <a href=\"{card.get('url', '')}\">{card.get('title', 'Без названия')}</a>\n⚠️ {e}",
                        notify_type="error"
                    )
                    continue
                finally:
                    from .runner import vacancy_lock_manager
                    await vacancy_lock_manager.release(card.get("url", ""))

            if target_reached or scheduler._check_stop():
                break

            if page_num < config.max_pages - 1:
                await anti_fraud.random_delay(is_page_change=True)

        return None

class MonitoringRunStrategy(RunStrategy):
    async def execute(
        self, scheduler, config, anti_fraud, resume_text: str, flow_id: str,
        session_success_count: int, session_skipped_count: int,
        session_error_count: int, session_total_processed: int
    ) -> str | None:
        last_newest_url = ""
        if flow_id:
            last_newest_url = await db.get_setting(f"last_newest_vacancy_{flow_id}", "")
            logger.info(f"Monitoring boundary for flow {flow_id}: {last_newest_url or 'not set'}")

        new_boundary_url = None
        max_search_pages = 40
        consecutive_cached_count = 0
        first_cached_in_sequence = None

        for page_num in range(max_search_pages):
            if scheduler._check_stop():
                break
            await scheduler._check_pause()
            if scheduler._check_stop():
                break

            allowed, reason = await anti_fraud.check_rate_limits()
            if not allowed:
                await scheduler._notify(f"🛑 Stopping: {reason}", notify_type="error")
                break

            if last_newest_url:
                await scheduler._notify(f"🔍 Searching page <b>{page_num + 1}/{max_search_pages}</b>...")
            scheduler.state.current_page = page_num + 1

            try:
                _, cards, _ = await scheduler._run_interruptible(
                    search_service.search_cards(anti_fraud=anti_fraud, page_num=page_num, url=config.search_url)
                )
            except Exception as e:
                logger.error(f"Search failed on page {page_num + 1}: {e}", exc_info=True)
                await scheduler._notify(f"⚠️ Search error on page {page_num + 1}, skipping: {e}", notify_type="error")
                continue

            if not cards:
                await scheduler._notify(f"📭 No vacancies found on page {page_num + 1}")
                break

            if page_num == 0:
                first_id = extract_vacancy_id(cards[0]["url"]) if cards[0].get("url") else ""
                new_boundary_url = first_id

                # First ever run
                if not last_newest_url:
                    await scheduler._notify(
                        f"🏁 <b>Базовая точка мониторинга установлена</b>:\n"
                        f"Вакансия ID: <code>{first_id}</code>\n"
                        f"С этого момента бот будет отслеживать новые вакансии относительно неё."
                    )
                    break

                # No new vacancies
                if first_id == last_newest_url:
                    await scheduler._notify("📭 <b>Новых вакансий нет</b> с момента запуска мониторинга. Завершаем.")
                    break

            await scheduler._notify(f"📋 Found <b>{len(cards)}</b> vacancies on page {page_num + 1}")

            boundary_reached = False
            for i, card in enumerate(cards):
                if scheduler._check_stop():
                    break
                await scheduler._check_pause()
                if scheduler._check_stop():
                    break

                card_id = extract_vacancy_id(card.get("url", ""))

                if last_newest_url and card_id == last_newest_url:
                    await scheduler._notify("🏁 <b>Достигнута граница ранее обработанных вакансий.</b> Завершаем.")
                    boundary_reached = True
                    break

                is_applied = await db.was_vacancy_applied(card.get("url", ""))
                is_cached = await db.is_vacancy_cached(card.get("url", ""))
                if is_applied or is_cached:
                    if consecutive_cached_count == 0:
                        first_cached_in_sequence = card_id
                    consecutive_cached_count += 1
                    if consecutive_cached_count >= 2:
                        await scheduler._notify("🏁 <b>Достигнута граница ранее обработанных вакансий (по кэшу).</b> Завершаем.")
                        new_boundary_url = first_cached_in_sequence
                        boundary_reached = True
                        break
                else:
                    consecutive_cached_count = 0

                allowed, reason = await anti_fraud.check_rate_limits()
                if not allowed:
                    await scheduler._notify(f"🛑 Stopping: {reason}", notify_type="error")
                    scheduler._stop_event.set()
                    break

                try:
                    applied = await asyncio.wait_for(
                        scheduler._process_card(card, None, anti_fraud, resume_text, config, i, len(cards)),
                        timeout=180.0
                    )
                    session_total_processed += 1
                    if applied:
                        session_success_count += 1
                        if session_success_count >= config.target_applies:
                            await scheduler._notify(
                                f"🎯 <b>Целевой лимит достигнут!</b> Отправлено <b>{session_success_count}</b> откликов за этот запуск."
                            )
                            scheduler._stop_event.set()
                            break
                    else:
                        session_skipped_count += 1
                except _CaptchaPause:
                    break
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    session_total_processed += 1
                    session_error_count += 1
                    logger.error(f"Timeout processing vacancy '{card.get('title', '?')}' ({card.get('url', '?')})")
                    await scheduler._save_error_record(card, "Превышен лимит ожидания обработки вакансии (3 минуты)")
                    await scheduler._notify(
                        f"⚠️ <b>Пропуск (Превышен таймаут):</b> <a href=\"{card.get('url', '')}\">{card.get('title', 'Без названия')}</a> @ {card.get('employer', 'Неизвестно')}\n"
                        f"<i>Процесс обработки вакансии завис и был принудительно остановлен через 3 минуты.</i>",
                        notify_type="error"
                    )
                    continue
                except Exception as e:
                    session_total_processed += 1
                    session_error_count += 1
                    logger.error(f"Failed processing vacancy '{card.get('title', '?')}' ({card.get('url', '?')}): {e}", exc_info=True)
                    await scheduler._save_error_record(card, str(e))
                    await scheduler._notify(
                        f"❌ <b>Ошибка обработки вакансии:</b> <a href=\"{card.get('url', '')}\">{card.get('title', 'Без названия')}</a>\n⚠️ {e}",
                        notify_type="error"
                    )
                    continue
                finally:
                    from .runner import vacancy_lock_manager
                    await vacancy_lock_manager.release(card.get("url", ""))

            if boundary_reached or scheduler._check_stop():
                break

            if page_num < config.max_pages - 1:
                await anti_fraud.random_delay(is_page_change=True)

        return new_boundary_url
