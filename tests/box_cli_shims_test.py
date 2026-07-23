"""gh/git-шимы standalone CLI `claude-box` (--wallet на host/inject-секрет).

Что проверяем:
  * vault.shims: отбор инструментов из commands (глобы мимо, дубликаты схлопнуты),
    ЖИВОЕ поведение git-шима (сетевая подкоманда → wallet exec, локальная →
    настоящий git), права каталога/скриптов;
  * box_cli.wallet.setup_wallet_shims: env-довесок (PATH начинается с каталога
    шимов, WALLET_FILE указывает на существующий 0600-файл с url+token), обёртка
    самого клиента `wallet` в PATH, teardown (демон остановлен, каталог снесён,
    повторный close не падает);
  * честные отказы с кодами: нет секрета / не разрешён сессии / нечего
    заворачивать → 2, демон не поднялся → 1.

Демон НЕ поднимается по-настоящему (это TCP-сокет и tty-хост) — подменяем
build_daemon фейком; всё, что касается шимов/env/прав/teardown, проверяется живьём.

Запуск: .venv/bin/python tests/box_cli_shims_test.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box_cli import wallet as boxwallet  # noqa: E402
from box_cli.wallet import WalletError  # noqa: E402
from vault.shims import (  # noqa: E402
    SHIM_DIRNAME,
    git_shim,
    tool_names,
    write_shims,
)

SESSION = "claude-box"


def _write_secrets(path: Path, body: str) -> None:
    """secrets.toml 0600 — иначе SecretStore его не загрузит."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)


_SECRETS = (
    # host-passthrough: дубликат gh и чистый глоб — в шимы попасть не должны.
    '[secrets.hostcred]\nsessions=["*"]\n'
    'commands=["gh", "git", "curl https://api/*", "*", "gh"]\n\n'
    # inject-секрет для другой сессии — claude-box его не получает.
    '[secrets.foreign]\nvalue="V"\nenv="TOK"\nsessions=["prod-*"]\ncommands=["kubectl"]\n\n'
    # секрет без единой команды (только глоб) — заворачивать нечего.
    '[secrets.nocmd]\nvalue="V"\nenv="TOK2"\nsessions=["*"]\ncommands=["*"]\n'
)


def _secrets_file() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="box_shims_"))
    path = tmp / "secrets.toml"
    _write_secrets(path, _SECRETS)
    return path


# ── фейковый демон ───────────────────────────────────────────────────

