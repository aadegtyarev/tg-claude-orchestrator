"""Статус-бабл: одно редактируемое сообщение в топике с ходом работы Claude.

Строки (🔧 инструменты, 🤖 сабагенты, 💬 промежуточные ответы) дописываются
по мере событий; сообщение редактируется на месте с троттлингом. После
финального ответа бабл удаляется (или остаётся журналом — DELETE_BUBBLE=false).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# Лимит текста бабла и минимальный интервал редактирования сообщения.
TEXT_LIMIT = 3800
EDIT_INTERVAL = 1.5


@dataclass
class Bubble:
    """Состояние бабла одной сессии."""

    message_id: int | None = None
    lines: list[str] = field(default_factory=list)
    sent_text: str = ""
    flush_task: asyncio.Task | None = None


class BubbleManager:
    def __init__(
        self,
        bot: Bot,
        get_chat_id: Callable[[], int | None],
        t: Callable[..., str],
        delete_after: bool,
    ):
        self._bot = bot
        self._get_chat_id = get_chat_id
        self._t = t
        self._delete_after = delete_after
        self._bubbles: dict[int, Bubble] = {}  # thread_id -> Bubble
        self._active: set[int] = set()  # топики, где сейчас идёт ход Claude

    def has(self, thread_id: int) -> bool:
        """Есть ли активный бабл (используется как признак «работает»)."""
        return thread_id in self._bubbles

    def open(self, thread_id: int) -> None:
        """Начало хода: с этого момента append создаёт/наполняет бабл."""
        self._active.add(thread_id)

    async def append(self, thread_id: int, line: str) -> None:
        # Событие после финала (запоздавший хук, «Стоп») не должно рождать
        # бабл-сироту — принимаем только внутри активного хода.
        if thread_id not in self._active:
            return
        bubble = self._bubbles.setdefault(thread_id, Bubble())
        bubble.lines.append(line)
        # Переполнение: вытесняем старые строки с начала.
        while len(bubble.lines) > 1 and len("\n".join(bubble.lines)) > TEXT_LIMIT:
            bubble.lines.pop(0)
        if bubble.flush_task is None or bubble.flush_task.done():
            bubble.flush_task = asyncio.create_task(self._flush(thread_id))

    async def _flush(self, thread_id: int) -> None:
        # Коалесцируем всплеск событий в одну правку сообщения.
        await asyncio.sleep(EDIT_INTERVAL)
        bubble = self._bubbles.get(thread_id)
        chat_id = self._get_chat_id()
        if bubble is None or not bubble.lines or chat_id is None:
            return
        text = f"<b>{self._t('bubble_working')}</b>\n" + "\n".join(bubble.lines)
        if text == bubble.sent_text:
            return
        # reply_markup нужен и при edit — иначе Telegram снимает кнопку.
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=self._t("bubble_stop"), callback_data=f"stop:{thread_id}"
            )
        ]])
        try:
            if bubble.message_id is None:
                msg = await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    message_thread_id=thread_id,
                    disable_notification=True,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
                bubble.message_id = msg.message_id
            else:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=bubble.message_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
            # Фиксируем только после успешной отправки — иначе бабл
            # «залипнет» на неотправленном тексте.
            bubble.sent_text = text
        except Exception as e:
            logger.debug("Бабл (топик %s): %s", thread_id, e)

    async def close(self, thread_id: int) -> None:
        self._active.discard(thread_id)  # ход завершён — append больше не создаёт бабл
        bubble = self._bubbles.pop(thread_id, None)
        chat_id = self._get_chat_id()
        if bubble is None or chat_id is None:
            return
        # Даём начатому flush завершиться, чтобы не осиротить только что
        # отправленное сообщение (wait_for сам отменит задачу по таймауту).
        if bubble.flush_task is not None and not bubble.flush_task.done():
            try:
                await asyncio.wait_for(bubble.flush_task, timeout=5)
            except Exception:
                pass
        if bubble.message_id is None:
            return
        try:
            if self._delete_after:
                await self._bot.delete_message(chat_id=chat_id, message_id=bubble.message_id)
            else:
                # Бабл остаётся как журнал работы — снимаем только кнопку Стоп.
                await self._bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=bubble.message_id, reply_markup=None
                )
        except Exception as e:
            logger.debug("Не удалось закрыть бабл (топик %s): %s", thread_id, e)
