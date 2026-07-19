"""Статус-бабл: редактируемое сообщение (или цепочка сообщений) с ходом
работы Claude.

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
уже новые события с нуля — история в чате остаётся линейной, ничего не прыгает.
Когда модель отвечает complete=True — close() разом убирает (удаляет/оставляет
журналом, DELETE_BUBBLE) все замороженные сообщения этого диалогового цикла
плюс текущий активный бабл.

Состояние бабла (строки, схлопывание, троттлинг) — здесь, в ядре; доставка —
через Transport-адаптеры: у каждого адаптера своё сообщение-бабл (Bubble.refs),
ядро правит их все. Ключ — имя сессии.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from html import escape as html_escape
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .sessions import Session
    from .transport import Transport

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
    """Состояние бабла одной сессии (общее для всех адаптеров)."""

    # Ссылки на материализованные сообщения: имя адаптера -> ref адаптера.
    refs: dict[str, str] = field(default_factory=dict)
    entries: list[BubbleLine] = field(default_factory=list)
    # «Живой пульс» — последний спиннер-глагол Claude Code (Cogitating…),
    # обновляемая строка внизу бабла: видно, что модель жива, когда
    # tool-событий нет (думает / ждёт API / фоновая задача).
    pulse: str = ""
    # Реальная модель последнего ответа (после подмены прокси) — в заголовке.
    model: str = ""
    sent_text: str = ""
    flush_task: asyncio.Task | None = None
    # Момент последнего добавления строки — метка «когда обновлено» в конце
    # списка. Замораживаем, чтобы бабл не перепечатывался каждую секунду:
    # время обновляется только на реальной активности (новая строка).
    updated_at: float = field(default_factory=time.time)


class BubbleManager:
    def __init__(
        self,
        transports: Callable[[], "list[Transport]"],
        get_session: Callable[[str], "Session | None"],
        t: Callable[..., str],
        delete_after: bool,
        unblock_available: Callable[[str], bool] | None = None,
    ):
        self._transports = transports
        self._get_session = get_session
        self._t = t
        self._delete_after = delete_after
        # Можно ли сейчас свернуть задачу сессии в фон (Ctrl+B) — для активности
        # кнопки ⏬. None = всегда False (адаптер без поддержки).
        self._unblock_available = unblock_available or (lambda name: False)
        self._bubbles: dict[str, Bubble] = {}  # имя сессии -> активный Bubble
        # имя сессии -> замороженные Bubble этого диалогового цикла (см. модуль
        # docstring) — убираются все разом на финальном close().
        self._frozen: dict[str, list[Bubble]] = {}
        self._active: set[str] = set()  # сессии, где сейчас идёт ход Claude

    def has(self, name: str) -> bool:
        """Есть ли активный бабл (используется как признак «работает»)."""
        return name in self._bubbles

    def open(self, name: str) -> None:
        """Начало хода: с этого момента append создаёт/наполняет бабл."""
        self._active.add(name)

    async def append(
        self,
        name: str,
        html: str,
        *,
        agent_id: str | None = None,
        tool: str | None = None,
    ) -> None:
        """Добавить строку (или схлопнуть с предыдущей, если tool задан и
        совпадает с последней строкой по (tool, agent_id) — см. BubbleLine).

        tool=None — строка никогда не схлопывается (📨 сообщение юзера, ⏹ стоп
        запрошен и т.п.), agent_id=None — главный поток (без отступа).
        """
        # Событие после финала (запоздавший хук, «Стоп») не должно рождать
        # бабл-сироту — принимаем только внутри активного хода.
        if name not in self._active:
            return
        bubble = self._bubbles.setdefault(name, Bubble())
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
            bubble.flush_task = asyncio.create_task(self._flush(name))

    def _render_text(self, bubble: Bubble) -> str:
        updated = time.strftime("%H:%M:%S", time.localtime(bubble.updated_at))
        # Заголовок + реальная модель (после подмены прокси), если известна.
        head = self._t("bubble_working")
        if bubble.model:
            head += f" · {html_escape(bubble.model)}"
        text = f"<b>{head}</b>\n" + "\n".join(
            e.render() for e in bubble.entries
        )
        if bubble.pulse:
            # Спиннер-глагол внизу (перед временем) — «пульс жизни».
            sep = "\n" if bubble.entries else ""
            text += f"{sep}✻ {html_escape(bubble.pulse)}"
        if bubble.entries or bubble.pulse or bubble.model:
            text += f"\n🕐 {updated}"
        return text

    async def set_status(self, name: str, pulse: str = "", model: str = "") -> None:
        """Обновить живой статус сессии: спиннер-глагол (pulse) и/или реальную
        модель (model, после подмены прокси). Создаёт бабл, если его ещё нет
        (модель думает до первого тул-события). Только в активном ходе;
        неизменный статус не триггерит лишнюю правку."""
        if name not in self._active:
            return
        bubble = self._bubbles.setdefault(name, Bubble())
        changed = False
        if pulse and bubble.pulse != pulse:
            bubble.pulse = pulse
            changed = True
        if model and bubble.model != model:
            bubble.model = model
            changed = True
        if not changed:
            return
        bubble.updated_at = time.time()
        if bubble.flush_task is None or bubble.flush_task.done():
            bubble.flush_task = asyncio.create_task(self._flush(name))

    async def _flush(self, name: str) -> None:
        # Коалесцируем всплеск событий в одну правку сообщения.
        await asyncio.sleep(EDIT_INTERVAL)
        bubble = self._bubbles.get(name)
        session = self._get_session(name)
        # Рендерим при наличии строк ИЛИ пульса/модели (живой статус без тулов).
        if bubble is None or session is None or not (
            bubble.entries or bubble.pulse or bubble.model
        ):
            return
        text = self._render_text(bubble)
        if text == bubble.sent_text:
            return
        unblock = self._unblock_available(name)
        delivered = False
        for tr in self._transports():
            try:
                ref = bubble.refs.get(tr.name)
                if ref is None:
                    new_ref = await tr.bubble_post(
                        session, text, stop_button=True, unblock_active=unblock
                    )
                    if new_ref is not None:
                        bubble.refs[tr.name] = new_ref
                        delivered = True
                else:
                    await tr.bubble_edit(session, ref, text, stop_button=True, unblock_active=unblock)
                    delivered = True
            except Exception as e:
                logger.debug("Бабл (%s/%s): %s", name, tr.name, e)
        # sent_text фиксируем ТОЛЬКО если хоть один адаптер реально доставил:
        # иначе транзиентный сбой (429/сеть/chat_id ещё None) пометил бы текст
        # «отправленным», следующий flush вышел бы по text == sent_text, и бабл
        # навсегда завис бы на неотправленном состоянии (последний ход без
        # новых событий — вообще без бабла).
        if delivered:
            bubble.sent_text = text

    async def _await_flush(self, bubble: Bubble) -> None:
        """Дождаться отложенной правки (или отправки), если она в очереди —
        общий шаг перед freeze/finish, чтобы не унести устаревший текст."""
        if bubble.flush_task is not None and not bubble.flush_task.done():
            try:
                await asyncio.wait_for(bubble.flush_task, timeout=5)
            except Exception:
                pass

    async def _finish_message(self, bubble: Bubble, session: "Session") -> None:
        """Закрыть сообщения бабла: дождаться flush, удалить/оставить журналом."""
        await self._await_flush(bubble)
        for tr in self._transports():
            ref = bubble.refs.get(tr.name)
            if ref is None:
                continue
            try:
                await tr.bubble_finish(session, ref, delete=self._delete_after)
            except Exception as e:
                logger.debug("Не удалось закрыть бабл (%s): %s", tr.name, e)

    async def _freeze_message(self, bubble: Bubble, session: "Session") -> None:
        """Заморозить сообщения на месте: дождаться последней правки, снять
        кнопку «Стоп» (её контекст — устаревший ход), сами сообщения НЕ
        трогать (ни удалять, ни редактировать дальше) — история остаётся
        линейной. Удаление/журнал — только на финальном close()."""
        await self._await_flush(bubble)
        for tr in self._transports():
            ref = bubble.refs.get(tr.name)
            if ref is None:
                continue
            try:
                await tr.bubble_freeze(session, ref)
            except Exception as e:
                logger.debug("Не удалось заморозить бабл (%s): %s", tr.name, e)

    async def close(self, name: str) -> None:
        """Конец диалогового цикла (complete=True): разом убрать текущий
        активный бабл и все замороженные сообщения, накопленные с начала
        цикла (см. freeze_and_open) — «групповое схлопывание на финале»."""
        self._active.discard(name)  # ход завершён — append больше не создаёт бабл
        bubble = self._bubbles.pop(name, None)
        frozen = self._frozen.pop(name, [])
        session = self._get_session(name)
        if session is None:
            return
        for old in frozen:
            await self._finish_message(old, session)
        if bubble is not None:
            await self._finish_message(bubble, session)

    async def freeze_and_open(self, name: str) -> None:
        """Пользователь шлёт новое сообщение, пока сессия ещё работает над
        предыдущим: заморозить текущий бабл на месте, открыть новый независимо.

        Новый Bubble ставится в self._bubbles СИНХРОННО, до единого await —
        поэтому окна, в котором tool-событие (handle_tool_event → append →
        setdefault) могло бы создать «паразитный» бабл и потерять его при
        последующей записи, физически не существует (гонка, найденная разбором
        живого инцидента: бабл пропал, ответ пришёл без индикации).
        """
        old = self._bubbles.get(name)
        self._bubbles[name] = Bubble()
        self.open(name)
        if old is not None:
            self._frozen.setdefault(name, []).append(old)
            session = self._get_session(name)
            if session is not None:
                await self._freeze_message(old, session)
