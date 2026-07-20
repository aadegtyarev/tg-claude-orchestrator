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

_TEXTS = {"bubble_working": "Работаю", "bubble_stop": "Стоп",
          "bubble_background": "🌙 Фоновая активность"}


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


SESSIONS = {n: SimpleNamespace(name=n)
            for n in ("s7", "s8", "s9", "s10", "s11", "s12", "s13", "s14", "s15")}


class FailingTransport:
    """Всегда отвергает доставку — модель устойчиво-битого бабла (Telegram 400
    «can't parse entities» / нередактируемое сообщение / длительный 5xx)."""

    name = "failing"

    def __init__(self):
        self.attempts = 0

    async def bubble_post(self, session, html, *, stop_button, unblock_active=False):
        self.attempts += 1
        raise RuntimeError("post rejected")

    async def bubble_edit(self, session, ref, html, *, stop_button, unblock_active=False):
        self.attempts += 1
        raise RuntimeError("edit rejected")

    async def bubble_finish(self, session, ref, *, delete):
        pass

    async def bubble_freeze(self, session, ref):
        pass


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

    # ЧЕРЕДОВАНИЕ агентов: повтор (tool, agent) схлопывается со «своей» строкой,
    # даже если между ними вклинился ДРУГОЙ агент (параллельные сабагенты). До
    # фикса сравнивали только entries[-1] и серия рвалась на дубли.
    await bm.append("s8", "⚡ <b>Bash</b> <code>pwd</code>", agent_id="a1", tool="Bash")  # снова a1
    await bm.append("s8", "⚡ <b>Bash</b> <code>id</code>", agent_id="a2", tool="Bash")   # снова a2
    await _settle(bm, "s8")
    e = bm._bubbles["s8"].entries
    assert len(e) == 4, [x.render() for x in e]  # новых строк нет — оба схлопнулись
    a1_bash = next(x for x in e if x.agent_id == "a1" and x.tool == "Bash")
    a2_bash = next(x for x in e if x.agent_id == "a2" and x.tool == "Bash")
    assert a1_bash.count == 36 and "pwd" in a1_bash.html, (a1_bash.count, a1_bash.html)
    assert a2_bash.count == 2 and "id" in a2_bash.html, (a2_bash.count, a2_bash.html)
    print("OK чередование агентов: повтор схлопывается со своей строкой, не рвётся")

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

    # ── живой статус: спиннер-глагол + реальная модель, обновляются ──
    bm.open("s7")
    await bm.set_status("s7", pulse="Cogitating", model="glm-5.2")
    await _settle(bm, "s7")
    text = bm._render_text(bm._bubbles["s7"])
    assert "✻ Cogitating" in text and "glm-5.2" in text, text
    # смена глагола и модели обновляет на месте (не копит)
    await bm.set_status("s7", pulse="Quantumizing", model="claude-opus-4-8")
    await _settle(bm, "s7")
    text2 = bm._render_text(bm._bubbles["s7"])
    assert "✻ Quantumizing" in text2 and "claude-opus-4-8" in text2
    assert "Cogitating" not in text2 and "glm-5.2" not in text2, text2
    # тот же статус повторно — без изменения (нет лишней правки)
    prev = bm._bubbles["s7"].sent_text
    await bm.set_status("s7", pulse="Quantumizing", model="claude-opus-4-8")
    assert bm._bubbles["s7"].sent_text == prev
    await bm.close("s7")
    print("OK статус: глагол+модель в бабле, обновляются на месте, дубль не копит")

    # ── фоновый бабл: активность между ходами (wallet-поллинг CI) ──
    import orchestrator.core.bubble as _bubmod
    _bubmod.BG_IDLE_SEC = 0.3       # быстрый простой для теста
    _bubmod.EDIT_INTERVAL = 0.05    # быстрый flush для теста

    # без активного хода append_background САМ открывает фоновый бабл
    await bm.append_background("s10", "🔐 wallet gh pr checks", tool="wallet")
    await _settle(bm, "s10")
    assert bm.has("s10") and bm._bubbles["s10"].background is True
    assert "Фоновая активность" in bm._render_text(bm._bubbles["s10"])
    print("OK фоновый бабл: открывается без хода, свой заголовок")

    # серия одинаковых фоновых вызовов схлопывается в одну строку (не спам)
    for i in range(5):
        await bm.append_background("s10", f"🔐 wallet gh pr checks {i}", tool="wallet")
    await _settle(bm, "s10")
    b10 = bm._bubbles["s10"]
    assert len(b10.entries) == 1 and b10.entries[0].count == 6, [e.render() for e in b10.entries]
    print("OK фоновый бабл: поллинг схлопывается в одну строку (не спам)")

    # начало хода превращает фоновый бабл в обычный (флаг и авто-закрытие сняты)
    bm.open("s10")
    assert bm._bubbles["s10"].background is False and "s10" not in bm._bg_deadline
    await bm.append("s10", "⚡ <b>Bash</b> <code>ls</code>", tool="Bash")
    await _settle(bm, "s10")
    assert "Работаю" in bm._render_text(bm._bubbles["s10"])
    print("OK начало хода: фоновый бабл становится обычным")
    await bm.close("s10")

    # авто-закрытие по простою: фоновый бабл сам закрывается сторожем
    await bm.append_background("s11", "🔐 wallet poll", tool="wallet")
    await _settle(bm, "s11")
    assert bm.has("s11")
    await asyncio.wait_for(bm._bg_task["s11"], timeout=5)  # дождаться сторожа
    assert not bm.has("s11"), "фоновый бабл должен авто-закрыться по простою"
    print("OK фоновый бабл: авто-закрытие по простою")

    # ── close_all: graceful shutdown убирает ВСЕ баблы (не осиротить при рестарте) ──
    bm.open("s12")
    await bm.append("s12", "⚡ <b>Bash</b> <code>ls</code>")
    await _settle(bm, "s12")
    bm.open("s13")
    await bm.append("s13", "🔧 <b>Edit</b> <code>x</code>")
    await _settle(bm, "s13")
    id12, id13 = _ref(bm, "s12"), _ref(bm, "s13")
    assert bm.has("s12") and bm.has("s13")
    await bm.close_all()
    assert not bm.has("s12") and not bm.has("s13")
    assert id12 in tr.deleted and id13 in tr.deleted  # сообщения-баблы убраны
    print("OK close_all: все баблы закрыты (рестарт не оставит сироту)")

    # ── персист refs: сирот убирают на старте (после краша/SIGKILL) ──
    import json as _json
    import tempfile
    from pathlib import Path as _Path
    pfile = _Path(tempfile.mkdtemp(prefix="bub_")) / "live.json"
    bm2 = BubbleManager(lambda: [tr], SESSIONS.get, lambda k, **kw: _TEXTS[k],
                        delete_after=True, persist_path=pfile)
    bm2.open("s7")
    await bm2.append("s7", "⚡ <b>Bash</b> <code>x</code>", tool="Bash")
    await _settle(bm2, "s7")
    saved = _json.loads(pfile.read_text())
    assert any(e["session"] == "s7" and e["adapter"] == "fake" for e in saved), saved
    print("OK персист: ref живого бабла записан (для очистки сирот при краше)")
    await bm2.close("s7")
    assert _json.loads(pfile.read_text()) == []  # закрыт штатно — сирот нет
    print("OK персист: после close персист пуст")

    # ── #18: текущий bash — подробно (full_html/pre), не-текущий — коротко ──
    bm.open("s14")
    await bm.append("s14", "⚡ <b>Bash</b> <code>grep foo</code>", tool="Bash",
                    full_html="⚡ <b>Bash</b>\n<pre>grep foo bar baz --long-flag</pre>")
    await _settle(bm, "s14")
    txt = bm._render_text(bm._bubbles["s14"])
    assert "<pre>grep foo bar baz --long-flag</pre>" in txt, txt
    print("OK #18: текущий bash рендерится полно (pre)")
    # приходит следующий тул → предыдущий bash больше не текущий → короткая строка
    await bm.append("s14", "📖 <b>Read</b> <code>x.py</code>", tool="Read")
    await _settle(bm, "s14")
    txt2 = bm._render_text(bm._bubbles["s14"])
    assert "<pre>" not in txt2 and "grep foo</code>" in txt2, txt2
    print("OK #18: не-текущий bash свёрнут в короткую строку")
    await bm.close("s14")

    # ── #18B: завершение вызова (PostToolUse) → короткая строка + статус ──
    bm.open("s14")
    await bm.append("s14", "⚡ <b>Bash</b> <code>echo hi</code>", tool="Bash",
                    full_html="⚡ <b>Bash</b>\n<pre>echo hi --flag</pre>", tool_use_id="tu1")
    await _settle(bm, "s14")
    assert "<pre>echo hi --flag</pre>" in bm._render_text(bm._bubbles["s14"])
    await bm.complete("s14", "tu1", "✓ · 42мс")
    await _settle(bm, "s14")
    tt = bm._render_text(bm._bubbles["s14"])
    assert "<pre>" not in tt and "✓ · 42мс" in tt and "echo hi</code>" in tt, tt
    print("OK #18B: завершение по tool_use_id → короткая строка + статус")
    await bm.close("s14")

    # ── фикс регрессии: complete() не вешает статус на ЧУЖУЮ строку ──
    bm.open("s14")
    await bm.append("s14", "⚡ <b>Bash</b> <code>sleep</code>", tool="Bash", tool_use_id="tuX")
    await bm.append("s14", "📨 <b>привет</b>")  # юзер-строка → current_line (не bash)
    await _settle(bm, "s14")
    await bm.complete("s14", "tuMISSING", "✓ · 99мс")  # id не найден (уехал в заморозку) → no-op
    await bm.complete("s14", "", "⚠ · 1мс")            # фолбэк, но current не Bash → no-op
    await _settle(bm, "s14")
    tf = bm._render_text(bm._bubbles["s14"])
    assert "✓ · 99мс" not in tf and "⚠ · 1мс" not in tf, tf
    print("OK фикс: статус не прилипает к чужой строке (freeze/фолбэк)")
    await bm.close("s14")

    # ── backoff: устойчиво-недоставляемый бабл не крутит _flush вечно ──
    import orchestrator.core.bubble as _bm
    fail = FailingTransport()
    bmf = BubbleManager(
        lambda: [fail], SESSIONS.get, lambda k, **kw: _TEXTS[k], delete_after=True
    )
    bmf.open("s15")
    await bmf.append("s15", "⚡ <b>Bash</b> <code>ls</code>", tool="Bash")
    # дождаться, пока цепочка само-респинов исчерпается (EDIT_INTERVAL=0.05 к этому
    # моменту): MAX_FLUSH_FAILS попыток * интервал + запас.
    await asyncio.sleep(_bm.MAX_FLUSH_FAILS * _bm.EDIT_INTERVAL + 0.5)
    b15 = bmf._bubbles["s15"]
    assert fail.attempts == _bm.MAX_FLUSH_FAILS, fail.attempts
    assert b15.fail_streak == _bm.MAX_FLUSH_FAILS
    assert b15.flush_task is None or b15.flush_task.done()
    # текст всё ещё «не доставлен» (sent_text не двигался), но респина больше нет
    assert b15.sent_text == ""
    print(f"OK backoff: устойчивый сбой доставки → ровно {_bm.MAX_FLUSH_FAILS} попыток, респин остановлен")

    # новое реальное событие поднимает свежий flush (доставка возобновляется)
    await bmf.append("s15", "📖 <b>Read</b> <code>x.py</code>", tool="Read")
    await asyncio.sleep(_bm.EDIT_INTERVAL + 0.3)
    assert fail.attempts == _bm.MAX_FLUSH_FAILS + 1, fail.attempts
    print("OK backoff: новое событие поднимает ровно один свежий flush (не спам)")
    await bmf.close("s15")

    print("ALL BUBBLE OK")


async def test_bubble():
    await main()

if __name__ == "__main__":
    asyncio.run(main())
