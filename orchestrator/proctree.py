"""Сигналы живости дерева процессов по /proc (Linux).

Вотчдог судит «завис/не завис» не только по байтам лога: спиннер «almost
done» может на секунды замолчать в нормальной работе — это не зависание.
Надёжный сигнал — CPU-время дерева процессов claude (он сам + запущенные
им тулы): если сумма utime+stime не растёт и дочерних процессов нет,
процесс правда стоит на месте.
"""

from __future__ import annotations

import os


def proc_tree_signals(root: int) -> tuple[int, bool]:
    """(сумма CPU-тиков дерева root, есть ли у root живые дочерние процессы).

    Один проход по /proc: для каждого процесса берём PPID (поле 4) и
    utime+stime (поля 14+15). Поле comm (2) может содержать пробелы и скобки,
    поэтому режем по последней ')' и нумеруем поля от неё.
    """
    by_ppid: dict[int, list[int]] = {}
    ticks: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0, False
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                raw = fh.read()
            after = raw[raw.rindex(b")") + 1:].split()
            ppid = int(after[1])              # поле 4 (ppid)
            tick = int(after[11]) + int(after[12])  # поля 14+15 (utime+stime)
        except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
            continue
        by_ppid.setdefault(ppid, []).append(int(name))
        ticks[int(name)] = tick
    if root not in ticks:
        return 0, False
    total = 0
    frontier = [root]
    seen: set[int] = set()
    while frontier:
        nxt: list[int] = []
        for pid in frontier:
            if pid in seen:
                continue
            seen.add(pid)
            total += ticks.get(pid, 0)
            nxt.extend(by_ppid.get(pid, ()))
        frontier = nxt
    return total, bool(by_ppid.get(root))
