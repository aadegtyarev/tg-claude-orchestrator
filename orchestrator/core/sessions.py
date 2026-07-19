"""Управление сессиями Claude Code: запуск, останов, мониторинг процессов.

Схема одной сессии:

    claude (CLI, интерактивный, под PTY) ──спавнит по .mcp.json──> channel_server.py
    launcher ──HTTP POST /notify──> channel_server ──JSON-RPC push──> claude

channel_server запускается ТОЛЬКО самим Claude Code (через .mcp.json) —
иначе два процесса дерутся за один порт.

Все операции жизненного цикла одной сессии (create/close/resume/clear/
set_model) сериализованы её локом `Session.ops` — параллельные команды
не могут запустить два процесса или осиротить один из них.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import shutil
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Awaitable, Callable

import aiohttp

from . import hookscript
from . import transcript
from .ansi import strip_ansi
from .proctree import proc_tree_signals
from .slug import slugify  # реэкспорт: адаптеры и тесты ждут sessions.slugify
from .. import runners as runner_mod
from ..config import Config

logger = logging.getLogger(__name__)

# Каталог пакета orchestrator/ (channel_server.py) и корень репозитория
# (.venv, RO-бинд песочницы) — этот модуль лежит в orchestrator/core/.
PKG_DIR = Path(__file__).resolve().parent.parent
ROOT = PKG_DIR.parent

# Сколько ждать, пока Claude стартует и поднимет channel-сервер.
READY_TIMEOUT = 60.0
# Пауза после resume: «claude --resume» без транскрипта умирает не сразу,
# а через несколько секунд после старта.
RESUME_GRACE = 5.0


class SessionError(Exception):
    """Ошибка создания/работы сессией — текст показывается пользователю."""


# Стартовые диалоги интерактивного claude и клавиши-ответы.
# Маркеры ищутся в тексте экрана без пробелов и в нижном регистре.
_DIALOGS = [
    ("trustthisfolder", b"\r"),        # «Yes, I trust this folder» — пункт по умолчанию
    ("bypasspermissions", b"2\r"),     # «Yes, I accept» — пункт 2
    ("localdevelopment", b"\r"),       # dev-channels: «I am using this for local development»
]


def _pty_driver(master: int, log_file: IO[bytes], name: str) -> None:
    """Поток при PTY: дренирует вывод claude (иначе буфер pty переполнится
    и процесс встанет), пишет его в лог и отвечает на стартовые диалоги.

    Поток владеет master-fd и сам закрывает его на выходе — закрытие из
    event loop могло бы освободить номер fd, пока поток блокирован в read.
    """
    buf = b""
    answered: set[str] = set()
    try:
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            try:
                log_file.write(chunk)
                log_file.flush()
            except ValueError:  # лог уже закрыт при остановке сессии
                pass
            buf = (buf + chunk)[-16384:]
            screen = strip_ansi(buf).replace(b" ", b"")
            screen_text = screen.decode(errors="replace").lower()
            for marker, keys in _DIALOGS:
                if marker in screen_text and marker not in answered:
                    answered.add(marker)
                    logger.info("Сессия %s: отвечаю на диалог '%s'", name, marker)
                    for key in keys:
                        try:
                            os.write(master, bytes([key]))
                        except OSError:
                            return
                        time.sleep(0.3)
                    buf = b""
                    break
    finally:
        try:
            os.close(master)
        except OSError:
            pass


@dataclass
class Session:
    name: str  # slug: первичный ключ, папка, MCP-ключ, хук — только [A-Za-z0-9_-]
    port: int
    session_dir: Path
    claude_session_id: str
    title: str = ""  # отображаемое имя (топик, сообщения); по умолчанию = name
    # Адреса сессии в транспорт-адаптерах: имя адаптера -> непрозрачная строка
    # (Telegram: id форум-топика). Заполняется ядром через Transport.bind_session.
    bindings: dict[str, str] = field(default_factory=dict)
    linked_path: str | None = None
    model: str | None = None  # None = модель Claude Code по умолчанию
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    process: asyncio.subprocess.Process | None = None
    pty_master: int | None = None
    log_file: IO[bytes] | None = None
    watcher: asyncio.Task | None = None
    # Сериализация операций жизненного цикла этой сессии.
    ops: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        if not self.title:
            self.title = self.name

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None


class SessionManager:
    """Хранилище активных сессий + жизненный цикл процессов Claude."""

    def __init__(self, config: Config):
        self.config = config
        self._lock = asyncio.Lock()  # защищает _by_name и выдачу портов
        self._by_name: dict[str, Session] = {}
        # Порты, выданные стартующим сессиям, но ещё не занятые их channel-
        # сервером (окно гонки при фиксированном пуле) — см. _find_free_port.
        self._inflight_ports: set[int] = set()
        # Базовый CPU-отсчёт дерева процессов для вотчдога (см. is_busy).
        self._cpu: dict[str, int] = {}
        # Общий HTTP-пул к channel-серверам (keep-alive, без сессии на запрос —
        # REVIEW.md E1). Ленивый: создаётся в event loop при первом обращении.
        self._http: aiohttp.ClientSession | None = None
        # Вызывается при внезапной смерти Claude (session, exit_code);
        # назначается в launcher.
        self.on_dead: Callable[[Session, int], Awaitable[None]] | None = None

    def _http_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._http

    def _channel_headers(self) -> dict[str, str]:
        """Auth на эндпоинты channel-сервера (/notify /permission /ping) —
        симметрично ORCH_TOKEN на стороне оркестратора: без него локальный
        процесс мог бы POST /notify и вбросить промпт в Claude или POST
        /permission behavior=allow и авто-разрешить запрос."""
        return {"Authorization": f"Bearer {self.config.orch_token}"}

    # ── состояние на диске ──────────────────────────────────────
    # Записи переживают /close_session и рестарт launcher'а: топик остаётся,
    # сессия возобновляется по первому сообщению (resume).

    def save_state(self) -> None:
        items = [
            {
                "name": s.name,
                "bindings": s.bindings,
                "cwd": str(s.session_dir),
                "port": s.port,
                "claude_session_id": s.claude_session_id,
                "title": s.title,
                "linked_path": s.linked_path,
                "model": s.model,
            }
            for s in self._by_name.values()
        ]
        state_file = self.config.sessions_dir / ".sessions.json"
        tmp = state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False))
        os.replace(tmp, state_file)  # атомарно: битого файла не бывает

    def load_state(self) -> None:
        state_file = self.config.sessions_dir / ".sessions.json"
        if not state_file.exists():
            return
        try:
            items = json.loads(state_file.read_text())
        except (OSError, ValueError) as e:
            logger.error("Не удалось прочитать %s: %s", state_file, e)
            return
        for item in items:
            # Миграция старого формата (до мульти-адаптеров): thread_id топика
            # Telegram становится binding'ом telegram-адаптера.
            bindings = dict(item.get("bindings") or {})
            if "thread_id" in item and "telegram" not in bindings:
                bindings["telegram"] = str(item["thread_id"])
            session = Session(
                name=item["name"],
                bindings=bindings,
                port=item.get("port", 0),
                session_dir=Path(item["cwd"]),
                claude_session_id=item["claude_session_id"],
                title=item.get("title", ""),
                linked_path=item.get("linked_path"),
                model=item.get("model"),
            )
            self._by_name[session.name] = session
            logger.info("Восстановлена запись сессии %s (остановлена)", session.name)

    # ── чтение (без блокировки: единственный поток event loop) ──

    def get(self, name: str) -> Session | None:
        return self._by_name.get(name)

    # Синоним для явности в местах, где имя приходит извне (хуки, URL).
    get_by_name = get

    def list_all(self) -> list[Session]:
        return list(self._by_name.values())

    def count(self) -> int:
        return len(self._by_name)

    def has_name(self, name: str) -> bool:
        return name in self._by_name

    def get_by_binding(self, adapter: str, address: str) -> Session | None:
        """Сессия по адресу в адаптере (Telegram: id топика)."""
        return next(
            (s for s in self._by_name.values() if s.bindings.get(adapter) == address),
            None,
        )

    # ── создание ────────────────────────────────────────────────

    async def create(self, title: str, project_path: str | None = None) -> Session:
        slug = slugify(title)
        session = Session(
            name=slug,
            port=0,
            session_dir=self.config.sessions_dir / slug,
            claude_session_id=str(uuid.uuid4()),
            title=title,
        )
        async with session.ops:
            # Проверки и регистрация — под общим локом (два /new подряд
            # не создадут дубль имени и не превысят лимит).
            async with self._lock:
                if self.has_name(slug):
                    raise SessionError(f"Сессия «{slug}» уже существует.")
                if len(self._by_name) >= self.config.max_instances:
                    raise SessionError(
                        f"Достигнут лимит сессий ({self.config.max_instances})."
                    )
                port = self._find_free_port()
                if port is None:
                    raise SessionError("Нет свободных портов для channel-сервера.")
                session.port = port
                self._by_name[slug] = session

            try:
                session.session_dir.mkdir(parents=True, exist_ok=True)
                if project_path:
                    session.linked_path = self._link_project(project_path)
                self._guard_unique_cwd(session)
                self._write_mcp_json(session)
                self._write_claude_settings(session)
                await self._start_claude(session)
                await self._wait_ready(session)
            except Exception:
                await self._terminate(session)
                self._inflight_ports.discard(session.port)
                async with self._lock:
                    self._by_name.pop(slug, None)
                self.save_state()
                raise

            self._start_watcher(session)
            self.save_state()
            return session

    def _guard_unique_cwd(self, session: Session) -> None:
        """Раннеры с unique_cwd (agent-vm: имя VM = hash(cwd)) не допускают
        двух сессий на один рабочий каталог — вторая молча убила бы VM первой."""
        if not getattr(self.runner, "unique_cwd", False):
            return
        cwd = self.effective_cwd(session)
        clash = next(
            (
                s for s in self._by_name.values()
                if s is not session and self.effective_cwd(s) == cwd
            ),
            None,
        )
        if clash is not None:
            raise SessionError(
                f"Раннер «{self.runner.name}» допускает одну сессию на каталог: "
                f"{cwd} уже занят сессией «{clash.name}»."
            )

    def _find_free_port(self) -> int | None:
        lo, hi = self.config.channel_port_start, self.config.channel_port_end
        # Авто-режим (пул не задан): ОС выдаёт свободный порт на localhost.
        if lo <= 0 or hi <= 0:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            self._inflight_ports.add(port)
            return port
        # Фиксированный пул: порт остановленной сессии уже свободен (процесс
        # убит) — учитываем только работающие, иначе resume ложно упадёт.
        # Плюс _inflight_ports: порты сессий, которые СЕЙЧАС стартуют, но ещё
        # не подняли channel-сервер (в этом окне они не running и порт не занят
        # реально) — иначе конкурентный /new выдал бы тот же порт, и сообщения
        # одной сессии ушли бы в channel-сервер другой.
        used = {sess.port for sess in self._by_name.values() if sess.running}
        used |= self._inflight_ports
        for port in range(lo, hi + 1):
            if port in used:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                except OSError:
                    continue
            self._inflight_ports.add(port)  # снимется в _start_watcher/_stop_process
            return port
        return None

    @staticmethod
    def _link_project(project_path: str) -> str:
        """Рабочая директория проекта: claude запускается прямо в ней
        (натуральный cwd — грузит CLAUDE.md/.mcp.json/.claude проекта).
        Несуществующая директория создаётся автоматически.
        """
        real_path = Path(project_path).expanduser().resolve()
        if real_path.is_file():
            raise SessionError(f"Это файл, а не директория: {project_path}")
        real_path.mkdir(parents=True, exist_ok=True)
        return str(real_path)

    def _write_mcp_json(self, session: Session) -> None:
        """Конфиг, по которому Claude Code сам запустит channel_server.py.

        Интерпретатор — sys.executable (venv), иначе channel_server
        не найдёт свои зависимости.
        """
        mcp = {
            "mcpServers": {
                f"channel-{session.name}": {
                    "command": sys.executable,
                    "args": [str(PKG_DIR / "channel_server.py")],
                    "env": {
                        "CHANNEL_PORT": str(session.port),
                        "SESSION_NAME": session.name,
                        "ORCH_HOST": self.config.orch_host,
                        "ORCH_PORT": str(self.config.orch_port),
                        "ORCH_TOKEN": self.config.orch_token,
                    },
                }
            }
        }
        (session.session_dir / ".mcp.json").write_text(json.dumps(mcp, indent=2))

    def _write_claude_settings(self, session: Session) -> None:
        """Настройки бота для headless-запуска. Грузятся через --settings и
        мержатся с профилем (CLAUDE_CONFIG_DIR) и проектом — не заменяют их.

        - enableAllProjectMcpServers: авто-апрув MCP из .mcp.json проекта,
          чтоб твои серверы стартовали без диалога «New MCP server found»
          (на канал из --mcp-config не влияет — он доверенный и так).
          Ставим ТОЛЬКО без linked_path: иначе /new к чужому проекту запустит
          его .mcp.json command без consent → RCE (REVIEW.md S3). Свой cwd
          (папка сессии) проектных .mcp.json не содержит, так что там флаг
          безвреден и сохраняет прежнее поведение;
        - permissions.allow для канал-тулов: только в небайпасных режимах,
          иначе Claude спросит разрешение на каждый ответ в Telegram;
        - PreToolUse-хук (если show_tool_calls): вызовы тулов → POST
          /event/<имя> → статус-бабл;
        - Stop-хук (всегда): конец хода → POST /stop/<имя> с
          last_assistant_message → фолбэк, если ход завершился голым текстом
          вместо reply_to_user (REVIEW: 9/9 длинных ходов в живой сессии
          теряли финал именно так — см. core/app.py handle_stop_event).
          Оба события ловит один скрипт-диспетчер (hookscript.py), никогда не
          блокирует Claude (except+exit 0).
        """
        settings: dict = {}
        settings_dir = session.session_dir / ".claude"
        settings_dir.mkdir(exist_ok=True)
        if session.linked_path is None:
            settings["enableAllProjectMcpServers"] = True
        deny = ["AskUserQuestion"]  # интерактивный вопрос-меню виснет без TUI;
        # Claude получит «tool not allowed» и переспросит через reply_to_user.

        # Анти-утечка секретов имеет смысл ТОЛЬКО в связке кошелёк + песочница —
        # это единая схема, звенья не работают порознь:
        #   • песочница прячет сырые креды хоста (~/.ssh, ~/.config/gh, keyring);
        #   • кошелёк даёт контролируемый доступ к ним (команда на хосте);
        #   • эти deny/autoMode-правила блокируют попытки достать секрет в обход.
        # Без песочницы модель видит все хостовые креды напрямую — правила были
        # бы театром; без кошелька нет и легитимного доступа, который защищаем.
        wallet_active = (
            "wallet" in self.config.modules and self.config.sandbox == "bwrap"
        )
        if wallet_active:
            # Чтение кред-файлов/keyring — жёсткий deny (работает во ВСЕХ режимах,
            # включая bypass). `env`/`printenv` глобально НЕ режем (ломает легит
            # `env VAR=x cmd`) — это отдаём судье auto-режима, он умнее glob.
            deny += [
                "Read(~/.config/gh/**)",
                "Read(~/.netrc)",
                "Read(~/.ssh/**)",
                "Read(~/.aws/**)",
                "Read(~/.git-credentials)",
                "Bash(security find-generic-password*)",
                "Bash(pass show*)",
            ]
        perms: dict = {"deny": deny}
        if self.config.permission_mode != "bypass":
            perms["allow"] = [
                f"mcp__channel-{session.name}__reply_to_user",
                f"mcp__channel-{session.name}__send_file_to_user",
            ]
        settings["permissions"] = perms

        # Судья auto-режима — отдельный классификатор (в 2.1.2x по умолчанию
        # Sonnet 5). Его ПРОМПТ не кастомизируется, но кастомизируются ПРАВИЛА,
        # которые он читает. Дополняем их (только в auto + при активном кошельке):
        # жёстко запретить добычу секретов и проверять ВСЕ bash-команды.
        if wallet_active and self.config.permission_mode == "auto":
            settings["autoMode"] = {
                "environment": [
                    "$defaults",
                    "Секреты держатся вне досягаемости модели (кошелёк/keyring): "
                    "~/.config/gh, ~/.netrc, ~/.ssh, ~/.aws и env-переменные с токенами.",
                ],
                "hard_deny": [
                    "$defaults",
                    "Никогда не добывай значения секретов: не дампи переменные "
                    "окружения (env, printenv, set) ради токенов; не читай кред-сторы "
                    "(~/.config/gh, ~/.netrc, ~/.ssh, ~/.aws, keyring); не кодируй "
                    "(base64/hex/reverse) и не пересылай секреты наружу. Для git/gh "
                    "используй кошелёк: wallet run <секрет> -- <команда>.",
                ],
                "classifyAllShell": self.config.automode_classify_all_shell,
            }

        # Токен уходит в 0600-скрипт (см. hookscript.py), команда хука = только
        # путь к интерпретатору и скрипту — ничего секретного в
        # /proc/<pid>/cmdline и в самом settings.local.json не остаётся.
        hook_script = settings_dir / "hook_dispatch.py"
        hook_script.write_text(
            hookscript.render(self.config.orch_port, session.name, self.config.orch_token)
        )
        os.chmod(hook_script, 0o600)
        hook_cmd = {"type": "command", "command": f'"{sys.executable}" "{hook_script}"'}
        hooks: dict = {"Stop": [{"hooks": [hook_cmd]}]}  # Stop не поддерживает matcher
        if self.config.show_tool_calls:
            hooks["PreToolUse"] = [{"matcher": "", "hooks": [hook_cmd]}]
            # Завершение вызова (bash: ✓/✗ + время) и сабагента; тот же
            # диспетчер — ядро роутит по hook_event_name (handle_tool_event).
            hooks["PostToolUse"] = [{"matcher": "", "hooks": [hook_cmd]}]
            hooks["SubagentStop"] = [{"matcher": "", "hooks": [hook_cmd]}]
        settings["hooks"] = hooks
        (settings_dir / "settings.local.json").write_text(json.dumps(settings, indent=2))

    async def _start_claude(self, session: Session, resume: bool = False) -> None:
        """Запустить интерактивный claude под PTY.

        Headless-запуск не работает: без TTY claude сваливается в --print,
        а в -p/stream-json режиме channel-события не запускают ход (проверено
        вживую). Интерактивная сессия под PTY — документированный сценарий
        «persistent terminal»: пуш в канал сам будит Claude.
        """
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        # CLI-обвязка оркестратора (bin/wallet и т.п.): репозиторий RO-виден
        # и в песочнице, поэтому PATH работает и там.
        env["PATH"] = f"{ROOT / 'bin'}:{env.get('PATH', '')}"
        if self.config.claude_config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.config.claude_config_dir)
        # Явные переменные для Claude Code (CLAUDE_ENV_ANTHROPIC_BASE_URL=…
        # и т.п.). Сами CLAUDE_ENV_* в дочерний процесс не тащим.
        for key in [k for k in env if k.startswith("CLAUDE_ENV_")]:
            del env[key]
        env.update(self.config.claude_env)

        # --session-id обязан быть UUID; --resume продолжает прежний диалог.
        session_arg = (
            ["--resume", session.claude_session_id]
            if resume
            else [f"--session-id={session.claude_session_id}"]
        )
        # Модель: /model на сессию → DEFAULT_MODEL из .env → дефолт Claude.
        # Синонимы (opus/sonnet/haiku/…) и полные имена мапит сам Claude Code.
        model = session.model or self.config.default_model
        if model:
            session_arg += ["--model", model]
        # Effort по умолчанию из .env (low/medium/high/xhigh/max).
        if self.config.default_effort:
            session_arg += ["--effort", self.config.default_effort]
        # Режим разрешений: bypass — без ограничений; остальные режимы
        # спрашивают, запросы прилетают в Telegram (permission relay).
        if self.config.permission_mode == "bypass":
            session_arg += ["--dangerously-skip-permissions"]
        else:
            session_arg += ["--permission-mode", self.config.permission_mode]

        self._rotate_log(session.session_dir / "claude.log")
        session.log_file = open(session.session_dir / "claude.log", "ab")
        master, slave = pty.openpty()
        try:
            # cwd = папка проекта (если задан линк): натуральное поведение —
            # Claude грузит CLAUDE.md/.mcp.json/.claude проекта. Канал-сервер
            # и настройки бота подсасываем флагами ниже (consent не просят).
            cwd = str(self.effective_cwd(session))
            # Подсказка: под bwrap $HOME процесса подменён приватным домом
            # сессии, поэтому реальный ~/.venv и глобальные инструменты не видны —
            # окружение проекта держи В ПРОЕКТЕ (он смонтирован RW). Персистентный
            # дом (.homes/<имя>) переживает рестарты, если агент ставит в ~.
            if self.config.sandbox == "bwrap" and session.linked_path:
                logger.info(
                    "Сессия %s: под bwrap $HOME изолирован (реальный ~/.venv не "
                    "виден) — держи окружение в проекте %s (RW) или в ~ сессии "
                    "(персистентный дом)", session.name, session.linked_path,
                )
            extra: list[str] = []
            mcp_json = session.session_dir / ".mcp.json"
            if mcp_json.exists():
                extra += ["--mcp-config", str(mcp_json)]
            settings_file = session.session_dir / ".claude" / "settings.local.json"
            if settings_file.exists():
                extra += ["--settings", str(settings_file)]
            # dev-записи каналов передаются --dangerously-load-development-channels
            # (server:<имя>; определено в .mcp.json из --mcp-config).
            # Изоляция — через раннер (runner.py): при bwrap claude и все его
            # дети (channel_server, хуки, Bash-тул) заперты в mount-namespace —
            # видны только папка сессии, папка проекта и конфиг Claude Code.
            extra_rw = [session.session_dir, Path(cwd)]
            argv = self.runner.wrap(
                [
                    self.config.claude_bin,
                    *session_arg,
                    *extra,
                    "--dangerously-load-development-channels",
                    f"server:channel-{session.name}",
                ],
                chdir=Path(cwd),
                extra_rw=extra_rw,
                home_dir=self.session_home(session),
                publish_ports=[session.port],
            )
            session.process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
        except Exception:
            os.close(master)
            self._close_log(session)
            raise
        finally:
            os.close(slave)
        session.pty_master = master
        threading.Thread(
            target=_pty_driver,
            args=(master, session.log_file, session.name),
            name=f"pty-{session.name}",
            daemon=True,
        ).start()

    def _rotate_log(self, path: Path) -> None:
        """Если лог перерос лимит — сдвинуть в .old (одна копия), начать заново."""
        cap = int(self.config.log_max_mb * 1024 * 1024)
        try:
            if cap > 0 and path.exists() and path.stat().st_size > cap:
                os.replace(path, path.with_suffix(".log.old"))
        except OSError as e:
            logger.warning("Ротация лога %s не удалась: %s", path, e)

    async def _wait_ready(self, session: Session) -> None:
        """Готовность = channel-сервер отвечает на /ping (его поднял Claude)."""
        deadline = asyncio.get_running_loop().time() + READY_TIMEOUT
        http = self._http_session()
        ping_timeout = aiohttp.ClientTimeout(total=2)
        while True:
            proc = session.process
            if proc is None or proc.returncode is not None:
                code = proc.returncode if proc else "?"
                raise SessionError(
                    f"Claude завершился при старте (код {code}). "
                    f"Лог: {session.session_dir / 'claude.log'}"
                )
            try:
                async with http.get(
                    f"http://127.0.0.1:{session.port}/ping",
                    timeout=ping_timeout,
                    headers=self._channel_headers(),
                ) as resp:
                    if resp.status == 200:
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            if asyncio.get_running_loop().time() > deadline:
                # Частая причина при обновлении Claude Code: channels —
                # research preview, флаги/протокол могут поменяться, и тогда
                # claude не спавнит channel_server или не отвечает handshake.
                raise SessionError(
                    f"Claude не поднял channel-сервер за {READY_TIMEOUT:.0f} с. "
                    "Возможно, обновилась версия Claude Code и изменился протокол "
                    "каналов (research preview) — проверь лог и совместимость. "
                    f"Лог: {session.session_dir / 'claude.log'}"
                )
            await asyncio.sleep(1)

    # ── жизненный цикл: close / resume / clear / set_model ─────

    async def close(self, session: Session) -> None:
        """Остановить процесс, сохранив запись: топик живёт, resume возможен."""
        async with session.ops:
            await self._stop_process(session)
            self.save_state()

    async def delete(self, session: Session) -> None:
        """Полностью удалить сессию (процесс + запись + приватный дом)."""
        async with session.ops:
            async with self._lock:
                self._by_name.pop(session.name, None)
                self._cpu.pop(session.name, None)
            await self._stop_process(session)
            # Приватный дом песочницы: без удаления /new с тем же slug
            # унаследовал бы прежний $HOME (ключи, ~/.wallet.json, ~/.bashrc-
            # foothold) и каталоги копились бы вечно.
            home = self.config.sessions_dir / ".homes" / session.name
            if home.is_dir():
                await asyncio.to_thread(shutil.rmtree, home, ignore_errors=True)
            self.save_state()

    async def resume(self, session: Session) -> bool:
        """Оживить остановленную сессию (после /close_session или рестарта).

        Сначала пробуем `claude --resume` (продолжить диалог); если резюмировать
        нечего — чистый старт с новым UUID. Возвращает True, если контекст
        удалось продолжить.
        """
        async with session.ops:
            if session.running:
                return True
            return await self._resume_locked(session)

    async def _resume_locked(self, session: Session) -> bool:
        # Раннеры с unique_cwd (agent-vm): гвард нужен и на resume/clear, не
        # только на create — иначе восстановленные из .sessions.json две сессии
        # на один cwd убьют VM друг друга при первом сообщении.
        self._guard_unique_cwd(session)
        async with self._lock:
            port = self._find_free_port()
            if port is None:
                raise SessionError("Нет свободных портов для channel-сервера.")
            session.port = port
        self._write_mcp_json(session)
        self._write_claude_settings(session)

        resumed = True
        await self._start_claude(session, resume=True)
        try:
            await self._wait_ready(session)
            # --resume без сохранённого транскрипта умирает не сразу,
            # а через несколько секунд после старта — даём ему проявиться.
            await asyncio.sleep(RESUME_GRACE)
            if not session.running:
                raise SessionError("resume-процесс умер после старта")
        except SessionError:
            # Транскрипта нет или resume не поддержался — чистый старт.
            logger.warning("Сессия %s: resume не удался, чистый старт", session.name)
            resumed = False
            await self._stop_process(session, save=False)
            await self._wait_port_free(session.port)
            session.claude_session_id = str(uuid.uuid4())
            try:
                await self._start_claude(session)
                await self._wait_ready(session)
            except Exception:
                # Не оставляем процесс-зомби без watcher'а.
                await self._stop_process(session, save=False)
                self.save_state()
                raise

        session.started_at = time.time()
        self._start_watcher(session)
        self.save_state()
        return resumed

    async def clear(self, session: Session) -> None:
        """Перезапустить Claude с чистым контекстом: та же папка, тот же топик."""
        async with session.ops:
            await self._stop_process(session, save=False)
            await self._wait_port_free(session.port)
            session.claude_session_id = str(uuid.uuid4())
            async with self._lock:
                port = self._find_free_port()
                if port is None:
                    raise SessionError("Нет свободных портов для channel-сервера.")
                session.port = port
            self._write_mcp_json(session)
            self._write_claude_settings(session)
            try:
                await self._start_claude(session)
                await self._wait_ready(session)
            except Exception:
                await self._stop_process(session, save=False)
                self.save_state()
                raise
            session.started_at = time.time()
            self._start_watcher(session)
            self.save_state()

    async def set_model(self, session: Session, model: str) -> bool:
        """Сменить модель: перезапуск с --model, контекст — через resume.

        Возвращает True, если контекст удалось продолжить. При ошибке
        (например, несуществующая модель) откатывает модель обратно.
        """
        old_model = session.model
        try:
            async with session.ops:
                session.model = model or None
                if session.running:
                    await self._stop_process(session, save=False)
                return await self._resume_locked(session)
        except Exception:
            session.model = old_model
            self.save_state()
            raise

    async def shutdown(self) -> None:
        """Остановка launcher'а: убить процессы, записи сохранить."""
        for session in self.list_all():
            await self._stop_process(session, save=False)
        self.save_state()
        if self._http is not None and not self._http.closed:
            await self._http.close()

    # ── внутренняя механика процессов ───────────────────────────

    def _start_watcher(self, session: Session) -> None:
        # Сессия поднялась и стабильна: её порт теперь реально держит живой
        # channel-сервер (учитывается через running), резерв «в полёте» снят.
        self._inflight_ports.discard(session.port)
        session.watcher = asyncio.create_task(
            self._watch(session), name=f"watch-{session.name}"
        )

    async def _watch(self, session: Session) -> None:
        """Ждёт завершения Claude; при внезапной смерти помечает остановленной."""
        proc = session.process
        assert proc is not None
        code = await proc.wait()
        if session.process is not proc:
            return  # процесс уже заменён resume/clear — не наш клиент
        if self._by_name.get(session.name) is not session:
            return  # уже удалена
        session.process = None
        session.watcher = None
        self._close_log(session)
        self.save_state()
        logger.warning("Сессия %s: Claude неожиданно завершился (код %s)", session.name, code)
        if self.on_dead is not None:
            # Колбэк (уведомление в Telegram) не должен ронять watcher-таск:
            # исключение здесь otherwise убило бы задачу молча (REVIEW.md B3).
            try:
                await self.on_dead(session, code)
            except Exception:
                logger.exception("on_dead для сессии %s — колбэк упал", session.name)

    async def _stop_process(self, session: Session, save: bool = True) -> None:
        """Погасить процесс сессии: watcher, группа процессов, лог."""
        if session.watcher is not None:
            session.watcher.cancel()
            session.watcher = None
        await self._terminate(session)
        session.process = None
        # Старт мог упасть до _start_watcher — снимаем резерв порта и здесь,
        # иначе он утёк бы из _inflight_ports навсегда.
        self._inflight_ports.discard(session.port)
        if save:
            self.save_state()

    @staticmethod
    async def _terminate(session: Session) -> None:
        # Убиваем всю группу процессов (start_new_session=True): иначе
        # channel_server переживает claude, держит порт и отвечает на /ping.
        proc = session.process
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, 15)  # SIGTERM группе
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, 9)  # SIGKILL
                    await proc.wait()
            except (ProcessLookupError, PermissionError):
                pass
        SessionManager._close_log(session)

    @staticmethod
    async def _wait_port_free(port: int, timeout: float = 10.0) -> None:
        """Подождать, пока старый channel-сервер отпустит порт."""
        if port <= 0:
            return
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return
                except OSError:
                    pass
            await asyncio.sleep(0.3)
        logger.warning("Порт %d не освободился за %.0f с", port, timeout)

    @staticmethod
    def _close_log(session: Session) -> None:
        if session.log_file is not None and not session.log_file.closed:
            session.log_file.close()

    # ── связь с работающей сессией ──────────────────────────────

    async def send_to_claude(self, session: Session, text: str, context_id: str) -> None:
        session.last_activity = time.time()
        http = self._http_session()
        async with http.post(
            f"http://127.0.0.1:{session.port}/notify",
            json={"content": text, "context_id": context_id},
            headers=self._channel_headers(),
        ) as resp:
            resp.raise_for_status()

    def touch(self, session: Session) -> None:
        """Отметить активность (ответ Claude) — сброс таймера простоя."""
        session.last_activity = time.time()

    def tail_log(self, session: Session, lines: int = 15) -> str:
        """Последние строки claude.log без ANSI — для диагностики в чате."""
        path = session.session_dir / "claude.log"
        try:
            raw = path.read_bytes()[-16384:]
        except OSError:
            return ""
        clean = strip_ansi(raw)
        text = clean.decode(errors="replace")
        tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
        return "\n".join(tail)

    def is_busy(self, session: Session) -> bool:
        """Делает ли сессия работу прямо сейчас — признак жизни для вотчдога.

        Жив, если CPU-время дерева процессов claude (он сам + запущенные тулы)
        выросло с прошлой проверки ИЛИ у него есть живые дочерние процессы
        (идёт тул — Bash/сборка и т.п.). Если /proc недоступен — считаем
        живым: лучше пропустить редкое реальное зависание, чем спамить ложным.

        Вызывать из единственного места (_watchdog_loop): метод хранит
        предыдущий отсчёт CPU по имени сессии между вызовами.
        """
        pid = session.process.pid if session.running and session.process else None
        if pid is None:
            return False
        try:
            cpu, has_kids = proc_tree_signals(pid)
        except Exception:  # /proc недоступен — перестраховочно «жив»
            logger.debug("is_busy: /proc недоступен для pid=%s", pid)
            return True
        prev = self._cpu.get(session.name)
        self._cpu[session.name] = cpu
        grew = prev is not None and cpu > prev
        return grew or has_kids

    def has_children(self, session: Session) -> bool:
        """Есть ли живые дочерние процессы (идёт тул) — для кнопки ⏭. В отличие
        от is_busy НЕ трогает CPU-baseline: is_busy обязан вызываться только из
        _watchdog_loop (хранит прошлый отсчёт), а кнопка дёргается из каждого
        _flush бабла (~1.5с) и сбивала бы 15-секундную CPU-дельту вотчдога."""
        pid = session.process.pid if session.running and session.process else None
        if pid is None:
            return False
        try:
            _cpu, has_kids = proc_tree_signals(pid)
        except Exception:
            return True  # /proc недоступен — перестраховочно «есть работа»
        return has_kids

    async def run_and_capture(self, session: Session, cmd: str, wait: float = 6.0) -> str:
        """Ввести слэш-команду в PTY и вернуть новый вывод claude.log без ANSI.

        Для команд Claude Code (/cost, /context…), чей вывод — TUI-перерисовка.
        """
        log = session.session_dir / "claude.log"
        before = log.stat().st_size if log.exists() else 0
        self.type_into_pty(session, cmd)
        await asyncio.sleep(wait)

        def _read_delta() -> str:
            # Читаем приращение через seek, а не весь файл в память (лог под
            # LOG_MAX_MB — десятки МБ); в потоке, чтобы не стопорить event loop.
            try:
                with open(log, "rb") as fh:
                    fh.seek(before)
                    raw = fh.read()
            except OSError:
                return ""
            return strip_ansi(raw).decode(errors="replace")

        return await asyncio.to_thread(_read_delta)

    async def close_idle(self) -> list[Session]:
        """Остановить работающие сессии, простаивавшие дольше IDLE_TIMEOUT_H.

        Возвращает список остановленных (для уведомления в чат).
        """
        timeout = self.config.idle_timeout_h * 3600
        if timeout <= 0:
            return []
        now = time.time()
        stale = [
            s for s in self.list_all()
            if s.running and now - s.last_activity > timeout
        ]
        for session in stale:
            logger.info("Сессия %s простаивала — авто-останов", session.name)
            await self.close(session)
        return stale

    async def send_permission(self, session: Session, request_id: str, behavior: str) -> None:
        """Вердикт по запросу разрешения — обратно в channel_server."""
        http = self._http_session()
        async with http.post(
            f"http://127.0.0.1:{session.port}/permission",
            json={"request_id": request_id, "behavior": behavior},
            headers=self._channel_headers(),
        ) as resp:
            resp.raise_for_status()

    def _send_raw(self, session: Session, data: bytes) -> None:
        """Записать сырые байты (управляющие коды) прямо в PTY claude."""
        if session.pty_master is None or not session.running:
            raise SessionError("Сессия не запущена.")
        try:
            os.write(session.pty_master, data)
        except OSError as e:
            raise SessionError(f"Терминал сессии недоступен: {e}") from e

    def interrupt_turn(self, session: Session) -> None:
        """Жёстко прервать текущий ход: Esc (\\x1b) в PTY-терминал Claude.

        В channels-протоколе прерывания нет, но интерактивный claude живёт под
        нашим PTY — байт \\x1b эквивалентен нажатию Esc в TUI и обрывает ход
        немедленно (в отличие от «мягкого стопа» push-сообщением, которое
        модель прочитает только когда доберётся). Контекст сессии сохраняется.
        """
        self._send_raw(session, b"\x1b")

    def background_turn(self, session: Session) -> None:
        """Отправить текущую задачу в фон: Ctrl+B (\\x02) в PTY.

        В TUI Claude Code Ctrl+B переводит долгую задачу (bash-команду) в фон —
        ход продолжается, не блокируясь на ней. Эквивалент нажатия Ctrl+B в
        терминале сессии."""
        self._send_raw(session, b"\x02")

    def type_into_pty(self, session: Session, text: str) -> None:
        """Напечатать команду прямо в терминал Claude (слэш-команды CC)."""
        if session.pty_master is None or not session.running:
            raise SessionError("Сессия не запущена.")
        # Только печатные символы одной строки — никаких управляющих кодов.
        clean = "".join(ch for ch in text if ch.isprintable())
        # PTY может принять не весь буфер за раз — дописываем хвост, иначе
        # длинная слэш-команда обрежется.
        data = clean.encode() + b"\r"
        try:
            while data:
                n = os.write(session.pty_master, data)
                data = data[n:]
        except OSError as e:
            raise SessionError(f"Терминал сессии недоступен: {e}") from e

    # ── статистика по транскрипту ───────────────────────────────

    def effective_cwd(self, session: Session) -> Path:
        """Реальный cwd процесса claude: папка проекта, если задан линк,
        иначе папка сессии. Натуральное поведение «cd в проект и claude» —
        Claude грузит CLAUDE.md/.mcp.json/.claude из проекта.

        Эту же строку используем для кодирования пути транскрипта (см.
        transcript_path) — Claude хранит его по cwd, должны совпадать.
        """
        if session.linked_path:
            try:
                return Path(session.linked_path).resolve()
            except OSError:
                return session.session_dir
        return session.session_dir

    @property
    def runner(self) -> runner_mod.Runner:
        """Раннер процессов (bwrap | direct) — ленивый, см. runner.py."""
        r = getattr(self, "_runner", None)
        if r is None:
            r = runner_mod.make_runner(self.config, ROOT)
            self._runner = r
        return r

    def session_home(self, session: Session) -> Path:
        """Персистентный приватный $HOME сессии для песочницы.

        Живёт в SESSIONS_DIR/.homes/<имя> (вне папки сессии, чтобы не
        светиться в её RW-бинде вторым путём): venv/кэши, которые агент
        кладёт «к себе домой», переживают рестарты — в отличие от прежнего
        tmpfs. Каталог создаётся здесь же (раннеру нужен существующий путь).
        """
        home = self.config.sessions_dir / ".homes" / session.name
        home.mkdir(parents=True, exist_ok=True)
        return home

    def sandbox_prefix(
        self, chdir: Path, extra_rw: list[Path], session: Session | None = None
    ) -> list[str]:
        """Префикс argv для запуска команды в изоляции текущего раннера.

        Пусто при SANDBOX=off. Политика allowlist — в runners.bwrap.
        session задана — команда получает тот же персистентный $HOME, что и
        claude этой сессии (/bash видит venv, который агент себе поставил).
        """
        home_dir = self.session_home(session) if session is not None else None
        return self.runner.wrap([], chdir=chdir, extra_rw=extra_rw, home_dir=home_dir)

    def transcript_path(self, session: Session) -> Path:
        """Транскрипт сессии в профиле Claude Code (см. transcript.py)."""
        config_dir = self.config.claude_config_dir or Path.home() / ".claude"
        return transcript.transcript_path(
            config_dir, self.effective_cwd(session), session.claude_session_id
        )

    def read_stats(self, session: Session) -> dict | None:
        """Статистика из транскрипта (None — ещё не создан). Блокирующее
        чтение — вызывать через asyncio.to_thread. Логика — transcript.py."""
        return transcript.read_stats(self.transcript_path(session))

    def read_last_model(self, session: Session) -> str | None:
        """Реальная модель последнего ответа (после подмены прокси) — дёшево
        из хвоста транскрипта. Логика — transcript.read_last_model."""
        return transcript.read_last_model(self.transcript_path(session))

    def read_pollution_excerpt(self, session: Session, max_entries: int = 25) -> str | None:
        """Эксцепт загрязнения чужим бэкендом из хвоста транскрипта (или None).
        Блокирующее чтение — вызывать через asyncio.to_thread. Логика —
        transcript.py."""
        return transcript.read_pollution_excerpt(self.transcript_path(session), max_entries)
