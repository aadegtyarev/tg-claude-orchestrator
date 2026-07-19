"""Регресс §1: linked-папка с project-хуками → предупреждение оператору.

При linked-сессии claude авто-доверяет чужой папке и исполняет её
project-хуки. Это не блокируется (модель угроз — страховки, папку выбрал
оператор), но должно быть ВИДНО в логах. Проверяем, что ругань появляется
на папке с хуками/.mcp.json и молчит на чистой.

Запуск: .venv/bin/python tests/link_trust_test.py
"""
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import sessions  # noqa: E402
from orchestrator.core.sessions import SessionManager  # noqa: E402


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs: list[str] = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def _warn_msgs(project_dir: Path) -> list[str]:
    cap = _Capture()
    sessions.logger.addHandler(cap)
    old = sessions.logger.level
    sessions.logger.setLevel(logging.WARNING)
    try:
        SessionManager._warn_project_trust(project_dir)
    finally:
        sessions.logger.removeHandler(cap)
        sessions.logger.setLevel(old)
    return cap.msgs


def test_warns_on_project_hooks():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "foreign"
        (proj / ".claude").mkdir(parents=True)
        (proj / ".claude" / "settings.json").write_text('{"hooks": {"Stop": []}}')
        msgs = _warn_msgs(proj)
    assert any("settings.json" in m for m in msgs), msgs
    print("OK предупреждение при linked-папке с project-хуками")


def test_warns_on_mcp_json():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "foreign"
        proj.mkdir()
        (proj / ".mcp.json").write_text("{}")
        msgs = _warn_msgs(proj)
    assert any(".mcp.json" in m for m in msgs), msgs
    print("OK предупреждение при .mcp.json в linked-папке")


def test_no_warn_on_clean():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "clean"
        proj.mkdir()
        # settings.json без хуков — не повод ругаться.
        (proj / ".claude").mkdir()
        (proj / ".claude" / "settings.json").write_text('{"model": "opus"}')
        msgs = _warn_msgs(proj)
    assert msgs == [], msgs
    print("OK нет предупреждения на чистой папке (без хуков/mcp)")


if __name__ == "__main__":
    test_warns_on_project_hooks()
    test_warns_on_mcp_json()
    test_no_warn_on_clean()
    print("ALL LINK-TRUST OK")
