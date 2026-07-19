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
    # Раннер agent-vm (SANDBOX=agent-vm): ресурсы и пин образа гостя.
    agent_vm_memory_gib: float | None
    agent_vm_cpus: int | None
    agent_vm_image: str | None
    # Кошелёк секретов (MODULES=wallet): файл секретов и политик (0600, вне
    # allowlist песочницы).
    wallet_secrets_file: Path

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

        return cls(
            adapters=adapters,
            modules=cls._parse_modules(os.getenv("MODULES", "")),
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
            sandbox=cls._parse_sandbox(os.getenv("SANDBOX", "bwrap")),
            sandbox_extra_rw=cls._parse_paths(os.getenv("SANDBOX_EXTRA_RW", "")),
            # mDNS/локальная сеть через system D-Bus. По умолчанию включено
            # (полезно: агент видит .local-хосты и сервисы), off — запретить
            # доступ к system D-Bus из песочницы.
            sandbox_dbus=cls._parse_bool(os.getenv("SANDBOX_DBUS", "true")),
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
        """Список путей из PATH-подобной строки (разделитель ':')."""
        out: list[Path] = []
        for part in raw.split(":"):
            part = part.strip()
            if part:
                out.append(Path(part).expanduser())
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
