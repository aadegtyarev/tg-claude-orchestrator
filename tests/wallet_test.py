"""Офлайн-тест кошелька секретов (modules/wallet + bin/wallet CLI).

Покрыто: авторизация per-session токеном, список секретов без значений и
только по policy, отказ по шаблону команды, подтверждение (allow/deny),
редакция значений секретов в выводе, отказ при широких правах файла,
CLI end-to-end через живого демона.

Запуск: .venv/bin/python tests/wallet_test.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp  # noqa: E402

from orchestrator.modules.wallet.module import WalletModule, _redact  # noqa: E402

ROOT = Path(__file__).parent.parent


def make_env(tmp: Path):
    """Фейковое ядро + конфиг: одна сессия dev, secrets.toml с двумя секретами."""
    home = tmp / "home-dev"
    home.mkdir()
    cwd = tmp / "proj"
    cwd.mkdir()
    session = SimpleNamespace(name="dev", session_dir=tmp / "dev")

    secrets_file = tmp / "secrets.toml"
    secrets_file.write_text(
        '[secrets.deploy]\n'
        'value = "S3CR3T-DEPLOY"\n'
        'env = "GITHUB_TOKEN"\n'
        'description = "deploy token"\n'
        'sessions = ["de*"]\n'
        'commands = ["sh -c *", "gh *"]\n'
        'confirm = false\n'
        '\n'
        '[secrets.other]\n'
        'value = "S3CR3T-OTHER"\n'
        'env = "OTHER_TOKEN"\n'
        'sessions = ["prod-*"]\n'          # НЕ для сессии dev
        'commands = ["*"]\n'
        'confirm = false\n'
        '\n'
        '[secrets.careful]\n'
        'value = "S3CR3T-CAREFUL"\n'
        'env = "CAREFUL"\n'
        'sessions = ["*"]\n'
        'commands = ["sh -c *"]\n'
        'confirm = true\n'
        '\n'
        '[secrets.hostgh]\n'                # host-passthrough: без value/env
        'description = "gh на хосте (keyring)"\n'
        'sessions = ["dev"]\n'
        'commands = ["sh -c *"]\n'
        'confirm = false\n'
    )
    os.chmod(secrets_file, 0o600)

    confirm_answer = {"value": True, "asked": 0}

    async def request_confirmation(session, tool, description, preview, timeout=300.0):
        confirm_answer["asked"] += 1
        return confirm_answer["value"]

    notices = []

    async def notice(session, text):
        notices.append(text)

    core = SimpleNamespace(
        manager=SimpleNamespace(
            list_all=lambda: [session],
            get=lambda n: session if n == "dev" else None,
            effective_cwd=lambda s: cwd,
            session_home=lambda s: home,
        ),
        session_hooks=[],
        bubbles=SimpleNamespace(append=_async_noop),
        notice=notice,
        t=lambda k, **kw: kw.get("line", k),
        _record=lambda *a, **kw: None,
        request_confirmation=request_confirmation,
    )
    config = SimpleNamespace(
        wallet_secrets_file=secrets_file, sandbox="bwrap", sessions_dir=tmp
    )
    return core, config, home, secrets_file, confirm_answer


async def _async_noop(*a, **kw):
    return None


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="wallet_test_"))
    core, config, home, secrets_file, confirm = make_env(tmp)

    module = WalletModule(config)
    await module.start(core)
    try:
        wallet_cfg = json.loads((home / ".wallet.json").read_text())
        assert wallet_cfg["session"] == "dev" and wallet_cfg["token"]
        assert oct((home / ".wallet.json").stat().st_mode & 0o777) == "0o600"
        print("OK provision: ~/.wallet.json в доме сессии, 0600")

        url = wallet_cfg["url"]
        good = {"Authorization": f"Bearer {wallet_cfg['token']}"}
        bad = {"Authorization": "Bearer wrong-token"}
        async with aiohttp.ClientSession() as http:
            # 401 с чужим токеном
            async with http.get(f"{url}/secrets", headers=bad) as r:
                assert r.status == 401
            print("OK auth: чужой токен → 401")

            # список: только разрешённые сессии dev, БЕЗ значений
            async with http.get(f"{url}/secrets", headers=good) as r:
                assert r.status == 200
                listed = await r.json()
            names = {s["name"] for s in listed}
            assert names == {"deploy", "careful", "hostgh"}, names
            assert "S3CR3T" not in json.dumps(listed)
            print("OK /secrets: policy по сессии, значений нет")

            # команда вне шаблонов → 403 без исполнения
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy", "cmd": ["rm", "-rf", "/"]}) as r:
                assert r.status == 403
            print("OK /run: команда вне шаблона → 403")

            # чужой секрет → 403
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "other", "cmd": ["sh", "-c", "true"]}) as r:
                assert r.status == 403
            print("OK /run: секрет чужой сессии → 403")

            # исполнение: секрет в env ребёнка, значение отредактировано в выводе
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy",
                                       "cmd": ["sh", "-c", "echo t=$GITHUB_TOKEN; pwd"]}) as r:
                assert r.status == 200
                data = await r.json()
            assert data["code"] == 0, data
            assert "S3CR3T-DEPLOY" not in data["stdout"], data
            assert "t=•••" in data["stdout"], data
            assert str(core.manager.effective_cwd(None)) in data["stdout"]  # cwd = проект
            print("OK /run: исполнение на хосте, значение → •••, cwd проекта")

            # confirm=true: отказ кнопкой → 403, команда не исполнялась
            confirm["value"] = False
            marker = tmp / "should_not_exist"
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "careful",
                                       "cmd": ["sh", "-c", f"touch {marker}"]}) as r:
                assert r.status == 403
            assert confirm["asked"] == 1 and not marker.exists()
            print("OK confirm: deny кнопкой → 403, команда не исполнялась")

            # confirm=true: allow → исполняется
            confirm["value"] = True
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "careful",
                                       "cmd": ["sh", "-c", "echo c=$CAREFUL"]}) as r:
                data = await r.json()
            assert data["code"] == 0 and "c=•••" in data["stdout"], data
            print("OK confirm: allow → исполнено, вывод отредактирован")

            # права шире 0600 → секреты не грузятся, всё в отказ
            os.chmod(secrets_file, 0o644)
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy", "cmd": ["sh", "-c", "true"]}) as r:
                assert r.status == 403
            async with http.get(f"{url}/secrets", headers=good) as r:
                assert await r.json() == []
            os.chmod(secrets_file, 0o600)
            print("OK права 0644 → секреты отключены целиком")

            # host-passthrough (без value/env): команда на хосте с ХОСТОВЫМ
            # окружением, секрет в env НЕ инжектится. Проверяем: доступен, и в
            # env нет инъекции (GITHUB_TOKEN/CAREFUL не появились от wallet).
            async with http.get(f"{url}/secrets", headers=good) as r:
                names = {s["name"] for s in await r.json()}
            assert "hostgh" in names, names
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "hostgh",
                                       "cmd": ["sh", "-c", "echo hg=[$GITHUB_TOKEN][$CAREFUL]"]}) as r:
                data = await r.json()
            assert data["code"] == 0 and "hg=[][]" in data["stdout"], data
            print("OK host-passthrough: команда на хосте без инъекции секрета в env")

        # редакция: вложенные значения, длинные первыми
        out = _redact(b"a=S3CR3T-DEPLOY b=S3CR3T-OTHER", ["S3CR3T-DEPLOY", "S3CR3T-OTHER"])
        assert out == "a=••• b=•••", out
        print("OK _redact: все известные значения вымараны")

        # CLI end-to-end через живого демона (stdlib-скрипт, как в песочнице)
        # CLI — в потоке: subprocess.run в самом event loop заблокировал бы
        # демона (он крутится в этом же цикле) → дедлок.
        env = {**os.environ, "WALLET_FILE": str(home / ".wallet.json")}

        async def cli(*args):
            return await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(ROOT / "bin" / "wallet"), *args],
                capture_output=True, text=True, env=env, timeout=30,
            )

        r = await cli("ls")
        assert r.returncode == 0 and "deploy" in r.stdout, (r.stdout, r.stderr)
        r = await cli("run", "deploy", "--", "sh", "-c", "echo cli=$GITHUB_TOKEN")
        assert r.returncode == 0 and "cli=•••" in r.stdout, (r.stdout, r.stderr)
        r = await cli("run", "deploy", "--", "evil-cmd")
        assert r.returncode == 3 and "отказано" in r.stderr, (r.returncode, r.stderr)
        print("OK bin/wallet: ls + run + отказ policy (end-to-end)")
    finally:
        await module.stop()

    print("ALL WALLET OK")


async def test_wallet():
    await main()

if __name__ == "__main__":
    asyncio.run(main())
