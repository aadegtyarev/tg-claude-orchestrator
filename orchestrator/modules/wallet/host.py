"""OrchestratorVaultHost — реализация vault.host.VaultHost поверх ядра
оркестратора: подтверждение = кнопки (permission-relay), наблюдаемость = строка
в статус-бабл, аудит = core._record, уведомление = notice в чат, cwd = проект
сессии.

Демон vault обращается по ИМЕНИ сессии; адаптер резолвит имя в Session через
manager.get (сессия могла быть удалена после выдачи токена — тогда мягкая
деградация: confirm→False, остальное тихо пропускается). i18n текста notice
живёт здесь, а не в vault.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class OrchestratorVaultHost:
    """VaultHost на ядре оркестратора. Один экземпляр на модуль кошелька."""

    def __init__(self, core):
        self._core = core

    async def confirm(self, session_name: str, description: str, preview: str) -> bool:
        session = self._core.manager.get(session_name)
        if session is None:
            return False
        return await self._core.request_confirmation(
            session, tool="wallet", description=description, preview=preview,
        )

    async def ask(self, session_name: str, description: str, preview: str) -> bool:
        # ЗАГЛУШКА (фаза 2 ASK-flow, vault-сторона): рендер кнопок гранта в
        # Telegram — следующий (оркестраторный) срез. Пока безопасный дефолт DENY,
        # чтобы не пропустить ASK без подтверждения (Р0) и не сломать сборку.
        # TODO(ASK-flow orchestrator): поднять кнопки гранта (эфемерный/persist)
        # через permission-relay, вернуть вердикт оператора.
        logger.info(
            "wallet ask: [%s] %s — ask пока не поддержан (DENY; рендер кнопок в "
            "след. срезе)", session_name, description,
        )
        return False

    async def observe(self, session_name: str, line_html: str) -> None:
        # append_background адресуется по имени сессии — резолв не нужен.
        await self._core.bubbles.append_background(session_name, line_html, tool="wallet")

    def record(self, session_name: str, *, secret: str, cmd: str, allowed: bool) -> None:
        session = self._core.manager.get(session_name)
        if session is None:
            return
        self._core._record(session, "wallet", secret=secret, cmd=cmd, allowed=allowed)

    async def notify_denied(self, session_name: str, cmd_display: str) -> None:
        session = self._core.manager.get(session_name)
        if session is None:
            return
        notice_md = f"🔐 wallet: `{cmd_display.replace('`', chr(39))}`"
        await self._core.notice(
            session,
            self._core.t("wallet_use", line=notice_md) + " — " + self._core.t("wallet_denied"),
        )
