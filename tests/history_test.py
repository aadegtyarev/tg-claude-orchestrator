"""Юнит-контракт HistoryLog (core/history.py): кольцевой журнал событий на
сессию + персист веб-истории через graceful-рестарт.

Запуск: .venv/bin/python tests/history_test.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import history as histmod  # noqa: E402
from orchestrator.core.history import HistoryLog  # noqa: E402


def test_record_and_events():
    """record добавляет событие (ts+kind+payload); events отдаёт КОПИЮ."""
    h = HistoryLog(Path("unused.json"))
    h.record("s", "reply", text="привет")
    ev = h.events("s")
    assert len(ev) == 1 and ev[0]["kind"] == "reply" and ev[0]["text"] == "привет"
    assert "ts" in ev[0]
    ev.append({"kind": "hack"})            # мутация копии не трогает журнал
    assert len(h.events("s")) == 1
    assert h.events("unknown") == []       # неизвестная сессия — пусто
    print("OK record/events: событие записано, events отдаёт копию")


def test_ring_buffer_cap():
    """Журнал сессии не растёт сверх HISTORY_LIMIT (вытесняет старые)."""
    h = HistoryLog(Path("unused.json"))
    for i in range(histmod.HISTORY_LIMIT + 50):
        h.record("s", "tool", n=i)
    ev = h.events("s")
    assert len(ev) == histmod.HISTORY_LIMIT
    assert ev[0]["n"] == 50 and ev[-1]["n"] == histmod.HISTORY_LIMIT + 49  # старые ушли
    print("OK кольцевой буфер: обрезка по HISTORY_LIMIT, старые вытеснены")


def test_forget():
    """forget снимает журнал сессии; другие сессии целы."""
    h = HistoryLog(Path("unused.json"))
    h.record("a", "reply", text="x")
    h.record("b", "reply", text="y")
    h.forget("a")
    assert h.events("a") == [] and h.events("b")[0]["text"] == "y"
    print("OK forget: журнал сессии снят, соседняя цела")


def test_persist_roundtrip():
    """save → load восстанавливает журнал (переживает graceful-рестарт)."""
    d = Path(tempfile.mkdtemp(prefix="hist_"))
    path = d / "history.json"
    h1 = HistoryLog(path)
    h1.record("s", "reply", text="сохранись")
    h1.record("s", "status", status="stopped")
    h1.save()

    h2 = HistoryLog(path)
    assert h2.events("s") == []  # до load — пусто (конструктор не трогает диск)
    h2.load()
    ev = h2.events("s")
    assert len(ev) == 2 and ev[0]["text"] == "сохранись" and ev[1]["status"] == "stopped"
    print("OK персист: save→load восстанавливает журнал")


def test_load_missing_and_broken():
    """Отсутствующий/битый файл → пустая история, без исключений."""
    h = HistoryLog(Path("/nonexistent-dir/history.json"))
    h.load()  # файла нет
    assert h.events("s") == []
    d = Path(tempfile.mkdtemp(prefix="hist_"))
    broken = d / "history.json"
    broken.write_text("{не json")
    h2 = HistoryLog(broken)
    h2.load()  # битый JSON
    assert h2.events("s") == []
    print("OK load: отсутствующий/битый файл → пустая история")


def main():
    test_record_and_events()
    test_ring_buffer_cap()
    test_forget()
    test_persist_roundtrip()
    test_load_missing_and_broken()
    print("ALL HISTORY OK")


if __name__ == "__main__":
    main()
