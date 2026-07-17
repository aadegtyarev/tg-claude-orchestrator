"""Точка входа tg-claude-orchestrator.

Собирает компоненты и запускает polling:
  config.py        — конфигурация из .env
  sessions.py      — SessionManager: процессы Claude Code
  bot.py           — TelegramBot: команды и пересылка сообщений
  reply_server.py  — HTTP /reply: ответы Claude -> Telegram
  channel_server.py — MCP-канал (запускается самим Claude, не отсюда)
"""

from __future__ import annotations

import asyncio
import logging

from bot import TelegramBot
from config import Config
from reply_server import start_reply_server
from sessions import SessionManager

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = Config.from_env()
    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Сессии: %s, максимум %d", config.sessions_dir, config.max_instances)

    if not config.allowed_user_ids:
        logger.warning(
            "ALLOWED_USER_IDS пуст — бот игнорирует ВСЕ сообщения. "
            "Добавь свой Telegram user id в .env, чтобы пользоваться ботом."
        )

    manager = SessionManager(config)
    manager.load_state()  # сессии с прошлого запуска: возобновятся по сообщению
    restored = manager.count()
    bot = TelegramBot(config, manager)
    manager.on_dead = bot.notify_session_dead

    reply_runner = await start_reply_server(
        config,
        bot.handle_reply,
        bot.handle_tool_event,
        bot.handle_permission_request,
    )

    sweeper = asyncio.create_task(_idle_sweeper(config, manager, bot))

    try:
        # Стартовое уведомление после короткой паузы (бот должен подняться).
        asyncio.get_running_loop().call_later(
            2, lambda: asyncio.ensure_future(bot.notify_startup(restored))
        )
        # aiogram сам обрабатывает SIGINT/SIGTERM и корректно выходит.
        await bot.start_polling()
    finally:
        logger.info("Останавливаю сессии (записи сохраняются)…")
        sweeper.cancel()
        await manager.shutdown()
        await reply_runner.cleanup()
        await bot.close()
        logger.info("Готово.")


async def _idle_sweeper(config, manager, bot) -> None:
    """№2: периодически останавливает сессии, простаивавшие дольше лимита."""
    if config.idle_timeout_h <= 0:
        return
    while True:
        await asyncio.sleep(600)  # проверка раз в 10 минут
        try:
            closed = await manager.close_idle()
            if closed:
                await bot.notify_idle_closed(closed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка sweeper'а простоя")


if __name__ == "__main__":
    asyncio.run(main())
