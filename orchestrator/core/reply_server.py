"""HTTP-сервер оркестратора.

POST /reply         — ответы Claude (тул reply_to_user через channel_server)
POST /event/{name}  — события PreToolUse-хука Claude Code (вызовы инструментов)
POST /stop/{name}   — конец хода (Stop-хук) — фолбэк на «потерянный финал»
"""

from __future__ import annotations

import logging
import secrets
from typing import Awaitable, Callable

from aiohttp import web

from ..config import Config

logger = logging.getLogger(__name__)

ReplyHandler = Callable[[dict], Awaitable[None]]
NamedHandler = Callable[[str, dict], Awaitable[None]]


async def start_reply_server(
    config: Config,
    reply_handler: ReplyHandler,
    tool_event_handler: NamedHandler,
    permission_handler: NamedHandler,
    stop_handler: NamedHandler,
) -> web.AppRunner:
    """Поднять сервер; вернуть runner (закрывать через runner.cleanup())."""

    # Один общий секрет на все эндпоинты: внутренний API живёт на 127.0.0.1,
    # но без токена любой локальный процесс (и вкладка браузера через DNS
    # rebinding) мог бы POST /reply с file_path и выгрузить任意 файл в чат
    # (REVIEW.md S1). Канал-сервер и curl-хук пробрасывают тот же токен.
    expected_auth = f"Bearer {config.orch_token}".encode()

    @web.middleware
    async def _auth(request: web.Request, handler):
        sent = request.headers.get("Authorization", "").encode("utf-8", "replace")
        # constant-time сравнение; на байтах — безопасно при любом (в т.ч.
        # не-ASCII) вводе, без TypeError.
        if not secrets.compare_digest(sent, expected_auth):
            return web.Response(status=401, text="unauthorized")
        return await handler(request)

    async def reply(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return web.Response(status=400, text="invalid json")
        try:
            await reply_handler(data)
        except Exception:
            logger.exception("Ошибка обработки reply")
            return web.Response(status=500, text="handler error")
        return web.Response(text="OK")

    async def tool_event(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except ValueError:
            return web.Response(status=400, text="invalid json")
        try:
            await tool_event_handler(request.match_info["name"], payload)
        except Exception:
            # Хук не должен мешать Claude — ошибку только логируем.
            logger.exception("Ошибка обработки tool-события")
        return web.Response(text="OK")

    async def permission(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except ValueError:
            return web.Response(status=400, text="invalid json")
        try:
            await permission_handler(request.match_info["name"], payload)
        except Exception:
            logger.exception("Ошибка обработки permission_request")
            return web.Response(status=500, text="handler error")
        return web.Response(text="OK")

    async def stop_event(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except ValueError:
            return web.Response(status=400, text="invalid json")
        try:
            await stop_handler(request.match_info["name"], payload)
        except Exception:
            # Хук не должен мешать Claude — ошибку только логируем (как /event).
            logger.exception("Ошибка обработки Stop-события")
        return web.Response(text="OK")

    app = web.Application(middlewares=[_auth])
    app.router.add_post("/reply", reply)
    app.router.add_post("/event/{name}", tool_event)
    app.router.add_post("/permission/{name}", permission)
    app.router.add_post("/stop/{name}", stop_event)

    runner = web.AppRunner(app)
    await runner.setup()
    # Слушаем на ORCH_HOST, а не хардкод 127.0.0.1: под agent-vm канал/хуки
    # из гостя достучатся до хоста только если сервер слушает на адресе
    # host-gateway (для bwrap/локального режима ORCH_HOST=127.0.0.1 как раньше).
    site = web.TCPSite(runner, host=config.orch_host, port=config.orch_port)
    await site.start()
    logger.info("Reply-сервер слушает %s:%d", config.orch_host, config.orch_port)
    return runner
