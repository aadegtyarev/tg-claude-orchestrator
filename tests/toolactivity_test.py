"""Юнит-контракт ToolActivity (core/toolactivity.py) — чистое per-session
состояние «какой тул идёт сейчас»: кнопка ⏭ и сигнал жизни для вотчдога.

Запуск: .venv/bin/python tests/toolactivity_test.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import toolactivity  # noqa: E402
from orchestrator.core.toolactivity import ToolActivity  # noqa: E402


def test_inflight_lifecycle():
    """start → inflight/foreground истинны; finish опустошает → grace-хвост."""
    ta = ToolActivity()
    assert ta.inflight("s") is False and ta.foreground_active("s") is False
    ta.start("s", "t1")
    assert ta.inflight("s") is True and ta.foreground_active("s") is True
    assert ta.unblock_action("s") == "background"
    ta.finish("s", "t1")
    # inflight пуст, но grace-окно ещё держит foreground/кнопку
    assert ta.inflight("s") is False
    assert ta.foreground_active("s") is True
    assert ta.unblock_action("s") == "background"
    print("OK lifecycle: start→inflight, finish→grace держит ⏭")


def test_empty_tool_use_id_ignored():
    """Пустой tool_use_id не создаёт in-flight (нечего сматчить с PostToolUse)."""
    ta = ToolActivity()
    ta.start("s", "")
    assert ta.inflight("s") is False and ta.unblock_action("s") is None
    print("OK пустой tool_use_id игнорируется")


def test_grace_expiry():
    """Когда grace-окно истекло — foreground гаснет (проверяем через отмотку
    _cleared_at в прошлое, без реального sleep)."""
    ta = ToolActivity()
    ta.start("s", "t1")
    ta.finish("s", "t1")
    ta._cleared_at["s"] = time.monotonic() - toolactivity.UNBLOCK_GRACE - 1
    assert ta.foreground_active("s") is False
    assert ta.unblock_action("s") is None
    print("OK grace истёк → ⏭ гаснет")


def test_taskoutput_is_kick():
    """last_tool == TaskOutput → ⏭ означает 'kick' (Esc), приоритет над grace."""
    ta = ToolActivity()
    ta.note_tool("s", "TaskOutput")
    assert ta.unblock_action("s") == "kick"
    # даже если параллельно есть grace-хвост — TaskOutput выигрывает
    ta.start("s", "t1")
    ta.finish("s", "t1")
    assert ta.unblock_action("s") == "kick"
    print("OK TaskOutput → kick (приоритет над background)")


def test_forget_clears_all():
    """forget снимает ВСЁ состояние сессии: last_tool + inflight + grace."""
    ta = ToolActivity()
    ta.note_tool("s", "TaskOutput")
    ta.start("s", "t1")
    ta.start("s", "t2")
    ta.finish("s", "t2")         # t1 ещё в работе → _cleared_at пока НЕ выставлен
    ta.finish("s", "t1")         # набор опустел → _cleared_at заполнен (grace-хвост)
    # Предусловие: все три диктата реально населены (иначе forget-проверка
    # прошла бы тривиально — см. ревью).
    assert "s" in ta._last_tool and "s" in ta._inflight and "s" in ta._cleared_at
    assert ta.unblock_action("s") == "kick"
    ta.forget("s")
    assert ta.last_tool("s") is None
    assert ta.inflight("s") is False
    assert ta.foreground_active("s") is False
    assert ta.unblock_action("s") is None
    # и внутренние словари реально пусты для сессии (без утечки пустых контейнеров)
    assert "s" not in ta._last_tool and "s" not in ta._inflight and "s" not in ta._cleared_at
    print("OK forget: всё состояние сессии снято")


def test_finish_unknown_session_noop():
    """finish для сессии без открытых тулов (или чужой id) — безопасный no-op."""
    ta = ToolActivity()
    ta.finish("nobody", "x")           # сессии нет в _inflight — не падаем
    assert ta.inflight("nobody") is False and "nobody" not in ta._cleared_at
    ta.start("s", "t1")
    ta.finish("s", "other")            # чужой id — t1 остаётся, набор не опустел
    assert ta.inflight("s") is True and "s" not in ta._cleared_at
    print("OK finish: неизвестная сессия/чужой id — безопасный no-op")


def test_sessions_isolated():
    """Состояние сессий не пересекается."""
    ta = ToolActivity()
    ta.start("a", "t1")
    assert ta.unblock_action("a") == "background"
    assert ta.unblock_action("b") is None
    ta.forget("a")
    assert ta.unblock_action("a") is None
    print("OK сессии изолированы")


def main():
    test_inflight_lifecycle()
    test_empty_tool_use_id_ignored()
    test_grace_expiry()
    test_taskoutput_is_kick()
    test_forget_clears_all()
    test_finish_unknown_session_noop()
    test_sessions_isolated()
    print("ALL TOOLACTIVITY OK")


if __name__ == "__main__":
    main()
