"""Провижининг сессии под agent-vm: интерпретатор channel_server/хуков.

Под agent-vm канал и хук-диспетчер спавнятся ВНУТРИ гостя microVM, где хостового
venv (sys.executable) нет — берём системный python3 (канал/хук на stdlib). Под
bwrap/off — sys.executable как раньше.

Запуск: .venv/bin/python tests/session_agentvm_test.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.core.sessions import SessionManager  # noqa: E402


def _mgr(sandbox: str) -> SessionManager:
    m = SessionManager.__new__(SessionManager)  # без __init__ — нужен только config
    m.config = SimpleNamespace(
        sandbox=sandbox,
        guest_orch_host="127.0.0.1",
        orch_port=18080,
        orch_token="tok",
    )
    return m


def _mcp_env(sandbox: str) -> dict:
    """Написать .mcp.json для фейковой сессии и вернуть env channel-сервера."""
    with tempfile.TemporaryDirectory() as d:
        session = SimpleNamespace(name="s", port=18600, session_dir=Path(d))
        _mgr(sandbox)._write_mcp_json(session)
        mcp = json.loads((Path(d) / ".mcp.json").read_text())
    return mcp["mcpServers"]["channel-s"]["env"]


def test_guest_python_agentvm():
    """agent-vm → системный python3 (хостового venv в госте нет)."""
    assert _mgr("agent-vm")._guest_python() == "python3"
    print("OK _guest_python: agent-vm → python3")


def test_guest_python_bwrap_off():
    """bwrap/off → sys.executable (хостовый venv), как раньше."""
    assert _mgr("bwrap")._guest_python() == sys.executable
    assert _mgr("off")._guest_python() == sys.executable
    print("OK _guest_python: bwrap/off → sys.executable")


def test_channel_host_agentvm():
    """agent-vm → CHANNEL_HOST=0.0.0.0 (docker-style --publish не достаёт loopback)."""
    assert _mcp_env("agent-vm")["CHANNEL_HOST"] == "0.0.0.0"
    print("OK CHANNEL_HOST: agent-vm → 0.0.0.0")


def test_channel_host_bwrap_off():
    """bwrap/off → CHANNEL_HOST=127.0.0.1 (общий loopback с хостом), как раньше."""
    assert _mcp_env("bwrap")["CHANNEL_HOST"] == "127.0.0.1"
    assert _mcp_env("off")["CHANNEL_HOST"] == "127.0.0.1"
    print("OK CHANNEL_HOST: bwrap/off → 127.0.0.1")


def main():
    test_guest_python_agentvm()
    test_guest_python_bwrap_off()
    test_channel_host_agentvm()
    test_channel_host_bwrap_off()
    print("ALL SESSION-AGENTVM OK")


if __name__ == "__main__":
    main()
