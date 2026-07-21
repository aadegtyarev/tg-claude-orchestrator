"""_pty_driver диалоги: маркеры авто-ответа матчат ДИАЛОГИ, но НЕ постоянную
строку статуса. Регресс: маркер «bypasspermissions» ложно совпадал с плашкой
«⏵⏵ bypass permissions on» и слал «2» как сообщение (замечено под agent-vm).

Запуск: .venv/bin/python tests/pty_dialogs_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.sessions import _DIALOGS  # noqa: E402


def _screen(text: str) -> str:
    """Как _pty_driver готовит экран для матча: убрать пробелы, lower."""
    return text.replace(" ", "").lower()


# Постоянная UI-плашка внизу экрана Claude Code (НЕ диалог).
STATUS_BAR = _screen(
    "⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents"
)

# Реальные стартовые диалоги (упрощённо, важен текст пункта).
DIALOG_BYPASS = _screen(
    "Bypass Permissions mode\nBy proceeding you accept...\n"
    "❯ 1. No, exit\n  2. Yes, I accept\nEnter to confirm"
)
DIALOG_TRUST = _screen("Do you trust the files in this folder?\n1. Yes, I trust this folder")
# agent-vm: managed-settings гостя. Enter = дефолтный (первый) пункт.
DIALOG_MANAGED = _screen(
    "Managed settings require approval\n❯ 1. Continue\n  2. Exit\nEnter to confirm"
)


def test_no_marker_matches_status_bar():
    """НИ один маркер авто-ответа не срабатывает на строке статуса (иначе спам «2»)."""
    hit = [m for m, _ in _DIALOGS if m in STATUS_BAR]
    assert hit == [], f"маркеры ложно матчат статус-бар: {hit}"
    print("OK ни один маркер не матчит строку статуса «bypass permissions on»")


def test_bypass_marker_matches_dialog():
    """bypass-диалог (пункт «Yes, I accept») ловится и отвечает «2»."""
    matched = [(m, keys) for m, keys in _DIALOGS if m in DIALOG_BYPASS]
    assert any(keys == b"2\r" for _, keys in matched), matched
    print("OK bypass-диалог «Yes, I accept» → ответ «2»")


def test_trust_dialog_matches():
    assert any(m in DIALOG_TRUST for m, _ in _DIALOGS)
    print("OK trust-диалог матчится")


def test_managed_settings_matches_with_enter():
    """managed-settings диалог ловится и отвечает Enter (дефолтный пункт)."""
    matched = [(m, keys) for m, keys in _DIALOGS if m in DIALOG_MANAGED]
    assert matched, "managed-settings маркер не сматчил диалог"
    assert all(keys == b"\r" for _, keys in matched), matched
    print("OK managed-settings диалог → Enter")


def main():
    test_no_marker_matches_status_bar()
    test_bypass_marker_matches_dialog()
    test_trust_dialog_matches()
    test_managed_settings_matches_with_enter()
    print("ALL PTY-DIALOGS OK")


if __name__ == "__main__":
    main()
