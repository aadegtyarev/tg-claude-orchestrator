"""Демон-кошелёк секретов: docs/secrets-wallet.md.

Модель угроз: всё, что лежит внутри песочницы, модель может прочитать (Bash и
Read работают от её имени). Поэтому секрет не появляется в песочнице вообще —
команда исполняется демоном НА ХОСТЕ с секретом в env короткоживущего ребёнка,
а в песочницу возвращается только вывод (значения ВСЕХ известных секретов в нём
заменены на плейсхолдер `•••`).

Прозрачный шлюз: чтобы модель не дёргала CLI вручную, при провижне сессии в её
приватный $HOME пишется каталог обёрток .wallet-bin (SHIM_DIRNAME), который ядро
ставит первым в PATH песочницы (SessionManager.path_hooks). Обёртка каждого
разрешённого инструмента — крошечный скрипт `wallet exec <tool> "$@"`; демон
подбирает секрет по команде (_resolve_secret) и исполняет. git — особый случай
(_git_shim): сетевые подкоманды идут через кошелёк, локальные — настоящим git в
песочнице. Inject-секреты видны как env-переменные $NAME с МАРКЕРОМ вместо
значения (session_env) — демон разворачивает маркер в реальное значение на хосте
(_execute). CLI `bin/wallet` остаётся ручным путём (run/exec/get/env/ls/help).

Аутентификация per-session: на старте (и через core.session_hooks для новых
сессий) в персистентный $HOME сессии кладётся ~/.wallet.json (0600) с URL
демона и токеном. Policy — TOML-файл config.wallet_secrets_file (0600, вне
allowlist песочницы): каким сессиям и каким командам разрешён секрет, нужно ли
подтверждение кнопками. Отказ по умолчанию: секрет без sessions/commands не
выдаётся никому и ни на что.
"""

from __future__ import annotations

import functools
import html
import json
import logging
import os
import secrets
import shutil
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

# Домен кошелька вынесен в автономный пакет vault/ (без зависимостей
# оркестратора) — фаза 1 редизайна, docs/ARCHITECTURE-claude-box.md. Модуль
# здесь — оркестраторный адаптер над ним (демон/провижн/хуки сессий).
from vault.execute import run_secret_command
from vault.redact import _redact, _redact_text
from vault.secret import (
    DEFAULT_HOST_COMMANDS,  # noqa: F401 — ре-экспорт для тестов/обратной совместимости
    GIT_NETWORK,
    Secret,
    _always_denied,
    _prints_token,
    marker,
)
from vault.store import DEFAULT_SECRETS_TOML, SecretStore

from .policy import PolicyEditor, PolicyError

if TYPE_CHECKING:
    from ...config import Config
    from ...core.sessions import Session

logger = logging.getLogger(__name__)

# Каталог per-session обёрток внутри приватного дома сессии; ставится ПЕРВЫМ в
# PATH песочницы (SessionManager.path_hooks), поэтому завёрнутые инструменты
# побеждают настоящие бинари. Имя знает и ядро (session_home) — держим синхронно.
SHIM_DIRNAME = ".wallet-bin"


def _authed(handler):
    """Декоратор wallet-роут-хендлеров: резолвит сессию через `_auth`, отдаёт 401
    если Bearer-токен не признан, иначе зовёт хендлер с готовым `session`. Единая
    точка 401-политики на все 4 роута (/secrets, /run, /exec, /get)."""
    @functools.wraps(handler)
    async def wrapper(self, request: web.Request) -> web.Response:
        session = self._auth(request)
        if session is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(self, request, session)
    return wrapper


