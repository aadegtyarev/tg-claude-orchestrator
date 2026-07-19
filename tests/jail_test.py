"""Регрессия jail'а send_file_to_user (core.path_in_workspace).

Без jail промпт-инъекция из чужого файла/CLAUDE.md могла заставить Клода
вызвать send_file_to_user на ~/.ssh/id_rsa или .env и выслать секреты в
чат (REVIEW.md S2). Здесь покрыты: легитимные пути (проект/сессия/incoming),
внешние абсолютные, `..`, симлинк-эскейп.

Запуск: .venv/bin/python tests/jail_test.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import OrchestratorCore  # noqa: E402


def make_core(root: Path, effective_cwd) -> OrchestratorCore:
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.manager = SimpleNamespace(effective_cwd=effective_cwd)
    core.config = SimpleNamespace(sessions_dir=root, incoming_dir="incoming")
    return core


def main():
    root = Path(tempfile.mkdtemp())
    sess_dir = root / "proj-sess"
    sess_dir.mkdir()
    (sess_dir / "incoming").mkdir()
    (sess_dir / "good.txt").write_text("ok")
    (sess_dir / "incoming" / "photo.jpg").write_text("x")
    # симлинк внутри сессии → внешний файл (эскейп)
    link = sess_dir / "escape"
    os.symlink("/etc/passwd", link)

    core = make_core(root, lambda s: s.session_dir)  # без linked_path
    session = SimpleNamespace(session_dir=sess_dir, linked_path=None)

    inside = [
        sess_dir / "good.txt",
        sess_dir / "incoming" / "photo.jpg",
        sess_dir / "subdir" / ".." / "good.txt",      # нормализуется внутрь
    ]
    outside = [
        Path("/etc/passwd"),
        Path("/etc/shadow"),
        link,                                          # симлинк-эскейп
        sess_dir.parent / "neighbour.txt",            # соседняя папка
    ]
    for p in inside:
        assert core.path_in_workspace(p, session) is True, f"должен быть внутри: {p}"
    for p in outside:
        assert core.path_in_workspace(p, session) is False, f"должен быть снаружи: {p}"
    print("OK session: проект/incoming внутри; /etc, симлинк, сосед — снаружи")

    # linked_path: cwd = проект, файл проекта — внутри, сессионный лог — тоже
    proj = Path(tempfile.mkdtemp())
    (proj / "src").mkdir()
    (proj / "src" / "app.py").write_text("x")
    core2 = make_core(root, lambda s: proj)
    session2 = SimpleNamespace(session_dir=sess_dir, linked_path=str(proj))
    assert core2.path_in_workspace(proj / "src" / "app.py", session2) is True
    assert core2.path_in_workspace(sess_dir / "claude.log", session2) is True  # session_dir тоже корень
    assert core2.path_in_workspace(Path("/etc/passwd"), session2) is False
    print("OK linked_path: файлы проекта и сессии внутри; /etc — снаружи")

    print("ALL JAIL OK")


def test_jail():
    main()

if __name__ == "__main__":
    main()
