"""VaultHost — интерфейс окружения, которое даёт демону vault его «внешние»
услуги: подтверждение, наблюдаемость, аудит, уведомление об отказе, рабочий
каталог сессии. Демон говорит ИМЕНАМИ сессий (str) и НЕ знает про Session/бота/
бабл/Telegram — их знает реализация host.

Реализации:
  * оркестратор — кнопки подтверждения, строка в статус-бабл, аудит-хук, notice
    в чат (orchestrator/modules/wallet/host.py);
  * standalone-лончер (позже) — tty-вопрос/deny, лог, cwd из аргументов.

Без зависимостей оркестратора: чистый Protocol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class VaultHost(Protocol):
    """Услуги окружения для демона vault (по имени сессии).

    Реализация сама резолвит сессию и решает, как доставить действие; если
    сессия уже недоступна (удалена) — confirm возвращает False (deny),
    наблюдаемость/аудит/уведомление тихо пропускаются.
    """

    async def confirm(self, session_name: str, description: str, preview: str) -> bool:
        """Спросить подтверждение перед выполнением. Оркестратор — кнопки;
        standalone — tty/deny. True = разрешено, False = отказано/некому спросить."""
        ...

    async def observe(self, session_name: str, line_html: str) -> None:
        """Наблюдаемость: показать факт вызова (НЕ значение секрета). Оркестратор —
        строка в статус-бабл, standalone — лог."""
        ...

    def record(self, session_name: str, *, secret: str, cmd: str, allowed: bool) -> None:
        """Аудит попытки (без значения секрета)."""
        ...

    async def notify_denied(self, session_name: str, cmd_display: str) -> None:
        """Уведомить оператора об отказе, требующем внимания (не self-correcting).
        Вызывающий сам решает, звать ли (напр. печать токена не уведомляет)."""
        ...

    def cwd_for(self, session_name: str) -> Path:
        """Рабочий каталог для исполнения команды сессии (cwd проекта)."""
        ...