class WalletModule:
    """Модуль ядра: aiohttp-демон на 127.0.0.1:<эфемерный порт> + токены сессий."""

    name = "wallet"

    def __init__(self, config: "Config"):
        self.config = config
        self.store = SecretStore(config.wallet_secrets_file)
        # Правка policy из бота (/wallet) — просмотр + sessions/commands/deny/
        # confirm/new/rm; значения токенов не показывает и не принимает.
        self.policy = PolicyEditor(config.wallet_secrets_file)
        self.core = None
        self.port: int | None = None
        self._runner: web.AppRunner | None = None
        # Токен → имя сессии. Session-объект резолвим на каждый запрос через
        # manager.get: сессия могла быть удалена после выдачи токена.
        self._tokens: dict[str, str] = {}

    def handle_command(self, args_str: str) -> str:
        """`/wallet <args>` — просмотр/правка policy кошелька (HTML-текст).
        Ошибки policy возвращаем текстом (не исключением) — ядру не нужно знать
        внутренние типы модуля."""
        try:
            return self.policy.apply(
                (args_str or "").split(),
                allow_edit=self.config.wallet_policy_edit,
            )
        except PolicyError as e:
            return f"⚠️ {e}"

    # ── жизненный цикл ──────────────────────────────────────────

    async def start(self, core) -> None:
        self.core = core
        # Провижн (~/.wallet.json в приватном доме сессии) виден CLI только
        # когда дом смонтирован КАК $HOME процесса claude — это делает лишь
        # BwrapRunner. Под off/agent-vm CLI не найдёт конфиг → предупреждаем.
        if self.config.sandbox != "bwrap":
            # Кошелёк НЕ страхует без песочницы: без bwrap $HOME не изолирован,
            # модель читает secrets.toml/keyring/env напрямую — весь смысл
            # (секрет вне досягаемости модели) пропадает. Не блокируем (кошелёк
            # может работать), но громко предупреждаем — использовать ТОЛЬКО в
            # связке с SANDBOX=bwrap (см. docs/secrets-wallet.md).
            logger.warning(
                "wallet: SANDBOX=%s — кошелёк НЕ страхует от утечки секретов! Без "
                "SANDBOX=bwrap модель читает %s напрямую (нет изоляции $HOME). "
                "Используй кошелёк ТОЛЬКО вместе с SANDBOX=bwrap. Под off/agent-vm "
                "также `wallet` в сессии не найдёт ~/.wallet.json.",
                self.config.sandbox, self.config.wallet_secrets_file,
            )
        if not self.config.wallet_secrets_file.exists():
            self._write_default_secrets()  # прокол на хост gh/git/ssh/scp «из коробки»
        self.store.load()  # ранняя диагностика прав/синтаксиса — в лог на старте

        app = web.Application()
        app.router.add_get("/secrets", self._handle_secrets)
        app.router.add_post("/run", self._handle_run)
        app.router.add_post("/exec", self._handle_exec)
        app.router.add_post("/get", self._handle_get)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # Порт выдаёт ОС: свой сокет вместо TCPSite, чтобы узнать номер без
        # залезания в приватные поля aiohttp.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        await web.SockSite(self._runner, sock).start()
        logger.info("wallet: демон на 127.0.0.1:%d", self.port)

        self._provision_cli()
        for session in core.manager.list_all():
            await self._provision(session)
        core.session_hooks.append(self._provision)
        # Прозрачный шлюз: каталог обёрток (.wallet-bin) первым в PATH песочницы.
        core.manager.path_hooks.append(self.session_path)
        # env для песочницы: shared → реальное значение, inject → маркер $NAME.
        core.manager.env_hooks.append(self.session_env)
        # Вымарывать значения секретов из чат-вывода (shared модель видит и может
        # случайно эхнуть — safety-net, чтобы не улетело в Telegram/историю).
        core.output_redactors.append(self.redact_output)

    def redact_output(self, text: str) -> str:
        """Заменить значения ВСЕХ секретов (inject/shared) на •••. У host значения
        нет. Общий примитив с `_redact` — см. `_redact_text`."""
        return _redact_text(text, [s.value for s in self.store.load().values()])

    def session_env(self, session) -> dict[str, str]:
        """env для песочницы сессии — чтобы модель писала привычный `$ENV`:
          * shared  → РЕАЛЬНОЕ значение (модель может видеть/использовать);
          * inject  → МАРКЕР `<<wallet:имя>>` (скрыто) — обёртка развернёт его в
            значение на хосте, в песочницу значение не попадает;
          * host-passthrough (нет env) → ничего.
        Плюс маркер пути к файлу `<<wallet:имя:file>>` в `$ENV_FILE` (ssh-ключ,
        сертификат) — для inject-секретов."""
        out: dict[str, str] = {}
        for s in self.store.load().values():
            if not s.env or not s.session_allowed(session.name):
                continue
            if s.shared:
                out[s.env] = s.value
            else:
                out[s.env] = marker(s.name)
                out[s.env + "_FILE"] = marker(s.name, as_file=True)
        return out

    def _write_default_secrets(self) -> None:
        """Создать secrets.toml с дефолтным host-passthrough (gh/git/ssh/scp на
        все сессии), чтобы кошелёк работал «из коробки». Пишем 0600 через
        O_EXCL (ни мгновения без прав); гонку/недоступный каталог не роняем."""
        path = self.config.wallet_secrets_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(DEFAULT_SECRETS_TOML)
            logger.info(
                "wallet: создан дефолтный %s — прокол на хост gh/git/ssh/scp для "
                "всех сессий (правь через /wallet или руками)", path,
            )
        except FileExistsError:
            pass  # появился параллельно — просто загрузим существующий
        except OSError as e:
            logger.error("wallet: не удалось создать дефолтный %s: %s", path, e)

    @staticmethod
    def _discard(seq: list, item) -> None:
        """Снять хук из списка, молча стерпев отсутствие (идемпотентный stop)."""
        try:
            seq.remove(item)
        except ValueError:
            pass

    async def stop(self) -> None:
        if self.core is not None:
            self._discard(self.core.session_hooks, self._provision)
            self._discard(self.core.manager.path_hooks, self.session_path)
            self._discard(self.core.manager.env_hooks, self.session_env)
            self._discard(self.core.output_redactors, self.redact_output)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._tokens.clear()

    def _provision_cli(self) -> None:
        """Симлинк ~/.local/bin/wallet → <репо>/bin/wallet, чтобы CLI был в PATH
        внутри песочницы.

        bwrap биндит и ~/.local/bin, и корень репо (RO, по тем же путям), поэтому
        симлинк разрешается внутри песочницы и самообновляется при обновлении
        bin/wallet. Без этого агент получит «wallet: command not found» — сам
        файл лежит в репо, а в PATH сессии его нет.
        """
        cli = Path(__file__).resolve().parents[3] / "bin" / "wallet"
        link = Path.home() / ".local" / "bin" / "wallet"
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink():
                if link.readlink() == cli:
                    return  # уже указывает куда надо
                link.unlink()
            elif link.exists():
                logger.warning("wallet: %s — не наш файл, CLI не провижу", link)
                return
            link.symlink_to(cli)
            logger.info("wallet: CLI в PATH сессии (%s → %s)", link, cli)
        except OSError as e:
            logger.error("wallet: не удалось провизить CLI %s: %s", link, e)

    async def _provision(self, session) -> None:
        """Выдать сессии токен и записать ~/.wallet.json в её приватный $HOME.

        Внутри песочницы session_home смонтирован КАК $HOME процесса claude,
        так что CLI найдёт файл по ~/.wallet.json без настройки.
        """
        token = secrets.token_urlsafe(32)
        path = self.core.manager.session_home(session) / ".wallet.json"
        payload = {
            "url": f"http://127.0.0.1:{self.port}",
            "token": token,
            "session": session.name,
        }
        # O_CREAT с 0600 — файл ни мгновения не живёт с широкими правами;
        # chmod дожимает случай, когда файл уже существовал с иными правами.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(path, 0o600)
        # Перевыдача (рестарт/повторный hook) отзывает прежний токен сессии.
        self._tokens = {t: n for t, n in self._tokens.items() if n != session.name}
        self._tokens[token] = session.name
        self._provision_shims(session)

    # ── прозрачные обёртки (шлюз в PATH) ─────────────────────────

    def session_path(self, session) -> list[str]:
        """Каталог обёрток кошелька для PATH песочницы (SessionManager.path_hooks,
        prepend). ВАЖНО: путь должен быть таким, как его видит процесс в песочнице.
        Под bwrap session_home смонтирован как $HOME, поэтому обёртки, записанные
        на хосте в session_home/.wallet-bin, видны изнутри по $HOME/.wallet-bin —
        именно этот путь и кладём в PATH (хостовый .homes/<имя>/.wallet-bin внутри
        песочницы не существует). Возвращаем всегда: файлы наполняются из
        session_hooks уже после старта, а каталог bind-смонтирован живым."""
        return [str(Path.home() / SHIM_DIRNAME)]

    def _session_tools(self, session) -> set[str]:
        """Голые имена инструментов, которые надо завернуть для этой сессии —
        из `commands` её НЕ-shared секретов. shared пропускаем: их значение уже
        лежит в env песочницы (модель зовёт инструмент напрямую, хостовый
        раунд-трип не нужен). Из шаблона берём первый токен (`curl https://a/*`
        → curl); чистые глобы (`*`) не заворачиваем — не бинарь."""
        tools: set[str] = set()
        for s in self.store.load().values():
            if s.shared or not s.session_allowed(session.name):
                continue
            for pat in s.effective_commands:
                parts = pat.split()
                tool = os.path.basename(parts[0]) if parts else ""
                if tool and not any(c in tool for c in "*?["):
                    tools.add(tool)
        return tools

    def _git_shim(self) -> str:
        """Обёртка git: сетевые подкоманды → на хост через кошелёк, локальные →
        настоящий git в песочнице. Путь настоящего git резолвим на хосте (/usr
        у песочницы тот же RO-бинд, поэтому путь совпадает)."""
        real = shutil.which("git") or "/usr/bin/git"
        nets = "|".join(GIT_NETWORK)
        return (
            "#!/bin/sh\n"
            "# Обёртка кошелька (генерируется): сетевые git-подкоманды идут на\n"
            "# хост через `wallet exec` (креды хоста), локальные — настоящим git.\n"
            f'case "${{1:-}}" in\n'
            f'  {nets}) exec wallet exec git "$@" ;;\n'
            "esac\n"
            f'exec {real} "$@"\n'
        )

    def _provision_shims(self, session) -> None:
        """Полная перегенерация обёрток в <дом-сессии>/.wallet-bin. Заворачиваем
        gh/curl/ssh/… целиком (→ `wallet exec <tool>`), git — особо (см. _git_shim).
        Перегенерация чистит устаревшие обёртки, если секрет/команду отозвали."""
        shim_dir = self.core.manager.session_home(session) / SHIM_DIRNAME
        try:
            if shim_dir.exists():
                for old in shim_dir.iterdir():
                    if old.is_file() or old.is_symlink():
                        old.unlink()
            tools = self._session_tools(session)
            if not tools:
                return
            shim_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(shim_dir, 0o700)
            for tool in sorted(tools):
                script = self._git_shim() if tool == "git" else (
                    f'#!/bin/sh\nexec wallet exec {tool} "$@"\n'
                )
                p = shim_dir / tool
                p.write_text(script)
                os.chmod(p, 0o755)
            logger.info(
                "wallet: обёртки сессии %s: %s", session.name, ", ".join(sorted(tools))
            )
        except OSError as e:
            logger.error("wallet: обёртки для %s не созданы: %s", session.name, e)

    # ── HTTP API ────────────────────────────────────────────────

    def _auth(self, request: web.Request):
        """Bearer-токен → Session. Сравнение constant-time (compare_digest),
        перебор без раннего выхода — тайминг не выдаёт «почти угадал»."""
        header = request.headers.get("Authorization", "")
        token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        # На байтах: compare_digest на str с не-ASCII бросает TypeError (был бы
        # 500 вместо 401). Перебор без раннего выхода — тайминг не выдаёт «почти».
        token_b = token.encode("utf-8", "replace")
        found: str | None = None
        for known, sname in self._tokens.items():
            if secrets.compare_digest(known.encode("utf-8"), token_b):
                found = sname
        if not token or found is None:
            return None
        return self.core.manager.get(found)

    @_authed
    async def _handle_secrets(self, request: web.Request, session: Session) -> web.Response:
        """Список секретов, разрешённых этой сессии, — БЕЗ значений."""
        out = [
            {
                "name": s.name,
                "description": s.description,
                "commands": list(s.effective_commands),
                "confirm": s.confirm,
                # host — команда на хосте с его окружением; inject — значение в
                # env-переменную `env` дочернего процесса; shared — значение
                # ВЫДАётся сессии (wallet get/env), не прячется.
                "mode": s.mode,
                "env": s.env or None,
            }
            for s in self.store.load().values()
            if s.session_allowed(session.name)
        ]
        return web.json_response(out)

    @_authed
    async def _handle_get(self, request: web.Request, session: Session) -> web.Response:
        """Выдать сессии ЗНАЧЕНИЕ shared-секрета (dev-ключ, логин/пароль).

        Только для shared — host/inject значения не выдаются НИКОГДА (в этом их
        смысл). shared — про хранение вне чата/репо, не про сокрытие от модели."""
        try:
            data = await request.json()
        except Exception:
            data = {}
        name = str(data.get("secret", ""))
        secret = self.store.load().get(name)
        if secret is None or not secret.session_allowed(session.name):
            return web.json_response(
                {"error": "denied",
                 "reason": f"нет shared-секрета «{name}» для этой сессии (см. wallet ls)"},
                status=403,
            )
        if not secret.shared:
            return web.json_response(
                {"error": "denied",
                 "reason": f"секрет «{name}» не shared — значение не выдаётся "
                           "(для host/inject используй wallet run)"},
                status=403,
            )
        if secret.confirm:
            ok = await self.core.request_confirmation(
                session, tool="wallet",
                description=f"выдать значение shared-секрета «{name}» сессии",
                preview=f"wallet get {name}",
            )
            if not ok:
                return web.json_response(
                    {"error": "denied", "reason": "отклонено кнопкой подтверждения"},
                    status=403,
                )
        # Наблюдаемость: выдача видна строкой (без значения).
        await self.core.bubbles.append_background(
            session.name,
            f"🔐 <b>wallet get</b> <code>{html.escape(name)}</code>", tool="wallet",
        )
        self.core._record(session, "wallet", secret=name, cmd=f"get {name}", allowed=True)
        return web.json_response(
            {"name": name, "env": secret.env or None, "value": secret.value}
        )

    @_authed
    async def _handle_run(self, request: web.Request, session: Session) -> web.Response:
        """Явный вызов: `wallet run <name> -- <cmd>` — секрет задан именем."""
        try:
            body = await request.json()
            name = str(body["secret"])
            cmd = [str(c) for c in body["cmd"]]
            if not cmd:
                raise ValueError("пустая команда")
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        return await self._run_secret(session, self.store.load().get(name), cmd, name)

    @_authed
    async def _handle_exec(self, request: web.Request, session: Session) -> web.Response:
        """Прозрачный шлюз: `wallet exec <cmd>` (зовут обёртки в PATH песочницы)
        — секрет подбирается ПО КОМАНДЕ (чей `commands` её разрешает). Модель
        зовёт инструмент как обычно."""
        try:
            body = await request.json()
            cmd = [str(c) for c in body["cmd"]]
            if not cmd:
                raise ValueError("пустая команда")
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        secret = self._resolve_secret(session, cmd)
        label = secret.name if secret is not None else os.path.basename(cmd[0])
        return await self._run_secret(session, secret, cmd, label)

    def _resolve_secret(self, session, cmd: list[str]):
        """Секрет, чьи commands разрешают эту команду для сессии (авто-подбор для
        /exec). Первый подходящий; None — если ни один не разрешает."""
        for s in self.store.load().values():
            if s.session_allowed(session.name) and s.command_allowed(cmd):
                return s
        return None

    async def _run_secret(self, session, secret, cmd: list[str], label: str) -> web.Response:
        """Общий путь /run и /exec: policy (guard/deny/confirm) + наблюдаемость +
        выполнение на хосте + редакция вывода."""
        cmd_str = " ".join(cmd)
        all_secrets = self.store.load()
        # Причина отказа для прозрачности: 1) встроенный guard (печать токена,
        # git-RCE), 2) точечный deny секрета.
        reason: str | None = None
        if secret is not None:
            if self.config.wallet_guard and not secret.allow_unsafe:
                reason = _always_denied(cmd)
            if reason is None and (pat := secret.denied_by(cmd)) is not None:
                reason = f"заблокировано policy этого секрета (deny: {pat})"
        allowed = (
            secret is not None
            and secret.session_allowed(session.name)
            and secret.command_allowed(cmd)
            and reason is None
        )
        if allowed and secret.confirm:
            allowed = await self.core.request_confirmation(
                session, tool="wallet",
                description=f"{label} → {cmd_str[:200]}", preview=cmd_str,
            )
        # Наблюдаемость: КАЖДАЯ попытка видна строкой в бабле; отдельное
        # уведомление — только на ОТКАЗ (нужно внимание).
        cmd_disp = f"{label} → {cmd_str[:120]}"
        bubble_line = f"🔐 <b>wallet</b> <code>{html.escape(cmd_disp)}</code>"
        await self.core.bubbles.append_background(session.name, bubble_line, tool="wallet")
        self.core._record(session, "wallet", secret=label, cmd=cmd_str, allowed=bool(allowed))
        if not allowed:
            if reason is None:
                if secret is None:
                    reason = f"нет секрета для «{cmd_str[:80]}» (проверь `wallet ls`)"
                elif not secret.session_allowed(session.name):
                    reason = "секрет не разрешён этой сессии (policy sessions)"
                elif not secret.command_allowed(cmd):
                    reason = "команда не в списке разрешённых (policy commands)"
                else:
                    reason = "отклонено кнопкой подтверждения"
            # Operator-notice — только для отказов, требующих его внимания.
            # `gh auth token`/`--show-token` (печать токена) НЕ шлём: отказ
            # самокорректирующийся (reason ушёл в stderr модели, аудит — в бабле),
            # а фоновый поллер Claude Code («PR status») зовёт её периодически —
            # иначе спам в чат на каждый опрос. git-RCE и policy-отказы — шлём.
            if not _prints_token(cmd):
                notice_md = f"🔐 wallet: `{cmd_disp.replace('`', chr(39))}`"
                await self.core.notice(
                    session,
                    self.core.t("wallet_use", line=notice_md) + " — " + self.core.t("wallet_denied"),
                )
            return web.json_response({"error": "denied", "reason": reason}, status=403)
        code, out, err = await self._execute(session, secret, cmd)
        values = [s.value for s in all_secrets.values()]
        return web.json_response(
            {"code": code, "stdout": _redact(out, values), "stderr": _redact(err, values)}
        )

    async def _execute(self, session, secret: Secret, cmd: list[str]) -> tuple[int, bytes, bytes]:
        """Запустить команду НА ХОСТЕ под секретом — делегат в vault.execute.
        Резолвит cwd проекта (effective_cwd) и текущий набор секретов (для
        разворачивания маркеров), остальное — автономная логика пакета vault."""
        return await run_secret_command(
            cmd, secret,
            cwd=self.core.manager.effective_cwd(session),
            all_secrets=self.store.load(),
            session_name=session.name,
        )
