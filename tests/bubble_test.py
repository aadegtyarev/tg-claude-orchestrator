"""Регрессия статус-бабла: гейт активности (событие после close не рождает
бабл-сироту, новый ход снова работает), схлопывание однотипных подряд идущих
тулов, отступ для агентских (agent_id), заморозка вместо fork (линейная
история без гонки — см. bubble.freeze_and_open docstring). Без сети и Telegram.

Запуск: .venv/bin/python tests/bubble_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.bubble import BubbleManager  # noqa: E402

_TEXTS = {"bubble_working": "Работаю", "bubble_stop": "Стоп"}


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []  # история send_message/edit_message_text
        self.deleted: list[int] = []
        self.markup_cleared: list[int] = []
        self._next_id = 1

    async def send_message(self, **k):
        mid = self._next_id
        self._next_id += 1
        self.sent.append({"message_id": mid, **k})
        return SimpleNamespace(message_id=mid)

    async def edit_message_text(self, **k):
        self.sent.append(k)

    async def delete_message(self, message_id, **k):
        self.deleted.append(message_id)

    async def edit_message_reply_markup(self, message_id, reply_markup=None, **k):
        if reply_markup is None:
            self.markup_cleared.append(message_id)


async def _settle(bm: BubbleManager, thread_id: int) -> None:
    """Дождаться отложенного flush конкретного бабла (без грубого sleep(2))."""
    bubble = bm._bubbles.get(thread_id)
    if bubble is not None and bubble.flush_task is not None:
        await asyncio.wait_for(bubble.flush_task, timeout=5)


async def main():
    fake_bot = FakeBot()
    bm = BubbleManager(fake_bot, lambda: -100, lambda k, **kw: _TEXTS[k], delete_after=True)

    # append без open — сироты быть не должно
    await bm.append(7, "🔧 late")
    await asyncio.sleep(2)
    assert not bm.has(7)
    print("OK append without open ignored")

    # активный ход
    bm.open(7)
    await bm.append(7, "🔧 Bash: ls")
    await asyncio.sleep(2)
    assert bm.has(7)
    print("OK bubble created during active turn")

    # событие после close — не сирота
    await bm.close(7)
    assert not bm.has(7)
    await bm.append(7, "🔧 late hook")
    await asyncio.sleep(2)
    assert not bm.has(7)
    print("OK event after close does not orphan")

    # новый ход снова работает
    bm.open(7)
    await bm.append(7, "🔧 new turn")
    await asyncio.sleep(2)
    assert bm.has(7)
    print("OK new turn after close works")
    await bm.close(7)

    # ── схлопывание: серия одинаковых (tool, agent_id) → 1 строка со счётчиком ──
    # Реальный кейс из живого лога noos: один сабагент — 49 тул-вызовов
    # (35× Bash, 13× Read, 1× Write) — без схлопывания это нечитаемая простыня.
    bm.open(8)
    for i in range(35):
        await bm.append(8, f"⚡ <b>Bash</b> <code>grep {i}</code>", agent_id="a1", tool="Bash")
    for i in range(13):
        await bm.append(8, f"📖 <b>Read</b> <code>file{i}.py</code>", agent_id="a1", tool="Read")
    await _settle(bm, 8)
    bubble = bm._bubbles[8]
    assert len(bubble.entries) == 2, [e.render() for e in bubble.entries]
    assert bubble.entries[0].count == 35 and "grep 34" in bubble.entries[0].html
    assert bubble.entries[1].count == 13 and "file12.py" in bubble.entries[1].html
    print("OK схлопывание: 35×Bash + 13×Read одного агента → 2 строки (счётчик + последний)")

    # рендер: счётчик "N× " виден, дерево-отступ для agent_id
    text = bm._render_text(bubble)
    assert "35× " in text and "13× " in text, text
    assert "  ↳ " in text, text
    print("OK рендер: счётчик виден, агентские строки с отступом")

    # разные tool НЕ схлопываются друг с другом; разные agent_id — тоже
    await bm.append(8, "🔧 <b>Edit</b> <code>x.py</code>", agent_id="a1", tool="Edit")
    await bm.append(8, "⚡ <b>Bash</b> <code>ls</code>", agent_id="a2", tool="Bash")  # другой агент
    await _settle(bm, 8)
    assert len(bm._bubbles[8].entries) == 4, [e.render() for e in bm._bubbles[8].entries]
    print("OK разные tool/agent_id не схлопываются друг с другом")

    # главный поток (agent_id=None) — без отступа, схлопывается независимо от агентских
    await bm.append(8, "⚡ <b>Bash</b> <code>git status</code>", tool="Bash")
    await bm.append(8, "⚡ <b>Bash</b> <code>git log</code>", tool="Bash")
    await _settle(bm, 8)
    main_entries = [e for e in bm._bubbles[8].entries if e.agent_id is None]
    assert len(main_entries) == 1 and main_entries[-1].count == 2
    assert main_entries[-1].render().startswith("2× "), main_entries[-1].render()
    print("OK главный поток схлопывается отдельно от агентского, без отступа")

    # tool=None (спавн агента, TodoWrite, 📨 юзер-сообщение) — никогда не схлопывается
    n_before = len(bm._bubbles[8].entries)
    await bm.append(8, "🤖 <b>Сабагент dev-reviewer</b>: <i>review 1</i>")
    await bm.append(8, "🤖 <b>Сабагент dev-reviewer</b>: <i>review 2</i>")
    await _settle(bm, 8)
    assert len(bm._bubbles[8].entries) == n_before + 2
    print("OK tool=None (спавн агента и т.п.) никогда не схлопывается")
    await bm.close(8)

    # ── заморозка вместо fork: линейная история, без гонки ──
    bm.open(9)
    await bm.append(9, "⚡ <b>Bash</b> <code>ls</code>")
    await _settle(bm, 9)
    old_id = bm._bubbles[9].message_id
    assert old_id is not None

    # пользователь шлёт новое сообщение, пока сессия ещё работает
    await bm.freeze_and_open(9)
    # старое сообщение НЕ удалено (в отличие от прежнего fork) — только кнопка снята
    assert old_id not in fake_bot.deleted
    assert old_id in fake_bot.markup_cleared
    # новый бабл — независимый объект, начинается с нуля (без переноса старых строк)
    assert bm._bubbles[9].entries == []
    assert bm._bubbles[9].message_id is None
    print("OK freeze_and_open: старое сообщение остаётся на месте (не удалено), новое — с нуля")

    # КРИТИЧНО: между freeze() (внутри которой await) и открытием нового бабла
    # нет окна для гонки — append() ПОСЛЕ freeze_and_open() всегда попадает в
    # новый (уже открытый) бабл, не в «паразитный» через setdefault на старом
    # thread_id (реальный баг: событие в момент fork создавало сиротский бабл,
    # который затем перезаписывался и терялся/зависал — REVIEW.md).
    await bm.append(9, "⚡ <b>Bash</b> <code>echo new</code>")
    await _settle(bm, 9)
    assert len(bm._bubbles[9].entries) == 1
    assert bm._bubbles[9].message_id != old_id
    print("OK freeze_and_open: новое tool-событие сразу попадает в новый бабл (гонка исключена)")

    # финальный close() убирает СРАЗУ и замороженное, и текущее активное
    new_id = bm._bubbles[9].message_id
    await bm.close(9)
    assert old_id in fake_bot.deleted and new_id in fake_bot.deleted
    assert not bm.has(9)
    print("OK close() разом убирает все замороженные сообщения цикла + текущее")

    print("ALL BUBBLE OK")


if __name__ == "__main__":
    asyncio.run(main())
