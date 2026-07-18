"""Статус-бабл: редактируемое сообщение (или цепочка сообщений) в топике с
ходом работы Claude.

Строки (🔧 инструменты, 🤖 сабагенты, 💬 промежуточные ответы) дописываются по
мере событий; сообщение редактируется на месте с троттлингом. Подряд идущие
одинаковые вызовы одного тула (тем же агентом) схлопываются в одну строку
«N× 🔧 Tool последняя-деталь» — самих по себе вызовов инструментов пользователю
не важно видеть все, важен факт «что-то происходит» и последнее состояние.
Тулы сабагента (agent_id из PreToolUse-payload) рендерятся с отступом —
визуально отделены от главного потока.

Если пользователь шлёт следующее сообщение до ответа модели (сессия ещё
работает над предыдущим), старый бабл ЗАМОРАЖИВАЕТСЯ на месте (кнопка «Стоп»
снимается, дальше не редактируется), а новый открывается независимо и копит
уже новые события с нуля — история в чате остаётся линейной, ничего не прыгает
(в отличие от прежнего fork(), который удалял старое сообщение и создавал
новое — а на короткой дистанции это ещё и гонка: см. freeze_and_open).
Когда модель отвечает complete=True — close() разом убирает (удаляет/оставляет
журналом, DELETE_BUBBLE) все замороженные сообщения этого диалогового цикла
плюс текущий активный бабл.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import cbdata

logger = logging.getLogger(__name__)

# Лимит текста бабла и минимальный интервал редактирования сообщения.
TEXT_LIMIT = 3800
EDIT_INTERVAL = 1.5
# Отступ для строк, приписанных сабагенту (agent_id задан) — визуально
# отделяет их от тулов главного потока.
AGENT_INDENT = "  ↳ "


@dataclass
class BubbleLine:
    """Одна визуальная строка бабла — возможно, схлопнутая серия подряд идущих
    одинаковых (tool, agent_id) вызовов; html хранит ПОСЛЕДНИЙ вызов серии."""

    html: str
    agent_id: str | None = None  # None = главный поток; иначе — id сабагента
    tool: str | None = None  # имя тула для схлопывания; None = не схлопывается
    count: int = 1

    def render(self) -> str:
        prefix = AGENT_INDENT if self.agent_id else ""
        count = f"{self.count}× " if self.count > 1 else ""
        return f"{prefix}{count}{self.html}"


@dataclass
class Bubble:
    """Состояние бабла одной сессии."""

    message_id: int | None = None
    entries: list[BubbleLine] = field(default_factory=list)
    sent_text: str = ""
    flush_task: asyncio.Task | None = None
    # Момент последнего добавления строки — метка «когда обновлено» в конце
    # списка. Замораживаем, чтобы бабл не перепечатывался каждую секунду:
    # время обновляется только на реальной активности (новая строка).
    updated_at: float = field(default_factory=time.time)


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
        self._bubbles: dict[int, Bubble] = {}  # thread_id -> активный Bubble
        # thread_id -> замороженные Bubble этого диалогового цикла (см. модуль
        # docstring) — убираются все разом на финальном close().
        self._frozen: dict[int, list[Bubble]] = {}
        self._active: set[int] = set()  # топики, где сейчас идёт ход Claude

    def has(self, thread_id: int) -> bool:
        """Есть ли активный бабл (используется как признак «работает»)."""
        return thread_id in self._bubbles

    def open(self, thread_id: int) -> None:
        """Начало хода: с этого момента append создаёт/наполняет бабл."""
        self._active.add(thread_id)

    async def append(
        self,
        thread_id: int,
        html: str,
        *,
        agent_id: str | None = None,
        tool: str | None = None,
    ) -> None:
        """Добавить строку (или схлопнуть с предыдущей, если tool задан и
        совпадает с последней строкой по (tool, agent_id) — см. BubbleLine).

        tool=None — строка никогда не схлопывается (📨 сообщение юзера, 💬 стоп
        запрошен и т.п.), agent_id=None — главный поток (без отступа).
        """
        # Событие после финала (запоздавший хук, «Стоп») не должно рождать
        # бабл-сироту — принимаем только внутри активного хода.
        if thread_id not in self._active:
            return
        bubble = self._bubbles.setdefault(thread_id, Bubble())
        last = bubble.entries[-1] if bubble.entries else None
        if tool is not None and last is not None and last.tool == tool and last.agent_id == agent_id:
            last.html = html
            last.count += 1
        else:
            bubble.entries.append(BubbleLine(html=html, agent_id=agent_id, tool=tool))
        bubble.updated_at = time.time()
        # Переполнение: вытесняем старые строки с начала.
        while len(bubble.entries) > 1 and len(self._render_text(bubble)) > TEXT_LIMIT:
            bubble.entries.pop(0)
        if bubble.flush_task is None or bubble.flush_task.done():
            bubble.flush_task = asyncio.create_task(self._flush(thread_id))

    def _render_text(self, bubble: Bubble) -> str:
        updated = time.strftime("%H:%M:%S", time.localtime(bubble.updated_at))
        text = f"<b>{self._t('bubble_working')}</b>\n" + "\n".join(
            e.render() for e in bubble.entries
        )
        if bubble.entries:
            text += f"\n🕐 {updated}"
        return text

    async def _flush(self, thread_id: int) -> None:
        # Коалесцируем всплеск событий в одну правку сообщения.
        await asyncio.sleep(EDIT_INTERVAL)
        bubble = self._bubbles.get(thread_id)
        chat_id = self._get_chat_id()
        if bubble is None or not bubble.entries or chat_id is None:
            return
        text = self._render_text(bubble)
        if text == bubble.sent_text:
            return
        # reply_markup нужен и при edit — иначе Telegram снимает кнопку.
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=self._t("bubble_stop"), callback_data=cbdata.stop_cb(thread_id)
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

    async def _await_flush(self, bubble: Bubble) -> None:
        """Дождаться отложенной правки (или отправки), если она в очереди —
        общий шаг перед freeze/finish, чтобы не унести устаревший текст."""
        if bubble.flush_task is not None and not bubble.flush_task.done():
            try:
                await asyncio.wait_for(bubble.flush_task, timeout=5)
            except Exception:
                pass

    async def _finish_message(self, bubble: Bubble, chat_id: int) -> None:
        """Закрыть сообщение бабла: дождаться flush, удалить/оставить журналом."""
        await self._await_flush(bubble)
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
            logger.debug("Не удалось закрыть бабл: %s", e)

    async def _freeze_message(self, bubble: Bubble, chat_id: int) -> None:
        """Заморозить сообщение на месте: дождаться последней правки, снять
        кнопку «Стоп» (её контекст — устаревший ход), само сообщение НЕ
        трогать (ни удалять, ни редактировать дальше) — история остаётся
        линейной. Удаление/журнал — только на финальном close()."""
        await self._await_flush(bubble)
        if bubble.message_id is None:
            return
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=bubble.message_id, reply_markup=None
            )
        except Exception as e:
            logger.debug("Не удалось заморозить бабл: %s", e)

    async def close(self, thread_id: int) -> None:
        """Конец диалогового цикла (complete=True): разом убрать текущий
        активный бабл и все замороженные сообщения, накопленные с начала
        цикла (см. freeze_and_open) — «групповое схлопывание на финале»."""
        self._active.discard(thread_id)  # ход завершён — append больше не создаёт бабл
        bubble = self._bubbles.pop(thread_id, None)
        frozen = self._frozen.pop(thread_id, [])
        chat_id = self._get_chat_id()
        if chat_id is None:
            return
        for old in frozen:
            await self._finish_message(old, chat_id)
        if bubble is not None:
            await self._finish_message(bubble, chat_id)

    async def freeze_and_open(self, thread_id: int) -> None:
        """Пользователь шлёт новое сообщение, пока сессия ещё работает над
        предыдущим: заморозить текущий бабл на месте, открыть новый независимо.

        Новый Bubble ставится в self._bubbles СИНХРОННО, до единого await —
        поэтому окна, в котором tool-событие (handle_tool_event → append →
        setdefault) могло бы создать «паразитный» бабл и потерять его при
        последующей записи, физически не существует (в отличие от прежнего
        fork(), где new ставился ПОСЛЕ await старого close — гонка, найденная
        разбором живого инцидента: бабл пропал, ответ пришёл без индикации).
        """
        old = self._bubbles.get(thread_id)
        self._bubbles[thread_id] = Bubble()
        self.open(thread_id)
        if old is not None:
            self._frozen.setdefault(thread_id, []).append(old)
            chat_id = self._get_chat_id()
            if chat_id is not None:
                await self._freeze_message(old, chat_id)
