"""Лёгкий markdown → Telegram HTML + разбивка под лимит сообщений.

Telegram рендерит ограниченный HTML (b/i/s/code/pre/a); превращаем в него
разметку из ответов Claude, остальное оставляем как есть, небезопасные символы
экранируем. Вынесено из bot.py (REVIEW.md D1) — чистые функции, тестируются
без Telegram.
"""

from __future__ import annotations

import html
import re

TG_MESSAGE_LIMIT = 4096

_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_CODE_INLINE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
# _italic_ со word-границами — чтобы не калечить snake_case (my_var_name).
_ITALIC_RE = re.compile(r"(?<![\w*])_(?!\s)(.+?)(?<!\s)_(?![\w*])", re.DOTALL)
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")


def split_text(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    """Разбить текст под лимит Telegram, по возможности по переводу строки."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", limit // 2, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def md_to_html(text: str) -> str:
    """Светлый markdown → HTML Telegram. Код выносится первым (внутри нет
    разметки), затем экранируется остальное, затем разметка."""
    stash: list[str] = []

    def _keep(html_body: str, tag: str) -> str:
        stash.append(f"<{tag}>{html_body}</{tag}>")
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(lambda m: _keep(html.escape(m.group(1)), "pre"), text)
    text = _CODE_INLINE_RE.sub(lambda m: _keep(html.escape(m.group(1)), "code"), text)
    text = html.escape(text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)
