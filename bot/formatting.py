"""HTML formatting helpers for Telegram messages."""

import html
from typing import Optional


def bold(text: str) -> str:
    return f"<b>{html.escape(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{html.escape(text)}</i>"


def code(text: str) -> str:
    return f"<code>{html.escape(text)}</code>"


def pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def link(text: str, url: str) -> str:
    return f'<a href="{url}">{html.escape(text)}</a>'


def quote(text: str) -> str:
    return f"<blockquote>{html.escape(text)}</blockquote>"


def spoiler(text: str) -> str:
    return f"<tg-spoiler>{html.escape(text)}</tg-spoiler>"


def section(title: str, content: str) -> str:
    """Bold title + content on next lines."""
    return f"{bold(title)}\n{content}"


def divider() -> str:
    return "─" * 24


def relevance_bar(score: int, max_score: int = 10) -> str:
    """Visual relevance bar like ██████░░░░."""
    filled = "█" * score
    empty = "░" * (max_score - score)
    return f"{filled}{empty}"


def status_emoji(status: str) -> str:
    return {
        "success": "✅",
        "error": "❌",
        "skipped": "⏭",
        "analyzed_skip": "🤖",
        "applied": "📨",
    }.get(status, "❓")


def relevance_color(score: int) -> str:
    if score >= 8:
        return "🟢"
    elif score >= 5:
        return "🟡"
    else:
        return "🔴"


def format_apply_success(
    title: str,
    url: str,
    employer: str,
    relevance: Optional[int] = None,
    summary: Optional[str] = None,
    cover_letter: Optional[str] = None,
) -> str:
    relevance_str = f" | {relevance}/10" if relevance is not None else ""
    summary = summary if summary else "Комментарий ИИ отсутствует"
    summary_str = f"\n💬 <b>Анализ:</b> {summary}"
    cover_preview = f"\n\n📝 <b>Сопроводительное письмо:</b>\n{cover_letter}" if cover_letter else "\n📝 <b>Сопроводительное:</b> —"
    return (
        f"✅ <b>Успешный отклик!</b>\n"
        f"🔗 <b>Вакансия:</b> {link(title, url)}\n"
        f"🏢 <b>Компания:</b> {employer}{relevance_str}"
        f"{summary_str}"
        f"{cover_preview}"
    )


def format_session_finished(total: int, successful: int, errors: int, skipped: int = 0) -> str:
    return (
        f"📊 <b>Сессия авто-откликов завершена</b>\n"
        f"📋 Обработано сегодня: <b>{total}</b>\n"
        f"🟢 Успешных откликов: <b>{successful}</b>\n"
        f"🟡 Пропущено (ИИ/фильтры): <b>{skipped}</b>\n"
        f"🔴 Ошибок: <b>{errors}</b>"
    )


def format_monitoring_finished(total: int, successful: int, errors: int, skipped: int = 0, next_run: str = "") -> str:
    return (
        f"📊 <b>Обход новых вакансий завершен</b>\n\n"
        f"⏱ <b>Статистика текущего обхода:</b>\n"
        f"• Всего проверено: <b>{total}</b>\n"
        f"• Откликов отправлено: <b>{successful}</b>\n"
        f"• Пропущено (ИИ/кэш): <b>{skipped}</b>\n"
        f"• Ошибок: <b>{errors}</b>\n\n"
        f"⏱ Следующий запуск: <b>{next_run}</b>"
    )


def format_scheduler_status(
    run_state_name: str,
    page: int,
    processed: int,
    applied_today: int,
    captcha_detected: bool,
) -> str:
    captcha_str = "⚠️ Обнаружена" if captcha_detected else "✅ Отсутствует"
    return (
        f"ℹ️ <b>Статус авто-откликов:</b>\n"
        f"👤 <b>Режим:</b> {run_state_name}\n"
        f"📄 <b>Текущая страница:</b> {page}\n"
        f"📋 <b>Обработано вакансий:</b> {processed}\n"
        f"✅ <b>Откликов сегодня:</b> {applied_today}\n"
        f"🔒 <b>Капча:</b> {captcha_str}"
    )

