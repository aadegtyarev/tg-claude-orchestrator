"""Регрессия инкрементального чтения claude.log (_read_log_delta).

Раньше _error_relay_loop читал ВЕСЬ лог каждые 6 c; теперь только приращение
через seek (REVIEW.md B5). Чистая функция: проверяем приращение, дописывание
и усечение/ротацию (offset > size → с начала).

Запуск: .venv/bin/python tests/logdelta_test.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.turn import read_log_delta as _read_log_delta  # noqa: E402


def main():
    log = Path(tempfile.mktemp())
    log.write_bytes(b"aaa")
    delta, off = _read_log_delta(log, 0)
    assert delta == b"aaa" and off == 3, (delta, off)
    print("OK первый read с 0 → весь файл")

    # дописали — получаем только приращение
    with open(log, "ab") as f:
        f.write(b"bbb")
    delta, off = _read_log_delta(log, off)
    assert delta == b"bbb" and off == 6, (delta, off)
    print("OK приращение: только новый хвост")

    # ничего нового → пустой delta, offset прежний
    delta, off = _read_log_delta(log, off)
    assert delta == b"" and off == 6, (delta, off)
    print("OK нет приращения → пусто")

    # ротация/усечение (файл стал короче offset) → читаем с начала
    log.write_bytes(b"xy")  # 2 байта < offset 6
    delta, off = _read_log_delta(log, 6)
    assert delta == b"xy" and off == 2, (delta, off)
    print("OK ротация (size<offset) → с начала")

    log.unlink()
    print("ALL LOGDELTA OK")


if __name__ == "__main__":
    main()
