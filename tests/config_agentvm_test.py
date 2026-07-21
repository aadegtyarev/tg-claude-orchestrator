"""Config.guest_orch_host — адрес оркестратора С ТОЧКИ ЗРЕНИЯ СЕССИИ (гостя).

Переключение движка изоляции — ОДНО действие (SANDBOX в .env): guest-facing адрес
выводится автоматически. Под agent-vm гость VM не видит хостовый loopback —
microsandbox мапит host.microsandbox.internal на хостовый 127.0.0.1 (+ раннер даёт
--allow-host); reply-сервер при этом остаётся на orch_host (bind, host-side).

Запуск: .venv/bin/python tests/config_agentvm_test.py
"""
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.config import AGENT_VM_GUEST_HOST, Config  # noqa: E402


def _base() -> Config:
    return Config.from_env()


def test_guest_host_bwrap_off_is_loopback():
    """bwrap/off: гость = orch_host (общий с хостом loopback) — как раньше."""
    base = _base()
    for sb in ("bwrap", "off"):
        c = replace(base, sandbox=sb, orch_host="127.0.0.1")
        assert c.guest_orch_host == "127.0.0.1", sb
    print("OK guest_orch_host: bwrap/off → orch_host (127.0.0.1)")


def test_guest_host_agentvm_is_gateway():
    """agent-vm: гость видит хост по имени microsandbox-гейтвея; bind (orch_host)
    остаётся хостовым loopback — reply_server биндится на него, не на имя гостя."""
    c = replace(_base(), sandbox="agent-vm", orch_host="127.0.0.1")
    assert c.guest_orch_host == AGENT_VM_GUEST_HOST == "host.microsandbox.internal"
    assert c.orch_host == "127.0.0.1", "bind остаётся host-resolvable loopback"
    print("OK guest_orch_host: agent-vm → host.microsandbox.internal (bind ≠ гость)")


def test_one_action_switch():
    """Смена ТОЛЬКО sandbox переключает guest-facing адрес — без правки orch_host."""
    base = replace(_base(), orch_host="127.0.0.1")
    assert replace(base, sandbox="bwrap").guest_orch_host == "127.0.0.1"
    assert replace(base, sandbox="agent-vm").guest_orch_host == "host.microsandbox.internal"
    print("OK переключение движка = одно действие (только SANDBOX)")


def main():
    test_guest_host_bwrap_off_is_loopback()
    test_guest_host_agentvm_is_gateway()
    test_one_action_switch()
    print("ALL CONFIG-AGENTVM OK")


if __name__ == "__main__":
    main()
