"""Разделение cwd у файловых команд оператора (как у /bash):

- /ls в сессии → папка проекта (effective_cwd), в главном чате → дом хоста;
  относительный аргумент (`./`, `sub`) резолвится ОТ этой базы, а не от cwd
  процесса-оркестратора (репозитория); абсолютный путь перекрывает базу.
- /new — команда главного чата: относительный путь проекта резолвится от дома
  пользователя, абсолютный — как указан.

Запуск: .venv/bin/python tests/ls_cwd_test.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.sessions import SessionManager  # noqa: E402


def _core(proj: Path) -> OrchestratorCore:
    c = OrchestratorCore.__new__(OrchestratorCore)
    c.manager = SimpleNamespace(effective_cwd=lambda s: proj)
    c.config = SimpleNamespace(sessions_dir=Path("/tmp"))
    return c


def _head(text: str) -> str:
    return text.splitlines()[0]


def test_ls_cwd_split():
    proj = Path(tempfile.mkdtemp()).resolve()
    (proj / "f.txt").write_text("x")
    home = Path(tempfile.mkdtemp()).resolve()
    (home / "h.txt").write_text("y")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        c = _core(proj)
        sess = SimpleNamespace(name="s")
        # сессия без аргумента → папка проекта
        assert _head(c.ls_text(None, sess)) == f"📁 {proj}"
        # сессия + ./ → тоже проект (относительный от базы)
        assert _head(c.ls_text("./", sess)) == f"📁 {proj}"
        # главный чат (session=None) → дом хоста
        assert _head(c.ls_text(None, None)) == f"📁 {home}"
        assert _head(c.ls_text("./", None)) == f"📁 {home}"
        # абсолютный путь перекрывает базу
        assert _head(c.ls_text(str(proj), None)) == f"📁 {proj}"
        print("OK /ls: сессия→проект, main→дом, относительный от базы, абсолютный как есть")
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home


def test_link_project_base():
    home = Path(tempfile.mkdtemp()).resolve()
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        # относительный путь → от дома пользователя (main-chat база)
        rel = SessionManager._link_project("proj/sub")
        assert Path(rel) == home / "proj" / "sub", rel
        assert (home / "proj" / "sub").is_dir()
        # абсолютный путь → как указан
        base = Path(tempfile.mkdtemp()).resolve()
        ab = SessionManager._link_project(str(base / "x"))
        assert Path(ab) == base / "x", ab
        print("OK /new: относительный от дома, абсолютный как есть")
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home


def main():
    test_ls_cwd_split()
    test_link_project_base()
    print("ALL LS-CWD OK")


if __name__ == "__main__":
    main()
