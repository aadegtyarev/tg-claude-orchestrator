"""Vault-перехват в CLI `claude-box` (--wallet, Launcher §5.2): standalone, без
оркестратора.

Два уровня:
  * ЮНИТ (везде, где есть openssl): box_cli.wallet.setup_wallet_intercept на
    прокси-секрете — env-довесок (HTTPS_PROXY + *_CA_BUNDLE + NO_PROXY), CA-bundle
    0644 во ВРЕМЕННОМ каталоге с корнем Vault, каталог отдан в extra_rw (бинд в
    песочницу), upstream пула ДЕФОЛТНЫЙ (системный trust — импостор кред не
    получит), close() снимает прокси (порт освобождён) и сносит временный каталог.
    Плюс honest-отказы (нет секрета / не прокси-секрет → код 2) и атомарность
    bundle против симлинк-подмены.
  * ЖИВОЙ (мягкий скип без bwrap/openssl): внутри bwrap-песочницы python через
    HTTPS_PROXY+CA (env от proxy_sandbox_env, каталог bundle биндится cli.build_argv)
    бьёт в локальный HTTPS-«сервис»; сервис видит впрыснутый Bearer, команда
    значения секрета не видела. Реориджин к локальному сервису требует ТЕСТОВОГО
    upstream trust — его получает ТОЛЬКО пул этого теста (прод оставляет системный).

Запуск: .venv/bin/python tests/box_cli_wallet_test.py
"""
from __future__ import annotations

import asyncio
import os
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent

try:
    from box_cli import cli  # noqa: E402
    from box_cli.wallet import WalletError, setup_wallet_intercept  # noqa: E402
    from orchestrator.runners import sandbox  # noqa: E402
    from vault.inject import DEFAULT_BUNDLE_NAME, proxy_sandbox_env  # noqa: E402
    from vault.proxy_pool import SessionProxyPool  # noqa: E402
    from vault.tls import VaultCA  # noqa: E402
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERR = exc

_TIMEOUT = 20
_SECRET_VALUE = "CLI-PROXY-SECRET-DO-NOT-LEAK-999"


def _skip(reason: str) -> bool:
    print(f"SKIP {reason}")
    return True


def _write_secrets(path: Path, body: str) -> None:
    """Записать secrets.toml 0600 (иначе SecretStore его не загрузит)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)


def _proxy_secrets_toml(scope_prefix: str) -> str:
    return (
        "[secrets.svc]\n"
        f'value = "{_SECRET_VALUE}"\n'
        'sessions = ["*"]\n'
        'connector = "generic-bearer"\n'
        "[secrets.svc.scope]\n"
        f'url_prefixes = ["{scope_prefix}"]\n'
        "\n"
        "[secrets.hostcred]\n"       # обычный host-passthrough (не прокси)
        'sessions = ["*"]\n'
        'commands = ["gh"]\n'
    )


def _have_openssl() -> bool:
    import shutil
    return shutil.which("openssl") is not None


# ── ЮНИТ ────────────────────────────────────────────────────────────

async def test_setup_refusals():
    """Honest-отказы: нет секрета и не-прокси-секрет → WalletError код 2."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт не удался: {_IMPORT_ERR}")
    tmp = Path(tempfile.mkdtemp(prefix="cli_wallet_refuse_"))
    secrets = tmp / "secrets.toml"
    _write_secrets(secrets, _proxy_secrets_toml("https://localhost/allowed"))
    # нет такого секрета
    try:
        await setup_wallet_intercept("nope", secrets_path=secrets)
    except WalletError as e:
        assert e.code == 2, e.code
    else:
        raise AssertionError("несуществующий секрет должен дать WalletError")
    # host-секрет — не прокси
    try:
        await setup_wallet_intercept("hostcred", secrets_path=secrets)
    except WalletError as e:
        assert e.code == 2, e.code
        assert "не прокси" in str(e), str(e)
    else:
        raise AssertionError("не-прокси-секрет должен дать WalletError")
    print("OK honest-отказ: нет секрета / не прокси-секрет → код 2")


