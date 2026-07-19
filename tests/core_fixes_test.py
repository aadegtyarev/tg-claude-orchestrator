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
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

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
    # close чистит — старая кнопка потом не пройдёт гейт.
    core._drop_pending_perms(SESSION)
    assert ("noos", "r1") not in core._pending_perms
    handled = await core.permission_verdict(SESSION, "r1", "allow", via="tg")
    assert handled is False and core.manager.perm_calls == []
    print("OK _pending_perms чистится: снятая кнопка не бьёт по мёртвому запросу")


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

        async def bubble_post(self, session, html, *, stop_button):
            if not delivered["ok"]:
                raise RuntimeError("429")  # первый раз сбой
            return "1"

        async def bubble_edit(self, session, ref, html, *, stop_button):
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


async def main():
    await test_exact_tool_name()
    await test_pending_perms_cleanup()
    await test_confirmation_timeout_clears_buttons()
    await test_create_rollback_on_bind_fail()
    await test_bubble_sent_text_only_on_delivery()
    print("ALL CORE-FIXES OK")


if __name__ == "__main__":
    asyncio.run(main())
