"""Журнал событий сессий для веб-истории.

Веб-интерфейс после перезагрузки страницы (или graceful-рестарта оркестратора)
показывает последние события сессии — ответы, вызовы инструментов, статусы,
запросы разрешений. Полная история живёт в транскрипте Claude Code; здесь —
компактный кольцевой буфер на сессию (последние HISTORY_LIMIT событий), который
переживает graceful-рестарт через персист в `.history.json`.

Чистый объект состояния: конструктор НЕ трогает диск (`load`/`save` — явный I/O),
поэтому тесты создают пустой журнал без файловой системы. Владелец (app.py) зовёт
`record` на каждом значимом событии и `forget(name)` при удалении сессии.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

# Сколько последних событий сессии держать в журнале (кольцевой буфер).
HISTORY_LIMIT = 300


class HistoryLog:
    """Кольцевой журнал событий на сессию + персист для веб-истории.

    Владеет `_log` ({имя_сессии: deque событий}); `forget(name)` снимает журнал
    сессии, `load`/`save` — обмен с диском (атомарный через tmp+replace).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._log: dict[str, deque] = {}

    def record(self, name: str, kind: str, **payload) -> None:
        """Добавить событие в журнал сессии (старые вытесняются за HISTORY_LIMIT)."""
        log = self._log.setdefault(name, deque(maxlen=HISTORY_LIMIT))
        log.append({"ts": time.time(), "kind": kind, **payload})

    def events(self, name: str) -> list[dict]:
        """Копия журнала сессии (для веб-истории)."""
        return list(self._log.get(name, ()))

    def forget(self, name: str) -> None:
        """Снять журнал сессии (при удалении/полной очистке)."""
        self._log.pop(name, None)

    def load(self) -> None:
        """Восстановить журнал с прошлого запуска (веб-история переживает
        graceful-рестарт). Битый/отсутствующий файл — просто пустая история."""
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        for name, events in (data or {}).items():
            if isinstance(events, list):
                self._log[name] = deque(events, maxlen=HISTORY_LIMIT)

    def save(self) -> None:
        """Сохранить журнал на диск (вызывать при graceful-остановке). Атомарно."""
        try:
            data = {name: list(dq) for name, dq in self._log.items() if dq}
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            os.replace(tmp, self._path)
        except OSError as e:
            logger.debug("save: %s", e)