async def test_setup_env_bundle_and_teardown():
    """setup_wallet_intercept: env-довесок + CA-bundle 0644 во временном каталоге
    (в extra_rw), upstream системный (None), close() освобождает порт и сносит каталог."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт не удался: {_IMPORT_ERR}")
    if not _have_openssl():
        return _skip("нет openssl — VaultCA недоступен")
    tmp = Path(tempfile.mkdtemp(prefix="cli_wallet_setup_"))
    secrets = tmp / "secrets.toml"
    _write_secrets(secrets, _proxy_secrets_toml("https://localhost/allowed"))
    # CA — в изолированный каталог (не трогаем реальный ~/.config).
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = str(tmp / "xdg")
    intercept = None
    try:
        intercept = await setup_wallet_intercept("svc", secrets_path=secrets)
        env = intercept.env
        # env-довесок: HTTPS_PROXY на loopback-порт, trust-bundle на все три пер-ва.
        assert env["HTTPS_PROXY"].startswith("http://127.0.0.1:"), env
        assert env["https_proxy"] == env["HTTPS_PROXY"], env
        assert "HTTP_PROXY" not in env, env  # только CONNECT/HTTPS
        ca_path = env["SSL_CERT_FILE"]
        assert env["REQUESTS_CA_BUNDLE"] == ca_path and env["CURL_CA_BUNDLE"] == ca_path
        assert "127.0.0.1" in env["NO_PROXY"] and "localhost" in env["NO_PROXY"], env
        # Значение секрета никуда в env не попало.
        assert _SECRET_VALUE not in "\n".join(f"{k}={v}" for k, v in env.items())

        # CA-bundle: файл во ВРЕМЕННОМ каталоге, 0644, содержит корень Vault.
        bundle = Path(ca_path)
        assert bundle.name == DEFAULT_BUNDLE_NAME, bundle
        assert bundle.exists() and oct(bundle.stat().st_mode & 0o777) == "0o644"
        assert "BEGIN CERTIFICATE" in bundle.read_text()
        # extra_rw = каталог bundle (для бинда в песочницу тем же путём).
        assert intercept.extra_rw == [bundle.parent], intercept.extra_rw

        # Прод-trust: пул НЕ переопределяет upstream_ssl (системный → импостор
        # под сервис кред не получит).
        pool = intercept._pool
        assert pool is not None and pool._upstream_ssl is None, "upstream_ssl не дефолтный!"
        port_before = pool.ports(_wallet_session())
        assert port_before, "прокси не поднят"

        # Стоп: порт освобождён, временный каталог снесён.
        tmpdir = bundle.parent
        await intercept.close()
        assert pool.ports(_wallet_session()) == {}, "порт не освобождён после close()"
        assert not tmpdir.exists(), "временный каталог bundle не снесён"
        print("OK setup: env+bundle(0644,tmp,extra_rw)+upstream None; close освобождает порт и каталог")
    finally:
        if intercept is not None:
            await intercept.close()
        if old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old_xdg


def _wallet_session() -> str:
    from box_cli.wallet import SESSION_NAME
    return SESSION_NAME


def test_bundle_atomic_symlink():
    """Атомарность bundle: подложенный симлинк на victim НЕ затирается (proxy_sandbox_env
    пишет через temp+replace), symlink заменён обычным файлом 0644."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт не удался: {_IMPORT_ERR}")
    if not _have_openssl():
        return _skip("нет openssl — VaultCA недоступен")
    tmp = Path(tempfile.mkdtemp(prefix="cli_wallet_symlink_"))
    ca = VaultCA(tmp / "ca")
    bundle_dir = tmp / "bdir"
    bundle_dir.mkdir()
    victim = tmp / "victim"
    victim.write_text("VICTIM-ORIGINAL")
    (bundle_dir / DEFAULT_BUNDLE_NAME).symlink_to(victim)
    result = proxy_sandbox_env(ca, 12345, bundle_dir=bundle_dir)
    assert result is not None, "нет системных корней — тест среды"
    _env, bundle_path, cleanup = result
    assert victim.read_text() == "VICTIM-ORIGINAL", "victim затёрт через симлинк!"
    assert not bundle_path.is_symlink(), "симлинк не заменён обычным файлом"
    assert ca.ca_cert_pem().strip() in bundle_path.read_text(), "bundle без корня Vault"
    assert oct(bundle_path.stat().st_mode & 0o777) == "0o644"
    cleanup()
    assert not bundle_path.exists(), "cleanup не снял bundle"
    print("OK симлинк-защита: proxy_sandbox_env не затирает victim, файл 0644, cleanup снимает")


# ── ЖИВОЙ bwrap ─────────────────────────────────────────────────────

class _Service:
    """Локальный HTTPS-«сервис»: запоминает Authorization, секрет в теле НЕ отражает."""

    def __init__(self, ctx: ssl.SSLContext) -> None:
        self._ctx = ctx
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.seen_auth: list[str] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self._ctx)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _handle(self, reader, writer) -> None:
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_TIMEOUT)
            auth = ""
            while True:
                hl = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_TIMEOUT)
                if hl in (b"\r\n", b"\n"):
                    break
                name, _, value = hl.rstrip(b"\r\n").decode("latin-1").partition(":")
                if name.strip().lower() == "authorization":
                    auth = value.strip()
            self.seen_auth.append(auth)
            body = b"CLI-INTERCEPT-OK"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


def _service_ctx(ca: VaultCA) -> ssl.SSLContext:
    leaf = ca.issue("localhost")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(leaf.cert_path), keyfile=str(leaf.key_path))
    return ctx


