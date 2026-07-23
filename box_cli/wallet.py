"""Кошелёк для CLI `claude-box` (Launcher §5.2): standalone, БЕЗ оркестратора.

`--wallet <secret>` работает по виду секрета — оба пути отдают лончеру одно и то
же: env-довесок + доп. RW-бинд + асинхронный teardown (WalletIntercept).

  * ПРОКСИ-секрет (connector) → MITM-перехват TLS: поднимается standalone-прокси,
    в песочницу уходят HTTPS_PROXY + объединённый CA-bundle; кред подставляется
    прокси между машиной и сервисом, в песочницу значение не попадает (§4.2).
  * host/inject-секрет → ШИМЫ: поднимается standalone-демон кошелька
    (vault.cli.build_daemon + TtyVaultHost) и в песочницу кладётся каталог
    PATH-обёрток (vault.shims): модель зовёт `git push`/`gh`/`curl` как обычно, а
    обёртка уходит в `wallet exec` — команда исполняется НА ХОСТЕ с кредом, в
    песочницу возвращается только (отредактированный) вывод. Плюс WALLET_FILE →
    временный wallet.json (url+token демона) и обёртка самого клиента `wallet`.

Что делает прокси-путь по шагам:

  1. secrets.toml → SecretStore → секрет по имени. Не найден / не прокси-секрет /
     не разрешён этой «сессии» → honest-отказ (WalletError, код 2).
  2. VaultCA (корень для MITM) + SessionProxyPool(host=TtyVaultHost) → port.
     upstream_ssl НЕ передаём — СИСТЕМНЫЙ trust: реориджин к реальному сервису
     проверяет его настоящий серт, самозванец под сервис кред не получит (§4.2).
  3. CA-bundle (системные корни + корень Vault) во ВРЕМЕННЫЙ каталог + env-довесок
     (HTTPS_PROXY + *_CA_BUNDLE + NO_PROXY) — общий примитив vault.inject.
     proxy_sandbox_env.

Куда класть файлы в CLI-песочнице. У box_cli под bwrap $HOME — пустой tmpfs
(home_dir не задаётся), а RW-виден только рабочий каталог (bind src==dst). Класть
bundle/шимы/wallet.json в проект оператора — сорить в его дереве; поэтому кладём
во ВРЕМЕННЫЙ каталог и биндим его в песочницу тем же путём (extra_rw). Пути тогда
одинаковы внутри и снаружи (для engine=off песочницы нет — путь просто хостовый).
На выходе весь временный каталог сносится — не течём.

Секрет наружу не уходит: значение живёт только в прокси между машиной и сервисом,
в env/лог/bundle попадает лишь публичный CA и адрес loopback-прокси.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from vault.cli import build_daemon, write_wallet
from vault.daemon import VaultDaemon
from vault.inject import proxy_sandbox_env
from vault.proxy_pool import ProxyPoolError, SessionProxyPool
from vault.shims import SHIM_DIRNAME, cli_shim, tool_names, write_shims
from vault.store import SecretStore
from vault.tls import VaultCA, VaultCAError
from vault.tty_host import TtyVaultHost

logger = logging.getLogger("claude-box.wallet")

# Синтетическое имя «сессии» для standalone-перехвата: у прокси-секрета в policy
# должно быть sessions = ["*"] или ["claude-box"], чтобы он разрешил этот запуск.
SESSION_NAME = "claude-box"

# Имя файла с url+token демона во временном каталоге. Не `.wallet.json` в $HOME:
# под bwrap $HOME — пустой tmpfs, туда писать бессмысленно; клиент находит файл
# по WALLET_FILE (bin/wallet смотрит его ПЕРВЫМ, до ~/.wallet.json).
WALLET_FILE_NAME = "wallet.json"


class WalletError(Exception):
    """Отказ настройки перехвата с кодом выхода CLI (2 — ошибка ввода/policy,
    1 — сбой окружения: нет openssl/системных корней/прокси не поднялся)."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class WalletIntercept:
    """Результат настройки: env-довесок к box.launch, доп. RW-бинды песочницы и
    асинхронный teardown. Один тип на оба пути (прокси/шимы) — лончеру всё равно,
    что именно поднято: он делает env.update + extra_rw + close() в finally."""

    env: dict[str, str]
    extra_rw: list[Path] = field(default_factory=list)
    _pool: SessionProxyPool | None = None
    _daemon: VaultDaemon | None = None
    _tmpdir: Path | None = None

    async def close(self) -> None:
        """Снять прокси/остановить демон (порты освобождаются) и удалить временный
        каталог (bundle либо шимы+wallet.json). Идемпотентно и не роняет выход —
        teardown обязан отработать в finally."""
        if self._pool is not None:
            try:
                await self._pool.stop_all()
            except Exception:  # noqa: BLE001 — teardown не должен ронять выход CLI
                logger.warning("wallet: сбой снятия прокси на выходе", exc_info=True)
            self._pool = None
        if self._daemon is not None:
            try:
                await self._daemon.stop()
            except Exception:  # noqa: BLE001 — teardown не должен ронять выход CLI
                logger.warning("wallet: сбой остановки демона на выходе", exc_info=True)
            self._daemon = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None


