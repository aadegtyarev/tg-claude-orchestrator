"""AgentVmRunner: сборка argv без реальной VM (KVM не требуется).

Живой сквозной прогон — отдельный ручной эксперимент (см.
docs/agent-vm-integration.md); здесь фиксируем контракт argv и preflight.

Запуск: .venv/bin/python tests/runner_agentvm_test.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.runners.agentvm import AgentVmRunner  # noqa: E402
from orchestrator.runners.direct import DirectRunner  # noqa: E402


def cfg(**kw):
    base = dict(agent_vm_memory_gib=None, agent_vm_cpus=None, agent_vm_image=None)
    base.update(kw)
    return SimpleNamespace(**base)


def main():
    root = Path("/opt/orch")
    r = AgentVmRunner(cfg(), root)
    assert r.unique_cwd is True  # имя VM = hash(cwd): вторая сессия запрещена

    argv = r.wrap(
        ["claude", "--session-id=u", "--mcp-config", "/s/.mcp.json"],
        chdir=Path("/proj"),
        extra_rw=[Path("/s"), Path("/proj")],
        publish_ports=[18761],
    )
    s = " ".join(argv)
    assert argv[0] == "agent-vm" and argv[1] == "claude", argv[:2]
    assert "--allow-host" in argv, "хуки/канал должны достучаться до хоста"
    assert "--publish 18761:18761" in s, "порт channel-сервера наружу"
    assert "--mount /opt/orch:/opt/orch" in s, "репозиторий (channel_server) в госте"
    assert "--mount /s:/s" in s, "папка сессии в госте"
    assert "--mount /proj:/proj" not in s, "cwd agent-vm монтирует сам"
    # Аргументы claude идут после -- нетронутыми.
    sep = argv.index("--")
    assert argv[sep + 1:] == ["--session-id=u", "--mcp-config", "/s/.mcp.json"], argv[sep:]
    print("OK agent-vm: argv (allow-host, publish, mounts, `--`)")

    argv = AgentVmRunner(
        cfg(agent_vm_memory_gib=8, agent_vm_cpus=4, agent_vm_image="ghcr.io/x:pin"),
        root,
    ).wrap(["claude"], chdir=Path("/p"), extra_rw=[], publish_ports=[])
    s = " ".join(argv)
    assert "--memory 8G" in s and "--cpus 4" in s and "--image ghcr.io/x:pin" in s
    print("OK agent-vm: ресурсы и пин образа из конфига")

    # Префикс-режим (/bash): пусто — вторую VM на тот же cwd поднимать нельзя.
    assert r.wrap([], chdir=Path("/p"), extra_rw=[]) == []
    print("OK agent-vm: пустой префикс для /bash")

    d = DirectRunner()
    assert d.preflight() == (True, "ok")
    assert d.wrap(["x", "y"], chdir=Path("/p"), extra_rw=[]) == ["x", "y"]
    print("OK direct: argv как есть")

    test_preflight_no_binary()
    test_preflight_no_kvm()
    test_preflight_ok()

    print("ALL RUNNER OK")


def test_preflight_no_binary():
    """Нет бинаря agent-vm в PATH → (False, «не установлен»), /dev/kvm не проверяется."""
    from unittest import mock
    from orchestrator.runners import agentvm
    r = AgentVmRunner(cfg(), Path("/opt/orch"))
    with mock.patch.object(agentvm.shutil, "which", return_value=None):
        ok, why = r.preflight()
    assert ok is False and "не установлен" in why
    print("OK preflight: нет agent-vm -> (False, установить)")


def test_preflight_no_kvm():
    """Бинарь есть, но нет /dev/kvm → (False, «нет /dev/kvm»)."""
    from unittest import mock
    from orchestrator.runners import agentvm
    r = AgentVmRunner(cfg(), Path("/opt/orch"))
    with mock.patch.object(agentvm.shutil, "which", return_value="/usr/bin/agent-vm"), \
         mock.patch.object(agentvm.Path, "exists", return_value=False):
        ok, why = r.preflight()
    assert ok is False and "/dev/kvm" in why
    print("OK preflight: нет KVM -> (False, нет /dev/kvm)")


def test_preflight_ok():
    """Бинарь + /dev/kvm на месте → (True, ok)."""
    from unittest import mock
    from orchestrator.runners import agentvm
    r = AgentVmRunner(cfg(), Path("/opt/orch"))
    with mock.patch.object(agentvm.shutil, "which", return_value="/usr/bin/agent-vm"), \
         mock.patch.object(agentvm.Path, "exists", return_value=True):
        ok, why = r.preflight()
    assert ok is True and why == "ok"
    print("OK preflight: agent-vm+KVM -> (True, ok)")


def test_runner_agentvm():
    main()

if __name__ == "__main__":
    main()