def _upstream_trust(ca: VaultCA) -> ssl.SSLContext:
    """ТЕСТОВЫЙ upstream trust к CA Vault — только для пула этого теста (реориджин к
    локальному сервису); прод оставляет системный дефолт."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=ca.ca_cert_pem())
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


_SANDBOX_SCRIPT = (
    "import os, ssl, urllib.request\n"
    "ctx = ssl.create_default_context()\n"  # честит SSL_CERT_FILE из env
    "op = urllib.request.build_opener(\n"
    "    urllib.request.ProxyHandler(urllib.request.getproxies()),\n"
    "    urllib.request.HTTPSHandler(context=ctx))\n"
    "r = op.open(os.environ['TARGET_URL'], timeout=10)\n"
    "print('STATUS', r.status)\n"
    "print(r.read().decode())\n"
)


async def test_live_cli_wallet_intercept():
    """ЖИВОЙ: внутри bwrap python через HTTPS_PROXY+CA (env от proxy_sandbox_env,
    каталог bundle биндит cli.build_argv) бьёт в сервис; сервис видит впрыснутый
    Bearer, команда значения секрета не видела."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт не удался: {_IMPORT_ERR}")
    ok, why = sandbox.available()
    if not ok:
        return _skip(f"bwrap недоступен: {why}")
    if not _have_openssl():
        return _skip("нет openssl — VaultCA недоступен")
    tmp = Path(tempfile.mkdtemp(prefix="cli_wallet_live_"))
    ca = VaultCA(tmp / "ca")
    service = _Service(_service_ctx(ca))
    await service.start()

    scope_prefix = f"https://localhost:{service.port}/allowed"
    secrets = tmp / "secrets.toml"
    _write_secrets(secrets, _proxy_secrets_toml(scope_prefix))

    from vault.store import SecretStore
    store = SecretStore(secrets)
    session = _wallet_session()
    # ТЕСТОВЫЙ upstream trust — только для этого пула (прод оставляет дефолт).
    pool = SessionProxyPool(ca, store, upstream_ssl=_upstream_trust(ca))
    bundle_dir = tmp / "bdir"
    bundle_dir.mkdir()
    try:
        port = await pool.start(session, "svc")
        result = proxy_sandbox_env(ca, port, bundle_dir=bundle_dir)
        if result is None:
            return _skip("нет системных корней — bundle не собран")
        env, _bundle_path, _cleanup = result
        assert "HTTPS_PROXY" in env and "SSL_CERT_FILE" in env, env

        # Песочница bwrap через штатный build_argv лончера: cwd + каталог bundle RW.
        runner = cli.make_engine_runner("bwrap", cli.repo_root())
        argv = cli.build_argv(runner, ["python3", "-c", _SANDBOX_SCRIPT], tmp, [bundle_dir])
        # no_proxy чистим: наш «сервис» ради теста на localhost (в проде реальный
        # хост сервиса в NO_PROXY не значится). Значения секрета в env НЕТ.
        run_env = {**os.environ, **env, "HOME": str(Path.home()),
                   "no_proxy": "", "NO_PROXY": "",
                   "TARGET_URL": f"https://localhost:{service.port}/allowed/x"}
        assert _SECRET_VALUE not in "\n".join(f"{k}={v}" for k, v in run_env.items()), (
            "значение секрета попало в env команды")

        proc = await asyncio.create_subprocess_exec(
            *argv, env=run_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        stdout = out.decode(errors="replace")
        stderr = err.decode(errors="replace")
        assert proc.returncode == 0, f"код {proc.returncode}\nSTDOUT:{stdout}\nSTDERR:{stderr}"
        assert "STATUS 200" in stdout, f"нет 200: {stdout!r} / {stderr!r}"
        assert "CLI-INTERCEPT-OK" in stdout, f"нет тела сервиса: {stdout!r}"
        assert _SECRET_VALUE not in stdout and _SECRET_VALUE not in stderr, (
            "значение секрета утекло в вывод команды")
        assert service.seen_auth == [f"Bearer {_SECRET_VALUE}"], (
            f"сервис не увидел впрыснутый Bearer: {service.seen_auth}")
        print("OK ЖИВОЙ CLI --wallet: сервис увидел Bearer, команда секрет не видела (§4.2)")
    finally:
        await pool.stop_all()
        await service.stop()


def main() -> None:
    if _IMPORT_ERR is not None:
        _skip(f"box_cli/vault недоступны: {_IMPORT_ERR}")
        return
    asyncio.run(test_setup_refusals())
    asyncio.run(test_setup_env_bundle_and_teardown())
    test_bundle_atomic_symlink()
    asyncio.run(test_live_cli_wallet_intercept())
    print("ALL BOX-CLI-WALLET OK")


if __name__ == "__main__":
    main()
