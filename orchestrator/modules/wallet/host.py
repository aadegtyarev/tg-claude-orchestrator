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

# Таймаут спроса гранта у оператора. Держим ЗАВЕДОМО ниже страховочного
# потолка прокси (vault.proxy._ASK_TIMEOUT = 180с), чтобы наш собственный
# таймаут сработал первым: request_confirmation по истечении гасит кнопки во
# всех адаптерах (иначе прокси уже вернул бы DENY, а «висящие» ✅/❌ вводили бы
# оператора в заблуждение). Оператор не ответил → False (DENY, Р0).
_ASK_CONFIRM_TIMEOUT = 150.0


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
        """Спрос ГРАНТа доступа ВНЕ scope (§4.6 ASK-flow). Рендер — те же кнопки
        permission-relay, что и confirm, но с текстом, явно маркирующим ЗАПРОС
        РАСШИРЕНИЯ доступа (🔓 «доступ ВНЕ scope»), чтобы оператор не спутал его
        со штатным подтверждением команды под секретом. Грант — РАЗОВЫЙ/эфемерный:
        просто возвращаем вердикт оператора на ЭТОТ запрос; policy не пишем и scope
        не расширяем.

        description — от коннектора (что за ресурс/почему вне scope); preview —
        факт запроса (метод+URL), куда уйдёт кред. Значение секрета сюда НЕ
        приходит и в текст не попадает. Сессия удалена → False (мягкая деградация,
        как confirm). Таймаут (оператор молчит) → False (Р0).

        TODO(ASK-flow persist): «расширить scope навсегда/на папку» — отдельный
        UX-срез (третья кнопка + запись в policy); здесь сознательно НЕ делаем.
        """
        session = self._core.manager.get(session_name)
        if session is None:
            return False
        return await self._core.request_confirmation(
            session,
            tool=self._core.t("wallet_ask_tool"),
            description=self._core.t("wallet_ask_desc", description=description),
            preview=preview,
            timeout=_ASK_CONFIRM_TIMEOUT,
        )

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
