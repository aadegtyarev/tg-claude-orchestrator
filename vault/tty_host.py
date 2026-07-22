"""TtyVaultHost — реализация VaultHost для ЗАПУСКА БЕЗ ОРКЕСТРАТОРА (standalone
`vault serve`). Никакого Telegram/бабла: подтверждение спрашивается на tty,
наблюдаемость и аудит идут в stderr-лог.

Симметрично OrchestratorVaultHost (кнопки/бабл/notice), но для локального
человека за терминалом. Часть автономного пакета vault/ — оркестратор не нужен.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

logger = logging.getLogger("vault.tty")

_TAG = re.compile(r"<[^>]+>")  # снять HTML-разметку из observe-строк для tty


class TtyVaultHost:
    """Standalone-хост: confirm через tty, observe/record/notify — в stderr-лог.

    assume_yes=True — не спрашивать, всё подтверждать (неинтерактивный режим,
    напр. CI/скрипт). По умолчанию: есть tty → спросить; нет tty → отказать
    (безопасная сторона — не подтверждаем вслепую)."""

    def __init__(self, *, assume_yes: bool = False, log: logging.Logger | None = None) -> None:
        self.assume_yes = assume_yes
        self.log = log or logger
        # Одна tty на весь демон → confirm'ы сериализуем: без этого конкурентные
        # add_reader на один fd затирали бы колбэк друг друга и более ранний
        # запрос висел бы вечно (нашло ревью 1.5).
        self._confirm_lock = asyncio.Lock()

    async def confirm(self, session_name: str, description: str, preview: str) -> bool:
        if self.assume_yes:
            return True
        if not sys.stdin.isatty():
            self.log.warning(
                "confirm без tty и без --yes → отказ: %s", description)
            return False
        try:
            # По одному вопросу за раз (одна tty). Читаем stdin через add_reader
            # (selectors), а НЕ input() в потоке: поток в input() нельзя отменить,
            # и asyncio.run на выходе ждал бы его вечно — SIGINT посреди confirm
            # вешал бы `vault serve`. Здесь чтение async и отменяемо: отмена
            # хендлера (shutdown) или ожидания лока → безопасный отказ.
            async with self._confirm_lock:
                sys.stderr.write(f"vault: подтвердить «{description}»?\n  {preview}\n[y/N] ")
                sys.stderr.flush()
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[str] = loop.create_future()
                fd = sys.stdin.fileno()

                def _readable() -> None:
                    if fut.done():
                        return
                    try:
                        data = os.read(fd, 4096)  # tty канонический: строка готова
                    except OSError:
                        data = b""
                    fut.set_result(data.decode("utf-8", "replace"))

                loop.add_reader(fd, _readable)
                try:
                    ans = await fut
                finally:
                    loop.remove_reader(fd)
            return ans.strip().lower() in ("y", "yes", "д", "да")
        except asyncio.CancelledError:
            return False

    async def observe(self, session_name: str, line_html: str) -> None:
        self.log.info("[%s] %s", session_name, _TAG.sub("", line_html))

    def record(self, session_name: str, *, secret: str, cmd: str, allowed: bool) -> None:
        self.log.info("audit [%s] %s: %s → %s", session_name, secret, cmd,
                      "allowed" if allowed else "denied")

    async def notify_denied(self, session_name: str, cmd_display: str) -> None:
        self.log.warning("[%s] ОТКАЗ: %s", session_name, cmd_display)


__all__ = ["TtyVaultHost"]
