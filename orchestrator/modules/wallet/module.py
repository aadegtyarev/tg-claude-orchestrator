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

import asyncio
import fnmatch
import html
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import tomllib  # stdlib с 3.11
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from aiohttp import web

from .policy import PolicyEditor, PolicyError

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger(__name__)

# Таймаут выполнения самой команды под секретом. Бюджет CLI-обёртки
# (bin/wallet HTTP_TIMEOUT=660с) должен покрывать confirm (request_confirmation
# по умолчанию 300с) + RUN_TIMEOUT + накладные: 300+300 < 660. Меняешь одно —
# держи неравенство, иначе CLI отвалится по таймауту, пока команда ещё бежит.
RUN_TIMEOUT = 300.0
# Потолок каждого потока вывода — защита от заливки памяти/чата гигабайтами.
STREAM_LIMIT = 200_000
# Чем заменяем значения секретов в выводе.
REDACTED = "•••"
# Маркер секрета для скрытых inject-секретов. В env песочницы вместо реального
# значения кладётся `<<wallet:имя>>`; модель пишет привычный `$ENV`, шелл
# разворачивает его в маркер, а демон подставляет РЕАЛЬНОЕ значение на хосте
# (в аргумент — inline, либо `:file` — во временный 0600-файл). Значение в
# песочницу/контекст модели не попадает; из вывода редактируется.
MARKER_RE = re.compile(r"<<wallet:([A-Za-z0-9_-]+)(:file)?>>")


def marker(name: str, as_file: bool = False) -> str:
    return f"<<wallet:{name}{':file' if as_file else ''}>>"
# Дефолтный набор для host-passthrough, когда `commands` не задан: инструменты,
# которые обычно уже авторизованы на хосте и не отдают сам секрет наружу. Смысл
# кошелька — «используй, но не читай»: gh/git/ssh/scp применяют креды сами,
# echo/cat/sh их бы просто распечатали, поэтому в дефолт НЕ входят. Хочешь
# curl/kubectl/своё — допиши commands явно.
DEFAULT_HOST_COMMANDS = ("gh", "git", "ssh", "scp")
# Каталог per-session обёрток внутри приватного дома сессии; ставится ПЕРВЫМ в
# PATH песочницы (SessionManager.path_hooks), поэтому завёрнутые инструменты
# побеждают настоящие бинари. Имя знает и ядро (session_home) — держим синхронно.
SHIM_DIRNAME = ".wallet-bin"
# Сетевые подкоманды git заворачиваем в кошелёк (креды хоста); всё остальное
# (status/add/commit/log/diff) бежит настоящим git прямо в песочнице — быстро и
# без хостового раунд-трипа. Список намеренно узкий: только то, что ходит в сеть.
GIT_NETWORK = ("push", "fetch", "pull", "clone", "ls-remote", "send-pack", "fetch-pack")
# Дефолтный secrets.toml — создаётся при первом запуске, если файла ещё нет, чтобы
# кошелёк работал «из коробки»: прокол на хост (host-passthrough) для gh/git/ssh/
# scp на все сессии, обёртки в PATH заворачивают их сами. Права строго 0600.
DEFAULT_SECRETS_TOML = """\
# Кошелёк секретов claude-orchestrator — создан автоматически при первом запуске.
# Формат, режимы и policy: docs/secrets-wallet.md. Права строго 0600 (иначе файл
# НЕ загрузится). Правится из бота командой /wallet или руками здесь.
#
# Дефолт ниже — «прокол на хост» (host-passthrough) для gh/git/ssh/scp: команды
# идут на ХОСТ с его кредами (keyring, gh-auth, ~/.ssh), модель их значений не
# видит. Обёртки в PATH заворачивают эти инструменты сами — модель зовёт gh/git/
# ssh как обычно. Встроенный guard рубит опасное (печать токена, git-RCE) и здесь.

[secrets.host]
description = "хостовые креды gh/git/ssh/scp"
sessions = ["*"]                          # все сессии; сузь при желании: ["dev-*"]
commands = ["gh", "git", "ssh", "scp"]    # эти инструменты завернутся обёртками
confirm = false                           # без кнопок подтверждения; guard — щит
"""


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
    # 1. Печатают сам секрет — редакция literal-only их не всегда ловит. Смотрим
    # ПЕРВУЮ не-флаговую подкоманду (чтобы `gh --флаг auth token` не проскочил, но
    # `gh pr create --title "auth token"` не ложно-сработал: там первая подкоманда
    # «pr», а не «auth»).
    if binary == "gh":
        subs = [a for a in cmd[1:] if not a.startswith("-")]
        if subs[:1] == ["auth"] and (subs[1:2] == ["token"] or "--show-token" in cmd):
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
    # shared: секрет, значение которого модель ДОЛЖНА получить (dev-ключ для её
    # сервиса, логин/пароль для ввода в браузер). Не про конфиденциальность от
    # модели — про хранение вне чата/репо. Выдаётся `wallet get`/`wallet env`;
    # при заданном `env` реальное значение сразу лежит в env песочницы (в отличие
    # от inject, где там маркер). host/inject значения НЕ выдаются никогда.
    shared: bool

    @property
    def host_passthrough(self) -> bool:
        return not (self.value and self.env)

    @property
    def mode(self) -> str:
        if self.shared:
            return "shared"
        return "host" if self.host_passthrough else "inject"

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
            is_shared = bool(raw.get("shared", False))
            # shared-секрет требует value (env опционален — для env-выдачи);
            # inject — ОБА поля; host-passthrough — НИ ОДНОГО. Иначе ошибка.
            if is_shared:
                if not has_value:
                    logger.error("wallet: shared-секрет %r без value — пропущен", name)
                    continue
            elif has_value != has_env:
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
                shared=is_shared,
            )
        return self._secrets


