"""Регрессия статус-бабла: гейт активности (событие после close не рождает
бабл-сироту, новый ход снова работает), схлопывание однотипных подряд идущих
тулов, отступ для агентских (agent_id), заморозка вместо fork (линейная
история без гонки — см. bubble.freeze_and_open docstring). Без сети и Telegram:
доставка — через FakeTransport (контракт core/transport.py).

Запуск: .venv/bin/python tests/bubble_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.bubble import BubbleManager  # noqa: E402

_TEXTS = {"bubble_working": "Работаю", "bubble_stop": "Стоп"}


class FakeTransport:
    name = "fake"

    def __init__(self):
        self.sent: list[dict] = []  # история post/edit
        self.deleted: list[int] = []
        self.markup_cleared: list[int] = []
        self._next_id = 1

    async def bubble_post(self, session, html, *, stop_button, unblock_active=False):
        mid = self._next_id
        self._next_id += 1
        self.sent.append({"ref": mid, "text": html, "stop": stop_button})
        return str(mid)

    async def bubble_edit(self, session, ref, html, *, stop_button, unblock_active=False):
        self.sent.append({"ref": int(ref), "text": html, "stop": stop_button})

    async def bubble_finish(self, session, ref, *, delete):
        if delete:
            self.deleted.append(int(ref))
        else:
            self.markup_cleared.append(int(ref))

    async def bubble_freeze(self, session, ref):
        self.markup_cleared.append(int(ref))


SESSIONS = {n: SimpleNamespace(name=n) for n in ("s7", "s8", "s9")}


def _ref(bm: BubbleManager, name: str) -> int | None:
    raw = bm._bubbles[name].refs.get("fake")
    return int(raw) if raw is not None else None


async def _settle(bm: BubbleManager, name: str) -> None:
    """Дождаться отложенного flush конкретного бабла (без грубого sleep(2))."""
    bubble = bm._bubbles.get(name)
    if bubble is not None and bubble.flush_task is not None:
        await asyncio.wait_for(bubble.flush_task, timeout=5)


async def main():
    tr = FakeTransport()
    bm = BubbleManager(
        lambda: [tr], SESSIONS.get, lambda k, **kw: _TEXTS[k], delete_after=True
    )

    # append без open — сироты быть не должно
    await bm.append("s7", "🔧 late")
    await asyncio.sleep(2)
    assert not bm.has("s7")
    print("OK append without open ignored")

    # активный ход
    bm.open("s7")
    await bm.append("s7", "🔧 Bash: ls")
    await asyncio.sleep(2)
    assert bm.has("s7")
    print("OK bubble created during active turn")

    # событие после close — не сирота
    await bm.close("s7")
    assert not bm.has("s7")
    await bm.append("s7", "🔧 late hook")
    await asyncio.sleep(2)
    assert not bm.has("s7")
    print("OK event after close does not orphan")

    # новый ход снова работает
    bm.open("s7")
    await bm.append("s7", "🔧 new turn")
    await asyncio.sleep(2)
    assert bm.has("s7")
    print("OK new turn after close works")
    await bm.close("s7")

    # ── схлопывание: серия одинаковых (tool, agent_id) → 1 строка со счётчиком ──
    # Реальный кейс из живого лога noos: один сабагент — 49 тул-вызовов
    # (35× Bash, 13× Read, 1× Write) — без схлопывания это нечитаемая простыня.
    bm.open("s8")
    for i in range(35):
        await bm.append("s8", f"⚡ <b>Bash</b> <code>grep {i}</code>", agent_id="a1", tool="Bash")
    for i in range(13):
        await bm.append("s8", f"📖 <b>Read</b> <code>file{i}.py</code>", agent_id="a1", tool="Read")
    await _settle(bm, "s8")
    bubble = bm._bubbles["s8"]
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
    await bm.append("s8", "🔧 <b>Edit</b> <code>x.py</code>", agent_id="a1", tool="Edit")
    await bm.append("s8", "⚡ <b>Bash</b> <code>ls</code>", agent_id="a2", tool="Bash")  # другой агент
    await _settle(bm, "s8")
    assert len(bm._bubbles["s8"].entries) == 4, [e.render() for e in bm._bubbles["s8"].entries]
    print("OK разные tool/agent_id не схлопываются друг с другом")

    # главный поток (agent_id=None) — без отступа, схлопывается независимо от агентских
    await bm.append("s8", "⚡ <b>Bash</b> <code>git status</code>", tool="Bash")
    await bm.append("s8", "⚡ <b>Bash</b> <code>git log</code>", tool="Bash")
    await _settle(bm, "s8")
    main_entries = [e for e in bm._bubbles["s8"].entries if e.agent_id is None]
    assert len(main_entries) == 1 and main_entries[-1].count == 2
    assert main_entries[-1].render().startswith("2× "), main_entries[-1].render()
    print("OK главный поток схлопывается отдельно от агентского, без отступа")

    # tool=None (спавн агента, TodoWrite, 📨 юзер-сообщение) — никогда не схлопывается
    n_before = len(bm._bubbles["s8"].entries)
    await bm.append("s8", "🤖 <b>Сабагент dev-reviewer</b>: <i>review 1</i>")
    await bm.append("s8", "🤖 <b>Сабагент dev-reviewer</b>: <i>review 2</i>")
    await _settle(bm, "s8")
    assert len(bm._bubbles["s8"].entries) == n_before + 2
    print("OK tool=None (спавн агента и т.п.) никогда не схлопывается")
    await bm.close("s8")

    # ── заморозка вместо fork: линейная история, без гонки ──
    bm.open("s9")
    await bm.append("s9", "⚡ <b>Bash</b> <code>ls</code>")
    await _settle(bm, "s9")
    old_id = _ref(bm, "s9")
    assert old_id is not None

    # пользователь шлёт новое сообщение, пока сессия ещё работает
    await bm.freeze_and_open("s9")
    # старое сообщение НЕ удалено (в отличие от прежнего fork) — только кнопка снята
    assert old_id not in tr.deleted
    assert old_id in tr.markup_cleared
    # новый бабл — независимый объект, начинается с нуля (без переноса старых строк)
    assert bm._bubbles["s9"].entries == []
    assert bm._bubbles["s9"].refs == {}
    print("OK freeze_and_open: старое сообщение остаётся на месте (не удалено), новое — с нуля")

    # КРИТИЧНО: между freeze() (внутри которой await) и открытием нового бабла
    # нет окна для гонки — append() ПОСЛЕ freeze_and_open() всегда попадает в
    # новый (уже открытый) бабл, не в «паразитный» через setdefault на старом
    # ключе (реальный баг: событие в момент fork создавало сиротский бабл,
    # который затем перезаписывался и терялся/зависал — REVIEW.md).
    await bm.append("s9", "⚡ <b>Bash</b> <code>echo new</code>")
    await _settle(bm, "s9")
    assert len(bm._bubbles["s9"].entries) == 1
    assert _ref(bm, "s9") != old_id
    print("OK freeze_and_open: новое tool-событие сразу попадает в новый бабл (гонка исключена)")

    # финальный close() убирает СРАЗУ и замороженное, и текущее активное
    new_id = _ref(bm, "s9")
    await bm.close("s9")
    assert old_id in tr.deleted and new_id in tr.deleted
    assert not bm.has("s9")
    print("OK close() разом убирает все замороженные сообщения цикла + текущее")

    print("ALL BUBBLE OK")


async def test_bubble():
    await main()

if __name__ == "__main__":
    asyncio.run(main())
