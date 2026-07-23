"""Vault-перехват для CLI `claude-box` (Launcher §5.2): standalone, БЕЗ оркестратора.

`--wallet <secret>` поднимает под капотом ровно то, что нужно для MITM-перехвата
одного прокси-секрета, и отдаёт лончеру env-довесок + доп. RW-бинд + teardown:

  1. secrets.toml → SecretStore → секрет по имени. Не найден / не прокси-секрет /
     не разрешён этой «сессии» → honest-отказ (WalletError, код 2).
  2. VaultCA (корень для MITM) + SessionProxyPool(host=TtyVaultHost) → port.
     upstream_ssl НЕ передаём — СИСТЕМНЫЙ trust: реориджин к реальному сервису
     проверяет его настоящий серт, самозванец под сервис кред не получит (§4.2).
  3. CA-bundle (системные корни + корень Vault) во ВРЕМЕННЫЙ каталог + env-довесок
     (HTTPS_PROXY + *_CA_BUNDLE + NO_PROXY) — общий примитив vault.inject.
     proxy_sandbox_env.

Куда bundle в CLI-песочнице. У box_cli под bwrap $HOME — пустой tmpfs (home_dir не
задаётся), а RW-виден только рабочий каталог (bind src==dst). Класть bundle в
проект оператора — сорить в его дереве; поэтому кладём во ВРЕМЕННЫЙ каталог и
биндим его в песочницу тем же путём (extra_rw). Путь bundle тогда одинаков внутри
и снаружи (для engine=off песочницы нет — путь просто хостовый). На выходе весь
временный каталог сносится — не течём.

Секрет наружу не уходит: значение живёт только в прокси между машиной и сервисом,
в env/лог/bundle попадает лишь публичный CA и адрес loopback-прокси.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from vault.inject import proxy_sandbox_env
from vault.proxy_pool import ProxyPoolError, SessionProxyPool
from vault.store import SecretStore
from vault.tls import VaultCA, VaultCAError
from vault.tty_host import TtyVaultHost

logger = logging.getLogger("claude-box.wallet")

# Синтетическое имя «сессии» для standalone-перехвата: у прокси-секрета в policy
# должно быть sessions = ["*"] или ["claude-box"], чтобы он разрешил этот запуск.
SESSION_NAME = "claude-box"


class WalletError(Exception):
    """Отказ настройки перехвата с кодом выхода CLI (2 — ошибка ввода/policy,
    1 — сбой окружения: нет openssl/системных корней/прокси не поднялся)."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class WalletIntercept:
    """Результат настройки: env-довесок к box.launch, доп. RW-бинды песочницы и
    асинхронный teardown (снять прокси, освободить порт, снести временный каталог)."""

    env: dict[str, str]
    extra_rw: list[Path] = field(default_factory=list)
    _pool: SessionProxyPool | None = None
    _tmpdir: Path | None = None

    async def close(self) -> None:
        """Снять прокси (порт освобождается) и удалить временный каталог bundle.
        Идемпотентно и не роняет выход — teardown обязан отработать в finally."""
        if self._pool is not None:
            try:
                await self._pool.stop_all()
            except Exception:  # noqa: BLE001 — teardown не должен ронять выход CLI
                logger.warning("wallet: сбой снятия прокси на выходе", exc_info=True)
            self._pool = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None


async def setup_wallet_intercept(
    secret_name: str, *, secrets_path: Path, session_name: str = SESSION_NAME,
) -> WalletIntercept:
    """Поднять standalone-перехват для прокси-секрета и вернуть WalletIntercept.

    Бросает WalletError (с кодом выхода) на любой честный отказ; при отказе ПОСЛЕ
    старта прокси/записи bundle всё уже поднятое сворачивается здесь же (не течём).
    """
    store = SecretStore(secrets_path)
    secret = store.load().get(secret_name)
    if secret is None:
        raise WalletError(
            f"секрет «{secret_name}» не найден в {secrets_path} "
            "(проверь имя и права файла 0600). См. `vault policy`.", code=2)
    if not secret.is_proxy:
        raise WalletError(
            f"секрет «{secret_name}» — не прокси-секрет (нет connector). "
            "--wallet перехватывает TLS только для прокси-секрета (§4.5); "
            "для host/inject-секретов используется `wallet run/exec` внутри сессии.",
            code=2)
    if not secret.session_allowed(session_name):
        raise WalletError(
            f"секрет «{secret_name}» не разрешён «{session_name}»: добавь "
            f'sessions = ["{session_name}"] (или ["*"]) в его запись secrets.toml.',
            code=2)

    # Корень CA для MITM. Нет openssl → перехват невозможен (сбой окружения, код 1).
    try:
        ca = VaultCA()
    except VaultCAError as e:
        raise WalletError(
            f"не удалось создать CA Vault ({e}) — нужен openssl. Перехват не поднят.",
            code=1) from e

    # upstream_ssl НЕ передаём (системный trust): реориджин проверяет НАСТОЯЩИЙ серт
    # сервиса — импостор под сервис кред не получит (§4.2). host=TtyVaultHost — ASK
    # спрашивается на tty (самотаймаут → DENY); confirm/ASK на том же tty, что и
    # relay, для этого среза не удобны (см. лончер), но scope-разрешённый трафик
    # generic-bearer идёт без вопросов.
    pool = SessionProxyPool(ca, store, host=TtyVaultHost())
    try:
        port = await pool.start(session_name, secret_name)
    except ProxyPoolError as e:
        await pool.stop_all()
        raise WalletError(f"прокси-секрет «{secret_name}» не поднят: {e}", code=2) from e

    tmpdir = Path(tempfile.mkdtemp(prefix="claude-box-vault-"))
    result = proxy_sandbox_env(ca, port, bundle_dir=tmpdir)
    if result is None:
        await pool.stop_all()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise WalletError(
            "системный набор доверенных корней не найден — перехват не включён "
            "(SSL_CERT_FILE только на Vault CA сломал бы прочий TLS).", code=1)
    env, bundle_path, _cleanup = result
    logger.info(
        "wallet: перехват секрета «%s» через 127.0.0.1:%d (trust %s)",
        secret_name, port, bundle_path)
    # Временный каталог биндится в песочницу тем же путём — bundle виден изнутри.
    return WalletIntercept(env=env, extra_rw=[tmpdir], _pool=pool, _tmpdir=tmpdir)