def _redact(data: bytes, values: list[str]) -> str:
    """Вывод команды → строка без значений секретов, обрезанная по лимиту.

    Заменяем значения ВСЕХ известных секретов (не только запрошенного):
    `gh auth status --show-token` и подобное не должно выносить соседний
    секрет. Длинные значения первыми — чтобы вложенные не оставляли хвостов.
    Редакция строго ДО обрезки: обрезка могла бы разрезать значение пополам.

    ВАЖНО: это ГИГИЕНА против СЛУЧАЙНОГО эха, НЕ барьер. Замена только точных
    вхождений — любая трансформация (base64/hex/reverse, запись в файл, вывод
    по буквам) проходит мимо. Настоящая защита от утечки — host-passthrough
    (значения секрета вообще нет в пространстве модели) + не давать шелл в
    `commands` секрета. См. модель угроз в docs/REVIEW-2026-07-19.md §1.
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
        нет. Длинные первыми — вложенные не оставляют хвостов."""
        for v in sorted(
            (s.value for s in self.store.load().values() if s.value),
            key=len, reverse=True,
        ):
            if v in text:
                text = text.replace(v, REDACTED)
        return text

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

    async def stop(self) -> None:
        if self.core is not None:
            try:
                self.core.session_hooks.remove(self._provision)
            except ValueError:
                pass
            try:
                self.core.manager.path_hooks.remove(self.session_path)
            except ValueError:
                pass
            try:
                self.core.manager.env_hooks.remove(self.session_env)
            except ValueError:
                pass
            try:
                self.core.output_redactors.remove(self.redact_output)
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

    async def _handle_get(self, request: web.Request) -> web.Response:
        """Выдать сессии ЗНАЧЕНИЕ shared-секрета (dev-ключ, логин/пароль).

        Только для shared — host/inject значения не выдаются НИКОГДА (в этом их
        смысл). shared — про хранение вне чата/репо, не про сокрытие от модели."""
        session = self._auth(request)
        if session is None:
            return web.json_response({"error": "unauthorized"}, status=401)
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

    async def _handle_run(self, request: web.Request) -> web.Response:
        """Явный вызов: `wallet run <name> -- <cmd>` — секрет задан именем."""
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
        return await self._run_secret(session, self.store.load().get(name), cmd, name)

    async def _handle_exec(self, request: web.Request) -> web.Response:
        """Прозрачный шлюз: `wallet exec <cmd>` (зовут обёртки в PATH песочницы)
        — секрет подбирается ПО КОМАНДЕ (чей `commands` её разрешает). Модель
        зовёт инструмент как обычно."""
        session = self._auth(request)
        if session is None:
            return web.json_response({"error": "unauthorized"}, status=401)
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

        tmpdir: str | None = None
        # Основной секрет-inject → реальное значение в env (env-читающие
        # инструменты, gh->GH_TOKEN, получают его на хосте).
        if not secret.host_passthrough:
            env[secret.env] = secret.value
            # Частичная защита git от подложенного локального конфига (inject).
            env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
            env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
        # Развернуть маркеры <<wallet:имя>> / <<wallet:имя:file>> в аргументах в
        # РЕАЛЬНОЕ значение на хосте (curl-заголовок, ssh-ключ): модель писала
        # $ENV → шелл развернул в маркер → тут подставляем значение. Файл — во
        # временный 0600 на хосте (песочнице невидим, tmpfs /tmp), сносится в
        # finally. Маркер неизвестного/недоступного секрета → пусто (не течём).
        all_secrets = self.store.load()

        def _sub(arg: str) -> str:
            nonlocal tmpdir

            def repl(m: "re.Match") -> str:
                nonlocal tmpdir
                s = all_secrets.get(m.group(1))
                if s is None or not s.session_allowed(session.name) or not s.value:
                    return ""
                if not m.group(2):
                    return s.value
                if tmpdir is None:
                    tmpdir = tempfile.mkdtemp(prefix="wallet-")
                path = os.path.join(tmpdir, m.group(1))
                if not os.path.exists(path):
                    data = s.value if s.value.endswith("\n") else s.value + "\n"
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(data)
                return path

            return MARKER_RE.sub(repl, arg)

        cmd = [_sub(a) for a in cmd]

        try:
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
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
