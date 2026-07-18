"""Снятие ANSI/управляющих escape-последовательностей из байтов вывода PTY.

Раньше один и тот же regex дублировался в sessions.py, bashshell.py и bot.py
(под именами _ANSI_RE / _LOG_ANSI_RE) — теперь единое место (REVIEW.md D2).
Все три модуля сыплют похожим набором кодов: CSI-последовательности, OSC
(заканчивается BEL) и кодировки скобок/charsets.
"""

from __future__ import annotations

import re

ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]")


def strip_ansi(raw: bytes) -> bytes:
    """Убрать ANSI-раскраску/управление и \\r — для показа в <pre> и парсинга лога."""
    return ANSI_RE.sub(b"", raw).replace(b"\r", b"")