class _FakeDaemon:
    """Заменитель VaultDaemon: без сокетов и tty. Помнит, что его остановили."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.started = False
        self.stopped = False
        self.token_cwd: Path | None = None
        self.url = "http://127.0.0.1:65000"

    async def start(self) -> None:
        if self.fail:
            raise RuntimeError("порт занят")
        self.started = True

    def issue_token(self, session: str, cwd: Path) -> str:
        self.token_cwd = cwd
        return "TOKEN-FAKE"

    async def stop(self) -> None:
        self.stopped = True


def _patch_daemon(daemon: _FakeDaemon):
    """Подменить build_daemon на фейк; вернуть восстановитель."""
    original = boxwallet.build_daemon
    boxwallet.build_daemon = lambda *a, **kw: daemon  # noqa: ARG005
    def restore() -> None:
        boxwallet.build_daemon = original
    return restore


# ── vault.shims ──────────────────────────────────────────────────────

def test_tool_names():
    """Отбор инструментов: basename первого токена, глобы мимо, дубликаты схлопнуты."""
    got = tool_names(["gh", "git", "curl https://api/*", "*", "gh", "/usr/bin/ssh", "s?h"])
    assert got == {"gh", "git", "curl", "ssh"}, got
    assert tool_names([]) == set()
    assert tool_names(["*", "?"]) == set(), "чистые глобы не имена бинарей"
    print("OK tool_names: глобы пропущены, дубликаты схлопнуты, basename взят")


def test_git_shim_behaviour():
    """ЖИВОЕ поведение git-шима: `push` уходит в wallet exec, `status` — настоящему
    git (подставляем эхо-заглушки, чтобы увидеть, кого позвали)."""
    tmp = Path(tempfile.mkdtemp(prefix="box_shims_git_"))
    fake_git = tmp / "realgit"
    fake_git.write_text('#!/bin/sh\necho "REALGIT $*"\n')
    os.chmod(fake_git, 0o755)
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "wallet").write_text('#!/bin/sh\necho "WALLET $*"\n')
    os.chmod(bindir / "wallet", 0o755)

    shim = tmp / "git"
    shim.write_text(git_shim(str(fake_git)))
    os.chmod(shim, 0o755)

    env = {**os.environ, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    for args, expect in (
        (["push", "origin", "main"], "WALLET exec git push origin main"),
        (["fetch"], "WALLET exec git fetch"),
        (["clone", "u"], "WALLET exec git clone u"),
        (["status"], "REALGIT status"),
        (["commit", "-m", "x"], "REALGIT commit -m x"),
        ([], "REALGIT "),
    ):
        out = subprocess.run([str(shim), *args], env=env, capture_output=True,
                             text=True, check=True).stdout.strip()
        assert out == expect.strip(), f"{args}: {out!r} != {expect!r}"
    print("OK git-шим: сетевые подкоманды → wallet exec, локальные → настоящий git")


def test_write_shims_permissions():
    """Каталог 0700, скрипты 0755; перегенерация чистит устаревшие; пусто = пусто."""
    tmp = Path(tempfile.mkdtemp(prefix="box_shims_perm_"))
    shim_dir = tmp / SHIM_DIRNAME
    names = write_shims(shim_dir, {"gh", "git", "curl"})
    assert names == ["curl", "gh", "git"], names
    assert oct(shim_dir.stat().st_mode & 0o777) == "0o700", oct(shim_dir.stat().st_mode)
    for n in names:
        p = shim_dir / n
        assert oct(p.stat().st_mode & 0o777) == "0o755", n
        assert os.access(p, os.X_OK), n
    assert (shim_dir / "gh").read_text() == '#!/bin/sh\nexec wallet exec gh "$@"\n'
    # Перегенерация под меньший набор — старое не переживает.
    write_shims(shim_dir, {"gh"})
    assert {p.name for p in shim_dir.iterdir()} == {"gh"}
    assert write_shims(shim_dir, []) == [] and not list(shim_dir.iterdir())
    print("OK write_shims: каталог 0700, скрипты 0755, перегенерация чистит старое")


# ── box_cli.wallet: env, teardown, отказы ────────────────────────────

async def test_setup_shims_env_and_teardown():
    """setup_wallet_intercept на host-секрете: PATH с каталогом шимов впереди,
    WALLET_FILE на существующий 0600-файл, `wallet` и обёртки на месте, close()
    останавливает демон и сносит каталог (повторный вызов не падает)."""
    secrets = _secrets_file()
    daemon = _FakeDaemon()
    restore = _patch_daemon(daemon)
    intercept = None
    try:
        intercept = await boxwallet.setup_wallet_intercept(
            "hostcred", secrets_path=secrets, session_name=SESSION)
        env = intercept.env
        assert set(env) == {"PATH", "WALLET_FILE"}, env
        shim_dir = Path(env["PATH"].split(os.pathsep)[0])
        assert shim_dir.name == SHIM_DIRNAME, shim_dir
        assert env["PATH"].split(os.pathsep)[1:] == os.environ["PATH"].split(os.pathsep), (
            "исходный PATH должен сохраниться после каталога шимов")

        # Обёртки: ровно инструменты секрета + сам клиент wallet.
        got = {p.name for p in shim_dir.iterdir()}
        assert got == {"gh", "git", "curl", "wallet"}, got
        assert "kubectl" not in got, "чужая сессия не должна попасть в шимы"
        assert os.access(shim_dir / "wallet", os.X_OK)
        assert str(boxwallet.wallet_cli_path()) in (shim_dir / "wallet").read_text()

        # WALLET_FILE: существует, 0600, содержит url+token демона.
        wf = Path(env["WALLET_FILE"])
        assert wf.exists() and oct(wf.stat().st_mode & 0o777) == "0o600", wf
        import json
        payload = json.loads(wf.read_text())
        assert payload == {"url": daemon.url, "token": "TOKEN-FAKE", "session": SESSION}
        assert daemon.token_cwd == Path.cwd(), daemon.token_cwd

        # Каталог отдан в RW-бинды песочницы (иначе шимы изнутри не видны).
        tmpdir = shim_dir.parent
        assert intercept.extra_rw == [tmpdir], intercept.extra_rw
        assert oct(tmpdir.stat().st_mode & 0o777) == "0o700", oct(tmpdir.stat().st_mode)

        await intercept.close()
        assert daemon.stopped, "демон не остановлен на выходе"
        assert not tmpdir.exists(), "временный каталог не снесён"
        await intercept.close()  # идемпотентность
        print("OK шимы: PATH+WALLET_FILE, обёртки+wallet, teardown снёс каталог и демон")
    finally:
        restore()
        if intercept is not None:
            await intercept.close()


async def test_shims_refusals():
    """Честные отказы: нет секрета / не разрешён сессии / нечего заворачивать → 2,
    демон не поднялся → 1."""
    secrets = _secrets_file()

    async def _expect(name: str, code: int, needle: str, *, daemon=None):
        restore = _patch_daemon(daemon or _FakeDaemon())
        try:
            await boxwallet.setup_wallet_intercept(
                name, secrets_path=secrets, session_name=SESSION)
        except WalletError as e:
            assert e.code == code, f"{name}: код {e.code} != {code} ({e})"
            assert needle in str(e), f"{name}: нет «{needle}» в «{e}»"
        else:
            raise AssertionError(f"{name}: ожидался WalletError")
        finally:
            restore()

    await _expect("nope", 2, "не найден")
    await _expect("foreign", 2, "не разрешён")
    await _expect("nocmd", 2, "нет ни одной команды")
    await _expect("hostcred", 1, "демон кошелька не поднят", daemon=_FakeDaemon(fail=True))
    print("OK отказы: нет секрета/не разрешён/нечего заворачивать → 2, сбой демона → 1")


def main() -> None:
    test_tool_names()
    test_git_shim_behaviour()
    test_write_shims_permissions()
    asyncio.run(test_setup_shims_env_and_teardown())
    asyncio.run(test_shims_refusals())
    print("ALL BOX-CLI-SHIMS OK")


if __name__ == "__main__":
    main()
