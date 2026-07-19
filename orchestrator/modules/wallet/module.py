"""Демон-кошелёк секретов: этап 1 дизайн-дока docs/secrets-wallet.md.

Модель угроз: всё, что лежит внутри песочницы, модель может прочитать (Bash и
Read работают от её имени). Поэтому секрет не появляется в песочнице вообще —
CLI `bin/wallet` шлёт команду демону по TCP-localhost (сеть у bwrap общая с
хостом), а демон исполняет её НА ХОСТЕ с секретом в env короткоживущего
ребёнка. В песочницу возвращается только вывод, причём значения ВСЕХ известных
секретов в нём заменены на плейсхолдер.

Аутентификация per-session: на старте (и через core.session_hooks для новых
сессий) в персистентный $HOME сессии кладётся ~/.wallet.json (0600) с URL
демона и токеном. Policy — TOML-файл config.wallet_secrets_file (0600, вне
allowlist песочницы): каким сессиям и каким командам разрешён секрет, нужно ли
подтверждение кнопками. Отказ по умолчанию: секрет без sessions/commands не
выдаётся никому и ни на что.
"""

from __future__ import annotations

import asyncio
import fnmatch
import html
import json
import logging
import os
import secrets
import signal
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger(__name__)

# Таймаут команды под секретом; CLI ждёт чуть дольше (310с).
RUN_TIMEOUT = 300.0
# Потолок каждого потока вывода — защита от заливки памяти/чата гигабайтами.
STREAM_LIMIT = 200_000
# Чем заменяем значения секретов в выводе.
REDACTED = "•••"
# Дефолтный набор для host-passthrough, когда `commands` не задан: инструменты,
# которые обычно уже авторизованы на хосте и не отдают сам секрет наружу. Смысл
# кошелька — «используй, но не читай»: gh/git/ssh/scp применяют креды сами,
# echo/cat/sh их бы просто распечатали, поэтому в дефолт НЕ входят. Хочешь
# curl/kubectl/своё — допиши commands явно.
DEFAULT_HOST_COMMANDS = ("gh", "git", "ssh", "scp")


def _always_denied(cmd: list[str]) -> str | None:
    """Опасные вызовы, запрещённые guard'ом — при любой policy, даже `commands=["gh"]`.

    Смысл: голое имя инструмента должно оставаться удобным, не превращаясь в
    утечку токена или запуск произвольного кода на хосте. Возвращает ПРОЗРАЧНОЕ
    сообщение модели (что не так + как правильно) либо None. Guard включается
    флагом WALLET_GUARD (по умолчанию on); применяется в _handle_run.

    Это НЕ полная защита — безфлаговый вектор (подложить `./.git/config` в
    проекте, который `git push` всё равно прочитает) закрыть аргументами нельзя,
    только доверием к сессии (см. docs «Известные дыры»).
    """
    binary = os.path.basename(cmd[0]) if cmd else ""
    # 1. Печатают сам секрет — редакция literal-only их не всегда ловит.
    if cmd[:2] == ["gh", "auth"] and ("token" in cmd[2:3] or "--show-token" in cmd):
        return ("Эта команда печатает сам токен, а кошелёк не выдаёт значения "
                "секретов. Используй gh для операций (gh pr …, gh api …, gh release …), "
                "а не для печати токена.")
    # 2. git → произвольное исполнение на хосте через конфиг/транспорт/флаги.
    if binary == "git":
        toks = cmd[1:]
        if "-c" in toks:  # -c core.sshCommand=… / protocol.ext.allow=… / core.fsmonitor=…
            return ("Флаг `git -c` переопределяет конфиг и может запустить произвольный "
                    "код на хосте — поэтому запрещён. Запусти git push/pull/fetch БЕЗ "
                    "`-c`; если нужен особый git-конфиг, попроси оператора настроить "
                    "его на хосте.")
        for t in toks:
            if t.startswith("ext::"):
                return ("git-транспорт `ext::` запускает произвольную команду — запрещён. "
                        "Используй обычный remote (https или ssh) для push/pull/fetch.")
            if t.startswith(("--receive-pack", "--upload-pack", "--exec")):
                return ("Флаги --receive-pack/--upload-pack/--exec запускают произвольную "
                        "команду на той стороне — запрещены. Запусти git push/pull/fetch "
                        "без них.")
    return None


