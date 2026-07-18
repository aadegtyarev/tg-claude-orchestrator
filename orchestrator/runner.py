"""Раннеры: как превратить команду (claude, /bash) в argv для запуска.

Шов изоляции: SessionManager и /bash не знают, во что заворачивается процесс —
они дают команду, рабочий каталог и список RW-путей, раннер возвращает готовый
argv. Сейчас два раннера (выбор — SANDBOX в .env):

  * DirectRunner — без изоляции (SANDBOX=off): argv как есть;
  * BwrapRunner  — файловая песочница bubblewrap (SANDBOX=bwrap, по умолчанию):
    argv с префиксом bwrap (см. sandbox.py), общий allowlist для claude и
    /bash — конфиг Claude Code RW, бинарь claude и репозиторий оркестратора RO.

Будущий AgentVmRunner (microVM через wirenboard/agent-vm) сядет на тот же
интерфейс: wrap(["claude", …]) → ["agent-vm", "claude", …] — см.
docs/agent-vm-integration.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, TYPE_CHECKING

from . import sandbox

if TYPE_CHECKING:
    from config import Config


class Runner(Protocol):
    """Интерфейс раннера: argv команды → argv запуска."""

    name: str

    def wrap(
        self, argv: Sequence[str], *, chdir: Path, extra_rw: list[Path]
    ) -> list[str]:
        """Завернуть команду. chdir — рабочий каталог процесса; extra_rw —
        рабочие пути этой сессии (папка сессии/проекта), доступные на запись."""
        ...


class DirectRunner:
    """Без изоляции: команда запускается как есть (SANDBOX=off)."""

    name = "direct"

    def wrap(
        self, argv: Sequence[str], *, chdir: Path, extra_rw: list[Path]
    ) -> list[str]:
        return list(argv)


class BwrapRunner:
    """Файловая песочница bubblewrap вокруг всего процесса (SANDBOX=bwrap).

    Allowlist: конфиг Claude Code (токены/скиллы/plugins/транскрипты) — RW,
    бинарь claude и репозиторий оркестратора (channel_server + .venv) — RO,
    плюс переданные рабочие каталоги (папка сессии/проекта) — RW.
    """

    name = "bwrap"

    def __init__(self, config: "Config", root: Path):
        self.config = config
        self.root = root  # репозиторий оркестратора (channel_server.py + .venv)

    def wrap(
        self, argv: Sequence[str], *, chdir: Path, extra_rw: list[Path]
    ) -> list[str]:
        home = Path.home()
        config_dir = self.config.claude_config_dir or (home / ".claude")
        rw = [
            *extra_rw,
            config_dir,
            home / ".claude.json",  # глобальное состояние claude (может писаться)
            *self.config.sandbox_extra_rw,
        ]
        ro = [
            home / ".local" / "share" / "claude",  # бинарь и versions/
            home / ".local" / "bin",               # симлинк claude
            self.root,                              # channel_server.py + .venv
        ]
        prefix = sandbox.build_argv(home=home, chdir=chdir, rw_paths=rw, ro_paths=ro)
        return prefix + list(argv)


def make_runner(config: "Config", root: Path) -> Runner:
    """Раннер по конфигу (валидацию значения SANDBOX делает config.py)."""
    if config.sandbox == "bwrap":
        return BwrapRunner(config, root)
    return DirectRunner()