async def setup_wallet_intercept(
    secret_name: str, *, secrets_path: Path, session_name: str = SESSION_NAME,
) -> WalletIntercept:
    """Поднять кошелёк под один секрет и вернуть WalletIntercept.

    Вид секрета решает КАК: прокси-секрет → MITM-перехват TLS, host/inject →
    PATH-шимы над standalone-демоном. Бросает WalletError (с кодом выхода) на
    любой честный отказ; всё уже поднятое при отказе сворачивается (не течём).
    """
    store = SecretStore(secrets_path)
    secret = store.load().get(secret_name)
    if secret is None:
        raise WalletError(
            f"секрет «{secret_name}» не найден в {secrets_path} "
            "(проверь имя и права файла 0600). См. `vault policy`.", code=2)
    if not secret.session_allowed(session_name):
        raise WalletError(
            f"секрет «{secret_name}» не разрешён «{session_name}»: добавь "
            f'sessions = ["{session_name}"] (или ["*"]) в его запись secrets.toml.',
            code=2)
    if not secret.is_proxy:
        return await setup_wallet_shims(
            secret_name, store=store, secrets_path=secrets_path,
            session_name=session_name)
    return await _setup_proxy_intercept(
        secret_name, store=store, session_name=session_name)


async def _setup_proxy_intercept(
    secret_name: str, *, store: SecretStore, session_name: str,
) -> WalletIntercept:
    """Прокси-секрет: MITM-перехват TLS (env HTTPS_PROXY + CA-bundle)."""
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


# ── host/inject-секрет: шимы над standalone-демоном ──────────────────────────

def wallet_cli_path() -> Path:
    """Путь к stdlib-клиенту `wallet` в репозитории. Репозиторий RO-биндится в
    песочницу тем же путём (BwrapRunner биндит root), поэтому файл достижим и
    изнутри — не хватает лишь имени в PATH, его даёт обёртка в каталоге шимов."""
    return Path(__file__).resolve().parent.parent / "bin" / "wallet"


def build_shim_dir(tmpdir: Path, tools: set[str]) -> Path:
    """Создать каталог шимов внутри tmpdir: обёртки инструментов + сам `wallet`.

    Возвращает путь каталога (он же — первый элемент PATH песочницы). Права:
    каталог 0700, скрипты 0755 (см. vault.shims.write_shims).
    """
    shim_dir = tmpdir / SHIM_DIRNAME
    write_shims(shim_dir, tools)
    # Каталог мог не появиться (пустой набор инструментов) — сам клиент кладём в
    # любом случае: без него шимы бесполезны, а ручной путь `wallet run` нужен.
    shim_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(shim_dir, 0o700)
    cli = shim_dir / "wallet"
    cli.write_text(cli_shim(wallet_cli_path()))
    os.chmod(cli, 0o755)
    return shim_dir


