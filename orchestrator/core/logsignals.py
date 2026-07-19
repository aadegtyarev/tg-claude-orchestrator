"""Разбор «живых сигналов» из claude.log для ретранслятора ошибок API.

Чистые функции над байтами: триггер — только настоящий баннер TUI
«API Error: <код>», классификация (ratelimit/protocol/generic), ретраи
«attempt K/M» и баннеры краш-рестарта. Тестируется без петли и Telegram
(error_relay_test). Вынесено из bot.py (REVIEW.md D1).
"""

from __future__ import annotations

import re

# Триггер — ТОЛЬКО настоящий баннер «API Error: <код> <детали>». Модель охотно
# пишет слова «rate-limit»/«api error» в ответах, и широкий греп ловил прозу как
# ложный алерт. Баннер с кодом модель дословно не цитирует — это надёжный сигнал.
# group(1)=код, group(2)=хвост строки с деталями (для класса).
API_ERR_BANNER_RE = re.compile(rb"API Error:\s*(\d{3})\b([^\n]{0,140})", re.IGNORECASE)
# Лимит-баннер БЕЗ 3-значного HTTP-кода: «API Error: Server is temporarily
# limiting requests … [1310][Weekly/Monthly Limit Exhausted. Your limit will
# reset at <дата>]». HTTP-код тут не 3-значный (1310 в скобках), поэтому
# основной регекс его не ловил — а именно на нём noos часами «молчал».
_QUOTA_BANNER_RE = re.compile(
    rb"API Error:[^\n]*?(?:temporarily limiting requests|Limit Exhausted)"
    rb"[^\n]{0,160}",
    re.IGNORECASE,
)
_QUOTA_RESET_RE = re.compile(rb"reset at\s*([0-9:\- ]{10,25})", re.IGNORECASE)
# Класс ошибки по деталям баннера (код разбираем отдельно).
_RL_DETAIL_RE = re.compile(rb"rate[\s_-]?limit|overloaded|\bcapacity\b", re.IGNORECASE)
_PROTO_DETAIL_RE = re.compile(rb"server_tool_use|tool_result|messages\.\d|thinking", re.IGNORECASE)
# Живые сигналы из claude.log (когда тулов нет, но что-то происходит):
#  • ретрай API-ошибки — «Retrying … attempt K/M»;
#  • краш-рестарт — баннер «Resume this session with» / «Welcome back».
RETRY_RE = re.compile(rb"attempt\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
RESTART_RE = re.compile(rb"Resume this session with|Welcome back", re.IGNORECASE)
# «Живой пульс» из TUI-спиннера Claude Code: забавные глаголы + таймер, напр.
# «✻ Cogitating…», «✳ Quantumizing…» (сейчас думает) или «✻ Cogitated for 5m 57s»
# (только что думал). Показываем последний в бабле как признак «модель жива»,
# когда tool-событий нет. Эллипсис — юникодный (…, \xe2\x80\xa6) или три точки.
_VERB_ACTIVE_RE = re.compile(rb"\b([A-Z][a-z]{2,}ing)(?:\xe2\x80\xa6|\.{3})")
_VERB_DONE_RE = re.compile(rb"\b([A-Z][a-z]{2,}(?:ed|ing))\s+for\s+(\d[\dhms ]*[hms])")


def classify_api_error(code: bytes, detail: bytes) -> str:
    """Класс ошибки API: ratelimit | protocol | generic.

    ratelimit — 429/529/overloaded: транзитно, помогает смена модели.
    protocol — 400 с кривым server_tool_use/tool_result/thinking: апстрим (z.ai)
              шлёт несогласованный блок, модель тут ни при чём — /clear или
              /close_session.
    generic  — прочее (5xx и т.п.).
    """
    if code in (b"429", b"529") or _RL_DETAIL_RE.search(detail):
        return "ratelimit"
    if code == b"400" and _PROTO_DETAIL_RE.search(detail):
        return "protocol"
    return "generic"


def detect_log_signals(chunk: bytes) -> dict:
    """Разобрать кусок claude.log на три класса сигналов.

    Возвращает {api_error, retry, restarts}:
      • api_error — (code, klass) баннера «API Error: <код>» либо None;
      • retry — (attempt, total) из «attempt K/M», последний в куске, либо None;
      • restarts — сколько баннеров рестарта.
    """
    out: dict = {
        "api_error": None, "retry": None, "restarts": 0, "pulse": None, "quota": None,
    }
    m = API_ERR_BANNER_RE.search(chunk)
    if m:
        out["api_error"] = (m.group(1), classify_api_error(m.group(1), m.group(2)))
    # Лимит-баннер без 3-значного кода (Weekly/Monthly exhausted и т.п.).
    qm = _QUOTA_BANNER_RE.search(chunk)
    if qm:
        reset = _QUOTA_RESET_RE.search(qm.group(0))
        out["quota"] = (
            b" ".join(reset.group(1).split()).decode("ascii", "replace") if reset else ""
        )
    rm = RETRY_RE.search(chunk)
    if rm:
        out["retry"] = (int(rm.group(1)), int(rm.group(2)))
    out["restarts"] = len(RESTART_RE.findall(chunk))
    out["pulse"] = _extract_pulse(chunk)
    return out


def _extract_pulse(chunk: bytes) -> str | None:
    """Последний спиннер-глагол Claude Code из куска лога → строка для бабла
    (напр. «Cogitating» или «Cogitated · 5m 57s») либо None.

    Приоритет активному «-ing…» (думает сейчас); иначе последний «… for <time>».
    """
    active = list(_VERB_ACTIVE_RE.finditer(chunk))
    if active:
        return active[-1].group(1).decode("ascii", "replace")
    done = list(_VERB_DONE_RE.finditer(chunk))
    if done:
        verb = done[-1].group(1).decode("ascii", "replace")
        dur = b" ".join(done[-1].group(2).split()).decode("ascii", "replace")
        return f"{verb} · {dur}"
    return None
