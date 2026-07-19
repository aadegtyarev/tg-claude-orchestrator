"""AgentVmRunner — сессии в microVM через wirenboard/agent-vm (каркас).

Дизайн: docs/agent-vm-integration.md. Статус: сборка argv и preflight готовы
и покрыты тестами; живой сквозной прогон (handshake канала через границу VM,
транскрипты, /stats) требует машины с KVM и отмечен в дизайн-доке как
отдельный ручной эксперимент — до него SANDBOX=agent-vm считать
экспериментальным.

Ключевые свойства agent-vm, на которые опирается сборка argv:
  * stateless CLI: `agent-vm claude [...args]` сам поднимает microVM и
    пробрасывает stdin/stdout — наш PTY-запуск совместим;
  * одна VM на директорию (имя = hash(cwd)) → unique_cwd=True, гвард
    в SessionManager не даст создать вторую сессию на тот же проект;
  * сеть гостя public_only → нужен --allow-host (хуки/канал стучатся на
    хостовый оркестратор) и --publish порта channel-сервера (оркестратор
    стучится внутрь);
  * креды Claude agent-vm сам держит на хосте и подменяет прокси.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

AGENT_VM_BIN = "agent-vm"


class AgentVmRunner:
    name = "agent-vm"
    unique_cwd = True  # имя VM = hash(cwd): вторая сессия убила бы VM первой
    # Отдельный /bash в VM не изолировать (unique_cwd) — отказываем, а не гоним
    # без изоляции (см. run_bash).
    supports_prefix = False

    def __init__(self, config: "Config", root: Path):
        self.config = config
        self.root = root

    def preflight(self) -> tuple[bool, str]:
        if shutil.which(AGENT_VM_BIN) is None:
            return False, (
                "agent-vm не установлен (см. github.com/wirenboard/agent-vm)"
            )
        if not Path("/dev/kvm").exists():
            return False, "нет /dev/kvm — agent-vm требует KVM"
        return True, "ok"

    def wrap(
        self,
        argv: Sequence[str],
        *,
        chdir: Path,
        extra_rw: list[Path],
        home_dir: Path | None = None,
        publish_ports: Sequence[int] = (),
    ) -> list[str]:
        """agent-vm <cmd> [--опции] -- <аргументы cmd>.

        cwd монтируется самим agent-vm; докидываем рабочие пути сессии и
        репозиторий оркестратора (channel_server внутри гостя). home_dir
        не пробрасывается: у гостя свой $HOME (Debian-образ), персистентность
        дома решается state-каталогом agent-vm, не нами.
        """
        if not argv:
            # Префикс-режим (sandbox_prefix для /bash): в VM интерактивный
            # bash отдельно от claude не заворачиваем — поднимать вторую VM
            # на тот же cwd нельзя (unique_cwd). /bash идёт без изоляции VM.
            return []
        cmd, *rest = argv
        out = [AGENT_VM_BIN, Path(cmd).name]
        # Хуки и channel_server внутри гостя должны достучаться до
        # оркестратора на хосте; оркестратор — до /notify канала в госте.
        out += ["--allow-host"]
        for port in publish_ports:
            out += ["--publish", f"{port}:{port}"]
        mounts = {str(self.root), *(str(p) for p in extra_rw if p != chdir)}
        for m in sorted(mounts):
            out += ["--mount", f"{m}:{m}"]
        if self.config.agent_vm_memory_gib:
            out += ["--memory", f"{self.config.agent_vm_memory_gib:g}G"]
        if self.config.agent_vm_cpus:
            out += ["--cpus", str(self.config.agent_vm_cpus)]
        if self.config.agent_vm_image:
            out += ["--image", self.config.agent_vm_image]
        return out + ["--", *rest]
