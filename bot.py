"""Telegram-бот: команды, пересылка сообщений в Claude и ответов обратно.

Прогресс работы Claude показывается статус-баблом: одно сообщение в топике,
куда дописываются вызовы инструментов (🔧, из PreToolUse-хука), сабагенты (🤖)
и промежуточные ответы (💬, reply с complete=false). Финальный ответ
(complete=true) приходит обычным сообщением; бабл удаляется (DELETE_BUBBLE).

Запросы разрешений (permission relay) приходят в топик кнопками ✅/❌.
Все тексты — в texts.py (BOT_LANG=ru|en).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

from bashshell import BashShellManager, clean as bash_clean
from bubble import BubbleManager
from config import Config
from sessions import Session, SessionError, SessionManager, slugify
from texts import get_texts

logger = logging.getLogger(__name__)

TG_MESSAGE_LIMIT = 4096

# Обрезка одной строки бабла / промежуточного ответа.
LINE_LIMIT = 100
# Лимит превью команды в запросе разрешения: показываем почти полностью,
# чтобы можно было прочитать и принять осмысленное решение. 4096 (лимит ТГ)
# минус шапка/описание; остаток маркируем, если урезали.
PERM_PREVIEW_LIMIT = 3500

# Индикатор «печатает…»: Telegram гасит его через ~5 с, обновляем чаще.
TYPING_INTERVAL = 4.0

# Сторож зависаний: если после отправки Claude молчит и claude.log не растёт
# STALL_CHECKS проверок подряд — предупреждаем в топик (рост лога = живой
# ход/размышление, отсутствие роста = завис).
WATCHDOG_GRACE = 20.0
WATCHDOG_CHECK = 15.0
# Ретранслятор ошибок API из claude.log: как часто нюхать хвост, как часто
# писать в чат (между любыми двумя алертами), и сколько глушить ОДНУ И ТУ ЖЕ
# ошибку — чтобы 10-минутная петля 400-х не родила 10 одинаковых сообщений.
ERROR_RELAY_INTERVAL = 6.0
ERROR_RELAY_COOLDOWN = 60.0
ERROR_RELAY_REPEAT = 600.0
# Живой сигнал ретраев: пере-файр при росте attempt, но не чаще раза в N сек.
RETRY_SURFACE_INTERVAL = 30.0

# /bash: как часто перечитывать вывод и перерисовывать статус-сообщение,
# сколько ждать команду и сколько текста показывать (лимит Telegram — 4096,
# запас под шапку/подвал/HTML-экранирование).
BASH_POLL_INTERVAL = 1.5
BASH_TIMEOUT = 600.0
BASH_OUTPUT_LIMIT = 3500
STALL_CHECKS = 2

# Окно контекста для процента в /stats. Захардкожено грубо: у моделей с
# 1M-окном цифра будет занижать реальный запас — это ориентир, не факт.
CONTEXT_WINDOW = 200_000

# Синонимы моделей для кнопок /model. Маппинг на конкретные версии делает
# сам Claude Code — мы не дублируем его каталог и не отстаём от переименований.
MODEL_ALIASES = ["fable", "opus", "sonnet", "haiku"]

# Иконки инструментов для статус-бабла.
TOOL_ICONS = {
    "Bash": "⚡", "Read": "📖", "Write": "✍️", "Edit": "✏️",
    "NotebookEdit": "✏️", "Grep": "🔍", "Glob": "🗂", "WebFetch": "🌐",
    "WebSearch": "🔎", "Task": "🤖", "TodoWrite": "📝",
}
# Из какого поля брать деталь и показывать ли её как имя файла (basename).
# TaskCreate/TaskUpdate — тему/статус вместо сырого JSON-объекта целиком.
_TOOL_DETAIL = {
    "Bash": ("command", False), "Read": ("file_path", True),
    "Write": ("file_path", True), "Edit": ("file_path", True),
    "NotebookEdit": ("notebook_path", True), "Grep": ("pattern", False),
    "Glob": ("pattern", False), "WebFetch": ("url", False),
    "WebSearch": ("query", False),
    "TaskCreate": ("subject", False), "TaskUpdate": ("status", False),
}

# Поля сторонних/незнакомых инструментов, из которых тянем осмысленную деталь —
# по порядку предпочтения (имя файла/путь важнее «описания»).
_MEANINGFUL_FIELDS = (
    "file_path", "path", "notebook_path", "pattern", "query",
    "url", "command", "subject", "description", "prompt", "text",
)

# Дробим bash-команду по конвейерам и связкам; «словесный» аргумент
# (подкоманду: log/test/status) отличаем от флагов и путей.
_BASH_SEP = re.compile(r"\s*(?:&&|\|\||\||;)\s*")
_WORD_ARG = re.compile(r"^[A-Za-z][\w:-]*$")


def _first_meaningful(tool_input: dict) -> str:
    """Первый осмысленный строковый аргумент стороннего инструмента.

    Сырой JSON в бабле не показываем — он не помещается в строку и шумит.
    """
    for key in _MEANINGFUL_FIELDS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _file_suffix(tool: str, tool_input: dict) -> str:
    """Короткий контекст к имени файла: какие строки читаем/сколько правим.

    Read c offset/limit → « L10–34»; Edit → « ±N строк»; замена по всему файлу
    → « ×N». Ничего осмысленного нет — пустая строка.
    """
    if tool == "Read":
        try:
            off = int(tool_input.get("offset"))
        except (TypeError, ValueError):
            return ""
        try:
            lim = int(tool_input.get("limit"))
            return f" L{off}–{off + lim}"
        except (TypeError, ValueError):
            return f" L{off}+"
    if tool in ("Edit", "NotebookEdit"):
        old = str(tool_input.get("old_string") or "")
        if tool_input.get("replace_all"):
            return " (все)"
        n = old.count("\n") + 1 if old else 0
        return f" ±{n} стр" if n else ""
    return ""


def _bash_head(command: str) -> str:
    """Короткая «голова» bash-команды: куда cd + имя команды (+ подкоманда).

    Ведущий `cd …` раньше выкидывали, но каталог работы — полезный контекст
    (какой ворктREE/папка), поэтому показываем его basename. Флаги и длинные
    аргументы отбрасываем: строчка должна помещаться в бабел в одну строку.
    """
    cmd = command.strip()
    cd_dest = ""
    head = ""
    for segment in _BASH_SEP.split(cmd):
        segment = segment.strip()
        if not segment:
            continue
        if segment.startswith("cd "):
            dest = segment[3:].strip().strip("\"'")
            cd_dest = Path(dest).name or dest
            continue
        head = segment
        break
    if not head:
        return cd_dest or cmd  # вся команда — это cd
    parts = head.split()
    main = parts[0] if parts else ""
    sub = parts[1] if len(parts) > 1 else ""
    if sub and not _WORD_ARG.match(sub):
        sub = ""  # флаг (-n), путь (/x) или кавычка (") — не интересно
    body = f"{main} {sub}".strip()
    return f"{cd_dest} · {body}" if cd_dest else body


def split_text(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    """Разбить текст под лимит Telegram, по возможности по переводу строки."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", limit // 2, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


# ── markdown → Telegram HTML ───────────────────────────────────
# Telegram рендерит ограниченный HTML: b/i/s/code/pre/a. Превращаем в него
# разметку из ответов Claude, остальное оставляем как есть. Небезопасные
# символы экранируем. Если итог бракованный — _send откатывается на plain.
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_CODE_INLINE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
# _italic_ с.word-границами — чтобы не калечить snake_case (my_var_name).
_ITALIC_RE = re.compile(r"(?<![\w*])_(?!\s)(.+?)(?<!\s)_(?![\w*])", re.DOTALL)
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")

