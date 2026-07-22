"""OrchestratorVaultHost — адаптер vault.host.VaultHost поверх ядра.

Проверяет контракт мягкой деградации: если сессия удалена (manager.get→None),
confirm→False, а record/notify_denied тихо ничего не делают (не падают, не зовут
ядро). При живой сессии — проксируют в core с верными аргументами. observe
адресуется по имени и резолв не делает.

Запуск: .venv/bin/python tests/wallet_host_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.wallet.host import OrchestratorVaultHost  # noqa: E402

SESSION = SimpleNamespace(name="dev")


def _core(session):
    """Фейк-ядро; session=None имитирует удалённую сессию (manager.get→None)."""
    calls = {"confirm": [], "record": [], "notice": [], "observe": []}

    async def rc(sess, *, tool, description, preview):
        calls["confirm"].append((sess, tool, description, preview))
        return True

    async def bg(name, line, *, tool):
        calls["observe"].append((name, line, tool))

    async def notice(sess, text):
        calls["notice"].append((sess, text))

    core = SimpleNamespace(
        manager=SimpleNamespace(get=lambda n: session),
        request_confirmation=rc,
        bubbles=SimpleNamespace(append_background=bg),
        _record=lambda s, tool, **kw: calls["record"].append((s, tool, kw)),
        notice=notice,
        t=lambda k, **kw: kw.get("line", k),
    )
    return core, calls


def run(coro):
    return asyncio.run(coro)


def test_live_session_proxies_to_core():
    core, calls = _core(SESSION)
    h = OrchestratorVaultHost(core)
    assert run(h.confirm("dev", "descr", "prev")) is True
    assert calls["confirm"] == [(SESSION, "wallet", "descr", "prev")]
    run(h.observe("dev", "<b>line</b>"))
    assert calls["observe"] == [("dev", "<b>line</b>", "wallet")]
    h.record("dev", secret="s", cmd="gh pr", allowed=True)
    assert calls["record"] == [(SESSION, "wallet", {"secret": "s", "cmd": "gh pr", "allowed": True})]
    run(h.notify_denied("dev", "s → gh auth"))
    assert len(calls["notice"]) == 1
    text = calls["notice"][0][1]
    assert "gh auth" in text and "wallet_denied" in text  # cmd_display + t(wallet_denied)
    print("OK живая сессия: confirm/observe/record/notify проксируют в core")


def test_deleted_session_degrades_gracefully():
    core, calls = _core(None)  # manager.get → None
    h = OrchestratorVaultHost(core)
    assert run(h.confirm("dev", "d", "p")) is False       # deny
    assert calls["confirm"] == []                          # ядро не звалось
    h.record("dev", secret="s", cmd="c", allowed=True)     # тихо
    assert calls["record"] == []
    run(h.notify_denied("dev", "s → x"))                   # тихо
    assert calls["notice"] == []
    # observe адресуется по имени — работает и без резолва сессии
    run(h.observe("dev", "line"))
    assert calls["observe"] == [("dev", "line", "wallet")]
    print("OK удалённая сессия: confirm→False, record/notify no-op, observe по имени")


def main():
    test_live_session_proxies_to_core()
    test_deleted_session_degrades_gracefully()
    print("ALL WALLET-HOST OK")


if __name__ == "__main__":
    main()
