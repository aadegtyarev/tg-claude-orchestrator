"""BwrapRunner — файловая песочница bubblewrap вокруг всего процесса.

Allowlist: конфиг Claude Code (токены/скиллы/plugins/транскрипты) — RW,
бинарь claude и репозиторий оркестратора (channel_server + .venv) — RO,
плюс переданные рабочие каталоги (папка сессии/проекта) — RW.
Политика сборки argv — в sandbox.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence, TYPE_CHECKING

from . import sandbox

if TYPE_CHECKING:
    from ..config import Config


class BwrapRunner:
    name = "bwrap"
    unique_cwd = False

    def __init__(self, config: "Config", root: Path):
        self.config = config
        self.root = root  # репозиторий оркестратора (channel_server.py + .venv)

    def preflight(self) -> tuple[bool, str]:
        return sandbox.available()

    def wrap(
        self,
        argv: Sequence[str],
        *,
        chdir: Path,
        extra_rw: list[Path],
        home_dir: Path | None = None,
        publish_ports: Sequence[int] = (),
    ) -> list[str]:
        # publish_ports не нужен: сеть у bwrap общая с хостом.
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
        prefix = sandbox.build_argv(
            home=home, chdir=chdir, rw_paths=rw, ro_paths=ro, home_dir=home_dir
        )
        return prefix + list(argv)
