"""Юнит-контракт SubagentNaming (core/subagentnaming.py) — сопоставление
agent_id → тип сабагента для строки «✅ Сабагент <тип> завершил».

Запуск: .venv/bin/python tests/subagentnaming_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.subagentnaming import SubagentNaming  # noqa: E402


def test_exact_match_by_agent_id():
    """Точный источник: тип из дочернего события снимается по agent_id."""
    sn = SubagentNaming()
    sn.note_child("s", "a1", "dev-planner")
    assert sn.pop("s", "a1") == "dev-planner"
    # тип расходуется один раз — повторный pop уже не находит
    assert sn.pop("s", "a1") == ""
    print("OK точный матч по agent_id, тип расходуется")


def test_spawn_fallback_in_order():
    """Фолбэк без agent_id: типы из спавнов снимаются по порядку запуска."""
    sn = SubagentNaming()
    sn.note_spawn("s", "dev-planner")
    sn.note_spawn("s", "dev-builder")
    # agent_id неизвестен в _types → берём самый ранний спавн
    assert sn.pop("s", "x1") == "dev-planner"
    assert sn.pop("s", "x2") == "dev-builder"
    assert sn.pop("s", "x3") == ""
    print("OK фолбэк по порядку спавнов")


def test_child_wins_over_spawn():
    """Точный матч по agent_id приоритетнее фолбэка на спавн."""
    sn = SubagentNaming()
    sn.note_spawn("s", "dev-planner")       # фолбэк
    sn.note_child("s", "a2", "dev-builder")  # точный
    assert sn.pop("s", "a2") == "dev-builder"   # точный выигрывает
    assert sn.pop("s", "zz") == "dev-planner"   # затем остаётся фолбэк
    print("OK точный матч приоритетнее спавн-фолбэка")


def test_unknown_degrades_to_empty():
    """Ничего не известно → '' (мягкая деградация, безымянная строка)."""
    sn = SubagentNaming()
    assert sn.pop("s", "a1") == ""
    print("OK неизвестный сабагент → '' (мягкая деградация)")


def test_forget_and_isolation():
    """forget снимает всё состояние сессии; сессии изолированы."""
    sn = SubagentNaming()
    sn.note_child("a", "a1", "dev-planner")
    sn.note_spawn("a", "dev-builder")
    sn.note_child("b", "b1", "dev-reviewer")
    sn.forget("a")
    assert sn.pop("a", "a1") == "" and sn.pop("a", "zz") == ""  # и _types, и _spawns
    assert sn.pop("b", "b1") == "dev-reviewer"                  # чужая сессия цела
    print("OK forget чистит сессию, сессии изолированы")


def main():
    test_exact_match_by_agent_id()
    test_spawn_fallback_in_order()
    test_child_wins_over_spawn()
    test_unknown_degrades_to_empty()
    test_forget_and_isolation()
    print("ALL SUBAGENTNAMING OK")


if __name__ == "__main__":
    main()
