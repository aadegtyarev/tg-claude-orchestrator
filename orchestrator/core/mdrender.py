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


def _is_table_sep(line: str) -> bool:
    """Разделитель markdown-таблицы: `|---|:--:|` (только -, :, |, пробелы)."""
    s = line.strip()
    return bool(s) and "-" in s and set(s) <= set("|-: ")


def _table_cells(line: str) -> list[str]:
    """Ячейки строки таблицы (внешние пайпы отрезаны, `\\|` → литеральный |)."""
    parts = [c.replace("\x01", "|").strip() for c in line.replace("\\|", "\x01").split("|")]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _render_table(lines: list[str]) -> str:
    """Строки md-таблицы → выровненный моноширинный текст (экранированный)."""
    rows = [_table_cells(lines[0])] + [_table_cells(ln) for ln in lines[2:]]
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    widths = [max(len(r[c]) for r in rows) for c in range(ncol)]

    def fmt(r: list[str]) -> str:
        return " | ".join(r[c].ljust(widths[c]) for c in range(ncol))

    sep = "-+-".join("-" * w for w in widths)
    body = "\n".join(fmt(r) for r in rows[1:])
    return html.escape(f"{fmt(rows[0])}\n{sep}\n{body}".rstrip())


def _reformat_tables(text: str, keep) -> str:
    """Найти md-таблицы (шапка + разделитель + тело) и заменить на выровненный
    <pre>-блок — Telegram таблиц не умеет, а моношрифт держит колонки ровно."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if ("|" in lines[i] and i + 1 < len(lines) and _is_table_sep(lines[i + 1])
                and lines[i].strip()):
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                j += 1
            out.append(keep(_render_table(lines[i:j]), "pre"))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


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
    # md-таблицы → выровненный <pre> (после выноса кода: таблицу внутри ``` не
    # трогаем, она уже в stash). До html.escape — _render_table экранирует сам.
    text = _reformat_tables(text, _keep)
    text = html.escape(text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)
