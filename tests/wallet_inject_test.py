"""Инъекция секрета В КОМАНДУ: плейсхолдеры {{secret}} и {{secret_file}}.

Нужно там, где токен должен попасть в аргумент (curl -H) или в файл (ssh -i),
а не в env (exec без шелла — «$ENV» в аргументе не развернулся бы). Подстановка
в демоне перед exec; значение к модели не попадает, временный файл 0600 на хосте
(песочнице невидим) убирается после.

Запуск: .venv/bin/python tests/wallet_inject_test.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.wallet.module import (  # noqa: E402
    PH_FILE, PH_VALUE, Secret, WalletModule,
)


def _secret(**kw) -> Secret:
    d = dict(name="api", value="TOPSECRET-abc", env="API_TOKEN", description="",
             sessions=("*",), commands=("*",), deny=(), allow_unsafe=False, confirm=False)
    d.update(kw)
    return Secret(**d)


def _mod(cwd: Path) -> WalletModule:
    m = WalletModule.__new__(WalletModule)
    m.core = SimpleNamespace(manager=SimpleNamespace(effective_cwd=lambda s: cwd))
    return m


async def run():
    cwd = Path(tempfile.mkdtemp())
    mod = _mod(cwd)
    sess = SimpleNamespace(name="noos")
    tmproot = tempfile.gettempdir()

    # {{secret}} → значение прямо в аргумент.
    code, out, err = await mod._execute(sess, _secret(), ["printf", "%s", PH_VALUE])
    assert code == 0 and out == b"TOPSECRET-abc", (code, out, err)
    print("OK {{secret}} → значение подставлено в строку аргумента")

    # {{secret_file}} → путь к 0600-файлу со значением; каталог убран после.
    before = {d for d in os.listdir(tmproot) if d.startswith("wallet-")}
    code, out, err = await mod._execute(sess, _secret(), ["cat", PH_FILE])
    assert code == 0 and out == b"TOPSECRET-abc\n", (code, out, err)
    after = {d for d in os.listdir(tmproot) if d.startswith("wallet-")}
    assert after == before, f"временный каталог не убран: {after - before}"
    print("OK {{secret_file}} → 0600-файл, путь подставлен, каталог убран после")

    # host-passthrough + плейсхолдер → понятная ошибка (значения нет).
    code, out, err = await mod._execute(sess, _secret(value="", env=""), ["curl", PH_VALUE])
    assert code == 2 and b"host-passthrough" in err, (code, err)
    print("OK host-passthrough + плейсхолдер → отказ с объяснением")

    # Без плейсхолдеров inject по-прежнему кладёт токен в env (gh/aws читают сами).
    code, out, err = await mod._execute(sess, _secret(), ["sh", "-c", "printf %s \"$API_TOKEN\""])
    assert code == 0 and out == b"TOPSECRET-abc", (code, out, err)
    print("OK inject env $API_TOKEN доступен процессу (для gh/aws/kubectl)")


def main():
    asyncio.run(run())
    print("ALL WALLET-INJECT OK")


if __name__ == "__main__":
    main()
