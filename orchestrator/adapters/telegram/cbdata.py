"""Callback-данные inline-кнопок: сборка и разбор в одном месте.

Раньше форматы «префикс:поля» были размазаны по хендлерам ad-hoc `split(":")`
с разным числом аргументов (REVIEW.md D4); колоны внутри request_id — известная
мина (Claude Code генерирует id с ':'). Здесь каждый формат собирается и
разбирается парой функций, разбор терпим к мусору (None вместо исключения).

Лимит Telegram на callback_data — 64 байта; сборщики не проверяют его
(request_id короткий на практике), но формат держим компактным.
"""

from __future__ import annotations


def stop_cb(thread_id: int) -> str:
    return f"stop:{thread_id}"


def parse_stop(data: str) -> int | None:
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def model_cb(thread_id: int, alias: str) -> str:
    return f"model:{thread_id}:{alias}"


def parse_model(data: str) -> tuple[int, str] | None:
    """(thread_id, alias) либо None."""
    try:
        _, thread_raw, alias = data.split(":", 2)
        return int(thread_raw), alias
    except ValueError:
        return None


def sess_cb(action: str, thread_id: int) -> str:
    return f"sess:{action}:{thread_id}"


def parse_sess(data: str) -> tuple[str, int] | None:
    """(action, thread_id) либо None."""
    try:
        _, action, thread_raw = data.split(":", 2)
        return action, int(thread_raw)
    except ValueError:
        return None


def delete_cb(thread_id: int, verdict: str) -> str:
    return f"del:{thread_id}:{verdict}"


def parse_delete(data: str) -> tuple[int, str] | None:
    """(thread_id, verdict) либо None."""
    try:
        _, thread_raw, verdict = data.split(":", 2)
        return int(thread_raw), verdict
    except ValueError:
        return None


def perm_cb(thread_id: int, request_id: str, behavior: str) -> str:
    return f"perm:{thread_id}:{request_id}:{behavior}"


def parse_perm(data: str) -> tuple[int, str, str] | None:
    """(thread_id, request_id, behavior) либо None.

    request_id может содержать ':' — behavior отрезаем с хвоста (rsplit),
    thread_id с головы, всё между ними — request_id как есть.
    """
    try:
        prefix, behavior = data.rsplit(":", 1)
        _, thread_raw, request_id = prefix.split(":", 2)
        return int(thread_raw), request_id, behavior
    except ValueError:
        return None
