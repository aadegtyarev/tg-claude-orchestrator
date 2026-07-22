"""Маркеры секрета в команде: <<wallet:имя>> (inline) и <<wallet:имя:file>>.

Модель пишет привычный `$ENV`, шелл разворачивает его в маркер, а демон
подставляет РЕАЛЬНОЕ значение на хосте (в аргумент curl или во временный файл
для ssh-ключа). Значение к модели/в песочницу не попадает; маркер недоступного
секрета → пусто.

Запуск: .venv/bin/python tests/wallet_inject_test.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.wallet.module import Secret, WalletModule, marker  # noqa: E402


def _secret(**kw) -> Secret:
    d = dict(name="api", value="TOPSECRET-abc", env="API_TOKEN", description="",
             sessions=("*",), commands=("*",), deny=(), allow_unsafe=False, confirm=False,
             shared=False)
    d.update(kw)
    return Secret(**d)


def _mod(cwd: Path, secret: Secret) -> WalletModule:
    m = WalletModule.__new__(WalletModule)
    # _execute берёт cwd из core.manager.effective_cwd(session) и store.load.
    m.core = SimpleNamespace(manager=SimpleNamespace(effective_cwd=lambda s: cwd))
    m.store = SimpleNamespace(load=lambda: {secret.name: secret})
    return m


async def test_wallet_inject():
    cwd = Path(tempfile.mkdtemp())
    sess = SimpleNamespace(name="noos")
    s = _secret()
    mod = _mod(cwd, s)
    tmproot = tempfile.gettempdir()

    # <<wallet:api>> → реальное значение в аргумент.
    code, out, err = await mod._execute(sess, s, ["printf", "%s", marker("api")])
    assert code == 0 and out == b"TOPSECRET-abc", (code, out, err)
    print("OK <<wallet:api>> → значение подставлено в аргумент")

    # <<wallet:api:file>> → путь к 0600-файлу; каталог убран после.
    before = {d for d in os.listdir(tmproot) if d.startswith("wallet-")}
    code, out, err = await mod._execute(sess, s, ["cat", marker("api", as_file=True)])
    assert code == 0 and out == b"TOPSECRET-abc\n", (code, out, err)
    after = {d for d in os.listdir(tmproot) if d.startswith("wallet-")}
    assert after == before, f"временный каталог не убран: {after - before}"
    print("OK <<wallet:api:file>> → 0600-файл, путь подставлен, каталог убран")

    # inject env: значение доступно процессу на хосте (для gh/aws/kubectl).
    code, out, err = await mod._execute(sess, s, ["sh", "-c", "printf %s \"$API_TOKEN\""])
    assert code == 0 and out == b"TOPSECRET-abc", (code, out, err)
    print("OK env $API_TOKEN доступен процессу на хосте")

    # Маркер секрета БЕЗ значения (host-passthrough) → пусто, не течёт.
    hp = _secret(value="", env="")
    mod_hp = _mod(cwd, hp)
    code, out, err = await mod_hp._execute(sess, hp, ["printf", "[%s]", marker("api")])
    assert code == 0 and out == b"[]", (code, out, err)
    print("OK маркер секрета без значения → пусто")


def main():
    asyncio.run(test_wallet_inject())
    print("ALL WALLET-INJECT OK")


if __name__ == "__main__":
    main()
