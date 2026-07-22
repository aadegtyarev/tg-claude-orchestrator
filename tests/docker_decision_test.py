"""Модель угроз docker-authz в чистом виде (orchestrator/modules/docker/decision.py).

Проверяем ровно то, что запрещаем (выход на хост + креды) и что СОЗНАТЕЛЬНО
пускаем (соседние проекты, публикация, network=host). Демон видит абсолютные
пути и распарсенное тело.

Запуск: .venv/bin/python tests/docker_decision_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.docker.decision import Policy, evaluate  # noqa: E402

HOME = "/home/op"
POL = Policy.for_home(HOME)


def _create(**hc):
    return {"Image": "postgres:16", "HostConfig": dict(hc)}


def ev(body, method="POST", uri="/v1.43/containers/create"):
    return evaluate(method, uri, body, policy=POL)


# ── не-create всегда пускаем ────────────────────────────────────────
def test_non_create_allowed():
    assert evaluate("GET", "/containers/json", None, policy=POL).allow
    assert evaluate("POST", "/containers/abc/start", None, policy=POL).allow
    assert evaluate("GET", "/_ping", None, policy=POL).allow
    assert evaluate("POST", "/images/create", None, policy=POL).allow
    print("OK не-create (list/start/ping/pull) → allow")


def test_unparsed_create_denied():
    # тело create не разобрано → deny (безопасная сторона)
    assert not ev(None).allow
    print("OK create без разобранного тела → deny")


# ── выход на хост → deny ────────────────────────────────────────────
def test_privileged_denied():
    v = ev(_create(Privileged=True))
    assert not v.allow and "privileged" in v.reason.lower()
    print("OK --privileged → deny")


def test_host_namespaces_denied():
    for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode"):
        assert not ev(_create(**{key: "host"})).allow, key
    # непустой, но не host — пускаем (напр. контейнерный ns)
    assert ev(_create(PidMode="container:abc")).allow
    print("OK Pid/Ipc/UTS/Userns=host → deny; container:ns → allow")


def test_devices_allowed():
    # --device СОЗНАТЕЛЬНО пускаем (USB/TTY по делу)
    assert ev(_create(Devices=[{"PathOnHost": "/dev/ttyUSB0"}])).allow
    print("OK --device → allow (сознательно)")


def test_system_path_binds_denied():
    for src in ("/usr", "/usr/bin", "/bin", "/lib", "/boot", "/sbin/init"):
        assert not ev(_create(Binds=[f"{src}:/x"])).allow, f"снести систему: {src}"
    print("OK bind системных путей (/usr,/bin,/lib,/boot,...) → deny")


def test_dangerous_caps_denied():
    assert not ev(_create(CapAdd=["SYS_ADMIN"])).allow
    assert not ev(_create(CapAdd=["net_admin", "SYS_PTRACE"])).allow  # регистр
    assert ev(_create(CapAdd=["NET_BIND_SERVICE"])).allow             # безобидная
    print("OK опасные caps → deny (регистронезависимо); безобидная → allow")


# ── чувствительные bind'ы → deny ────────────────────────────────────
def test_sensitive_binds_denied():
    for src in ("/", "/etc", "/etc/shadow", "/root/.bashrc",
                "/home/op/.ssh", "/home/op/.ssh/id_ed25519",
                "/home/op/.aws/credentials",
                "/home/op/.config/claude-orchestrator/secrets.toml",
                "/var/run/docker.sock", "/run/docker.sock"):
        v = ev(_create(Binds=[f"{src}:/x"]))
        assert not v.allow, f"должен быть deny: {src}"
    print("OK bind /, /etc, ~/.ssh, ~/.aws, secrets, docker.sock → deny")


def test_sensitive_via_dotdot_denied():
    # обход нормализацией: /home/op/proj/../.ssh → /home/op/.ssh
    v = ev(_create(Binds=["/home/op/proj/../.ssh:/x"]))
    assert not v.allow, "нормализация .. должна ловить обход"
    print("OK bind с .. в чувствительный путь → deny (нормализация)")


def test_mount_bind_sensitive_denied():
    body = _create(Mounts=[{"Type": "bind", "Source": "/etc", "Target": "/x"}])
    assert not ev(body).allow
    # volume-mount не трогаем
    body2 = _create(Mounts=[{"Type": "volume", "Source": "v", "Target": "/x"}])
    assert ev(body2).allow
    print("OK --mount type=bind в /etc → deny; type=volume → allow")


def test_volume_create_bind_device_denied():
    body = {"Name": "v", "Driver": "local",
            "DriverOpts": {"type": "none", "o": "bind", "device": "/home/op/.ssh"}}
    assert not evaluate("POST", "/volumes/create", body, policy=POL).allow
    # обычный именованный том без device → allow
    assert evaluate("POST", "/volumes/create", {"Name": "v"}, policy=POL).allow
    print("OK volume create с o=bind,device=~/.ssh → deny; обычный том → allow")


# ── сознательно РАЗРЕШАЕМ ────────────────────────────────────────────
def test_neighbor_project_bind_allowed():
    # соседний проект (несенситивный путь) — пофиг на ограничение по проекту
    v = ev(_create(Binds=["/home/op/other-project:/app"]))
    assert v.allow, "несенситивный bind должен проходить"
    print("OK bind соседнего проекта → allow (сознательно)")


def test_named_volume_allowed():
    assert ev(_create(Binds=["pgdata:/var/lib/postgresql/data"])).allow
    print("OK именованный том → allow")


def test_publish_and_host_net_allowed():
    body = _create(
        NetworkMode="host",
        PortBindings={"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5432"}]},
    )
    assert ev(body).allow, "публикация и network=host — песочница и так на сети хоста"
    print("OK network=host + публикация на 0.0.0.0 → allow (сознательно)")


def test_plain_run_allowed():
    assert ev(_create()).allow
    print("OK обычный запуск без опасных опций → allow")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL DOCKER-DECISION OK")


if __name__ == "__main__":
    main()
