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
    # claude_env: раннер смотрит его, чтобы открыть гостю egress к хостовому
    # прокси оператора (--allow-egress); по умолчанию его нет.
    base = dict(
        agent_vm_memory_gib=None, agent_vm_cpus=None, agent_vm_image=None,
        claude_env={}, agent_vm_host_ip=None,
        agent_vm_egress_proxy=None, agent_vm_egress_ca=None,
    )
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

    test_root_equal_cwd_not_mounted_twice()
    test_preflight_no_binary()
    test_preflight_no_kvm()
    test_preflight_ok()
    test_egress_flags_absent_by_default()
    test_egress_flags_emitted()
    test_preflight_rejects_egress_without_fork()
    test_preflight_rejects_missing_ca()

    print("ALL RUNNER OK")


# ── egress на наш прокси (флаги форка agent-vm) ─────────────────────────────
def test_egress_flags_absent_by_default():
    """Без AGENT_VM_EGRESS_PROXY argv не меняется ни на байт.

    «Выключено = не существует»: апстримный agent-vm не должен видеть чужих
    флагов, иначе он упадёт на «unexpected argument».
    """
    argv = AgentVmRunner(cfg(), Path("/opt/orch")).wrap(
        ["claude"], chdir=Path("/p"), extra_rw=[], publish_ports=[])
    s = " ".join(argv)
    assert "--egress-proxy" not in s and "--egress-ca" not in s, argv


def test_egress_flags_emitted():
    """С прокси (и CA) флаги уходят в argv; CA только вместе с прокси."""
    argv = AgentVmRunner(
        cfg(agent_vm_egress_proxy="http://192.168.1.44:9000",
            agent_vm_egress_ca=Path("/tmp/vault-ca.pem")),
        Path("/opt/orch"),
    ).wrap(["claude"], chdir=Path("/p"), extra_rw=[], publish_ports=[])
    s = " ".join(argv)
    assert "--egress-proxy http://192.168.1.44:9000" in s, argv
    assert "--egress-ca /tmp/vault-ca.pem" in s, argv

    # CA без прокси бессмыслен (доверять upstream-плечу нечему) — не шлём.
    argv = AgentVmRunner(
        cfg(agent_vm_egress_ca=Path("/tmp/vault-ca.pem")), Path("/opt/orch"),
    ).wrap(["claude"], chdir=Path("/p"), extra_rw=[], publish_ports=[])
    assert "--egress-ca" not in " ".join(argv), argv
    print("OK agent-vm: --egress-proxy/--egress-ca только когда заданы")


def test_preflight_rejects_egress_without_fork():
    """Апстримный agent-vm (нет --egress-proxy в --help) + заданный прокси →
    честный отказ на preflight, а не падение каждой сессии на unexpected argument.
    А главное — не тихая работа с обойдённым кошельком."""
    from unittest import mock
    from orchestrator.runners import agentvm
    r = AgentVmRunner(cfg(agent_vm_egress_proxy="http://10.0.0.2:9000"), Path("/opt/orch"))
    upstream_help = SimpleNamespace(stdout=b"--allow-host --publish", stderr=b"")
    with mock.patch.object(agentvm.shutil, "which", return_value="/usr/bin/agent-vm"), \
         mock.patch.object(agentvm.Path, "exists", return_value=True), \
         mock.patch.object(agentvm.subprocess, "run", return_value=upstream_help):
        ok, why = r.preflight()
    assert ok is False and "--egress-proxy" in why and "форк" in why, why

    # Форк (флаг есть в --help) — пропускаем.
    fork_help = SimpleNamespace(stdout=b"--egress-proxy <URL> --egress-ca <PEM>", stderr=b"")
    with mock.patch.object(agentvm.shutil, "which", return_value="/usr/bin/agent-vm"), \
         mock.patch.object(agentvm.Path, "exists", return_value=True), \
         mock.patch.object(agentvm.subprocess, "run", return_value=fork_help):
        ok, why = r.preflight()
    assert ok is True and why == "ok", why

    # Сбой запуска бинаря = «флага нет» (честный отказ, не падение сессий).
    with mock.patch.object(agentvm.shutil, "which", return_value="/usr/bin/agent-vm"), \
         mock.patch.object(agentvm.Path, "exists", return_value=True), \
         mock.patch.object(agentvm.subprocess, "run", side_effect=OSError("boom")):
        ok, why = r.preflight()
    assert ok is False and "--egress-proxy" in why
    print("OK preflight: egress-флаги требуют форка (апстрим → честный отказ)")


def test_preflight_rejects_missing_ca():
    """Указанный, но отсутствующий CA — отказ на старте: иначе VM поднимется и
    КАЖДОЕ TLS-соединение упадёт с невнятной ошибкой сертификата."""
    import tempfile
    from unittest import mock
    from orchestrator.runners import agentvm
    fork_help = SimpleNamespace(stdout=b"--egress-proxy", stderr=b"")
    with tempfile.TemporaryDirectory() as d:
        missing = Path(d) / "nope.pem"
        r = AgentVmRunner(
            cfg(agent_vm_egress_proxy="http://10.0.0.2:9000", agent_vm_egress_ca=missing),
            Path("/opt/orch"),
        )
        with mock.patch.object(agentvm.shutil, "which", return_value="/usr/bin/agent-vm"), \
             mock.patch.object(agentvm.subprocess, "run", return_value=fork_help), \
             mock.patch.object(agentvm.Path, "exists", return_value=True):
            ok, why = r.preflight()
        assert ok is False and "файла нет" in why, why

        present = Path(d) / "ca.pem"
        present.write_text("-----BEGIN CERTIFICATE-----\n")
        r = AgentVmRunner(
            cfg(agent_vm_egress_proxy="http://10.0.0.2:9000", agent_vm_egress_ca=present),
            Path("/opt/orch"),
        )
        with mock.patch.object(agentvm.shutil, "which", return_value="/usr/bin/agent-vm"), \
             mock.patch.object(agentvm.subprocess, "run", return_value=fork_help), \
             mock.patch.object(agentvm.Path, "exists", return_value=True):
            ok, why = r.preflight()
        assert ok is True, why
    print("OK preflight: отсутствующий --egress-ca отвергается на старте")


def test_root_equal_cwd_not_mounted_twice():
    """root == chdir (standalone claude-box из корня репозитория) → НЕ дублируем
    --mount: cwd монтирует сам agent-vm и второй том на тот же гостевой путь он
    отвергает («multiple volumes cannot mount the same guest path»). Поймано живым
    прогоном `claude-box --vm` в этом репозитории — VM не поднималась вообще."""
    root = Path("/opt/orch")
    argv = AgentVmRunner(cfg(), root).wrap(
        ["claude"], chdir=root, extra_rw=[root, Path("/s")], publish_ports=[])
    s = " ".join(argv)
    assert "--mount /opt/orch:/opt/orch" not in s, argv
    assert "--mount /s:/s" in s, "прочие пути монтируются как раньше"
    print("OK agent-vm: cwd не монтируется вторым томом (даже если он же root)")


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
