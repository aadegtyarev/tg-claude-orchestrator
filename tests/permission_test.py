"""Юнит-контракт PermissionRelay (core/permission.py): запросы разрешений,
«первый ответ побеждает», локальные подтверждения, таймаут, отказ отправки.

Запуск: .venv/bin/python tests/permission_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.errors import UserError  # noqa: E402
from orchestrator.core.permission import PermissionRelay  # noqa: E402

SESSION = SimpleNamespace(name="noos")


class Harness:
    """Фейковые коллабораторы: транспорт (prompt/resolved), журнал, send_permission."""

    def __init__(self, send_fail: bool = False):
        self.prompts: list = []
        self.resolved: list = []
        self.records: list = []
        self.sent: list = []
        self._send_fail = send_fail
        self.name = "tg"

    # роль fake-транспорта
    async def permission_prompt(self, session, request):
        self.prompts.append(request.request_id)

    async def permission_resolved(self, session, rid, behavior, via):
        self.resolved.append((rid, behavior, via))

    # инъекции в PermissionRelay
    async def each_transport(self, action, label, *, warn=False):
        await action(self)

    def record(self, session, kind, **payload):
        self.records.append((kind, payload.get("request_id"), payload.get("behavior")))

    async def send_permission(self, session, request_id, behavior):
        if self._send_fail:
            raise RuntimeError("канал мёртв")
        self.sent.append((request_id, behavior))


def _relay(h: Harness) -> PermissionRelay:
    mgr = SimpleNamespace(send_permission=h.send_permission)
    return PermissionRelay(mgr, lambda k, **kw: k, h.each_transport, h.record)


async def test_request_and_verdict():
    """Запрос от Claude → pending + prompt; вердикт → send_permission + resolved."""
    h = Harness()
    r = _relay(h)
    await r.request_from_claude(SESSION, {"request_id": "r1", "tool_name": "Bash"})
    assert ("noos", "r1") in r._pending and h.prompts == ["r1"]
    ok = await r.verdict(SESSION, "r1", "allow", via="tg")
    assert ok is True and h.sent == [("r1", "allow")]
    assert ("r1", "allow", "tg") in h.resolved
    print("OK запрос от Claude → pending+prompt; вердикт → send+resolved")


async def test_first_answer_wins():
    """Второй вердикт по тому же запросу → False, без второго send_permission."""
    h = Harness()
    r = _relay(h)
    await r.request_from_claude(SESSION, {"request_id": "r1", "tool_name": "Bash"})
    assert await r.verdict(SESSION, "r1", "allow", via="tg") is True
    assert await r.verdict(SESSION, "r1", "deny", via="web") is False
    assert h.sent == [("r1", "allow")]  # ровно один вердикт ушёл в Claude
    print("OK первый ответ побеждает: второй вердикт проигнорирован")


async def test_send_failure_readds_and_raises():
    """Отказ send_permission → ключ возвращается в pending, UserError наружу."""
    h = Harness(send_fail=True)
    r = _relay(h)
    await r.request_from_claude(SESSION, {"request_id": "r1", "tool_name": "Bash"})
    try:
        await r.verdict(SESSION, "r1", "allow", via="tg")
        assert False, "ожидался UserError"
    except UserError:
        pass
    assert ("noos", "r1") in r._pending, "ключ должен вернуться для повтора"
    print("OK отказ отправки: ключ возвращён, UserError проброшен")


async def test_local_confirmation_resolve():
    """request_confirmation: вердикт из адаптера будит Future, в Claude НЕ шлём."""
    h = Harness()
    r = _relay(h)
    task = asyncio.ensure_future(
        r.request_confirmation(SESSION, "wallet", "deploy", "gh pr", timeout=5)
    )
    await asyncio.sleep(0.01)  # дать prompt уйти и Future зарегистрироваться
    assert len(r._local) == 1
    rid = h.prompts[0]
    assert await r.verdict(SESSION, rid, "allow", via="tg") is True
    assert await task is True
    assert h.sent == [], "локальное подтверждение в Claude Code не уходит"
    print("OK локальное подтверждение: Future разбужен, в Claude ничего не ушло")


async def test_local_confirmation_timeout():
    """Таймаут request_confirmation → deny + гашение кнопок (resolved)."""
    h = Harness()
    r = _relay(h)
    ok = await r.request_confirmation(SESSION, "wallet", "deploy", "gh pr", timeout=0.03)
    assert ok is False
    assert h.resolved and h.resolved[-1][1] == "deny" and h.resolved[-1][2] == "timeout"
    assert not r._local  # ключ снят в finally
    print("OK таймаут: deny + гашение кнопок, ключ снят")


async def test_forget_clears_and_wakes():
    """forget: pending-кнопки гасятся (cancelled), ожидающий Future будится в deny."""
    h = Harness()
    r = _relay(h)
    await r.request_from_claude(SESSION, {"request_id": "r1", "tool_name": "Bash"})
    task = asyncio.ensure_future(
        r.request_confirmation(SESSION, "wallet", "x", "y", timeout=5)
    )
    await asyncio.sleep(0.01)
    await r.forget(SESSION)
    assert ("noos", "r1") not in r._pending
    assert any(x[1] == "deny" and x[2] == "cancelled" for x in h.resolved)
    assert await task is False, "ожидающий request_confirmation разбужен в deny"
    print("OK forget: pending погашены, ожидающий Future разбужен в deny")


async def main():
    await test_request_and_verdict()
    await test_first_answer_wins()
    await test_send_failure_readds_and_raises()
    await test_local_confirmation_resolve()
    await test_local_confirmation_timeout()
    await test_forget_clears_and_wakes()
    print("ALL PERMISSION OK")


async def test_permission():
    await main()


if __name__ == "__main__":
    asyncio.run(main())
