"""Юнит-контракт core/reports.py::parse_cost — разбор TUI-каши `/cost`.

Запуск: .venv/bin/python tests/reports_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.reports import parse_cost  # noqa: E402


def test_parse_cost_resets_regex():
    """Тянет cost/session%/week%/reset из настоящего блока; сужённый Resets? не
    матчит мусор вроде «Reteets»."""
    text = (
        "cost: $1.23\n"
        "Current session · 12% used\n"
        "Current week (all models) · 34% used\n"
        "Resets Jul 20 3:00pm (in 5h 20m)\n"
    )
    out = parse_cost(text)
    assert out.get("cost") == "1.23", out
    assert out.get("session_pct") == "12", out
    assert out.get("week_pct") == "34", out
    assert out.get("session_reset", "").startswith("Jul 20"), out
    assert "session_reset" not in parse_cost("Reteets X (y)\n")  # не слово Resets
    print("OK parse_cost: тянет reset/cost/%, мусор не матчится")


def test_parse_cost_per_model_and_week_reset():
    """Per-model проценты (не 'all models') и второй Resets → week_reset."""
    text = (
        "Current week (all models) · 34% used\n"
        "Current week (claude-opus-4) · 8% used\n"
        "Resets Jul 20 3:00pm (in 5h)\n"
        "Resets Jul 27 (in 7d)\n"
    )
    out = parse_cost(text)
    assert ("claude-opus-4", "8") in out.get("models", []), out
    assert out.get("week_reset", "").startswith("Jul 27"), out
    print("OK parse_cost: per-model % + week_reset (второй Resets)")


def test_parse_cost_garbage():
    """Мусор/пустой ввод → пустой dict (адаптер деградирует в usage_failed)."""
    assert parse_cost("случайный текст без цифр") == {}
    assert parse_cost("") == {}
    print("OK parse_cost: мусор → {} (деградация)")


def main():
    test_parse_cost_resets_regex()
    test_parse_cost_per_model_and_week_reset()
    test_parse_cost_garbage()
    print("ALL REPORTS OK")


if __name__ == "__main__":
    main()
