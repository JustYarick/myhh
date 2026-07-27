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
