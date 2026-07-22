"""Файловая песочница (bubblewrap): структура argv + реальная изоляция.

Покрыто:
  - build_argv: порядок (tmpfs $HOME до биндов, RW после RO), «-try»-флаги;
  - sandbox_prefix у SessionManager: пусто при SANDBOX=off, allowlist при bwrap;
  - интеграция: настоящий bash под bwrap видит cwd на запись, но НЕ видит
    ~/.ssh и не пишет на реальный диск (если bwrap доступен в окружении).

Запуск: .venv/bin/python tests/sandbox_test.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.runners import sandbox  # noqa: E402
from orchestrator.core.bashshell import BashSession  # noqa: E402
from orchestrator.core.sessions import SessionManager  # noqa: E402


def test_build_argv_order():
    home = Path("/home/tester")
    argv = sandbox.build_argv(
        home=home,
        chdir=home / "proj",
        rw_paths=[home / "proj"],
        ro_paths=[home / "code"],
    )
    s = " ".join(argv)
    assert argv[0] == "bwrap"
    assert argv[-1] == "--"
    assert "--chdir" in argv and argv[argv.index("--chdir") + 1] == str(home / "proj")
    # tmpfs $HOME должен идти РАНЬШЕ биндов под ним, иначе они затрутся.
    i_tmpfs = argv.index(str(home))  # аргумент "--tmpfs <home>"
    i_ro = s.index("--ro-bind-try /home/tester/code")
    i_rw = s.index("--bind-try /home/tester/proj")
    assert (len(" ".join(argv[:i_tmpfs]))) < i_ro < i_rw, "порядок tmpfs<RO<RW нарушен"
    # безопасные флаги присутствуют
    for flag in ("--die-with-parent", "--unshare-pid", "--proc", "--dev"):
        assert flag in argv, flag
    # сеть НЕ изолируется (нужна для API/localhost)
    assert "--unshare-net" not in argv
    # DNS при systemd-resolved: цель симлинка /etc/resolv.conf возвращена в /run
    assert "--ro-bind-try /run/systemd/resolve /run/systemd/resolve" in s
    # system D-Bus (по умолчанию вкл): проброшен для mDNS/avahi-browse
    assert "--ro-bind-try /run/dbus /run/dbus" in s
    print("OK build_argv: порядок tmpfs<RO<RW, die-with-parent, сеть общая, DNS+D-Bus")


def test_build_argv_dbus_off():
    argv = sandbox.build_argv(
        home=Path("/home/tester"), chdir=Path("/home/tester/proj"),
        rw_paths=[], ro_paths=[], system_dbus=False,
    )
    s = " ".join(argv)
    # Базовый DNS остаётся, а system D-Bus — нет.
    assert "/run/systemd/resolve" in s, "базовый DNS должен остаться"
    assert "/run/dbus" not in s, "SANDBOX_DBUS=off не должен пробрасывать system D-Bus"
    print("OK build_argv: system_dbus=False убирает D-Bus, оставляет DNS")


def test_build_argv_persistent_home():
    home = Path("/home/tester")
    argv = sandbox.build_argv(
        home=home, chdir=home / "proj", rw_paths=[], ro_paths=[],
        home_dir=Path("/data/homes/sess"),
    )
    s = " ".join(argv)
    # Персистентный дом монтируется НА МЕСТО $HOME вместо tmpfs.
    assert "--bind /data/homes/sess /home/tester" in s
    assert f"--tmpfs {home}" not in s
    print("OK build_argv: персистентный $HOME вместо tmpfs")


def _mgr(mode: str) -> SessionManager:
    cfg = SimpleNamespace(
        sandbox=mode,
        sandbox_extra_rw=(),
        sandbox_dbus=True,
        sandbox_docker=False,
        claude_config_dir=Path("/home/tester/.claude-proxy"),
    )
    m = SessionManager.__new__(SessionManager)
    m.config = cfg
    return m


def test_prefix_off_empty():
    assert _mgr("off").sandbox_prefix(Path("/x"), [Path("/x")]) == []
    print("OK sandbox_prefix: SANDBOX=off → пустой префикс")


def test_prefix_allowlist():
    m = _mgr("bwrap")
    work = str(Path.home() / "proj")
    argv = m.sandbox_prefix(Path(work), [Path(work)])
    s = " ".join(argv)
    # claude_config_dir из конфига — RW (контролируемое значение)
    assert "--bind-try /home/tester/.claude-proxy" in s
    # прочие пути привязаны к реальному $HOME процесса
    assert f"--bind-try {Path.home() / '.claude.json'}" in s
    assert f"--bind-try {work}" in s                       # рабочая папка RW
    assert "--ro-bind-try" in s and "/.local/share/claude" in s  # бинарь RO
    print("OK sandbox_prefix: конфиг+проект RW, бинарь+репозиторий RO")


def test_docker_sock_bound_when_passed():
    # build_argv с docker_sock → прокси-сокет биндится на /run/docker.sock + DOCKER_HOST
    dsock = Path("/run/user/1000/claude-orchestrator/docker-noos.sock")
    s = " ".join(sandbox.build_argv(
        home=Path("/home/t"), chdir=Path("/x"), rw_paths=[Path("/x")], ro_paths=[],
        docker_sock=dsock))
    assert f"--bind-try {dsock} /run/docker.sock" in s
    assert "--setenv DOCKER_HOST unix:///run/docker.sock" in s
    # без docker_sock → ни сокета, ни DOCKER_HOST
    s2 = " ".join(sandbox.build_argv(
        home=Path("/home/t"), chdir=Path("/x"), rw_paths=[Path("/x")], ro_paths=[]))
    assert "docker.sock" not in s2 and "DOCKER_HOST" not in s2
    print("OK build_argv: docker.sock+DOCKER_HOST только при переданном docker_sock")


def test_real_isolation():
    ok, why = sandbox.available()
    if not ok:
        print(f"SKIP real_isolation: bwrap недоступен ({why})")
        return
    home = Path.home()
    work = Path(tempfile.mkdtemp(prefix="sbx_", dir=home / "tg-claude-sessions"
                                  if (home / "tg-claude-sessions").exists() else None))
    try:
        wrapper = sandbox.build_argv(
            home=home, chdir=work, rw_paths=[work],
            ro_paths=[home / ".local"],
        )
        sh = BashSession(work, wrapper)
        try:
            marker = "SBXDONE"
            # Результат — через код возврата ($?), чтобы токен результата
            # («SSHRC=1») не совпадал с текстом команды («SSHRC=$?»): иначе
            # эхо интерактивного bash фальшиво «подтверждает» проверку.
            sh.write(f"echo -n canwrite > {work}/in.txt; "
                     f"test -e ~/.ssh; echo \"SSHRC=$?\"; "
                     f"echo -n leak > ~/leaktest.txt 2>/dev/null; "
                     f"echo {marker}\n")
            deadline = time.time() + 15
            while time.time() < deadline:
                if marker.encode() in sh.snapshot():
                    break
                time.sleep(0.3)
            out = sh.snapshot().decode(errors="replace")
            assert "SSHRC=1" in out, f"~/.ssh должен быть невидим (SSHRC=1)\n{out}"
            assert "SSHRC=0" not in out, f"~/.ssh виден в песочнице!\n{out}"
            assert (work / "in.txt").read_text() == "canwrite", "не записал в рабочую папку"
            # запись в ~ уходит в эфемерный tmpfs — на реальном диске файла нет
            assert not (home / "leaktest.txt").exists(), "УТЕЧКА: файл появился в реальном $HOME"
            print("OK real_isolation: cwd пишется, ~/.ssh скрыт, записи в $HOME не текут на диск")
        finally:
            sh.close()
        # Персистентный дом: запись в ~ остаётся в home_dir и переживает шелл.
        priv = work / "privhome"
        priv.mkdir()
        wrapper2 = sandbox.build_argv(
            home=home, chdir=work, rw_paths=[work],
            ro_paths=[home / ".local"], home_dir=priv,
        )
        sh2 = BashSession(work, wrapper2)
        try:
            sh2.write("echo -n kept > ~/kept.txt; echo PHDONE\n")
            deadline = time.time() + 15
            while time.time() < deadline:
                if b"PHDONE" in sh2.snapshot():
                    break
                time.sleep(0.3)
        finally:
            sh2.close()
        assert (priv / "kept.txt").read_text() == "kept", "персистентный $HOME не сохранил файл"
        assert not (home / "kept.txt").exists(), "УТЕЧКА в реальный $HOME"
        print("OK real_isolation: персистентный $HOME сохраняет записи")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        (home / "leaktest.txt").unlink(missing_ok=True)


def test_available_no_bwrap():
    """Нет bwrap в PATH → (False, «не установлен»), probe не запускается."""
    from unittest import mock
    with mock.patch.object(sandbox.shutil, "which", return_value=None), \
         mock.patch.object(sandbox.subprocess, "run") as run:
        ok, why = sandbox.available()
    assert ok is False and "не установлен" in why
    run.assert_not_called()  # без bwrap probe даже не пробуем
    print("OK available: нет bwrap -> (False, установить)")


def test_available_probe_raises():
    """bwrap есть, но probe-subprocess падает (exec/таймаут) → (False, «не запускается»)."""
    from unittest import mock
    with mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/bwrap"), \
         mock.patch.object(sandbox.subprocess, "run", side_effect=OSError("boom")):
        ok, why = sandbox.available()
    assert ok is False and "не запускается" in why and "boom" in why
    print("OK available: probe бросил -> (False, не запускается)")


def test_available_userns_rejected():
    """Ядро отвергает unpriv userns (Ubuntu 24.04+ AppArmor): probe returncode!=0
    → (False, «ядро отклонило …»), stderr прокидывается в причину."""
    from unittest import mock
    probe = subprocess.CompletedProcess([], returncode=1, stdout=b"", stderr=b"bwrap: setting up uid map: Permission denied")
    with mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/bwrap"), \
         mock.patch.object(sandbox.subprocess, "run", return_value=probe):
        ok, why = sandbox.available()
    assert ok is False and "ядро отклонило" in why and "uid map" in why
    print("OK available: userns отвергнут -> (False, ядро отклонило)")


def test_available_ok():
    """bwrap есть и probe returncode=0 → (True, ok)."""
    from unittest import mock
    probe = subprocess.CompletedProcess([], returncode=0, stdout=b"", stderr=b"")
    with mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/bwrap"), \
         mock.patch.object(sandbox.subprocess, "run", return_value=probe):
        ok, why = sandbox.available()
    assert ok is True and why == "ok"
    print("OK available: bwrap+userns -> (True, ok)")


def main():
    test_build_argv_order()
    test_build_argv_dbus_off()
    test_build_argv_persistent_home()
    test_prefix_off_empty()
    test_prefix_allowlist()
    test_real_isolation()
    test_available_no_bwrap()
    test_available_probe_raises()
    test_available_userns_rejected()
    test_available_ok()
    print("ALL SANDBOX OK")


if __name__ == "__main__":
    main()
