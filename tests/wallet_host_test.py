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


def _core(session, verdict=True):
    """Фейк-ядро; session=None имитирует удалённую сессию (manager.get→None).
    verdict — что вернёт permission-relay (True=✅, False=❌/таймаут)."""
    calls = {"confirm": [], "record": [], "notice": [], "observe": []}

    async def rc(sess, *, tool, description, preview, timeout=300.0):
        calls["confirm"].append((sess, tool, description, preview, timeout))
        return verdict

    async def bg(name, line, *, tool):
        calls["observe"].append((name, line, tool))

    async def notice(sess, text):
        calls["notice"].append((sess, text))

    # t: подставляет line (для notice) либо description (для ask-desc), иначе ключ.
    def t(k, **kw):
        if "line" in kw:
            return kw["line"]
        if "description" in kw:
            return f"{k}[{kw['description']}]"
        return k

    core = SimpleNamespace(
        manager=SimpleNamespace(get=lambda n: session),
        request_confirmation=rc,
        bubbles=SimpleNamespace(append_background=bg),
        _record=lambda s, tool, **kw: calls["record"].append((s, tool, kw)),
        notice=notice,
        t=t,
    )
    return core, calls


def run(coro):
    return asyncio.run(coro)


def test_live_session_proxies_to_core():
    core, calls = _core(SESSION)
    h = OrchestratorVaultHost(core)
    assert run(h.confirm("dev", "descr", "prev")) is True
    assert calls["confirm"] == [(SESSION, "wallet", "descr", "prev", 300.0)]
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


def test_ask_live_grant_returns_true():
    """ask при живой сессии зовёт permission-relay с ask-маркировкой; ✅ → True."""
    from orchestrator.modules.wallet.host import _ASK_CONFIRM_TIMEOUT
    core, calls = _core(SESSION, verdict=True)
    h = OrchestratorVaultHost(core)
    # description от коннектора, preview = метод+URL (значение секрета НЕ передаём).
    assert run(h.ask("dev", "push в чужой репо", "POST https://api.example/x")) is True
    assert len(calls["confirm"]) == 1
    sess, tool, desc, preview, timeout = calls["confirm"][0]
    assert sess is SESSION
    # tool/desc отличимы от штатного confirm ("wallet"): это ЗАПРОС РАСШИРЕНИЯ.
    assert tool == "wallet_ask_tool"                       # свой i18n-ключ, не "wallet"
    assert desc == "wallet_ask_desc[push в чужой репо]"    # description коннектора вшит
    assert preview == "POST https://api.example/x"         # факт запроса (метод+URL)
    assert timeout == _ASK_CONFIRM_TIMEOUT                 # свой таймаут < страховки прокси
    # Значение секрета нигде не фигурирует.
    assert all("secret" not in str(x).lower() for x in (tool, desc, preview))
    print("OK ask живая+✅: relay зван с ask-маркировкой, свой таймаут, → True")


def test_ask_deny_and_timeout_return_false():
    """Оператор ❌ / таймаут (relay вернул False) → ask False."""
    core, _ = _core(SESSION, verdict=False)
    h = OrchestratorVaultHost(core)
    assert run(h.ask("dev", "d", "GET https://x/y")) is False
    print("OK ask ❌/таймаут: relay=False → ask=False")


def test_ask_deleted_session_denies():
    """Сессия удалена (manager.get→None) → ask False, relay не звался (Р0)."""
    core, calls = _core(None)
    h = OrchestratorVaultHost(core)
    assert run(h.ask("dev", "d", "GET https://x/y")) is False
    assert calls["confirm"] == []
    print("OK ask удалённая сессия: relay не зван, → False")


def test_ask_reaches_operator_from_vault_proxy():
    """E2E vault-сторона: proxy._ask_grant(host.ask) реально доходит до relay при
    живом OrchestratorVaultHost и возвращает вердикт оператора (не заглушку)."""
    from vault.connectors.contract import ScopeVerdict
    from vault.proxy import VaultProxy

    core, calls = _core(SESSION, verdict=True)
    host = OrchestratorVaultHost(core)
    proxy = VaultProxy.__new__(VaultProxy)  # без сети: только _ask_grant + host
    proxy.host = host
    proxy.session_name = "dev"
    proxy._ask_timeout = 5.0
    verdict = ScopeVerdict.ask("нужен доступ к соседнему репо")
    granted = run(proxy._ask_grant(verdict, "GET", "https://api.example/repo"))
    assert granted is True                                 # дошло до оператора → ✅
    assert len(calls["confirm"]) == 1                      # relay реально позван
    _, _, desc, preview, _ = calls["confirm"][0]
    assert "нужен доступ к соседнему репо" in desc         # descr коннектора виден
    assert preview == "GET https://api.example/repo"       # метод+URL проброшены
    # А ❌ оператора (relay=False) прокси трактует как отказ гранта.
    core2, _ = _core(SESSION, verdict=False)
    proxy.host = OrchestratorVaultHost(core2)
    assert run(proxy._ask_grant(verdict, "GET", "https://api.example/repo")) is False
    print("OK e2e: proxy ASK→host.ask→relay→вердикт (✅→True, ❌→False)")


def main():
    test_live_session_proxies_to_core()
    test_deleted_session_degrades_gracefully()
    test_ask_live_grant_returns_true()
    test_ask_deny_and_timeout_return_false()
    test_ask_deleted_session_denies()
    test_ask_reaches_operator_from_vault_proxy()
    print("ALL WALLET-HOST OK")


if __name__ == "__main__":
    main()