@dataclass(frozen=True)
class Secret:
    """Один секрет/доступ из secrets.toml вместе со своей policy.

    Два вида:
      * inject — value+env: команда получает секрет в env-переменной (env=…,
        value=…). Классический кошелёк.
      * host-passthrough — БЕЗ value/env: команда просто исполняется на ХОСТЕ
        с хостовым окружением (keyring, gh/git auth). Для инструментов, уже
        авторизованных на хосте (gh, git), чьи токены лежат в keyring/файле
        вне песочницы — модель их не видит, а команда работает. Ничего в env
        не инжектим.
    """

    name: str
    value: str  # "" для host-passthrough
    env: str    # "" для host-passthrough
    description: str
    sessions: tuple[str, ...]  # fnmatch-шаблоны имён сессий; пусто = никому
    # commands: где кошелёк доступен (allow-лист). Голое имя инструмента («gh»,
    # «ssh») = любой его вызов; строка с пробелом/глобом («curl https://api/*») =
    # fnmatch по всей команде (тонкая настройка). Для host-passthrough пустое
    # поле = DEFAULT_HOST_COMMANDS; для inject пустое = ничего (сырой токен не
    # открываем без явного списка).
    commands: tuple[str, ...]
    # deny: точечный запрет ПОВЕРХ commands (deny побеждает allow). Голый токен
    # («--force», «--hard») = блок этого флага/аргумента где угодно; строка с
    # пробелом/глобом = fnmatch по всей команде. Для «разрешаю инструмент, но
    # не эти опасные флаги».
    deny: tuple[str, ...]
    # allow_unsafe: точечно отключить встроенный guard (печать токена, git-RCE)
    # для ЭТОГО секрета — для доверенных специфичных случаев. Глобально guard
    # рубится WALLET_GUARD=0; это — гранулярно, на один секрет.
    allow_unsafe: bool
    confirm: bool  # спрашивать ли подтверждение кнопками перед запуском

    @property
    def host_passthrough(self) -> bool:
        return not (self.value and self.env)

    @property
    def effective_commands(self) -> tuple[str, ...]:
        if self.commands:
            return self.commands
        return DEFAULT_HOST_COMMANDS if self.host_passthrough else ()

    def session_allowed(self, session_name: str) -> bool:
        return any(fnmatch.fnmatch(session_name, pat) for pat in self.sessions)

    @staticmethod
    def _matches(pat: str, binary: str, cmd: list[str], cmd_str: str) -> bool:
        """Один шаблон против команды: голый токен = имя инструмента или любой
        аргумент; строка с пробелом/глобом = fnmatch по всей строке команды."""
        if " " not in pat and not any(c in pat for c in "*?["):
            return binary == pat or pat in cmd[1:]
        return fnmatch.fnmatch(cmd_str, pat)

    def denied_by(self, cmd: list[str]) -> str | None:
        """Точечный запрет секрета (deny). Возвращает сматчивший шаблон или None."""
        if not cmd:
            return None
        binary = os.path.basename(cmd[0])
        cmd_str = " ".join(cmd)
        for pat in self.deny:
            if self._matches(pat, binary, cmd, cmd_str):
                return pat
        return None

    def command_allowed(self, cmd: list[str]) -> bool:
        """Allow-проверка по commands (guard и deny — отдельно, в _handle_run)."""
        if not cmd:
            return False
        binary = os.path.basename(cmd[0])
        cmd_str = " ".join(cmd)
        for pat in self.effective_commands:
            # allow голым именем — только имя инструмента (не «аргумент где-то»),
            # иначе commands=["gh"] разрешил бы «git … gh …». Потому не _matches.
            if " " not in pat and not any(c in pat for c in "*?["):
                if binary == pat:
                    return True
            elif fnmatch.fnmatch(cmd_str, pat):
                return True
        return False


class SecretStore:
    """Ленивое чтение secrets.toml с кэшем по (mtime, mode, size).

    mode входит в ключ кэша не случайно: chmod не меняет mtime, а ослабление
    прав должно немедленно отключать выдачу секретов.
    """

    def __init__(self, path: Path):
        self._path = path
        self._cache_key: tuple | None = None
        self._secrets: dict[str, Secret] = {}

    def load(self) -> dict[str, Secret]:
        try:
            st = self._path.stat()
        except OSError:
            # Файла нет — кошелёк работает, но секретов нет (warning на старте).
            self._cache_key, self._secrets = None, {}
            return {}
        key = (st.st_mtime_ns, st.st_mode, st.st_size)
        if key == self._cache_key:
            return self._secrets
        self._cache_key, self._secrets = key, {}
        # Права шире 0600 — любой локальный пользователь/группа прочитал бы
        # значения; отказываемся грузить целиком, а не «только пошире-часть».
        if st.st_mode & 0o077:
            logger.error(
                "wallet: %s доступен group/other (права %o) — секреты НЕ загружены; "
                "выполни chmod 600", self._path, st.st_mode & 0o777,
            )
            return {}
        try:
            data = tomllib.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            logger.error("wallet: не удалось прочитать %s: %s", self._path, e)
            return {}
        for name, raw in (data.get("secrets") or {}).items():
            if not isinstance(raw, dict):
                logger.error("wallet: запись %r — не таблица, пропущена", name)
                continue
            has_value, has_env = "value" in raw, "env" in raw
            # inject-секрет требует ОБА поля; host-passthrough — НИ ОДНОГО.
            # Одно без другого — ошибка конфига (пропускаем).
            if has_value != has_env:
                logger.error(
                    "wallet: секрет %r — value и env задаются только вместе "
                    "(inject) либо оба отсутствуют (host-passthrough)", name)
                continue
            self._secrets[name] = Secret(
                name=str(name),
                value=str(raw.get("value", "")),
                env=str(raw.get("env", "")),
                description=str(raw.get("description", "")),
                sessions=tuple(str(p) for p in raw.get("sessions", ())),
                commands=tuple(str(p) for p in raw.get("commands", ())),
                deny=tuple(str(p) for p in raw.get("deny", ())),
                allow_unsafe=bool(raw.get("allow_unsafe", False)),
                confirm=bool(raw.get("confirm", True)),
            )
        return self._secrets


