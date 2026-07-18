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


@dataclass(frozen=True)
class Secret:
    """Один секрет из secrets.toml вместе со своей policy."""

    name: str
    value: str
    env: str  # имя env-переменной, в которой секрет получит команда
    description: str
    sessions: tuple[str, ...]  # fnmatch-шаблоны имён сессий; пусто = никому
    commands: tuple[str, ...]  # fnmatch-шаблоны строки команды; пусто = ничего
    confirm: bool  # спрашивать ли подтверждение кнопками перед запуском

    def session_allowed(self, session_name: str) -> bool:
        return any(fnmatch.fnmatch(session_name, pat) for pat in self.sessions)

    def command_allowed(self, cmd_str: str) -> bool:
        return any(fnmatch.fnmatch(cmd_str, pat) for pat in self.commands)


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
            if not isinstance(raw, dict) or "value" not in raw or "env" not in raw:
                logger.error("wallet: секрет %r без value/env — пропущен", name)
                continue
            self._secrets[name] = Secret(
                name=str(name),
                value=str(raw["value"]),
                env=str(raw["env"]),
                description=str(raw.get("description", "")),
                sessions=tuple(str(p) for p in raw.get("sessions", ())),
                commands=tuple(str(p) for p in raw.get("commands", ())),
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
        if self.config.sandbox == "off":
            logger.warning(
                "wallet: SANDBOX=off — модель может прочитать %s напрямую, "
                "кошелёк в таком режиме бессмыслен", self.config.wallet_secrets_file,
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
        found: str | None = None
        for known, sname in self._tokens.items():
            if secrets.compare_digest(known, token):
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
                "commands": list(s.commands),
                "confirm": s.confirm,
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
        allowed = (
            secret is not None
            and secret.session_allowed(session.name)
            and secret.command_allowed(cmd_str)
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

        # Наблюдаемость: КАЖДАЯ попытка видна в статус-бабле и журнале —
        # в том числе отказанная (промпт-инъекция не пройдёт незамеченной).
        await self.core.bubbles.append(
            session.name,
            f"🔐 <b>wallet</b> <code>{html.escape(name + ' → ' + cmd_str[:120])}</code>",
        )
        self.core._record(session, "wallet", secret=name, cmd=cmd_str, allowed=bool(allowed))
        if not allowed:
            return web.json_response({"error": "denied"}, status=403)

        code, out, err = await self._execute(session, secret, cmd)
        values = [s.value for s in all_secrets.values()]
        return web.json_response(
            {"code": code, "stdout": _redact(out, values), "stderr": _redact(err, values)}
        )

    async def _execute(self, session, secret: Secret, cmd: list[str]) -> tuple[int, bytes, bytes]:
        """Запустить команду НА ХОСТЕ (вне песочницы) с секретом в env ребёнка.

        Это суть дизайна: секрет живёт только в env короткоживущего процесса
        на хосте и никогда — в адресном пространстве песочницы.
        """
        env = dict(os.environ)
        env[secret.env] = secret.value
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
