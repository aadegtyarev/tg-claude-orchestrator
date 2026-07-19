"""Рендер строк статус-бабла: вызов инструмента → компактная HTML-строка.

Чистые функции без Telegram/сессий: bot.handle_tool_event передаёт сюда
имя тула и tool_input из PreToolUse-хука, обратно получает одну строку
для бабла (иконка + имя жирным + короткая деталь моноширинно).
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Callable

# Обрезка одной строки бабла / промежуточного ответа.
LINE_LIMIT = 100

# Иконки инструментов для статус-бабла. "Agent" — актуальное имя тула спавна
# сабагента (Claude Code ≥2.1); "Task" — синоним для более старых версий,
# оставлен для совместимости (проверять tool == "Task" or tool == "Agent"
# было бы дублированием — см. AGENT_SPAWN_TOOLS).
TOOL_ICONS = {
    "Bash": "⚡", "Read": "📖", "Write": "✍️", "Edit": "✏️",
    "NotebookEdit": "✏️", "Grep": "🔍", "Glob": "🗂", "WebFetch": "🌐",
    "WebSearch": "🔎", "Agent": "🤖", "Task": "🤖", "TodoWrite": "📝",
}
# Имена тула, которым Claude Code спавнит сабагента — менялось между версиями
# (REVIEW: 2.1.214 использует "Agent", не "Task" — из-за этого бабл раньше не
# распознавал спавн и падал в generic-фолбэк, показывая description без имени
# агента). Держим оба, чтобы не отвалиться на следующем переименовании тоже.
AGENT_SPAWN_TOOLS = frozenset({"Agent", "Task"})
# Из какого поля брать деталь и показывать ли её как имя файла (basename).
# TaskCreate/TaskUpdate — тему/статус вместо сырого JSON-объекта целиком.
_TOOL_DETAIL = {
    "Bash": ("command", False), "Read": ("file_path", True),
    "Write": ("file_path", True), "Edit": ("file_path", True),
    "NotebookEdit": ("notebook_path", True), "Grep": ("pattern", False),
    "Glob": ("pattern", False), "WebFetch": ("url", False),
    "WebSearch": ("query", False),
    "TaskCreate": ("subject", False), "TaskUpdate": ("status", False),
}

# Поля сторонних/незнакомых инструментов, из которых тянем осмысленную деталь —
# по порядку предпочтения (имя файла/путь важнее «описания»).
_MEANINGFUL_FIELDS = (
    "file_path", "path", "notebook_path", "pattern", "query",
    "url", "command", "subject", "description", "prompt", "text",
)

# Дробим bash-команду по конвейерам и связкам; «словесный» аргумент
# (подкоманду: log/test/status) отличаем от флагов и путей.
_BASH_SEP = re.compile(r"\s*(?:&&|\|\||\||;)\s*")
_WORD_ARG = re.compile(r"^[A-Za-z][\w:-]*$")


def shorten(text: str, limit: int = LINE_LIMIT) -> str:
    """Схлопнуть пробелы и обрезать до limit с многоточием."""
    text = " ".join(text.split())
    return text[:limit] + "…" if len(text) > limit else text


def first_meaningful(tool_input: dict) -> str:
    """Первый осмысленный строковый аргумент стороннего инструмента.

    Сырой JSON в бабле не показываем — он не помещается в строку и шумит.
    """
    for key in _MEANINGFUL_FIELDS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def file_suffix(tool: str, tool_input: dict) -> str:
    """Короткий контекст к имени файла: какие строки читаем/сколько правим.

    Read c offset/limit → « L10–34»; Edit → « ±N строк»; замена по всему файлу
    → « ×N». Ничего осмысленного нет — пустая строка.
    """
    if tool == "Read":
        try:
            off = int(tool_input.get("offset"))
        except (TypeError, ValueError):
            return ""
        try:
            lim = int(tool_input.get("limit"))
            return f" L{off}–{off + lim}"
        except (TypeError, ValueError):
            return f" L{off}+"
    if tool in ("Edit", "NotebookEdit"):
        old = str(tool_input.get("old_string") or "")
        if tool_input.get("replace_all"):
            return " (все)"
        n = old.count("\n") + 1 if old else 0
        return f" ±{n} стр" if n else ""
    return ""


def bash_head(command: str) -> str:
    """Короткая «голова» bash-команды: куда cd + имя команды (+ подкоманда).

    Ведущий `cd …` раньше выкидывали, но каталог работы — полезный контекст
    (какой ворктри/папка), поэтому показываем его basename. Флаги и длинные
    аргументы отбрасываем: строчка должна помещаться в бабл в одну строку.
    """
    cmd = command.strip()
    cd_dest = ""
    head = ""
    for segment in _BASH_SEP.split(cmd):
        segment = segment.strip()
        if not segment:
            continue
        if segment.startswith("cd "):
            dest = segment[3:].strip().strip("\"'")
            cd_dest = Path(dest).name or dest
            continue
        head = segment
        break
    if not head:
        return cd_dest or cmd  # вся команда — это cd
    parts = head.split()
    main = parts[0] if parts else ""
    sub = parts[1] if len(parts) > 1 else ""
    if sub and not _WORD_ARG.match(sub):
        sub = ""  # флаг (-n), путь (/x) или кавычка (") — не интересно
    # Хвост: первый ЗНАЧИМЫЙ аргумент (не флаг) — паттерн grep, файл, URL.
    # Нужен, чтобы серия схлопнутых команд (6× grep разных паттернов) не
    # выглядела застывшей: меняющийся хвост показывает, что модель реально
    # перебирает разное, а не зависла на одном.
    tail = ""
    for tok in parts[1:]:
        if tok.startswith("-"):
            continue  # флаг
        cleaned = tok.strip("\"'")
        if cleaned and cleaned != sub:
            tail = shorten(cleaned, 32)
            break
    body = " ".join(p for p in (main, sub, tail) if p)
    return f"{cd_dest} · {body}" if cd_dest else body


def tool_line(tool: str, tool_input: dict, t: Callable[..., str]) -> str:
    """HTML-строка бабла: иконка + имя жирным + короткая деталь моноширинно.

    Деталь режем до осмысленного кусочка в одну строку: длинные bash-команды
    показываем «головой» (имя команды + подкоманда), у TaskCreate берём
    тему, у незнакомых тулов — первый строковый аргумент, а не весь JSON.
    t — функция локализации бота (texts.get_texts).
    """
    icon = TOOL_ICONS.get(tool, "🔧")
    if tool in AGENT_SPAWN_TOOLS:
        # t("subagent") уже содержит иконку 🤖 — свою не добавляем.
        agent = html.escape(str(tool_input.get("subagent_type") or "agent"))
        desc = html.escape(shorten(str(tool_input.get("description") or "")))
        base = f"<b>{t('subagent', agent=agent)}</b>"
        return f"{base}: <i>{desc}</i>" if desc else base

    if tool == "TodoWrite":
        todos = tool_input.get("todos")
        detail = f"{len(todos)} задач" if isinstance(todos, list) else ""
    elif tool == "Bash":
        detail = bash_head(str(tool_input.get("command") or ""))
    else:
        field, as_name = _TOOL_DETAIL.get(tool, (None, False))
        if field:
            detail = str(tool_input.get(field, "")).strip()
            if as_name and detail:
                detail = Path(detail).name  # длинный путь → имя файла
                suffix = file_suffix(tool, tool_input)  # строки/диапазон
                if suffix:
                    detail += suffix
        else:
            detail = first_meaningful(tool_input)  # сторонний тул — не JSON

    detail = html.escape(shorten(detail))
    head = f"{icon} <b>{html.escape(tool)}</b>"
    return f"{head} <code>{detail}</code>" if detail else head
