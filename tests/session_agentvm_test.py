"""Провижининг сессии под agent-vm: интерпретатор channel_server/хуков.

Под agent-vm канал и хук-диспетчер спавнятся ВНУТРИ гостя microVM, где хостового
venv (sys.executable) нет — берём системный python3 (канал/хук на stdlib). Под
bwrap/off — sys.executable как раньше.

Запуск: .venv/bin/python tests/session_agentvm_test.py
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.core.sessions import SessionManager  # noqa: E402


def _mgr(sandbox: str) -> SessionManager:
    m = SessionManager.__new__(SessionManager)  # без __init__ — нужен только config
    m.config = SimpleNamespace(sandbox=sandbox)
    return m


def test_guest_python_agentvm():
    """agent-vm → системный python3 (хостового venv в госте нет)."""
    assert _mgr("agent-vm")._guest_python() == "python3"
    print("OK _guest_python: agent-vm → python3")


def test_guest_python_bwrap_off():
    """bwrap/off → sys.executable (хостовый venv), как раньше."""
    assert _mgr("bwrap")._guest_python() == sys.executable
    assert _mgr("off")._guest_python() == sys.executable
    print("OK _guest_python: bwrap/off → sys.executable")


def main():
    test_guest_python_agentvm()
    test_guest_python_bwrap_off()
    print("ALL SESSION-AGENTVM OK")


if __name__ == "__main__":
    main()
