"""Permission relay: кнопки «разрешить?» во все адаптеры + сбор ПЕРВОГО ответа.

Две модели запроса:
  * ОТ Claude Code (`request_from_claude`) — вердикт уходит обратно в Claude через
    `manager.send_permission`; параллельно открыт и локальный TUI-диалог, поэтому
    применяется ПЕРВЫЙ ответ (любой адаптер ИЛИ сам TUI). «Первый побеждает»
    обеспечивается claim'ом ключа из `_pending` ДО await отправки вердикта.
  * локальное подтверждение модулей (`request_confirmation`, напр. wallet) —
    вердикт остаётся в ядре (Future), в Claude Code НЕ уходит.

Владеет ожидающими запросами и снимает их `forget(session)` на границе хода/
teardown: иначе `_pending` растёт вечно, а старая кнопка после resume ударила бы
по новому процессу с несуществующим request_id; плюс гасит кнопки в адаптерах.

Коллаборатор ядра: `manager` (send_permission), `t` (тексты), `each_transport`
(бродкаст в адаптеры) и `record` (журнал событий) приходят инъекцией — сам класс
в app.py не смотрит (иначе цикл через god-object).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Awaitable, Callable

from .errors import UserError
from .transport import PermissionRequest

if TYPE_CHECKING:
    from .sessions import Session, SessionManager

logger = logging.getLogger(__name__)

# Потолок предпросмотра ввода инструмента в кнопке-запросе (символы).
PERM_PREVIEW_LIMIT = 3500


class PermissionRelay:
    """Состояние и логика запросов разрешений на сессию.

    Владеет:
      * `_pending` — {(имя_сессии, request_id)} запросов ОТ Claude, ждущих ответа;
      * `_local`   — {(имя_сессии, request_id): Future} локальных подтверждений.

    Инвариант: всё состояние сессии снимается одним `forget(session)`.
    """

    def __init__(
        self,
        manager: "SessionManager",
        t: Callable[..., str],
        each_transport: Callable[..., Awaitable[None]],
        record: Callable[..., None],
    ) -> None:
        self.manager = manager
        self.t = t
        self._each_transport = each_transport
        self._record = record
        self._pending: set[tuple[str, str]] = set()
        self._local: dict[tuple[str, str], asyncio.Future] = {}

    async def request_from_claude(self, session: "Session", payload: dict) -> None:
        """Запрос разрешения ОТ Claude Code — кнопками во все адаптеры; применяется
        первый ответ (параллельно остаётся открытым и локальный TUI-диалог)."""
        raw_preview = str(payload.get("input_preview", ""))
        if len(raw_preview) > PERM_PREVIEW_LIMIT:
            raw_preview = raw_preview[:PERM_PREVIEW_LIMIT] + " …(обрезано)"
        request = PermissionRequest(
            request_id=str(payload.get("request_id", "")),
            tool=str(payload.get("tool_name", "?")),
            description=str(payload.get("description", "")),
            preview=raw_preview,
        )
        self._pending.add((session.name, request.request_id))
        self._record(
            session, "perm_request",
            request_id=request.request_id, tool=request.tool,
            description=request.description, preview=request.preview,
        )
        await self._each_transport(
            lambda tr: tr.permission_prompt(session, request), "permission_prompt"
        )

    async def request_confirmation(
        self,
        session: "Session",
        tool: str,
        description: str,
        preview: str,
        timeout: float = 300.0,
    ) -> bool:
        """Спросить пользователя «разрешить?» кнопками во всех адаптерах и дождаться
        ответа (для модулей: wallet и т.п. — вердикт остаётся в ядре, в Claude Code
        не уходит). Таймаут/ошибка = отказ (deny по умолчанию)."""
        request_id = f"local-{uuid.uuid4().hex[:12]}"
        key = (session.name, request_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._local[key] = fut
        request = PermissionRequest(
            request_id=request_id, tool=tool, description=description,
            preview=preview[:PERM_PREVIEW_LIMIT],
        )
        self._record(
            session, "perm_request",
            request_id=request_id, tool=tool, description=description,
            preview=request.preview,
        )
        await self._each_transport(
            lambda tr: tr.permission_prompt(session, request), "permission_prompt (local)"
        )
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # Истёк без ответа: гасим кнопки во всех адаптерах, иначе они висят
            # вечно, а поздний клик потом молча проваливается.
            await self._broadcast_resolved(session, request_id, "deny", "timeout")
            return False
        finally:
            self._local.pop(key, None)

    async def _broadcast_resolved(
        self, session: "Session", request_id: str, behavior: str, via: str
    ) -> None:
        self._record(session, "perm_resolved", request_id=request_id, behavior=behavior)
        await self._each_transport(
            lambda tr: tr.permission_resolved(session, request_id, behavior, via),
            "permission_resolved",
        )

    async def verdict(
        self, session: "Session", request_id: str, behavior: str, via: str
    ) -> bool:
        """Вердикт из адаптера `via`. False — запрос уже разрешён/снят (адаптеру
        стоит просто убрать кнопки: см. Transport.permission_resolved)."""
        key = (session.name, request_id)
        # Локальное подтверждение (request_confirmation): будим ожидающего, в
        # Claude Code ничего не шлём.
        local = self._local.get(key)
        if local is not None:
            if local.done():
                return False  # уже отвечено/истекло — повторный клик игнорируем
            local.set_result(behavior == "allow")
            await self._broadcast_resolved(session, request_id, behavior, via)
            return True
        if key not in self._pending:
            return False
        # Claim ДО await: иначе второй клик (другой адаптер / дабл-клик), пришедший
        # во время send_permission, пройдёт membership-check и отправит ВТОРОЙ
        # вердикт (allow и deny наперегонки). Discard сейчас — гарантия «первый
        # ответ побеждает»; при ошибке отправки возвращаем ключ для повтора.
        self._pending.discard(key)
        try:
            await self.manager.send_permission(session, request_id, behavior)
        except Exception as e:
            self._pending.add(key)
            logger.error("Сессия %s: не удалось передать вердикт: %s", session.name, e)
            raise UserError(self.t("perm_fail", error=e)) from e
        await self._broadcast_resolved(session, request_id, behavior, via)
        return True

    async def forget(self, session: "Session") -> None:
        """Снять все ожидающие запросы разрешений сессии (close/clear/delete/idle):
        иначе `_pending` растёт вечно, а старая кнопка после resume била бы по
        чужому (новому) процессу с несуществующим request_id. Плюс ГАСИМ кнопки в
        адаптерах (broadcast resolved) — иначе ✅/❌ висят навсегда."""
        stale = [k for k in self._pending if k[0] == session.name]
        for k in stale:
            self._pending.discard(k)
            await self._broadcast_resolved(session, k[1], "deny", "cancelled")
        for k, fut in list(self._local.items()):
            if k[0] == session.name and not fut.done():
                fut.set_result(False)  # разбудить ожидающего request_confirmation
