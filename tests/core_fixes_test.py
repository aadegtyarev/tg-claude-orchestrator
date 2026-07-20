"""Регрессы корректностных фиксов ревью в OrchestratorCore.

- точное имя канального тула (endswith) vs подстрока — Stop-фолбэк не глушится
  чужим MCP-тулом;
- _pending_perms/_local_perms чистятся при close/clear/delete;
- request_confirmation на таймауте гасит кнопки (permission_resolved);
- create_session откатывает сессию, если requires_binding-адаптер не привязал;
- bubble._flush фиксирует sent_text только при реальной доставке.

Запуск: .venv/bin/python tests/core_fixes_test.py
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import orchestrator.core.app as appmod  # noqa: E402
from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.bubble import BubbleManager  # noqa: E402
from orchestrator.core.texts import get_texts  # noqa: E402
from orchestrator.core.turn import TurnSupervisor  # noqa: E402

SESSION = SimpleNamespace(name="noos", title="noos", bindings={})


class FakeMgr:
    def __init__(self):
        self.perm_calls = []

    def get(self, name):
        return SESSION if name == "noos" else None

    get_by_name = get

    async def send_permission(self, session, request_id, behavior):
        self.perm_calls.append((request_id, behavior))


def make_core():
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.manager = FakeMgr()
    core._texts = get_texts("ru")
    core.config = SimpleNamespace(max_instances=5)
    core._history = {}
    core._last_tool = {}
    core._inflight_tools = {}
    core._inflight_cleared_at = {}
    core._agent_types = {}
    core._agent_spawns = {}
    core.adapters = {}
    core._pending_perms = set()
    core._local_perms = {}
    core.turns = TurnSupervisor(
        core.manager, core.t,
        lambda session, text: asyncio.sleep(0),
        lambda session: asyncio.sleep(0),
    )
    core.bubbles = SimpleNamespace(
        append=lambda *a, **kw: asyncio.sleep(0),
        close=lambda name: asyncio.sleep(0),
    )
    return core


async def test_exact_tool_name():
    core = make_core()
    deliveries = []

    async def fake_deliver(session, text, origin, intermediate):
        deliveries.append(text)
    core._deliver_text = fake_deliver

    # Чужой MCP-тул с подстрокой reply_to_user НЕ должен ставить reply-флаг.
    await core.handle_tool_event("noos", {"tool_name": "mcp__notes__draft_reply_to_user_email"})
    await core.handle_stop_event("noos", {"last_assistant_message": "финал хода"})
    assert deliveries == ["финал хода"], deliveries
    print("OK точное имя тула: чужой *_reply_to_user_* не глушит Stop-фолбэк")

    deliveries.clear()
    # Настоящий канальный тул — ставит флаг, фолбэк не срабатывает.
    await core.handle_tool_event("noos", {"tool_name": "mcp__channel-noos__reply_to_user"})
    await core.handle_stop_event("noos", {"last_assistant_message": "уже отправлено"})
    assert deliveries == [], deliveries
    print("OK настоящий reply_to_user ставит флаг (без дубля)")


async def test_taskoutput_and_bg_available():
    core = make_core()
    lines = []

    async def fake_append(name, html, **kw):
        lines.append(html)
    core.bubbles = SimpleNamespace(
        append=fake_append, close=lambda n: asyncio.sleep(0),
        complete=lambda *a, **kw: asyncio.sleep(0),
    )

    # Кнопка ⏭ теперь на in-flight тулах (хуки PreToolUse/PostToolUse), НЕ на
    # /proc: под bwrap у процесса сессии всегда есть дочерний → has_children там
    # вечно True. Bash стартовал (Pre без Post) → можно свернуть в фон (Ctrl+B).
    await core.handle_tool_event("noos", {"tool_name": "Bash", "tool_use_id": "t1",
                                          "tool_input": {"command": "make build"}})
    assert core._unblock_available("noos") is True
    assert core.unblock_action("noos") == "background"
    # Bash завершился (PostToolUse) → в grace-окне кнопка ещё держится (дебаунс
    # против мигания на паузах между тулами).
    await core.handle_tool_event("noos", {"hook_event_name": "PostToolUse",
                                          "tool_name": "Bash", "tool_use_id": "t1",
                                          "tool_response": {}})
    assert core._unblock_available("noos") is True, "grace: сразу после Post ещё активна"
    # Grace истёк (нет тулов дольше окна) → ⏭ гаснет.
    core._inflight_cleared_at["noos"] = time.monotonic() - appmod.UNBLOCK_GRACE - 1
    assert core._unblock_available("noos") is False, "grace истёк — ⏭ гаснет"
    # TaskOutput → «ждёт фон»: строка в бабле + ⏭ активна (пнуть Esc'ом).
    await core.handle_tool_event("noos", {"tool_name": "TaskOutput", "tool_input": {}})
    assert any("Ждёт фоновую задачу" in ln for ln in lines), lines
    assert core._unblock_available("noos") is True, "ожидание фона можно прервать"
    assert core.unblock_action("noos") == "kick"
    # Простой (нет тула в работе, не ждёт фон) → ⏭ неактивна.
    core._inflight_tools.pop("noos", None)
    core._last_tool["noos"] = "Read"
    assert core._unblock_available("noos") is False
    print("OK ⏭ разблокировка: Bash-inflight→background, Post→гаснет, TaskOutput→kick, покой→неактивна")


async def test_pending_perms_cleanup():
    core = make_core()
    resolved = []

    class FakeTr:
        name = "tg"

        async def permission_prompt(self, session, request):
            pass

        async def permission_resolved(self, session, rid, behavior, via):
            resolved.append((rid, behavior, via))

    core.adapters = {"tg": FakeTr()}
    await core.handle_permission_request("noos", {"request_id": "r1", "tool": "Bash"})
    assert ("noos", "r1") in core._pending_perms
    # close чистит — старая кнопка потом не пройдёт гейт + гасится в адаптере.
    await core._drop_pending_perms(SESSION)
    assert ("noos", "r1") not in core._pending_perms
    assert ("r1", "deny", "cancelled") in resolved  # кнопка погашена
    handled = await core.permission_verdict(SESSION, "r1", "allow", via="tg")
    assert handled is False and core.manager.perm_calls == []
    print("OK _pending_perms чистится: кнопка гасится и не бьёт по мёртвому запросу")


async def test_confirmation_timeout_clears_buttons():
    core = make_core()
    resolved = []

    class FakeTr:
        name = "tg"

        async def permission_prompt(self, session, request):
            pass

        async def permission_resolved(self, session, rid, behavior, via):
            resolved.append((rid, behavior, via))

    core.adapters = {"tg": FakeTr()}
    ok = await core.request_confirmation(SESSION, "wallet", "deploy", "gh pr", timeout=0.05)
    assert ok is False
    assert resolved and resolved[-1][1] == "deny" and resolved[-1][2] == "timeout"
    print("OK request_confirmation таймаут → deny + гашение кнопок (permission_resolved)")


async def test_create_rollback_on_bind_fail():
    core = make_core()
    created = {"n": 0}
    deleted = []

    class Mgr(FakeMgr):
        def has_name(self, n):
            return False

        def count(self):
            return 0

        async def create(self, title, project_path=None):
            created["n"] += 1
            return SimpleNamespace(name="x", title=title, bindings={})

        async def delete(self, session):
            deleted.append(session.name)

        def save_state(self):
            pass

    core.manager = Mgr()

    class BadTelegram:
        name = "telegram"
        requires_binding = True

        async def bind_session(self, session):
            return None  # не смог создать топик

        async def unbind_session(self, session, address):
            pass

    core.adapters = {"telegram": BadTelegram()}
    core.session_hooks = []
    try:
        await core.create_session("proj")
        assert False, "должно было откатиться"
    except Exception as e:
        assert "телеграм" in str(e).lower() or "telegram" in str(e).lower() or "adapter" in str(e).lower() \
            or "поверхность" in str(e).lower() or "surface" in str(e).lower(), str(e)
    assert deleted == ["x"], deleted
    print("OK create_session откатывает сессию при провале обязательной привязки")


async def test_bubble_sent_text_only_on_delivery():
    delivered = {"ok": False}

    class FlakyTr:
        name = "tg"

        async def bubble_post(self, session, html, *, stop_button, unblock_active=False):
            if not delivered["ok"]:
                raise RuntimeError("429")  # первый раз сбой
            return "1"

        async def bubble_edit(self, session, ref, html, *, stop_button, unblock_active=False):
            pass

    tr = FlakyTr()
    bm = BubbleManager(lambda: [tr], lambda n: SESSION if n == "noos" else None,
                       lambda k, **kw: {"bubble_working": "Работаю"}.get(k, k), delete_after=True)
    bm.open("noos")
    await bm.append("noos", "🔧 tool")
    await asyncio.wait_for(bm._bubbles["noos"].flush_task, timeout=5)
    # Доставка упала → sent_text НЕ зафиксирован, бабл не «залип».
    assert bm._bubbles["noos"].sent_text == "", bm._bubbles["noos"].sent_text
    # Следующий flush при живой доставке должен пройти (text != sent_text).
    delivered["ok"] = True
    await bm.append("noos", "🔧 tool2")
    await asyncio.wait_for(bm._bubbles["noos"].flush_task, timeout=5)
    assert bm._bubbles["noos"].refs.get("tg") == "1"
    print("OK bubble sent_text фиксируется только при реальной доставке")


async def test_teardown_runtime_unified():
    core = make_core()
    events = []
    core.turns = SimpleNamespace(
        stop=lambda n: events.append(("stop", n)),
        forget=lambda n: events.append(("forget", n)),
    )

    async def fake_drop(s):
        events.append(("drop", s.name))
    core._drop_pending_perms = fake_drop
    core.bash = SimpleNamespace(
        close_for_session=lambda n: events.append(("bash", n))
    )

    async def fake_bclose(n):
        events.append(("bubble", n))
    core.bubbles = SimpleNamespace(close=fake_bclose)

    # Терминальная остановка: stop + drop + очистка _last_tool + bash + бабл.
    core._last_tool["noos"] = "Bash"
    await core._teardown_runtime(SESSION)
    assert ("stop", "noos") in events and ("drop", "noos") in events
    assert "noos" not in core._last_tool
    assert ("bash", "noos") in events and ("bubble", "noos") in events

    # Продолжение (clear/switch): forget_turn + НЕ трогаем bash.
    events.clear()
    core._last_tool["noos"] = "Read"
    await core._teardown_runtime(SESSION, close_bash=False, forget_turn=True)
    assert ("forget", "noos") in events
    assert not any(e[0] == "bash" for e in events), events
    assert "noos" not in core._last_tool
    print("OK _teardown_runtime: единый разбор; close_bash/forget_turn ветвятся")


async def test_notify_state_changed_broadcasts():
    core = make_core()
    seen = []

    class Tr:
        name = "web"

        async def session_state_changed(self, s):
            seen.append(s.name if s else None)

    class Bad:
        name = "boom"

        async def session_state_changed(self, s):
            raise RuntimeError("adapter down")

    core.adapters = {"web": Tr(), "boom": Bad()}
    # Сбой одного адаптера не ломает операцию, остальные получают событие.
    await core._notify_state_changed(SESSION)
    assert seen == ["noos"], seen
    print("OK _notify_state_changed: бродкаст всем, сбой адаптера не ломает")


def test_parse_cost_resets_regex():
    # Regex сужен Res[et]+s → Resets? (перестал матчить мусор вроде «Retess»),
    # но должен продолжать тянуть настоящий блок /cost.
    text = (
        "cost: $1.23\n"
        "Current session · 12% used\n"
        "Current week (all models) · 34% used\n"
        "Resets Jul 20 3:00pm (in 5h 20m)\n"
    )
    out = OrchestratorCore._parse_cost(text)
    assert out.get("cost") == "1.23", out
    assert out.get("session_pct") == "12", out
    assert out.get("week_pct") == "34", out
    assert out.get("session_reset", "").startswith("Jul 20"), out
    # «Reteets» больше не считается словом Resets.
    assert "session_reset" not in OrchestratorCore._parse_cost("Reteets X (y)\n")
    print("OK _parse_cost: Resets? тянет reset/cost/%, мусор не матчится")


async def main():
    test_parse_cost_resets_regex()
    await test_teardown_runtime_unified()
    await test_notify_state_changed_broadcasts()
    await test_exact_tool_name()
    await test_taskoutput_and_bg_available()
    await test_pending_perms_cleanup()
    await test_confirmation_timeout_clears_buttons()
    await test_create_rollback_on_bind_fail()
    await test_bubble_sent_text_only_on_delivery()
    print("ALL CORE-FIXES OK")


if __name__ == "__main__":
    asyncio.run(main())
