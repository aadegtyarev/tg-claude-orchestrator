"""Регрессия вотчдога: is_busy отличает живую работу от зависания.

Раньше вотчдог судил «завис/не завис» только по байтам claude.log и ложно
кричал «завис» во время долгого тула или «молчащего» спиннера. Теперь признак
жизни — рост CPU-времени дерева процессов claude (он сам + запущенные тулы).

Запуск: .venv/bin/python tests/watchdog_test.py
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.proctree import proc_tree_signals as _proc_tree_signals  # noqa: E402
from orchestrator.core.sessions import Session, SessionManager  # noqa: E402


def _burn(n: int = 3_000_000) -> int:
    """Пожечь CPU текущего процесса — utime+stime должны заметно вырасти."""
    s = 0
    for i in range(n):
        s += i * i
    return s


def main():
    me = os.getpid()

    # ── _proc_tree_signals: CPU живого процесса растёт, дети детектятся ──
    cpu1, kids1 = _proc_tree_signals(me)
    _burn()
    cpu2, kids2 = _proc_tree_signals(me)
    assert cpu2 > cpu1, f"CPU живого процесса должен расти: {cpu1} -> {cpu2}"
    assert kids1 is False and kids2 is False   # у тест-процесса нет детей
    print("OK _proc_tree_signals: CPU растёт, дочерние процессы детектятся")

    # ── is_busy: после CPU-нагрузки — True (сессия жива) ──
    mgr = SessionManager(config=object())
    sess = Session(
        name="t", port=0, session_dir=Path("/tmp"),
        claude_session_id="x",
        process=SimpleNamespace(pid=me, returncode=None),
    )
    mgr.is_busy(sess)            # базовый отсчёт CPU
    _burn()
    assert mgr.is_busy(sess) is True, "ожившая работа должна давать busy=True"
    print("OK is_busy: CPU вырос -> True")

    # ── is_busy: процесс умер (returncode задан) -> не жив ──
    sess.process.returncode = 0
    assert mgr.is_busy(sess) is False
    print("OK is_busy: мёртвый процесс -> False")

    # ── is_busy: процесса нет -> не жив ──
    sess.process = None
    assert mgr.is_busy(sess) is False
    print("OK is_busy: нет процесса -> False")

    print("ALL WATCHDOG OK")


def test_watchdog():
    main()

if __name__ == "__main__":
    main()