async def setup_wallet_shims(
    secret_name: str, *, store: SecretStore, secrets_path: Path, session_name: str,
) -> WalletIntercept:
    """host/inject-секрет: поднять standalone-демон и положить в PATH шимы.

    Что получает песочница: PATH=<каталог шимов>:<исходный PATH> и WALLET_FILE на
    временный wallet.json (url+token демона). Значение секрета в песочницу не
    попадает — команда исполняется демоном НА ХОСТЕ, в песочницу идёт только
    отредактированный вывод.

    Демон здесь ОДИН на процесс claude-box и знает всю policy файла secrets.toml
    (демон подбирает секрет по команде сам — так же, как у оркестратора). Шимы же
    ставим только под запрошенный `--wallet <секрет>`: явный запрос оператора, а
    не «всё, что лежит в secrets.toml».
    """
    secret = store.load().get(secret_name)
    if secret is None:
        raise WalletError(
            f"секрет «{secret_name}» не найден в {secrets_path} "
            "(проверь имя и права файла 0600). См. `vault policy`.", code=2)
    tools = tool_names(secret.effective_commands)
    if not tools:
        raise WalletError(
            f"у секрета «{secret_name}» нет ни одной команды для заворачивания: "
            "в commands либо пусто, либо только глобы (`*`). Добавь имя "
            'инструмента, напр. commands = ["gh", "git"], — тогда claude-box '
            "положит его обёртку в PATH песочницы. См. `vault policy`.", code=2)

    # Демон standalone (TtyVaultHost): confirm/ASK спрашиваются на ТОЙ ЖЕ tty, что
    # держит relay claude — для секретов с confirm=true это неудобно (вопрос уедет
    # в терминал под raw-режимом), поэтому такие секреты в claude-box лучше не
    # использовать. Отказ по умолчанию сохраняется: без tty confirm = DENY.
    daemon = build_daemon(secrets_path)
    try:
        await daemon.start()
    except Exception as e:  # noqa: BLE001 — сбой окружения, честный отказ (код 1)
        try:
            await daemon.stop()  # частично поднятый демон не оставляем висеть
        except Exception:  # noqa: BLE001
            pass
        raise WalletError(
            f"демон кошелька не поднят ({e}) — секрет «{secret_name}» недоступен.",
            code=1) from e

    tmpdir = Path(tempfile.mkdtemp(prefix="claude-box-wallet-"))
    os.chmod(tmpdir, 0o700)
    try:
        # Токен привязан к рабочему каталогу: команды под секретом исполняются на
        # хосте именно в нём (тот же каталог, что RW-биндится в песочницу).
        token = daemon.issue_token(session_name, Path.cwd())
        wallet_file = tmpdir / WALLET_FILE_NAME
        write_wallet(wallet_file, daemon.url, token, session_name)
        shim_dir = build_shim_dir(tmpdir, tools)
    except OSError as e:
        await daemon.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise WalletError(
            f"не удалось подготовить обёртки кошелька ({e}).", code=1) from e

    env = {
        "PATH": os.pathsep.join(
            [str(shim_dir), os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")]),
        "WALLET_FILE": str(wallet_file),
    }
    # Прозрачность: оператор должен видеть, что именно завёрнуто (эти команды
    # уедут на хост с реальным кредом).
    git_note = " (git — только сетевые подкоманды)" if "git" in tools else ""
    sys.stderr.write(
        f"claude-box: --wallet «{secret_name}»: через кошелёк идут "
        + ", ".join(sorted(tools)) + git_note
        + "; клиент `wallet` в PATH.\n")
    logger.info(
        "wallet: шимы секрета «%s» в %s, демон %s", secret_name, shim_dir, daemon.url)
    return WalletIntercept(
        env=env, extra_rw=[tmpdir], _daemon=daemon, _tmpdir=tmpdir)