def _redact(data: bytes, values: list[str]) -> str:
    """Вывод команды → строка без значений секретов, обрезанная по лимиту.

    Заменяем значения ВСЕХ известных секретов (не только запрошенного):
    `gh auth status --show-token` и подобное не должно выносить соседний
    секрет. Длинные значения первыми — чтобы вложенные не оставляли хвостов.
    Редакция строго ДО обрезки: обрезка могла бы разрезать значение пополам.
    """
    text = data.decode("utf-8", errors="replace")
    for value in sorted((v for v in values if v), key=len, reverse=True):
        text = text.replace(value, REDACTED)
    if len(text) > STREAM_LIMIT:
        text = text[:STREAM_LIMIT] + "\n…(обрезано)"
    return text


class WalletModule:
    """Модуль ядра: aiohttp-демон на 127.0.0.1:<эфемерный порт> + токены сессий."""

    name = "wallet"

    def __init__(self, config: "Config"):
        self.config = config
        self.store = SecretStore(config.wallet_secrets_file)
        self.core = None
        self.port: int | None = None
        self._runner: web.AppRunner | None = None
        # Токен → имя сессии. Session-объект резолвим на каждый запрос через
        # manager.get: сессия могла быть удалена после выдачи токена.
        self._tokens: dict[str, str] = {}

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
            logger.warning(
                "wallet: файл секретов %s отсутствует — модуль работает, но секретов нет",
                self.config.wallet_secrets_file,
            )
        else:
            self.store.load()  # ранняя диагностика прав/синтаксиса — в лог на старте

        app = web.Application()
        app.router.add_get("/secrets", self._handle_secrets)
        app.router.add_post("/run", self._handle_run)
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

    async def stop(self) -> None:
        if self.core is not None:
            try:
                self.core.session_hooks.remove(self._provision)
            except ValueError:
                pass
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

    async def _handle_secrets(self, request: web.Request) -> web.Response:
        """Список секретов, разрешённых этой сессии, — БЕЗ значений."""
        session = self._auth(request)
        if session is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        out = [
            {
                "name": s.name,
                "description": s.description,
                "commands": list(s.effective_commands),
                "confirm": s.confirm,
                # host — команда идёт на хосте с его окружением (keyring/gh/git),
                # инъекции нет; inject — значение секрета кладётся в env-переменную
                # `env` дочернего процесса (модель ссылается на неё как $env).
                "mode": "host" if s.host_passthrough else "inject",
                "env": None if s.host_passthrough else s.env,
            }
            for s in self.store.load().values()
            if s.session_allowed(session.name)
        ]
        return web.json_response(out)

    async def _handle_run(self, request: web.Request) -> web.Response:
        session = self._auth(request)
        if session is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            name = str(body["secret"])
            cmd = [str(c) for c in body["cmd"]]
            if not cmd:
                raise ValueError("пустая команда")
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        cmd_str = " ".join(cmd)

        all_secrets = self.store.load()
        secret = all_secrets.get(name)
        # Причина отказа для прозрачности (что не так + как правильно). Порядок:
        #   1. встроенный guard (печать токена, git-RCE) — если включён глобально
        #      (WALLET_GUARD) и не снят на секрете (allow_unsafe);
        #   2. точечный deny секрета — поверх commands (deny побеждает allow).
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
            # Вердикт остаётся в ядре (кнопки ✅/❌ во всех адаптерах),
            # таймаут/ошибка = отказ.
            allowed = await self.core.request_confirmation(
                session,
                tool="wallet",
                description=f"{name} → {cmd_str[:200]}",
                preview=cmd_str,
            )

        # Наблюдаемость: КАЖДАЯ попытка видна — в том числе отказанная
        # (промпт-инъекция не пройдёт незамеченной). Бабл живёт только во время
        # активного хода (append дропается вне его), а wallet может вызываться
        # из фонового шелла между ходами — поэтому пишем И в бабл (когда есть),
        # И служебным уведомлением через notice (доходит всегда, во все
        # адаптеры), И в журнал.
        # Наблюдаемость. Успешный вызов виден строкой в статус-бабле (ходовом
        # ИЛИ фоновом — append_background сам разрулит; tool="wallet" схлопывает
        # серию одинаковых, поллинг не спамит). Отдельное top-level уведомление
        # шлём ТОЛЬКО на ОТКАЗ (нужно внимание, бабл может быть незаметен). Формат
        # notice — markdown (идёт через md_to_html); бабл — HTML напрямую.
        cmd_disp = f"{name} → {cmd_str[:120]}"
        bubble_line = f"🔐 <b>wallet</b> <code>{html.escape(cmd_disp)}</code>"
        await self.core.bubbles.append_background(
            session.name, bubble_line, tool="wallet"
        )
        self.core._record(session, "wallet", secret=name, cmd=cmd_str, allowed=bool(allowed))
        if not allowed:
            # Прозрачность: причина у ВСЕХ отказов. guard/deny заполнили reason
            # выше; policy-промах и отказ кнопкой — здесь. reason доходит до
            # терминала модели (bin/wallet печатает его), а не глухое «denied».
            if reason is None:
                if secret is None:
                    reason = f"нет секрета «{name}» для этой сессии (см. `wallet ls`)"
                elif not secret.session_allowed(session.name):
                    reason = "секрет не разрешён этой сессии (policy sessions)"
                elif not secret.command_allowed(cmd):
                    reason = "команда не в списке разрешённых (policy commands)"
                else:
                    reason = "отклонено кнопкой подтверждения"
            notice_md = f"🔐 wallet: `{cmd_disp.replace('`', chr(39))}`"  # md code-спан
            await self.core.notice(
                session,
                self.core.t("wallet_use", line=notice_md) + " — " + self.core.t("wallet_denied"),
            )
            return web.json_response(
                {"error": "denied", "reason": reason},
                status=403,
            )

        code, out, err = await self._execute(session, secret, cmd)
        values = [s.value for s in all_secrets.values()]
        return web.json_response(
            {"code": code, "stdout": _redact(out, values), "stderr": _redact(err, values)}
        )

    async def _execute(self, session, secret: Secret, cmd: list[str]) -> tuple[int, bytes, bytes]:
        """Запустить команду НА ХОСТЕ (вне песочницы).

        Суть дизайна: секрет/auth живёт только на хосте, никогда — в адресном
        пространстве песочницы. Два режима (см. Secret):
          * inject — секрет в env ребёнка (env=…, value=…);
          * host-passthrough — чистое хостовое окружение (keyring, gh/git auth):
            ничего не инжектим, глобальный git-конфиг НЕ обнуляем (в нём живёт
            gh credential helper).

        ⚠️ ОГРАНИЧЕНИЕ (docs/secrets-wallet.md): команда исполняется в cwd
        проекта, куда модель пишет из песочницы. Узкий шаблон НЕ гарантирует
        безопасность — модель может подложить `./.git/config` и получить
        исполнение на хосте. Барьер — policy (sessions/commands/confirm).
        """
        env = dict(os.environ)
        # Строго НЕинтерактивно: команда бежит без TTY (демон). Любой интерактив
        # (ssh host-verify/пароль, git credential-промпт) иначе всплывает
        # GUI-диалогом askpass (Ksshaskpass) на десктопе хоста — висит невидимо
        # для модели. Глушим GUI: пусть команда падает с понятной ошибкой в
        # stderr (модель увидит и починит/скажет оператору), а не подвешивает.
        env["SSH_ASKPASS_REQUIRE"] = "never"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.pop("DISPLAY", None)
        env.pop("SSH_ASKPASS", None)
        if not secret.host_passthrough:
            env[secret.env] = secret.value
            # Частичная защита git от подложенного локального конфига (только
            # inject-режим: у host-passthrough gh credential helper в глобальном
            # ~/.gitconfig, обнулять его нельзя — иначе push потеряет auth).
            env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
            env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.core.manager.effective_cwd(session),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # своя группа процессов — killpg по таймауту
            )
        except OSError as e:
            return 127, b"", str(e).encode()
        try:
            out, err = await asyncio.wait_for(proc.communicate(), RUN_TIMEOUT)
            return proc.returncode if proc.returncode is not None else 1, out, err
        except asyncio.TimeoutError:
            # Убиваем всю группу: сама команда могла наплодить детей.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                out, err = await proc.communicate()
            except Exception:
                out = err = b""
            return 124, out, err
