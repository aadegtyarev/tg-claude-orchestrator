"""Конфигурация: всё читается из .env / переменных окружения."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _auto_orch_token() -> str:
    """Разовый токен внутреннего HTTP-API, если ORCH_TOKEN не задан явно.

    Локальный (127.0.0.1) токен — защита от любого локального процесса и
    DNS-rebinding из браузера (см. REVIEW.md S1). Перезапуск launcher'а убивает
    все процессы claude (manager.shutdown), значит и channel_server'ы, поэтому
    токен, сгенерированный на пуск, согласован со всеми сессиями этого пуска
    (resume переписывает .mcp.json/settings с актуальным токеном). Для
    предсказуемости лучше зафиксировать ORCH_TOKEN в .env.
    """
    tok = secrets.token_urlsafe(24)
    logger.warning(
        "ORCH_TOKEN не задан — сгенерирован разовый токен внутреннего API. "
        "Для стабильности между перезапусками зафиксируй ORCH_TOKEN в .env.",
    )
    return tok


# Имя, по которому ГОСТЬ microVM (SANDBOX=agent-vm) видит хост: microsandbox
# мапит его на хостовый 127.0.0.1 (аналог host.docker.internal), при условии
# --allow-host (его даёт AgentVmRunner). На самом ХОСТЕ это имя НЕ резолвится —
# поэтому оно только guest-facing (см. Config.guest_orch_host), а reply-сервер
# биндится на orch_host (host-side loopback). Подтверждено живым экспериментом,
# docs/agent-vm-integration.md.
AGENT_VM_GUEST_HOST = "host.microsandbox.internal"


@dataclass(frozen=True)
class Config:
    # Активные транспорт-адаптеры (ADAPTERS=telegram,web) и модули (MODULES=…).
    adapters: tuple[str, ...]
    modules: tuple[str, ...]
    telegram_bot_token: str
    telegram_chat_id: int | None
    web_host: str
    web_port: int
    web_token: str
    channel_port_start: int
    channel_port_end: int
    sessions_dir: Path
    max_instances: int
    claude_bin: str
    orch_host: str
    orch_port: int
    orch_token: str
    # systemd-юнит для команды /restart (self-restart). Пусто → авто-детект из
    # /proc/self/cgroup, фолбэк claude-orchestrator.service.
    orch_systemd_unit: str
    allowed_user_ids: frozenset[int]
    show_tool_calls: bool
    delete_bubble: bool
    show_command_menu: bool
    claude_config_dir: Path | None
    incoming_dir: str
    permission_mode: str
    bot_lang: str
    idle_timeout_h: float
    log_max_mb: float
    default_model: str | None  # --model по умолчанию (None = решение Claude/профиля/проекта)
    default_effort: str | None  # --effort по умолчанию (low/medium/high/xhigh/max)
    claude_env: dict[str, str]  # доп. env для процесса claude (CLAUDE_ENV_*)
    sandbox: str  # "bwrap" (файловая песочница) | "agent-vm" (microVM) | "off"
    sandbox_extra_rw: tuple[Path, ...]  # доп. пути, доступные из песочницы на запись
    sandbox_dbus: bool  # прокидывать ли system D-Bus для mDNS/avahi-browse (bwrap)
    # Прокидывать ли X/Wayland в песочницу (bwrap). Сеть у песочницы общая с
    # хостом, поэтому абстрактный сокет X достижим даже при tmpfs /tmp — с
    # $DISPLAY процесс мог бы дёрнуть хостовый GUI (askpass-диалоги, скриншоты,
    # ввод). Off (дефолт) вырезает DISPLAY/XAUTHORITY/WAYLAND_DISPLAY; on —
    # оставить (если модели действительно нужен X в песочнице).
    sandbox_x11: bool
    # Раннер agent-vm (SANDBOX=agent-vm): ресурсы и пин образа гостя.
    agent_vm_memory_gib: float | None
    agent_vm_cpus: int | None
    agent_vm_image: str | None
    # Кошелёк секретов (MODULES=wallet): файл секретов и политик (0600, вне
    # allowlist песочницы).
    wallet_secrets_file: Path
    # Guard кошелька: всегда-запрет опасных вызовов (печать токена, git-RCE через
    # флаги) поверх policy, с прозрачным объяснением модели. WALLET_GUARD=0 —
    # отключить (модель сможет что угодно в рамках commands; менее безопасно).
    wallet_guard: bool
    # Судья auto-режима проверяет ВСЕ bash-команды (classifyAllShell), а не только
    # «на вид рискованные» — чтобы хитрый secret-exfil не проскочил. Побочка:
    # больше подтверждений на легит-но-рискованное (force-push и т.п.).
    # AUTOMODE_CLASSIFY_ALL_SHELL=0 — выключить (меньше вопросов, слабее защита).
    automode_classify_all_shell: bool
    # Разрешить правку policy кошелька из чата (команда /wallet). По умолчанию
    # включено. WALLET_POLICY_EDIT=0 — только просмотр, правки лишь host-файлом.
    wallet_policy_edit: bool

    @property
    def guest_orch_host(self) -> str:
        """Адрес оркестратора С ТОЧКИ ЗРЕНИЯ СЕССИИ (гостя) — для `.mcp.json`
        (channel_server) и хук-диспетчера. Выводится из `sandbox`, поэтому
        переключение движка = ОДНО действие (SANDBOX в .env), без ручной правки
        адресов. Под agent-vm гость VM не видит хостовый loopback → имя
        host-gateway (`AGENT_VM_GUEST_HOST`); под bwrap/off — общий loopback
        `orch_host`. reply-сервер биндится на `orch_host` (host-side) — оно
        host-resolvable, а guest-facing имя резолвится только внутри гостя."""
        return AGENT_VM_GUEST_HOST if self.sandbox == "agent-vm" else self.orch_host

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        adapters = cls._parse_adapters(os.getenv("ADAPTERS", "telegram"))
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if "telegram" in adapters and not token:
            raise SystemExit(
                "TELEGRAM_BOT_TOKEN не задан — заполни .env (см. .env.example) "
                "или убери telegram из ADAPTERS."
            )

        chat_id_raw = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

        sandbox = cls._parse_sandbox(os.getenv("SANDBOX", "bwrap"))
        modules = cls._default_modules(os.getenv("MODULES"), sandbox)

        return cls(
            adapters=adapters,
            modules=modules,
            telegram_bot_token=token,
            telegram_chat_id=cls._parse_chat_id(chat_id_raw),
            web_host=os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1",
            web_port=int(os.getenv("WEB_PORT", "8180")),
            # Токен веб-интерфейса. Пустой = сгенерировать на запуск (URL с
            # токеном печатается в лог, как у Jupyter).
            web_token=os.getenv("WEB_TOKEN", "").strip(),
            # 0/не задано = авто: ОС выдаёт свободный localhost-порт на сессию.
            channel_port_start=int(os.getenv("CHANNEL_PORT_START", "0")),
            channel_port_end=int(os.getenv("CHANNEL_PORT_END", "0")),
            sessions_dir=Path(os.getenv("SESSIONS_DIR", "~/tg-claude-sessions")).expanduser(),
            max_instances=int(os.getenv("MAX_INSTANCES", "5")),
            claude_bin=os.getenv("CLAUDE_BIN", "claude"),
            orch_host=os.getenv("ORCH_HOST", "127.0.0.1"),
            orch_port=int(os.getenv("ORCH_PORT", "18080")),
            # Токен внутреннего HTTP-API (см. _auto_orch_token / REVIEW.md S1).
            orch_token=os.getenv("ORCH_TOKEN", "").strip() or _auto_orch_token(),
            orch_systemd_unit=os.getenv("ORCH_SYSTEMD_UNIT", "").strip(),
            allowed_user_ids=cls._parse_user_ids(os.getenv("ALLOWED_USER_IDS", "")),
            show_tool_calls=cls._parse_bool(os.getenv("SHOW_TOOL_CALLS", "true")),
            delete_bubble=cls._parse_bool(os.getenv("DELETE_BUBBLE", "true")),
            # Меню команд (кнопка «/»). В группе Telegram всё равно показывает
            # «/команда@бот» (клиентский роутинг) — false скрывает меню целиком.
            show_command_menu=cls._parse_bool(os.getenv("SHOW_COMMAND_MENU", "true")),
            claude_config_dir=(
                Path(raw).expanduser() if (raw := os.getenv("CLAUDE_CONFIG_DIR", "").strip()) else None
            ),
            incoming_dir=os.getenv("INCOMING_DIR", "incoming").strip() or "incoming",
            permission_mode=cls._parse_permission_mode(os.getenv("PERMISSION_MODE", "auto")),
            bot_lang=(os.getenv("BOT_LANG", "ru").strip().lower() or "ru"),
            idle_timeout_h=float(os.getenv("IDLE_TIMEOUT_H", "6")),
            log_max_mb=float(os.getenv("LOG_MAX_MB", "10")),
            # Модель/effort по умолчанию для всех сессий. Не заданы — Claude
            # берёт свои дефолты (или то, что в профиле/проекте). /model на
            # сессию перекрывает DEFAULT_MODEL.
            default_model=(raw.strip() or None) if (raw := os.getenv("DEFAULT_MODEL", "")).strip() else None,
            default_effort=(
                (raw.strip() or None) if (raw := os.getenv("DEFAULT_EFFORT", "")).strip() else None
            ),
            # CLAUDE_ENV_ANTHROPIC_BASE_URL=... → в процесс claude уйдёт
            # ANTHROPIC_BASE_URL=... (префикс снимается).
            claude_env={
                k[len("CLAUDE_ENV_"):]: v
                for k, v in os.environ.items()
                if k.startswith("CLAUDE_ENV_") and k != "CLAUDE_ENV_"
            },
            # Файловая песочница (bubblewrap). По умолчанию включена: процесс
            # claude и /bash видят только папку сессии/проекта и конфиг Claude
            # Code, всё остальное в $HOME и системе — недоступно. SANDBOX=off
            # отключает (нужно на машинах без bwrap/без unprivileged userns).
            sandbox=sandbox,
            sandbox_extra_rw=cls._parse_paths(os.getenv("SANDBOX_EXTRA_RW", "")),
            # mDNS/локальная сеть через system D-Bus. По умолчанию включено
            # (полезно: агент видит .local-хосты и сервисы), off — запретить
            # доступ к system D-Bus из песочницы.
            sandbox_dbus=cls._parse_bool(os.getenv("SANDBOX_DBUS", "true")),
            # X/Wayland в песочницу по умолчанию НЕ прокидываем (закрываем доступ
            # к хостовому GUI); SANDBOX_X11=1 — оставить, если модели нужен X.
            sandbox_x11=cls._parse_bool(os.getenv("SANDBOX_X11", "false")),
            agent_vm_memory_gib=(
                float(raw) if (raw := os.getenv("AGENT_VM_MEMORY_GIB", "").strip()) else None
            ),
            agent_vm_cpus=(
                int(raw) if (raw := os.getenv("AGENT_VM_CPUS", "").strip()) else None
            ),
            agent_vm_image=(os.getenv("AGENT_VM_IMAGE", "").strip() or None),
            wallet_secrets_file=Path(
                os.getenv(
                    "WALLET_SECRETS_FILE",
                    "~/.config/claude-orchestrator/secrets.toml",
                )
            ).expanduser(),
            wallet_guard=cls._parse_bool_default_on(os.getenv("WALLET_GUARD", "1")),
            automode_classify_all_shell=cls._parse_bool_default_on(
                os.getenv("AUTOMODE_CLASSIFY_ALL_SHELL", "1")
            ),
            wallet_policy_edit=cls._parse_bool_default_on(os.getenv("WALLET_POLICY_EDIT", "1")),
        )

    @staticmethod
    def _parse_adapters(raw: str) -> tuple[str, ...]:
        valid = {"telegram", "web"}
        names = [p.strip().lower() for p in raw.split(",") if p.strip()]
        bad = [n for n in names if n not in valid]
        if bad:
            raise SystemExit(
                f"ADAPTERS: неизвестные адаптеры {', '.join(bad)} — "
                f"допустимо: {', '.join(sorted(valid))}"
            )
        if not names:
            raise SystemExit("ADAPTERS пуст — нужен хотя бы один адаптер (telegram, web)")
        return tuple(dict.fromkeys(names))  # без дублей, порядок сохранён

    @staticmethod
    def _parse_modules(raw: str) -> tuple[str, ...]:
        valid = {"wallet"}
        names = [p.strip().lower() for p in raw.split(",") if p.strip()]
        bad = [n for n in names if n not in valid]
        if bad:
            raise SystemExit(
                f"MODULES: неизвестные модули {', '.join(bad)} — "
                f"допустимо: {', '.join(sorted(valid))}"
            )
        return tuple(dict.fromkeys(names))

    @classmethod
    def _default_modules(cls, raw: str | None, sandbox: str) -> tuple[str, ...]:
        """Набор модулей. MODULES не задан (`None`) → кошелёк включён по умолчанию
        при песочнице bwrap: работа с хостовыми секретами (gh/git/ssh как на
        хосте) без выдачи их значений модели — безопасно. Без bwrap кошелёк не
        страхует, поэтому не тащим. Явный MODULES (в т.ч. пустой) уважаем как есть."""
        if raw is None:
            return ("wallet",) if sandbox == "bwrap" else ()
        return cls._parse_modules(raw)

    @staticmethod
    def _parse_sandbox(raw: str) -> str:
        mode = raw.strip().lower() or "bwrap"
        # Синонимы «выключено».
        if mode in ("off", "none", "0", "false", "no"):
            return "off"
        if mode in ("bwrap", "agent-vm"):
            return mode
        raise SystemExit(f"SANDBOX={raw!r} — допустимо: bwrap | agent-vm | off")

    @staticmethod
    def _parse_paths(raw: str) -> tuple[Path, ...]:
        """Список путей из PATH-подобной строки (разделитель ':').

        Широкий RW-путь (дом/корень) молча разворачивает песочницу — ругаемся
        в лог, чтобы оператор не открыл наружу больше, чем хотел."""
        out: list[Path] = []
        home = Path.home()
        for part in raw.split(":"):
            part = part.strip()
            if not part:
                continue
            p = Path(part).expanduser()
            out.append(p)
            try:
                rp = p.resolve()
            except OSError:
                continue
            if rp == home or rp in home.parents:
                logger.warning(
                    "SANDBOX_EXTRA_RW открывает широкий путь %s (дом/корень) — "
                    "песочница фактически разворачивается. Указывай узкие каталоги.",
                    p,
                )
        return tuple(out)

    @staticmethod
    def _parse_chat_id(raw: str) -> int | None:
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            raise SystemExit(
                f"TELEGRAM_CHAT_ID={raw!r} — должно быть целое число (ID группы). "
                "Узнать: добавь бота в группу и пошли /chat_id."
            )

    @staticmethod
    def _parse_permission_mode(raw: str) -> str:
        mode = raw.strip()
        # По `claude --help`: acceptEdits, auto, bypassPermissions, manual,
        # dontAsk, plan. "bypass" — наш синоним --dangerously-skip-permissions.
        valid = {"bypass", "auto", "acceptEdits", "manual", "dontAsk", "plan", "default"}
        if mode not in valid:
            raise SystemExit(
                f"PERMISSION_MODE={mode!r} — допустимые значения: {', '.join(sorted(valid))}"
            )
        return mode

    @staticmethod
    def _parse_bool(raw: str) -> bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _parse_bool_default_on(raw: str) -> bool:
        """Как `_parse_bool`, но fail-safe: ЛЮБОЕ незнакомое значение → True.

        Для security-флагов (wallet_guard, automode_classify_all_shell,
        wallet_policy_edit), где опечатка/мусор в env должны оставлять защиту
        ВКЛючённой, а не молча снимать её (в отличие от `_parse_bool`, где
        незнакомое = False)."""
        return raw.strip().lower() not in ("0", "false", "no", "off")

    @staticmethod
    def _parse_user_ids(raw: str) -> frozenset[int]:
        ids: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                logger.warning("ALLOWED_USER_IDS: пропущено некорректное значение %r", part)
        return frozenset(ids)
