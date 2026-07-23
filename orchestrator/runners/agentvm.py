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
import subprocess
from pathlib import Path
from typing import Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

AGENT_VM_BIN = "agent-vm"

# Чем claude может авторизоваться у СВОЕГО (не agent-vm) эндпоинта.
AUTH_KEYS = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}


def auth_problem(claude_env: dict[str, str]) -> str | None:
    """Почему свой ANTHROPIC_BASE_URL не заработает под agent-vm (или None).

    Замерено живьём: в госте у claude СВОИХ кред нет — их подставляет прокси
    agent-vm на проводе, и только для СВОЕГО эндпоинта. При своём base_url
    подстановки нет, и claude падает «Execution error» ещё до запроса. С явным
    токеном тот же путь работает (запросы дошли до хостового прокси). Молчать
    нельзя — оператор получил бы нерабочие сессии без объяснения.
    """
    if "ANTHROPIC_BASE_URL" not in claude_env or (AUTH_KEYS & claude_env.keys()):
        return None
    keys = " или ".join(f"CLAUDE_ENV_{k}" for k in sorted(AUTH_KEYS))
    return (
        f"CLAUDE_ENV_ANTHROPIC_BASE_URL задан, но нет токена ({keys}). "
        "Под SANDBOX=agent-vm кред-прокси agent-vm подставляет токен только "
        "для своего эндпоинта — для своего прокси нужен свой токен, иначе "
        "сессии падают без внятной ошибки. Добавь токен или убери свой "
        "base_url (в VM трафик к Anthropic ведёт сам agent-vm)."
    )


def egress_hosts(claude_env: dict[str, str], host_ip: str | None) -> list[str]:
    """Хостовые адреса из CLAUDE_ENV_*, которым нужен `--allow-egress`.

    Config уже переписал loopback на LAN-адрес хоста; здесь ищем именно его,
    потому что политика гостя по умолчанию (`public_only`) запрещает RFC1918 —
    без явного разрешения прокси оператора из VM недостижим.
    """
    if not host_ip:
        return []
    return [host_ip] if any(host_ip in v for v in claude_env.values()) else []


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
        problem = auth_problem(self.config.claude_env)
        if problem:
            return False, problem
        if self.config.agent_vm_egress_proxy and not self._supports_egress_flags():
            return False, (
                "AGENT_VM_EGRESS_PROXY задан, но установленный agent-vm не знает "
                "--egress-proxy: это флаги форка (docs/FORK-agent-vm-egress-proxy.md). "
                "Апстримный бинарь упал бы на «unexpected argument», а без флага "
                "egress гостя молча MITM-ит собственный прокси agent-vm и кошелёк "
                "обойдён — поэтому отказываем сразу. Поставь форк или убери "
                "AGENT_VM_EGRESS_PROXY."
            )
        ca = self.config.agent_vm_egress_ca
        if ca and not ca.is_file():
            return False, f"AGENT_VM_EGRESS_CA={ca} — файла нет (нужен PEM CA прокси)."
        return True, "ok"

    def _supports_egress_flags(self) -> bool:
        """Знает ли установленный agent-vm про --egress-proxy (форк или апстрим).

        Спрашиваем сам бинарь (`--help`), а не версию: флаг может приехать в
        апстрим, и тогда форк перестанет быть нужен — проверка переживёт это
        без правок. Сбой запуска считаем «не знает»: лучше честный отказ на
        старте, чем падение каждой сессии.
        """
        try:
            out = subprocess.run(
                [AGENT_VM_BIN, "claude", "--help"],
                capture_output=True, timeout=20,
            )
        except Exception:  # noqa: BLE001 — любой сбой = считаем, что флага нет
            return False
        return b"--egress-proxy" in out.stdout + out.stderr

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
        # Прокси оператора (CLAUDE_ENV_ANTHROPIC_BASE_URL и т.п.) живёт на
        # ХОСТЕ. Гостю его LAN-адрес по умолчанию запрещён политикой
        # public_only — открываем ровно этот адрес, не всю LAN (--allow-lan
        # дал бы гостю всю подсеть). Config уже переписал loopback на него.
        for host_ip in egress_hosts(
            self.config.claude_env, self.config.agent_vm_host_ip
        ):
            out += ["--allow-egress", host_ip]
        # Egress гостя на наш прокси (кошелёк). Без этого весь HTTPS гостя
        # прозрачно MITM-ит собственный прокси agent-vm своим CA, и наш
        # перехват обойдён (замер F10 в ARCHITECTURE-claude-box.md). Флаги
        # понимает форк (docs/FORK-agent-vm-egress-proxy.md); preflight
        # проверяет их наличие, чтобы апстримный бинарь не падал на
        # «unexpected argument» без объяснения.
        if self.config.agent_vm_egress_proxy:
            out += ["--egress-proxy", self.config.agent_vm_egress_proxy]
            if self.config.agent_vm_egress_ca:
                # CA доверяется на UPSTREAM-плече перехвата, в гостя не едет.
                out += ["--egress-ca", str(self.config.agent_vm_egress_ca)]
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