# Очистка claude.log от ANSI и детектор ошибок API Клода (для ретранслятора).
#
# Триггер — ТОЛЬКО настоящий баннер TUI «API Error: <код> <детали>». Клод и сам
# охотно пишет слова «rate-limit»/«api error» в ответах (диагностика чужой
# сессии, описание самой этой фичи), и прежний широкий греп ловил эту прозу как
# ложный алерт о лимите. Баннер с кодом модель дословно не цитирует, поэтому он
# — надёжный сигнал. group(1)=код, group(2)=хвост строки с деталями (для класса).
_LOG_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]")
_API_ERR_BANNER_RE = re.compile(rb"API Error:\s*(\d{3})\b([^\n]{0,140})", re.IGNORECASE)
# Класс ошибки по деталям баннера (код разбираем отдельно).
_RL_DETAIL_RE = re.compile(rb"rate[\s_-]?limit|overloaded|\bcapacity\b", re.IGNORECASE)
_PROTO_DETAIL_RE = re.compile(rb"server_tool_use|tool_result|messages\.\d", re.IGNORECASE)
# Живые сигналы из claude.log (когда тулов нет, но что-то происходит):
#  • ретрай API-ошибки — Claude Code пишет «Retrying … attempt K/M»;
#  • краш-рестарт — баннер «Resume this session with» / «Welcome back».
_RETRY_RE = re.compile(rb"attempt\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_RESTART_RE = re.compile(rb"Resume this session with|Welcome back", re.IGNORECASE)


def _classify_api_error(code: bytes, detail: bytes) -> str:
    """Класс ошибки API для ретранслятора: ratelimit | protocol | generic.

    ratelimit — 429/529/overloaded: транзитно, помогает смена модели.
    protocol — 400 с кривым server_tool_use/tool_result: апстрим (z.ai) шлёт
              несогласованный блок, модель тут ни при чём — /clear или /close_session.
    generic  — прочее (5xx и т.п.).
    """
    if code in (b"429", b"529") or _RL_DETAIL_RE.search(detail):
        return "ratelimit"
    if code == b"400" and _PROTO_DETAIL_RE.search(detail):
        return "protocol"
    return "generic"


def _detect_log_signals(chunk: bytes) -> dict:
    """Разобрать кусок claude.log на три класса сигналов (для ретранслятора).

    Возвращает {api_error, retry, restarts}:
      • api_error — (code, klass) баннера «API Error: <код>» либо None;
      • retry — (attempt, total) из «attempt K/M», последний в куске, либо None;
      • restarts — сколько баннеров рестарта («Resume this session»/«Welcome back»).
    Чистая функция: только разбор байтов — тестируется без петли/Telegram.
    """
    out: dict = {"api_error": None, "retry": None, "restarts": 0}
    m = _API_ERR_BANNER_RE.search(chunk)
    if m:
        out["api_error"] = (m.group(1), _classify_api_error(m.group(1), m.group(2)))
    rm = _RETRY_RE.search(chunk)
    if rm:
        out["retry"] = (int(rm.group(1)), int(rm.group(2)))
    out["restarts"] = len(_RESTART_RE.findall(chunk))
    return out


def md_to_html(text: str) -> str:
    """Светлый markdown → HTML Telegram. Код выносится первым (внутри нет
    разметки), затем экранируется остальное, затем разметка."""
    stash: list[str] = []

    def _keep(html_body: str, tag: str) -> str:
        stash.append(f"<{tag}>{html_body}</{tag}>")
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(lambda m: _keep(html.escape(m.group(1)), "pre"), text)
    text = _CODE_INLINE_RE.sub(lambda m: _keep(html.escape(m.group(1)), "code"), text)
    text = html.escape(text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)


class TelegramBot:
    def __init__(self, config: Config, manager: SessionManager):
        self.config = config
        self.manager = manager
        self.bot = Bot(token=config.telegram_bot_token)
        self.dp = Dispatcher()
        self.chat_id = config.telegram_chat_id
        self._texts = get_texts(config.bot_lang)
        self.bubbles = BubbleManager(
            self.bot, lambda: self.chat_id, self.t, config.delete_bubble
        )
        # thread_id -> задача, шлющая «печатает…» пока Claude обрабатывает запрос.
        self._typing: dict[int, asyncio.Task] = {}
        # thread_id -> сторож зависаний (стартует/гаснет вместе с typing).
        self._watchdogs: dict[int, asyncio.Task] = {}
        # thread_id -> ретранслятор ошибок API (rate-limit/5xx) из claude.log.
        self._error_relays: dict[int, asyncio.Task] = {}
        # /bash — постоянный терминал на топик, в обход Claude Code.
        self.bash = BashShellManager()
        self._register_handlers()

    def t(self, key: str, **kwargs) -> str:
        return self._texts[key].format(**kwargs)

    async def start_polling(self) -> None:
        # Меню команд в интерфейсе Telegram (кнопка «/» у поля ввода).
        commands = [
            BotCommand(command="new", description=self.t("menu_new")),
            BotCommand(command="list", description=self.t("menu_list")),
            BotCommand(command="ls", description=self.t("menu_ls")),
            BotCommand(command="stats", description=self.t("menu_stats")),
            BotCommand(command="usage", description=self.t("menu_usage")),
            BotCommand(command="model", description=self.t("menu_model")),
            BotCommand(command="skills", description=self.t("menu_skills")),
            BotCommand(command="compact", description=self.t("menu_compact")),
            BotCommand(command="clear", description=self.t("menu_clear")),
            BotCommand(command="close_session", description=self.t("menu_close")),
            BotCommand(command="delete_session", description=self.t("menu_delete")),
            BotCommand(command="bash", description=self.t("menu_bash")),
            BotCommand(command="bashin", description=self.t("menu_bashin")),
            BotCommand(command="chat_id", description=self.t("menu_chat_id")),
            BotCommand(command="help", description=self.t("menu_help")),
        ]
        # Бот работает в группах — меню «/» там берёт команды из группового
        # scope; без него список часто пуст. Ставим и default, и для групп.
        # SHOW_COMMAND_MENU=false — скрываем меню целиком (в группе Telegram
        # всё равно печатает «/команда@бот», что не всем нравится).
        if self.config.show_command_menu:
            await self.bot.set_my_commands(commands)
            await self.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
        else:
            try:
                await self.bot.delete_my_commands()
            except Exception as e:
                logger.warning("Не удалось очистить меню команд: %s", e)
        await self.dp.start_polling(self.bot)

    async def close(self) -> None:
        await self.bot.session.close()

    # ── регистрация и доступ ────────────────────────────────────

    def _register_handlers(self) -> None:
        dp = self.dp
        dp.message.register(self.cmd_help, Command("start", "help"))
        dp.message.register(self.cmd_new, Command("new"))
        dp.message.register(self.cmd_list, Command("list"))
        dp.message.register(self.cmd_ls, Command("ls"))
        dp.message.register(self.cmd_close, Command("close_session", "stop"))
        dp.message.register(self.cmd_delete, Command("delete_session"))
        dp.message.register(self.cmd_compact, Command("compact"))
        dp.message.register(self.cmd_clear, Command("clear"))
        dp.message.register(self.cmd_stats, Command("stats"))
        dp.message.register(self.cmd_usage, Command("usage", "cost"))
        dp.message.register(self.cmd_model, Command("model"))
        dp.message.register(self.cmd_skills, Command("skills"))
        dp.message.register(self.cmd_chat_id, Command("chat_id"))
        dp.message.register(self.cmd_bash, Command("bash"))
        dp.message.register(self.cmd_bashin, Command("bashin"))
        dp.message.register(self.on_text, F.text & ~F.text.startswith("/"))
        dp.message.register(self.on_file, F.photo | F.document)
        # Последним: неизвестные /команды уходят в терминал Claude.
        dp.message.register(self.on_slash, F.text.startswith("/"))
        dp.callback_query.register(self.on_stop_button, F.data.startswith("stop:"))
        dp.callback_query.register(self.on_model_button, F.data.startswith("model:"))
        dp.callback_query.register(self.on_session_button, F.data.startswith("sess:"))
        dp.callback_query.register(self.on_perm_button, F.data.startswith("perm:"))
        dp.callback_query.register(self.on_delete_button, F.data.startswith("del:"))

    def _accept(self, message: Message) -> bool:
        """Доступ строго по ALLOWED_USER_IDS + привязка к одной группе.

        Пустой список = игнорировать всех. Чужих игнорируем молча
        (не выдаём существование бота).
        """
        if message.chat.type not in ("group", "supergroup"):
            return False
        if not self._user_allowed(message.from_user):
            return False
        if self.chat_id is None:
            self.chat_id = message.chat.id
            logger.info(
                "Чат привязан: %s (зафиксируй TELEGRAM_CHAT_ID=%s в .env)",
                message.chat.id, message.chat.id,
            )
        return message.chat.id == self.chat_id

    def _user_allowed(self, user) -> bool:
        if user is None or user.id not in self.config.allowed_user_ids:
            logger.warning("Отказ в доступе: %s", user)
            return False
        return True

    # ── команды основного чата ──────────────────────────────────

    async def cmd_chat_id(self, message: Message) -> None:
        """Показать ID чата и привязать бота, если он ещё не привязан.

        Намеренно НЕ через _accept: должна работать и в чужой группе —
        иначе ID новой группы не узнать. Белый список пользователей действует.
        """
        if message.chat.type not in ("group", "supergroup"):
            return
        if not self._user_allowed(message.from_user):
            return
        chat_id = message.chat.id
        if self.chat_id is None:
            self.chat_id = chat_id
            logger.info("Чат привязан через /chat_id: %s", chat_id)
            text = self.t("chat_id_bound_now", id=chat_id)
        elif self.chat_id == chat_id:
            text = self.t("chat_id_current", id=chat_id)
        else:
            text = self.t("chat_id_other", id=chat_id, bound=self.chat_id)
        await message.reply(text, parse_mode="HTML")

    async def cmd_help(self, message: Message) -> None:
        if not self._accept(message):
            return
        await message.reply(self.t("help"), parse_mode="HTML")

    async def cmd_new(self, message: Message, command: CommandObject) -> None:
        if not self._accept(message):
            return
        if message.message_thread_id is not None:
            await message.reply(self.t("only_main_chat"))
            return

        title, project_path = self._parse_new_args(command.args or "")
        if not title:
            await message.reply(self.t("new_usage"))
            return
        title = title[:128]  # предел названия топика в Telegram
        slug = slugify(title)  # непустой: транслит + автослаг для экзотики
        if self.manager.has_name(slug):
            await message.reply(self.t("name_exists", name=slug))
            return
        if self.manager.count() >= self.config.max_instances:
            await message.reply(self.t("limit_reached", limit=self.config.max_instances))
            return

        status = await message.reply(self.t("creating"))
        try:
            topic = await self.bot.create_forum_topic(chat_id=self.chat_id, name=title)
        except Exception as e:
            # Нет права «Manage Topics» или в группе выключены темы.
            logger.error("Не удалось создать топик для %s: %s", title, e)
            await status.edit_text(self.t("create_fail", error=e))
            return
        try:
            await self.manager.create(title, topic.message_thread_id, project_path)
        except Exception as e:
            try:
                await self.bot.delete_forum_topic(
                    chat_id=self.chat_id, message_thread_id=topic.message_thread_id
                )
            except Exception:
                pass
            logger.exception("Ошибка создания сессии %s", title)
            await status.edit_text(self.t("create_fail", error=e))
            return
        await status.edit_text(self.t("created", name=title))

    @staticmethod
    def _parse_new_args(raw: str) -> tuple[str, str | None]:
        """Разобрать аргументы /new → (отображаемое имя, путь-или-None).

        Поддерживает: имя с пробелами, обрамляющие кавычки, форму
        `/new /path`, форму `имя /path` (путь = токен, начинающийся с /).
        """
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
            raw = raw[1:-1].strip()
        if not raw:
            return "", None
        # Путь = токен, начинающийся с / или ~ (домашняя папка).
        is_path = lambda tok: tok.startswith("/") or tok.startswith("~")
        if is_path(raw):
            return Path(raw).name, raw
        tokens = raw.split()
        path_idx = next((i for i, tok in enumerate(tokens) if is_path(tok)), None)
        if path_idx is not None:
            project_path = " ".join(tokens[path_idx:])
            title = " ".join(tokens[:path_idx]) or Path(project_path).name
            return title, project_path
        return raw, None

    async def cmd_list(self, message: Message) -> None:
        if not self._accept(message):
            return
        sessions = self.manager.list_all()
        if not sessions:
            await message.reply(self.t("list_empty"))
            return
        lines, rows = [], []
        for s in sessions:
            if not s.running:
                status = self.t("st_stopped")
            elif self.bubbles.has(s.thread_id):
                status = self.t("st_working")
            else:
                status = self.t("st_waiting")
            line = f"{status} — {s.title}"
            if s.model:
                line += f" [{s.model}]"
            if s.running:
                uptime = self._fmt_duration(time.time() - s.started_at)
                line += ", " + self.t("uptime", uptime=uptime)
            if s.linked_path:
                line += f"\n     📁 {s.linked_path}"
            lines.append(line)
            row = [InlineKeyboardButton(
                text=f"📊 {s.title}", callback_data=f"sess:stats:{s.thread_id}")]
            if s.running:
                row.append(InlineKeyboardButton(
                    text="⏸", callback_data=f"sess:close:{s.thread_id}"))
            rows.append(row)
        await message.reply(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def cmd_ls(self, message: Message, command: CommandObject) -> None:
        if not self._accept(message):
            return
        target = self.config.sessions_dir
        if command.args:
            target = Path(command.args.strip()).expanduser().resolve()

        if not target.exists():
            await message.reply(self.t("ls_not_exists", path=target))
            return
        if not target.is_dir():
            await message.reply(self.t("ls_file", path=target))
            return
        try:
            entries = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as e:
            await message.reply(self.t("ls_no_access", error=e))
            return

        lines = [f"📁 {target}"]
        if not entries:
            lines.append(self.t("ls_empty"))
        for entry in entries[:30]:
            icon = "📁" if entry.is_dir() else "📄"
            lines.append(f"{icon} {entry.name}{'/' if entry.is_dir() else ''}")
        if len(entries) > 30:
            lines.append(self.t("ls_more", n=len(entries) - 30))
        await message.reply("\n".join(lines))

    async def cmd_skills(self, message: Message) -> None:
        """Список скиллов из профиля Claude Code (глобальные + плагины)."""
        if not self._accept(message):
            return
        skills = await asyncio.to_thread(self._collect_skills)
        if not skills:
            await message.reply(self.t("skills_none"))
            return
        lines = [self.t("skills_header", n=len(skills))]
        for name, desc in skills:
            lines.append(f"• {name}" + (f" — {desc}" if desc else ""))
        for chunk in split_text("\n".join(lines)):
            await message.reply(chunk)

    def _collect_skills(self) -> list[tuple[str, str]]:
        config_dir = self.config.claude_config_dir or Path.home() / ".claude"
        skill_files: list[Path] = []
        skill_files += sorted((config_dir / "skills").glob("*/SKILL.md"))
        plugins = config_dir / "plugins"
        if plugins.is_dir():
            skill_files += sorted(plugins.glob("**/skills/*/SKILL.md"))
        result, seen = [], set()
        for path in skill_files:
            name, desc = path.parent.name, ""
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            for line in text.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    desc = " ".join(line.split(":", 1)[1].split())[:120]
            if name not in seen:
                seen.add(name)
                result.append((name, desc))
        return result

    # ── команды топика ──────────────────────────────────────────

    def _topic_session(self, message: Message) -> Session | None:
        return self.manager.get(message.message_thread_id or 0)

    def _bash_cwd(self, message: Message) -> Path:
        """Стартовый cwd терминала: папка проекта сессии, иначе sessions_dir.

        Дальше по /bash можно `cd` куда угодно — оболочка постоянная,
        cwd между вызовами сохраняется (это отдельный процесс, не Claude Code).
        """
        session = self._topic_session(message)
        if session is not None:
            return self.manager.effective_cwd(session)
        return self.config.sessions_dir

    async def cmd_bash(self, message: Message, command: CommandObject) -> None:
        """Выполнить команду в постоянном bash-терминале топика, мимо Claude.

        Права и окружение — как у процесса бота. Стримит вывод в одно
        редактируемое сообщение (троттлинг ~1.5с), в конце — код возврата.
        Если во время выполнения появляется интерактивный промпт (y/n и т.п.),
        ответить можно через /bashin <текст>, не дожидаясь завершения.
        """
        if not self._accept(message):
            return
        cmd = (command.args or "").strip()
        if not cmd:
            await message.reply(self.t("bash_usage"))
            return
        thread_id = message.message_thread_id or 0
        shell = self.bash.get_or_create(thread_id, self._bash_cwd(message))
        if shell.busy:
            await message.reply(self.t("bash_busy"))
            return
        shell.busy = True
        marker = f"__DONE_{uuid.uuid4().hex}__"
        start = len(shell.snapshot())
        status = await message.reply(self.t("bash_running", cmd=cmd))
        try:
            # $? сразу за меткой — код возврата именно команды пользователя.
            shell.write(f"{cmd}\necho {marker} $?\n")
            out = b""
            code = None
            deadline = asyncio.get_running_loop().time() + BASH_TIMEOUT
            last_edit = ""
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(BASH_POLL_INTERVAL)
                raw = shell.snapshot()[start:]
                out = bash_clean(raw)
                # Маркер ищем как «marker <цифры>»: интерактивный bash эхом
                # прокручивает введённую `echo marker $?` (с литералом $?),
                # и наивный find цеплял это эхо — код возврата получался «$?».
                m = re.search(re.escape(marker).encode() + rb"\s+(\d+)", out)
                if m:
                    code = m.group(1).decode()
                    out = out[: m.start()]
                # Вымарываем эхо команды-маркера из показа.
                mbytes = marker.encode()
                out = b"\n".join(ln for ln in out.split(b"\n") if mbytes not in ln)
                shown = self._bash_render(cmd, out, code)
                if shown != last_edit:
                    try:
                        await status.edit_text(shown, parse_mode="HTML")
                        last_edit = shown
                    except Exception:
                        pass  # «текст не изменился» и т.п. — не критично
                if code is not None:
                    break
            else:
                await status.edit_text(
                    self._bash_render(cmd, out, None, timeout=True), parse_mode="HTML"
                )
                return
        finally:
            shell.busy = False

    async def cmd_bashin(self, message: Message, command: CommandObject) -> None:
        """Досыл сырого ввода в уже открытый /bash-терминал (ответ на y/n и т.п.)."""
        if not self._accept(message):
            return
        thread_id = message.message_thread_id or 0
        shell = self.bash.get(thread_id)
        if shell is None:
            await message.reply(self.t("bash_not_running"))
            return
        text = command.args or ""
        shell.write(text + "\n")
        await message.reply(self.t("bashin_sent", text=text or "⏎"))

    def _bash_render(
        self, cmd: str, out: bytes, code: str | None, timeout: bool = False
    ) -> str:
        """HTML для статус-сообщения /bash: команда + вывод в <pre>, обрезка с хвоста."""
        text = out.decode(errors="replace")
        if len(text) > BASH_OUTPUT_LIMIT:
            text = "…" + text[-BASH_OUTPUT_LIMIT:]
        body = html.escape(text) if text else self.t("bash_no_output")
        header = f"⚡ <code>{html.escape(cmd)}</code>"
        if timeout:
            footer = self.t("bash_timeout")
        elif code is None:
            footer = self.t("bash_wait")
        else:
            footer = self.t("bash_done", code=code)
        return f"{header}\n<pre>{body}</pre>\n{footer}"

    async def cmd_close(self, message: Message) -> None:
        """Остановить сессию; топик и запись остаются, resume по сообщению."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        self._stop_typing(session.thread_id)
        await self.bubbles.close(session.thread_id)
        await self.manager.close(session)
        await message.reply(self.t("close_done"))

    async def cmd_delete(self, message: Message) -> None:
        """Полностью удалить сессию вместе с топиком — с подтверждением."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=self.t("delete_confirm_yes"),
                callback_data=f"del:{session.thread_id}:yes"),
            InlineKeyboardButton(
                text=self.t("delete_confirm_no"),
                callback_data=f"del:{session.thread_id}:no"),
        ]])
        await message.reply(
            self.t("delete_confirm", title=session.title), reply_markup=markup
        )

    async def on_delete_button(self, callback: CallbackQuery) -> None:
        """Подтверждение удаления: yes — снести сессию+топик, no — отменить."""
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        try:
            _, thread_raw, verdict = (callback.data or "").split(":", 2)
            session = self.manager.get(int(thread_raw))
        except ValueError:
            session, verdict = None, "no"
        if session is None:
            await callback.answer(self.t("delete_gone"))
            await self._strip_markup(callback)
            return
        if verdict != "yes":
            await callback.answer()
            await self._edit_or_pass(callback, self.t("delete_canceled", title=session.title))
            return
        await callback.answer(self.t("delete_doing"))
        self._stop_typing(session.thread_id)
        await self.bubbles.close(session.thread_id)
        await self.manager.delete(session)
        try:
            await self.bot.delete_forum_topic(
                chat_id=self.chat_id, message_thread_id=session.thread_id
            )
        except Exception as e:
            # Топик мог остаться, а сессия уже удалена — показываем результат.
            logger.warning("Не удалось удалить топик %s: %s", session.thread_id, e)
            await self._edit_or_pass(callback, self.t("topic_delete_fail", error=e))

    async def _strip_markup(self, callback: CallbackQuery) -> None:
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    async def _edit_or_pass(self, callback: CallbackQuery, text: str) -> None:
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(text)
            except Exception:
                pass

    async def cmd_clear(self, message: Message) -> None:
        """Чистый контекст: перезапуск Claude с новым UUID, топик остаётся."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        status = await message.reply(self.t("clear_progress"))
        await self.bubbles.close(session.thread_id)
        try:
            await self.manager.clear(session)
        except Exception as e:
            logger.exception("Сессия %s: ошибка /clear", session.name)
            await self.manager.close(session)
            await status.edit_text(self.t("clear_fail", error=e))
            return
        await status.edit_text(self.t("clear_done"))

    async def cmd_compact(self, message: Message) -> None:
        """/compact — в терминал Claude (проверенный путь: PTY)."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        try:
            self.manager.type_into_pty(session, "/compact")
            await message.reply(self.t("compact_sent"))
        except Exception as e:
            logger.error("Сессия %s: не удалось отправить /compact: %s", session.name, e)
            await message.reply(self.t("send_fail", error=e))

    async def cmd_stats(self, message: Message) -> None:
        """Контекст и статистика из транскрипта сессии."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        await message.reply(await asyncio.to_thread(self._stats_text, session))

    def _model_display(self, session: Session, stats: dict | None = None) -> str:
        """Имя модели для показа: реальная из транскрипта → установленная
        (алиас вроде opus) → «по умолчанию Claude Code».

        Без транскрипта (stats=None) читает его — блокирующее чтение, дёргать
        через asyncio.to_thread. Если stats уже есть — лишнего I/O нет.
        """
        model = (stats or self.manager.read_stats(session) or {}).get("model", "")
        return model or session.model or self.t("default_model")

    def _stats_text(self, session: Session) -> str:
        stats = self.manager.read_stats(session)
        uptime = self._fmt_duration(time.time() - session.started_at)
        header = f"📊 {session.title}" + (
            "" if session.running else self.t("stats_stopped_suffix")
        )
        if stats is None:
            return self.t("stats_no_transcript", header=header, uptime=uptime)
        ctx = stats["context_tokens"]
        return self.t(
            "stats_body",
            header=header,
            model=self._model_display(session, stats),
            ctx=self._fmt_num(ctx),
            pct=f"{ctx / CONTEXT_WINDOW * 100:.0f}",
            out=self._fmt_num(stats["output_tokens"]),
            turns=stats["turns"],
            kb=f"{stats['transcript_bytes'] / 1024:.0f}",
            uptime=uptime,
        )

    async def cmd_usage(self, message: Message) -> None:
        """Расходы и лимиты плана: прогоняем /cost в терминале Claude и парсим."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        if not await self._ensure_running(session, message):
            return
        status = await message.reply(self.t("usage_collecting"))
        delta = await self.manager.run_and_capture(session, "/cost")
        data = self._parse_cost(delta)
        if not data:
            await status.edit_text(self.t("usage_failed"))
            return
        lines = [self.t("usage_title", name=session.title)]
        if "cost" in data:
            lines.append(self.t("usage_cost", cost=data["cost"]))
        if "session_pct" in data:
            reset = data.get("session_reset", "")
            lines.append(self.t("usage_session", pct=data["session_pct"],
                                reset=f" · {reset}" if reset else ""))
        if "week_pct" in data:
            reset = data.get("week_reset", "")
            lines.append(self.t("usage_week", pct=data["week_pct"],
                                reset=f" · {reset}" if reset else ""))
        for name, pct in data.get("models", []):
            lines.append(self.t("usage_model", model=name, pct=pct))
        await status.edit_text("\n".join(lines))

    @staticmethod
    def _parse_cost(text: str) -> dict:
        """Выдрать цифры из TUI-каши /cost (наложенные кадры, рамки)."""
        t = re.sub(r"[│▏▐▔▕█▌▊▋▉▛▜✶✢·…✻✽✼✾*]+", " ", text)
        t = re.sub(r"\s+", " ", t)
        out: dict = {}
        if m := re.search(r"cost:\s*\$([\d.]+)", t):
            out["cost"] = m.group(1)
        if m := re.search(r"Current session.*?(\d+)%\s*used", t):
            out["session_pct"] = m.group(1)
        if m := re.search(r"Current week \(all models\).*?(\d+)%\s*used", t):
            out["week_pct"] = m.group(1)
        for mm in re.finditer(r"Current week \((?!all models)([^)]+)\).*?(\d+)%\s*used", t):
            out.setdefault("models", []).append((mm.group(1).strip(), mm.group(2)))
        resets = re.findall(r"Res[et]+s ([A-Za-z0-9:, ]+?\([^)]+\))", t)
        if resets:
            out["session_reset"] = resets[0].strip()
            if len(resets) > 1:
                out["week_reset"] = resets[1].strip()
        return out

    async def cmd_model(self, message: Message, command: CommandObject) -> None:
        """Модель сессии: /model — кнопки-синонимы, /model <имя> — установить."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        arg = (command.args or "").strip()
        if arg:
            await self._switch_model(session, arg, message)
            return
        buttons = [
            InlineKeyboardButton(text=alias, callback_data=f"model:{session.thread_id}:{alias}")
            for alias in MODEL_ALIASES
        ]
        model = await asyncio.to_thread(self._model_display, session)
        await message.reply(
            self.t("model_prompt", model=model),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
        )

    async def on_model_button(self, callback: CallbackQuery) -> None:
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        try:
            _, thread_raw, alias = (callback.data or "").split(":", 2)
            session = self.manager.get(int(thread_raw))
        except ValueError:
            session = None
        if session is None:
            await callback.answer(self.t("stop_not_active"))
            return
        await callback.answer(self.t("model_switching_btn", model=alias))
        if isinstance(callback.message, Message):
            await self._switch_model(session, alias, callback.message)

    async def _switch_model(self, session: Session, model: str, message: Message) -> None:
        # message.answer() сам наследует message_thread_id исходного сообщения —
        # явно передавать нельзя (иначе TypeError: multiple values).
        status = await message.answer(
            self.t("model_switching", name=session.title, model=model)
        )
        self._stop_typing(session.thread_id)
        await self.bubbles.close(session.thread_id)
        try:
            resumed = await self.manager.set_model(session, model)
        except Exception as e:
            logger.exception("Сессия %s: ошибка смены модели", session.name)
            await status.edit_text(self.t("model_fail", model=model, error=e))
            return
        note = "" if resumed else self.t("model_ctx_lost")
        await status.edit_text(self.t("model_done", model=model) + note)

    async def on_session_button(self, callback: CallbackQuery) -> None:
        """Кнопки в /list: статистика и остановка."""
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        try:
            _, action, thread_raw = (callback.data or "").split(":", 2)
            session = self.manager.get(int(thread_raw))
        except ValueError:
            session = None
        if session is None or not isinstance(callback.message, Message):
            await callback.answer(self.t("session_not_found"))
            return
        if action == "stats":
            await callback.answer()
            await callback.message.answer(await asyncio.to_thread(self._stats_text, session))
        elif action == "close":
            self._stop_typing(session.thread_id)
            await self.bubbles.close(session.thread_id)
            await self.manager.close(session)
            await callback.answer(self.t("sess_closed", name=session.title))
        else:
            await callback.answer()

    # ── текст и файлы: Telegram -> Claude ───────────────────────

    async def _react(self, message: Message, emoji: str) -> None:
        """Поставить реакцию-отклик на сообщение пользователя.

        Двухступенчатый ack: 👀 «бот принял» при приёме → ✅ «дошло до модели»
        после успешного push в канал Клода (❌ — если не удалось). В чатах, где
        реакции боту запрещены, тихо мимо — это косметика, не критично.
        """
        try:
            await self.bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception as e:
            # Раньше был debug — сбой реакции проходил незаметно (ниже порога
            # INFO), и «пропала галочка» было нечем диагностировать. warning,
            # пока не поймём частую причину (лимит реакций в чате и т.п.).
            logger.warning("react %s (msg %s) не удалась: %s", emoji, message.message_id, e)

    async def on_text(self, message: Message) -> None:
        if not self._accept(message):
            return
        await self._react(message, "👀")
        session = self._topic_session(message)
        if session is None or not message.text:
            return
        if not await self._ensure_running(session, message):
            return
        await self._forward(session, message, self._with_quote(message))

    def _with_quote(self, message: Message) -> str:
        """Текст сообщения + процитированный фрагмент, если это reply-с-цитатой.

        Телеграм присылает цитату отдельно от текста; без склейки модель не
        видит, на что отвечает пользователь. Формат: цитата в блоке, ниже —
        сам вопрос. quote.text (выделенный фрагмент) точнее полного reply_to.
        """
        text = message.text or ""
        quoted = ""
        quote_txt = message.quote.text if message.quote else None
        reply_txt = message.reply_to_message.text if message.reply_to_message else None
        if quote_txt:
            quoted = quote_txt                                # выделенный фрагмент
        elif reply_txt:
            quoted = reply_txt                                # весь процитированный пост
        # Диагностика: дошли ли до бота данные цитаты (в форумах/при privacy
        # mode их может не быть — тогда функцию надо крепить иначе).
        logger.info(
            "on_text quote=%s reply_msg=%s quote_len=%d reply_len=%d",
            bool(quote_txt), message.reply_to_message is not None,
            len(quote_txt or ""), len(reply_txt or ""),
        )
        if not quoted:
            return text
        quoted = quoted.strip()
        if len(quoted) > 500:
            quoted = quoted[:500] + " …"
        block = "\n".join(f"> {ln}" for ln in quoted.splitlines())
        return f"{block}\n\n{text}"

    async def on_file(self, message: Message) -> None:
        """Фото (в.т. из буфера обмена) и документы: скачать в сессию."""
        if not self._accept(message):
            return
        await self._react(message, "👀")
        session = self._topic_session(message)
        if session is None:
            return
        if not await self._ensure_running(session, message):
            return

        # INCOMING_DIR: относительный путь — внутри папки сессии,
        # абсолютный — общий для всех сессий.
        incoming = Path(self.config.incoming_dir).expanduser()
        if not incoming.is_absolute():
            incoming = session.session_dir / incoming
        incoming.mkdir(parents=True, exist_ok=True)

        if message.document:
            file_id = message.document.file_id
            fname = Path(message.document.file_name or f"file_{message.message_id}").name
        else:
            file_id = message.photo[-1].file_id  # максимальное разрешение
            fname = f"photo_{message.message_id}.jpg"
        dest = incoming / fname
        try:
            await self.bot.download(file_id, destination=dest)
        except Exception as e:
            logger.error("Сессия %s: не удалось скачать файл: %s", session.name, e)
            await message.reply(self.t("file_dl_fail", error=e))
            return

        text = self.t("file_received", path=dest)
        if message.caption:
            text += "\n" + self.t("file_caption", caption=message.caption)
        await self._forward(session, message, text)

    async def on_slash(self, message: Message) -> None:
        """Неизвестные /команды — прямо в терминал Claude (команды Claude Code).

        Например /context, /mcp, /usage. Вывод команды остаётся в TUI
        (claude.log); в Telegram приходит только то, что Claude сам отправит.
        """
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None or not message.text:
            return
        if not await self._ensure_running(session, message):
            return
        cmd = message.text.strip().splitlines()[0]
        try:
            self.manager.type_into_pty(session, cmd)
        except Exception as e:
            await message.reply(self.t("send_fail", error=e))
            return
        await message.reply(self.t("slash_sent", cmd=cmd))

    async def _ensure_running(self, session: Session, message: Message) -> bool:
        """Возобновить остановленную сессию перед пересылкой сообщения."""
        if session.running:
            return True
        status = await message.reply(self.t("resume_progress"))
        try:
            resumed = await self.manager.resume(session)
        except Exception as e:
            logger.exception("Сессия %s: ошибка resume", session.name)
            await status.edit_text(self.t("resume_fail", error=e))
            return False
        await status.edit_text(self.t("resume_ok") if resumed else self.t("resume_fresh"))
        return True

    async def _forward(self, session: Session, message: Message, text: str) -> None:
        context_id = f"tg:{self.chat_id}:{session.thread_id}:{message.message_id}"
        # Новый бабл поверх старого, но с переносом строк: если Клод ещё не
        # ответил, а пользователь уже шлёт следующее — список вызванных тулов
        # не теряется, а переезжает в свежий бабл и дополняется дальше.
        # Схлопывается бабл только финальным ответом (close в handle_reply).
        await self.bubbles.fork(session.thread_id)
        try:
            await self.manager.send_to_claude(session, text, context_id)
        except Exception as e:
            logger.error("Сессия %s: не удалось передать сообщение: %s", session.name, e)
            await self._react(message, "👎")  # не дошло до модели
            await message.reply(self.t("forward_fail", error=e))
            return
        # Push в канал Клода прошёл — сообщение в стеке модели: 👀 → 👍.
        # 👍/👎, не ✅/❌: Telegram запрещает галочку/крестик как реакцию
        # (REACTION_INVALID), а большой палец разрешён везде.
        # Подстраховка реакцией: дублируем в бабле иконку + начало текста —
        # строка в бабле не зависит от прав на реакции в чате.
        await self._react(message, "👍")
        # Для фото/документов текст живёт в caption, не в text — берём оба.
        snippet = html.escape(self._shorten(message.text or message.caption or "", 28))
        await self.bubbles.append(session.thread_id, f"📨 {snippet}")
        self._start_typing(session.thread_id)

    # ── индикатор «печатает…» ───────────────────────────────────

    def _start_typing(self, thread_id: int) -> None:
        """«печатает…» + сторож зависаний на время обработки запроса.

        Оба гаснут финальным ответом (_stop_typing в handle_reply).
        """
        self._stop_typing(thread_id)
        self._typing[thread_id] = asyncio.create_task(self._typing_loop(thread_id))
        self._watchdogs[thread_id] = asyncio.create_task(self._watchdog_loop(thread_id))
        self._error_relays[thread_id] = asyncio.create_task(self._error_relay_loop(thread_id))

    def _stop_typing(self, thread_id: int) -> None:
        for registry in (self._typing, self._watchdogs, self._error_relays):
            task = registry.pop(thread_id, None)
            if task is not None:
                task.cancel()

    async def _watchdog_loop(self, thread_id: int) -> None:
        """Если Claude молчит И claude.log не растёт несколько проверок подряд —
        это зависание (а не долгое размышление): предупреждаем в топик.
        """
        session = self.manager.get(thread_id)
        if session is None:
            return
        log = session.session_dir / "claude.log"
        await asyncio.sleep(WATCHDOG_GRACE)
        try:
            last_size = log.stat().st_size
        except OSError:
            last_size = 0
        stalls = 0
        while True:
            await asyncio.sleep(WATCHDOG_CHECK)
            try:
                size = log.stat().st_size
            except OSError:
                size = last_size
            # Жизнь = лог растёт ИЛИ claude (с потомками-тулами) ест CPU.
            # Одних байт мало: спиннер «almost done» может на секунды замолчать
            # в нормальной работе, и это не зависание.
            alive = size != last_size or self.manager.is_busy(session)
            last_size = size
            stalls = 0 if alive else stalls + 1
            if stalls < STALL_CHECKS:
                continue
            # Завис: снимаем «печатает», шлём диагностику один раз.
            t = self._typing.pop(thread_id, None)
            if t is not None:
                t.cancel()
            tail = await asyncio.to_thread(self.manager.tail_log, session, 10)
            msg = self.t("stalled")
            if tail:
                msg += "\n\n" + self.t("session_died_tail", tail=tail[:1200])
            await self._send(self.chat_id, thread_id, msg)
            return

    async def _error_relay_loop(self, thread_id: int) -> None:
        """Транслировать в чат, что Claude делает, когда тулов ещё нет.

        Сигналы нюхаем из ПРИРАЩЕНИЯ claude.log (отрисовка TUI) с момента старта
        хода; разбирает их _detect_log_signals. Три класса:
          1. «API Error: <код>» — настоящая ошибка API. Триггер — именно баннер с
             кодом, не слова «rate-limit» в прозе ответов (раньше ловили ложное).
             Класс задаёт действие: rate-limit→/model, 400-протокол→/clear,
             прочее→«задерживается». Раз в ERROR_RELAY_COOLDOWN, та же — REPEAT.
          2. Ретрай «attempt K/M» — ЖИВОЙ: пере-файр при росте K (видно прогресс
             3/100 → 47/100), throttle RETRY_SURFACE_INTERVAL. Главное против
             «5 минут тишины, непонятно что делает».
          3. Краш-рестарт «Resume this session» mid-хода — Claude упал и поднялся.
             Растёт счётчик → предупреждаем (раз в COOLDOWN), намекаем /close_session.
        """
        session = self.manager.get(thread_id)
        if session is None or self.chat_id is None:
            return
        log = session.session_dir / "claude.log"
        await asyncio.sleep(WATCHDOG_GRACE)
        try:
            offset = log.stat().st_size  # всё, что уже в логе до хода, — не наше
        except OSError:
            return
        last_surfaced = -ERROR_RELAY_COOLDOWN
        last_sig: str | None = None
        last_sig_at = -ERROR_RELAY_REPEAT
        last_retry_k = 0
        last_retry_at = -RETRY_SURFACE_INTERVAL
        restart_count = 0
        last_restart_at = -ERROR_RELAY_COOLDOWN
        loop_time = asyncio.get_running_loop().time

        async def _surf(text: str) -> None:
            try:
                await self._send(self.chat_id, thread_id, text)
            except Exception as e:
                logger.debug("error_relay: не удалось отправить: %s", e)

        while True:
            await asyncio.sleep(ERROR_RELAY_INTERVAL)
            try:
                data = log.read_bytes()
            except OSError:
                continue
            if offset > len(data):  # лог обнулили (resume/ротация) — смотрим с начала
                offset = 0
            chunk = _LOG_ANSI_RE.sub(b"", data[offset:]).replace(b"\r", b"")
            offset = len(data)
            sig = _detect_log_signals(chunk)
            now = loop_time()

            # 1. Ошибка API (с дедупом: раз в COOLDOWN, та же — раз в REPEAT).
            if sig["api_error"]:
                code, klass = sig["api_error"]
                s = f"{code.decode()}:{klass}"
                cooled = now - last_surfaced >= ERROR_RELAY_COOLDOWN
                fresh = not (s == last_sig and now - last_sig_at < ERROR_RELAY_REPEAT)
                if cooled and fresh:
                    last_surfaced = now
                    last_sig, last_sig_at = s, now
                    await _surf(self.t(f"api_error_{klass}"))

            # 2. Живой ретрай: пере-файр, когда attempt вырос — чтобы было видно
            #    прогресс (3/100 → 47/100), а не одна тишина. Throttle — RETRY_SURFACE_INTERVAL.
            if sig["retry"]:
                k, total = sig["retry"]
                if k > last_retry_k and now - last_retry_at >= RETRY_SURFACE_INTERVAL:
                    last_retry_k = k
                    last_retry_at = now
                    await _surf(self.t("api_retrying", attempt=k, total=total))

            # 3. Краш-рестарт-луп: баннер «Resume this session» mid-хода = Claude
            #    упал и поднялся. Растёт счётчик — предупреждаем (раз в COOLDOWN).
            if sig["restarts"]:
                restart_count += sig["restarts"]
                if now - last_restart_at >= ERROR_RELAY_COOLDOWN:
                    last_restart_at = now
                    await _surf(self.t("session_restart_loop", count=restart_count))

    async def _typing_loop(self, thread_id: int) -> None:
        while True:
            chat_id = self.chat_id
            if chat_id is None:
                return
            try:
                await self.bot.send_chat_action(
                    chat_id=chat_id, action="typing", message_thread_id=thread_id
                )
            except Exception as e:
                logger.debug("typing (топик %s): %s", thread_id, e)
            await asyncio.sleep(TYPING_INTERVAL)

    # ── ответы: Claude -> Telegram ──────────────────────────────

    async def handle_reply(self, data: dict) -> None:
        """Вызывается reply-сервером: текстовый ответ или файл от Claude."""
        chat_id, thread_id, reply_to = self._parse_context(str(data.get("context_id", "")))
        if chat_id is None:
            return

        if thread_id is not None:  # ответ/файл = активность (сброс таймера простоя)
            session = self.manager.get(thread_id)
            if session is not None:
                self.manager.touch(session)

        if data.get("file_path"):
            if thread_id is not None:
                self._stop_typing(thread_id)
            await self._send_file(
                chat_id, thread_id,
                str(data["file_path"]), str(data.get("caption", "")), reply_to,
            )
            return

        text = str(data.get("text", ""))
        complete = bool(data.get("complete", False))
        logger.info("reply топик=%s complete=%s len=%d", thread_id, complete, len(text))

        if not complete:
            # Промежуточный ответ Клода — отдельным полным сообщением (с 💬),
            # а не обрезанной строчкой в бабле: важно видеть, что он пишет, пока
            # работает дальше.
            if text:
                await self._send(chat_id, thread_id, f"💬 {text}")
            return

        # Финал (даже с пустым текстом): гасим typing и бабл, чтобы индикатор
        # не крутился вечно. Сначала уведомляющее сообщение (тянет чат вниз),
        # потом чистка тихого бабла — окна «ответа ещё нет» не остаётся.
        if thread_id is not None:
            self._stop_typing(thread_id)
        if text:
            await self._send(chat_id, thread_id, text, reply_to)
        if thread_id is not None:
            await self.bubbles.close(thread_id)

    async def _send_file(
        self, chat_id: int, thread_id: int | None,
        file_path: str, caption: str, reply_to: int | None,
    ) -> None:
        path = Path(file_path).expanduser()
        if not path.is_file():
            await self._send(chat_id, thread_id, self.t("sendfile_not_found", path=path))
            return
        if path.stat().st_size > 50 * 1024 * 1024:
            await self._send(chat_id, thread_id, self.t("sendfile_too_big", path=path))
            return
        kwargs: dict = {"chat_id": chat_id, "document": FSInputFile(path)}
        if caption:
            kwargs["caption"] = caption[:1024]
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        try:
            await self.bot.send_document(**kwargs)
        except Exception as e:
            logger.error("Не удалось отправить файл %s: %s", path, e)
            await self._send(chat_id, thread_id, self.t("sendfile_fail", error=e))

    async def handle_tool_event(self, session_name: str, payload: dict) -> None:
        """Вызывается reply-сервером на событие PreToolUse-хука."""
        session = self.manager.get_by_name(session_name)
        if session is None or self.chat_id is None:
            return
        tool = str(payload.get("tool_name") or "?")
        if "reply_to_telegram" in tool or "send_file_to_telegram" in tool:
            return  # результат и так придёт сообщением — в бабле это шум
        await self.bubbles.append(
            session.thread_id, self._tool_line(tool, payload.get("tool_input") or {})
        )

    def _tool_line(self, tool: str, tool_input: dict) -> str:
        """HTML-строка бабла: иконка + имя жирным + короткая деталь моноширинно.

        Деталь режем до осмысленного кусочка в одну строку: длинные bash-команды
        показываем «головой» (имя команды + подкоманда), у TaskCreate берём
        тему, у незнакомых тулов — первый строковый аргумент, а не весь JSON.
        """
        icon = TOOL_ICONS.get(tool, "🔧")
        if tool == "Task":
            # t("subagent") уже содержит иконку 🤖 — свою не добавляем.
            agent = html.escape(str(tool_input.get("subagent_type") or "agent"))
            desc = html.escape(self._shorten(str(tool_input.get("description") or "")))
            base = f"<b>{self.t('subagent', agent=agent)}</b>"
            return f"{base}: <i>{desc}</i>" if desc else base

        if tool == "TodoWrite":
            todos = tool_input.get("todos")
            detail = f"{len(todos)} задач" if isinstance(todos, list) else ""
        elif tool == "Bash":
            detail = _bash_head(str(tool_input.get("command") or ""))
        else:
            field, as_name = _TOOL_DETAIL.get(tool, (None, False))
            if field:
                detail = str(tool_input.get(field, "")).strip()
                if as_name and detail:
                    detail = Path(detail).name  # длинный путь → имя файла
                    suffix = _file_suffix(tool, tool_input)  # строки/диапазон
                    if suffix:
                        detail += suffix
            else:
                detail = _first_meaningful(tool_input)  # сторонний тул — не JSON

        detail = html.escape(self._shorten(detail))
        head = f"{icon} <b>{html.escape(tool)}</b>"
        return f"{head} <code>{detail}</code>" if detail else head

    @staticmethod
    def _shorten(text: str, limit: int = LINE_LIMIT) -> str:
        text = " ".join(text.split())
        return text[:limit] + "…" if len(text) > limit else text

    # ── permission relay: Claude Code -> Telegram -> Claude Code ──

    async def handle_permission_request(self, session_name: str, payload: dict) -> None:
        """Запрос разрешения — в топик кнопками ✅/❌.

        Параллельно открыт и локальный TUI-диалог; применяется первый ответ.
        description/input_preview — недоверенный текст, экранируем.
        """
        session = self.manager.get_by_name(session_name)
        if session is None or self.chat_id is None:
            return
        request_id = str(payload.get("request_id", ""))
        tool = html.escape(str(payload.get("tool_name", "?")))
        desc = html.escape(str(payload.get("description", "")))
        raw_preview = str(payload.get("input_preview", ""))
        if len(raw_preview) > PERM_PREVIEW_LIMIT:
            raw_preview = raw_preview[:PERM_PREVIEW_LIMIT] + " …(обрезано)"
        preview = html.escape(raw_preview)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=self.t("perm_allow"),
                callback_data=f"perm:{session.thread_id}:{request_id}:allow"),
            InlineKeyboardButton(
                text=self.t("perm_deny"),
                callback_data=f"perm:{session.thread_id}:{request_id}:deny"),
        ]])
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=self.t("perm_request", tool=tool, desc=desc, preview=preview),
                message_thread_id=session.thread_id,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as e:
            logger.error("Не удалось отправить permission-запрос: %s", e)

    async def on_perm_button(self, callback: CallbackQuery) -> None:
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        try:
            prefix, behavior = (callback.data or "").rsplit(":", 1)
            _, thread_raw, request_id = prefix.split(":", 2)
            session = self.manager.get(int(thread_raw))
        except ValueError:
            session = None
        if session is None:
            await callback.answer(self.t("stop_not_active"))
            return
        try:
            await self.manager.send_permission(session, request_id, behavior)
        except Exception as e:
            logger.error("Сессия %s: не удалось передать вердикт: %s", session.name, e)
            await callback.answer(self.t("perm_fail", error=e))
            return
        verdict_key = "perm_allowed" if behavior == "allow" else "perm_denied"
        await callback.answer()
        if isinstance(callback.message, Message):
            try:
                first_line = (callback.message.text or "").splitlines()[1:2]
                tool = first_line[0].split(":", 1)[0] if first_line else "?"
                await callback.message.edit_text(
                    (callback.message.text or "") + "\n\n" + self.t(verdict_key, tool=tool)
                )
            except Exception:
                pass

    async def on_stop_button(self, callback: CallbackQuery) -> None:
        """Мягкий стоп: просим Claude свернуть текущую работу.

        Жёсткого прерывания хода в channels нет; жёсткий вариант — /close_session.
        """
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        try:
            thread_id = int((callback.data or "").split(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer()
            return
        session = self.manager.get(thread_id)
        if session is None:
            await callback.answer(self.t("stop_not_active"))
            return
        context_id = f"tg:{self.chat_id}:{thread_id}:0"
        try:
            await self.manager.send_to_claude(session, self.t("stop_message"), context_id)
            await callback.answer(self.t("stop_requested"))
            await self.bubbles.append(thread_id, self.t("bubble_stop_requested"))
            # Видимый отклик: меняем кнопку «Стоп» → «⏹ Останавливаю…»,
            # чтобы было ясно, что бот нажатие зафиксировал (тост легко пропустить).
            if isinstance(callback.message, Message):
                try:
                    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(
                                text=self.t("bubble_stopping"),
                                callback_data=f"stop:{thread_id}")]]))
                except Exception:
                    pass
        except Exception as e:
            logger.error("Сессия %s: не удалось отправить стоп: %s", session.name, e)
            await callback.answer(self.t("stop_fail"))

    # ── служебное ───────────────────────────────────────────────

    def _parse_context(self, context_id: str) -> tuple[int | None, int | None, int | None]:
        """context_id = tg:chat_id:thread_id:message_id (thread_id=0 — без топика)."""
        parts = context_id.split(":")
        if len(parts) == 4 and parts[0] == "tg":
            try:
                chat_id, thread_raw, reply_to = int(parts[1]), int(parts[2]), int(parts[3])
                return chat_id, thread_raw or None, reply_to or None
            except ValueError:
                pass
        logger.warning("Некорректный context_id: %r", context_id)
        return self.chat_id, None, None

    async def notify_session_dead(self, session: Session, code: int | str) -> None:
        """Колбэк SessionManager: Claude умер сам по себе."""
        if self.chat_id is None:
            return
        self._stop_typing(session.thread_id)
        await self.bubbles.close(session.thread_id)
        text = self.t("session_died", name=session.title, code=code)
        tail = await asyncio.to_thread(self.manager.tail_log, session)
        if tail:
            text += "\n\n" + self.t("session_died_tail", tail=tail[:1500])
        await self._send(self.chat_id, session.thread_id, text)

    async def notify_idle_closed(self, sessions: list[Session]) -> None:
        """Колбэк sweeper: сессии остановлены по простою."""
        if self.chat_id is None:
            return
        for session in sessions:
            self._stop_typing(session.thread_id)
            await self.bubbles.close(session.thread_id)
            await self._send(
                self.chat_id, session.thread_id,
                self.t("idle_closed", hours=f"{self.config.idle_timeout_h:g}"),
            )

    async def notify_startup(self, restored: int) -> None:
        """№4: сообщить в привязанный чат, что бот онлайн (+ профиль и URL)."""
        if self.chat_id is None:
            return
        config_dir = self.config.claude_config_dir or Path.home() / ".claude"
        base_url = self.config.claude_env.get("ANTHROPIC_BASE_URL") or self.t("url_default")
        try:
            await self._send(
                self.chat_id, None,
                self.t("startup", n=restored, config=config_dir, url=base_url),
            )
        except Exception as e:
            logger.warning("Не удалось отправить стартовое уведомление: %s", e)

    async def _send(
        self, chat_id: int, thread_id: int | None, text: str, reply_to: int | None = None
    ) -> None:
        """Отправка с разметкой (markdown→HTML), фолбэк — чистый текст.

        Деградация: без reply (исходное сообщение удалено) → без топика
        (топик удалили руками) — финальный ответ не должен теряться.
        """
        for i, plain in enumerate(split_text(text)):
            hchunk = md_to_html(plain)
            kwargs: dict = {"chat_id": chat_id}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            if reply_to and i == 0:
                kwargs["reply_to_message_id"] = reply_to
            use_html = True
            while True:
                sent = False
                if use_html:
                    try:
                        msg = await self.bot.send_message(text=hchunk, parse_mode="HTML", **kwargs)
                        sent = True
                    except Exception as e:
                        logger.warning("HTML-отправка не удалась, фолбэк на plain: %s", e)
                        use_html = False
                if not sent:
                    try:
                        msg = await self.bot.send_message(text=plain, **kwargs)
                        sent = True
                    except Exception:
                        if "reply_to_message_id" in kwargs:
                            kwargs.pop("reply_to_message_id")
                        elif "message_thread_id" in kwargs:
                            kwargs.pop("message_thread_id")
                        else:
                            logger.error("Не удалось отправить сообщение в чат %s", chat_id)
                            return
                if sent:
                    break

    @staticmethod
    def _fmt_num(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    def _fmt_duration(self, seconds: float) -> str:
        m = int(seconds) // 60
        if m < 60:
            return self.t("min", m=m)
        return self.t("hour_min", h=m // 60, m=m % 60)
