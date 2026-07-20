"""Режим shared: секрет, значение которого ВЫДаётся сессии (dev-ключ, логин/
пароль). Проверяем парсинг и эндпоинт /get: shared отдаёт значение, host/inject —
никогда; отказ кнопкой не выдаёт значение.

Запуск: .venv/bin/python tests/wallet_shared_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.wallet.module import SecretStore, WalletModule  # noqa: E402

SESSION = SimpleNamespace(name="noos")


def _store(toml: str) -> SecretStore:
    d = tempfile.mkdtemp()
    p = Path(d) / "s.toml"
    p.write_text(toml)
    os.chmod(p, 0o600)
    return SecretStore(p)


class FakeReq:
    def __init__(self, token: str, body: dict):
        self.headers = {"Authorization": f"Bearer {token}"}
        self._b = body

    async def json(self):
        return self._b


def _mod(store: SecretStore, confirm_ok: bool = True) -> WalletModule:
    m = WalletModule.__new__(WalletModule)
    m.store = store
    m._tokens = {"tok": "noos"}

    async def rc(*a, **k):
        return confirm_ok

    async def bg(*a, **k):
        return None

    m.core = SimpleNamespace(
        manager=SimpleNamespace(get=lambda n: SESSION if n == "noos" else None),
        request_confirmation=rc,
        bubbles=SimpleNamespace(append_background=bg),
        _record=lambda *a, **k: None,
    )
    return m


def _body(resp) -> dict:
    return json.loads(resp.body)


async def run():
    st = _store(
        '[secrets.openai]\nshared=true\nvalue="SHV-123"\nenv="OPENAI_API_KEY"\n'
        'sessions=["*"]\nconfirm=false\n\n'
        '[secrets.pw]\nshared=true\nvalue="hunter2"\nsessions=["*"]\nconfirm=false\n\n'
        '[secrets.bad]\nshared=true\nsessions=["*"]\n\n'
        '[secrets.host]\nsessions=["*"]\ncommands=["gh"]\n'
    )
    secrets = st.load()
    assert secrets["openai"].mode == "shared"
    assert secrets["pw"].env == ""          # value без env — допустимо для shared
    assert "bad" not in secrets             # shared без value отброшен
    assert secrets["host"].mode == "host"
    print("OK parse: shared с value ок (env опц.), без value отброшен")

    m = _mod(st)
    r = await m._handle_get(FakeReq("tok", {"secret": "openai"}))
    assert r.status == 200
    j = _body(r)
    assert j["value"] == "SHV-123" and j["env"] == "OPENAI_API_KEY", j
    print("OK get shared: значение и env выданы")

    r = await m._handle_get(FakeReq("tok", {"secret": "host"}))
    assert r.status == 403 and b"SHV" not in r.body
    assert "не shared" in _body(r).get("reason", "")
    print("OK get не-shared (host) → отказ, значение НЕ выдаётся")

    r = await m._handle_get(FakeReq("tok", {"secret": "ghost"}))
    assert r.status == 403
    print("OK get неизвестного секрета → отказ")

    st2 = _store('[secrets.k]\nshared=true\nvalue="V-secret"\nsessions=["*"]\nconfirm=true\n')
    m2 = _mod(st2, confirm_ok=False)
    r = await m2._handle_get(FakeReq("tok", {"secret": "k"}))
    assert r.status == 403 and b"V-secret" not in r.body
    print("OK get с confirm: отказ кнопкой → значение не выдаётся")

    # session_env: авто-инъект — только shared + inject_at_start + env.
    st3 = _store(
        '[secrets.svc]\nshared=true\nvalue="KEYVAL"\nenv="OPENAI_API_KEY"\n'
        'inject_at_start=true\nsessions=["*"]\n\n'
        '[secrets.manual]\nshared=true\nvalue="V"\nenv="X"\nsessions=["*"]\n\n'
        '[secrets.other]\nshared=true\nvalue="Z"\nenv="Y"\ninject_at_start=true\n'
        'sessions=["prod-*"]\n\n'
        '[secrets.host]\nsessions=["*"]\ncommands=["gh"]\n'
    )
    m3 = WalletModule.__new__(WalletModule)
    m3.store = st3
    env = m3.session_env(SESSION)
    # svc: shared+inject_at_start+env+доступен → да; manual: без inject_at_start → нет;
    # other: сессия не матчится (prod-*) → нет; host: не shared → нет.
    assert env == {"OPENAI_API_KEY": "KEYVAL"}, env
    print("OK session_env: только shared+inject_at_start+env+доступ попадают в env")


def main():
    asyncio.run(run())
    print("ALL WALLET-SHARED OK")


if __name__ == "__main__":
    main()
