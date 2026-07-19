"""OrchestratorCore — транспорт-независимое ядро оркестратора.

Владеет всем, что не зависит от конкретного мессенджера/интерфейса:
  * командной логикой (создание/остановка/удаление сессий, /stats, /model…);
  * маршрутизацией ответов Claude (handle_reply/tool/stop/permission из
    reply_server) во ВСЕ активные адаптеры;
  * статус-баблом (bubble.py) и фоновыми задачами хода (turn.py);
  * jail'ом на отправку файлов и bash-терминалом;
  * журналом событий сессии (история для веб-интерфейса).

Адаптеры (orchestrator/adapters/*) реализуют Transport (core/transport.py):
принимают команды пользователя и вызывают методы ядра; ядро доставляет
исходящее во все адаптеры разом. Происхождение сообщения (Origin) ездит через
Claude как context_id = "<адаптер>:<имя-сессии>:<токен-адаптера>" — по нему
ядро находит сессию и отдаёт origin-адаптеру возможность ответить цитатой.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

from .bashshell import BashShellManager, clean as bash_clean
from .bubble import BubbleManager
from .sessions import Session, SessionError, SessionManager
from .slug import slugify
from .texts import get_texts
from .toolline import AGENT_SPAWN_TOOLS, shorten, tool_line
from .transport import Origin, PermissionRequest, Transport
from .turn import TurnSupervisor
from ..config import Config

logger = logging.getLogger(__name__)

# Лимит превью команды в запросе разрешения: показываем почти полностью,
# чтобы можно было прочитать и принять осмысленное решение.
PERM_PREVIEW_LIMIT = 3500

# /bash: как часто перечитывать вывод и перерисовывать статус, сколько ждать
# команду и сколько текста показывать.
BASH_POLL_INTERVAL = 1.5
BASH_TIMEOUT = 600.0
BASH_OUTPUT_LIMIT = 3500

# Окно контекста для процента в /stats. Захардкожено грубо: у моделей с
# 1M-окном цифра будет занижать реальный запас — это ориентир, не факт.
CONTEXT_WINDOW = 200_000

# Синонимы моделей для кнопок /model. Маппинг на конкретные версии делает
# сам Claude Code — мы не дублируем его каталог и не отстаём от переименований.
MODEL_ALIASES = ["fable", "opus", "sonnet", "haiku"]

# Сколько последних событий сессии держать в журнале (история веб-интерфейса).
HISTORY_LIMIT = 300


class UserError(Exception):
    """Ошибка с готовым текстом для пользователя (адаптер показывает как есть)."""


class OrchestratorCore:
    def __init__(self, config: Config, manager: SessionManager):
        self.config = config
        self.manager = manager
        self._texts = get_texts(config.bot_lang)
        self.adapters: dict[str, Transport] = {}
        self.modules: list = []  # модули (modules/*) — start/stop вместе с ядром
        self.bubbles = BubbleManager(
            self._transports, manager.get, self.t, config.delete_bubble,
            unblock_available=self._unblock_available,
            persist_path=config.sessions_dir / ".live_bubbles.json",
        )
        # Последний значимый тул на сессию — для детекта состояния «ждёт
        # фоновую задачу» (TaskOutput) и активности кнопки ⏬.
        self._last_tool: dict[str, str] = {}
        # Фоновые задачи хода (typing/watchdog/error-relay) и Stop-гейт —
        # единым владельцем (turn.py). Доставка — колбэками в адаптеры.
        self.turns = TurnSupervisor(
            manager, self.t, self.notice, self._typing_any, self.bubbles.set_status
        )
        # Постоянные bash-терминалы (мимо Claude Code): ключ — см. bash_key.
        self.bash = BashShellManager()
        # Журнал событий per-сессия: показать историю в веб-интерфейсе после
        # перезагрузки страницы. Персистится через graceful-рестарт (иначе
        # пропадала при каждом рестарте — «история потерялась»); полная — в
        # транскрипте CC.
        self._history: dict[str, deque] = {}
        self._history_path = config.sessions_dir / ".history.json"
        self._load_history()
        # Ожидающие permission-запросы: (имя сессии, request_id) — от повторного
        # вердикта из второго адаптера (применяется первый ответ).
        self._pending_perms: set[tuple[str, str]] = set()
        # Локальные подтверждения (request_confirmation): вердикт остаётся в
        # ядре (Future), а не уходит в Claude Code. Используют модули (wallet).
        self._local_perms: dict[tuple[str, str], asyncio.Future] = {}
        # Хуки «сессия создана» — модули дописывают свою обвязку в папку сессии.
        self.session_hooks: list[Callable[[Session], Awaitable[None]]] = []
        manager.on_dead = self.notify_session_dead

    def t(self, key: str, **kwargs) -> str:
        return self._texts[key].format(**kwargs)

    # ── адаптеры и модули ───────────────────────────────────────

    def register_adapter(self, transport: Transport) -> None:
        self.adapters[transport.name] = transport

    def _transports(self) -> list[Transport]:
        return list(self.adapters.values())

    async def start(self) -> None:
        for tr in self._transports():
            await tr.start()
        for mod in self.modules:
            await mod.start(self)

    async def cleanup_stale_bubbles(self) -> None:
        """Убрать сообщения-баблы, осиротевшие при НЕ-graceful смерти прошлого
        процесса (краш/SIGKILL — close_all не отработал). refs персистятся в
        .live_bubbles.json; читаем, удаляем через адаптеры, чистим файл.
        Вызывать на старте ПОСЛЕ подъёма адаптеров и load_state сессий."""
        path = self.config.sessions_dir / ".live_bubbles.json"
        try:
            entries = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        adapters = {tr.name: tr for tr in self._transports()}
        removed = 0
        for e in entries:
            session = self.manager.get(str(e.get("session", "")))
            tr = adapters.get(str(e.get("adapter", "")))
            ref = e.get("ref")
            if session is None or tr is None or ref is None:
                continue
            try:
                await tr.bubble_finish(session, str(ref), delete=True)
                removed += 1
            except Exception as ex:
                logger.debug("cleanup сироты бабла %s: %s", e.get("session"), ex)
        if removed:
            logger.info("Убрал осиротевших баблов с прошлого запуска: %d", removed)
        try:
            path.unlink()
        except OSError:
            pass

    async def close(self) -> None:
        for mod in self.modules:
            try:
                await mod.stop()
            except Exception:
                logger.exception("Модуль %s: ошибка остановки", getattr(mod, "name", mod))
        # Прибираем постоянные bash-оболочки, иначе они осиротеют и переживут
        # процесс. В потоке — proc.wait(timeout) блокирующий.
        await asyncio.to_thread(self.bash.close_all)
        for tr in self._transports():
            try:
                await tr.stop()
            except Exception:
                logger.exception("Адаптер %s: ошибка остановки", tr.name)

    # ── журнал событий (история веб-интерфейса) ─────────────────

    def _record(self, session: Session, kind: str, **payload) -> None:
        log = self._history.setdefault(session.name, deque(maxlen=HISTORY_LIMIT))
        log.append({"ts": time.time(), "kind": kind, **payload})

    def history(self, name: str) -> list[dict]:
        return list(self._history.get(name, ()))

    def _load_history(self) -> None:
        """Восстановить журнал событий с прошлого запуска (веб-история переживает
        graceful-рестарт). Битый/отсутствующий файл — просто пустая история."""
        try:
            data = json.loads(self._history_path.read_text())
        except (OSError, ValueError):
            return
        for name, events in (data or {}).items():
            if isinstance(events, list):
                self._history[name] = deque(events, maxlen=HISTORY_LIMIT)

    def save_history(self) -> None:
        """Сохранить журнал на диск (вызывать при graceful-остановке). Атомарно."""
        try:
            data = {name: list(dq) for name, dq in self._history.items() if dq}
            tmp = self._history_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            os.replace(tmp, self._history_path)
        except OSError as e:
            logger.debug("save_history: %s", e)

    # ── доставка во все адаптеры ────────────────────────────────

    async def notice(self, session: Session | None, text: str) -> None:
        """Служебное сообщение (сторож, релей ошибок, смерть сессии…)."""
        if session is not None:
            self._record(session, "notice", text=text)
        for tr in self._transports():
            try:
                await tr.notify(session, text)
            except Exception as e:
                logger.warning("notify через %s: %s", tr.name, e)

    async def _typing_any(self, session: Session) -> bool:
        alive = False
        for tr in self._transports():
            try:
                alive = await tr.typing(session) or alive
            except Exception as e:
                logger.debug("typing через %s: %s", tr.name, e)
        return alive

    async def _deliver_text(
        self, session: Session, text: str, origin: Origin | None, intermediate: bool
    ) -> None:
        self._record(
            session, "intermediate" if intermediate else "reply", text=text
        )
        for tr in self._transports():
            try:
                await tr.deliver_text(
                    session, text,
                    origin=origin if origin and origin.adapter == tr.name else None,
                    intermediate=intermediate,
                )
            except Exception as e:
                logger.warning("Доставка ответа через %s: %s", tr.name, e)

    # ── context_id: происхождение сообщений ─────────────────────

    @staticmethod
    def context_id(session: Session, origin: Origin) -> str:
        return f"{origin.adapter}:{session.name}:{origin.token}"

    def _parse_context(self, context_id: str) -> tuple[Session | None, Origin | None]:
        """context_id = <адаптер>:<имя-сессии>:<токен>. Кривой — drop, а не
        «дефолт куда-нибудь»: иначе баг канала мог бы вбросить ответ не туда."""
        parts = context_id.split(":", 2)
        if len(parts) == 3:
            adapter, sname, token = parts
            session = self.manager.get(sname)
            if session is not None:
                origin = Origin(adapter, token) if adapter in self.adapters else None
                return session, origin
        logger.warning("Некорректный context_id (игнорирую): %r", context_id)
        return None, None

    # ── статусы и справочная информация ─────────────────────────

    def session_status(self, session: Session) -> str:
        """stopped | working | waiting — общий словарь статусов для адаптеров."""
        if not session.running:
            return "stopped"
        if self.bubbles.has(session.name):
            return "working"
        return "waiting"

    # ── команды: жизненный цикл сессий ──────────────────────────

    async def create_session(self, title: str, project_path: str | None = None) -> Session:
        """Создать сессию и привязать её ко всем адаптерам (топик и т.п.).

        Если адаптер, которому поверхность обязательна (requires_binding —
        Telegram), не смог привязаться, сессия откатывается: «сессия-призрак»
        без интерфейса заняла бы слот/порт и держала бы процесс claude, куда
        никто не может писать. Адаптеры без привязки (веб) на None не влияют.
        """
        # Пре-чек с локализованными сообщениями: manager.create бросает
        # SessionError с русским текстом (гонка-защита под локом), который при
        # BOT_LANG=en дал бы смесь языков. Здесь — на языке бота.
        slug = slugify(title)
        if self.manager.has_name(slug):
            raise UserError(self.t("name_exists", name=slug))
        if self.manager.count() >= self.config.max_instances:
            raise UserError(self.t("limit_reached", limit=self.config.max_instances))
        try:
            session = await self.manager.create(title, project_path)
        except SessionError as e:
            raise UserError(str(e)) from e
        for tr in self._transports():
            try:
                address = await tr.bind_session(session)
            except Exception as e:
                logger.exception("Адаптер %s: bind_session(%s)", tr.name, session.name)
                if getattr(tr, "requires_binding", False):
                    await self._rollback_session(session)
                    raise UserError(self.t("bind_fail", adapter=tr.name, error=e)) from e
                address = None
            if address is None and getattr(tr, "requires_binding", False):
                await self._rollback_session(session)
                raise UserError(self.t("bind_none", adapter=tr.name))
            if address is not None:
                session.bindings[tr.name] = address
        self.manager.save_state()
        for hook in self.session_hooks:
            try:
                await hook(session)
            except Exception:
                logger.exception("session_hook для %s", session.name)
        return session

    async def _rollback_session(self, session: Session) -> None:
        """Снести только что созданную сессию (привязка не удалась): гасим
        процесс, освобождаем слот/порт, убираем уже сделанные привязки."""
        bindings = dict(session.bindings)
        await self.manager.delete(session)
        for tr in self._transports():
            address = bindings.get(tr.name)
            if address is not None:
                try:
                    await tr.unbind_session(session, address)
                except Exception:
                    logger.exception("Адаптер %s: unbind при откате(%s)", tr.name, session.name)

    async def close_session(self, session: Session) -> None:
        self.turns.stop(session.name)
        self._drop_pending_perms(session)
        await asyncio.to_thread(self.bash.close_for_session, session.name)
        await self.bubbles.close(session.name)
        await self.manager.close(session)
        self._record(session, "status", status="stopped")

    async def delete_session(self, session: Session) -> None:
        self.turns.forget(session.name)
        self._drop_pending_perms(session)
        await asyncio.to_thread(self.bash.close_for_session, session.name)
        await self.bubbles.close(session.name)
        bindings = dict(session.bindings)
        await self.manager.delete(session)
        self._history.pop(session.name, None)
        for tr in self._transports():
            address = bindings.get(tr.name)
            if address is None:
                continue
            try:
                await tr.unbind_session(session, address)
            except Exception:
                logger.exception("Адаптер %s: unbind_session(%s)", tr.name, session.name)

    async def clear_session(self, session: Session) -> None:
        """Чистый контекст: перезапуск Claude с новым UUID, привязки остаются."""
        # Гасим фоновые задачи прошлого хода (typing/watchdog/error-relay) —
        # иначе после /clear «печатает…» крутится вечно на уже пустой сессии,
        # а error-relay тайлит лог нового процесса (как close/switch_model).
        self.turns.stop(session.name)
        self._drop_pending_perms(session)
        await self.bubbles.close(session.name)
        try:
            await self.manager.clear(session)
        except Exception as e:
            logger.exception("Сессия %s: ошибка /clear", session.name)
            await self.manager.close(session)
            raise UserError(self.t("clear_fail", error=e)) from e

    async def switch_model(self, session: Session, model: str) -> bool:
        """Сменить модель; вернуть resumed (контекст продолжен?)."""
        self.turns.stop(session.name)
        self._drop_pending_perms(session)
        await self.bubbles.close(session.name)
        try:
            return await self.manager.set_model(session, model)
        except Exception as e:
            logger.exception("Сессия %s: ошибка смены модели", session.name)
            raise UserError(self.t("model_fail", model=model, error=e)) from e

    async def ensure_running(self, session: Session) -> str:
        """Возобновить остановленную сессию. Возвращает running|resumed|fresh."""
        if session.running:
            return "running"
        try:
            resumed = await self.manager.resume(session)
        except SessionError as e:
            raise UserError(self.t("resume_fail", error=e)) from e
        return "resumed" if resumed else "fresh"

    # ── сообщения пользователя ──────────────────────────────────

    async def user_message(self, session: Session, text: str, origin: Origin) -> None:
        """Переслать сообщение пользователя в Claude (сессия уже запущена).

        Если Клод ещё не ответил на предыдущее, а пользователь уже шлёт
        следующее — старый бабл замораживается на месте, новый открывается
        независимо (см. bubble.freeze_and_open).
        """
        await self.bubbles.freeze_and_open(session.name)
        try:
            await self.manager.send_to_claude(
                session, text, self.context_id(session, origin)
            )
        except Exception as e:
            logger.error("Сессия %s: не удалось передать сообщение: %s", session.name, e)
            # Бабл уже открыт (freeze_and_open) — закрываем, иначе сессия
            # навсегда числится «working» (session_status по bubbles.has),
            # а _active копит поздние хук-события в бабл, который никто не
            # закроет.
            await self.bubbles.close(session.name)
            raise UserError(self.t("forward_fail", error=e)) from e
        self._record(session, "user", text=text, via=origin.adapter)
        self._last_tool.pop(session.name, None)  # новый ход — сброс bg-состояния
        snippet = html.escape(shorten(text, 28))
        await self.bubbles.append(session.name, f"📨 {snippet}")
        self.turns.start(session.name)

    async def request_report(self, session: Session, origin: Origin) -> None:
        """Запрос статус-отчёта (кнопка 📋): push-сообщение модели «отчитайся и
        продолжай» (не останавливаться). Модель прочитает, когда доберётся.
        Настоящее прерывание хода — hard_stop (Esc в PTY, кнопка ⛔)."""
        await self.manager.send_to_claude(
            session, self.t("stop_message"), self.context_id(session, origin)
        )
        await self.bubbles.append(session.name, self.t("bubble_stop_requested"))

    async def hard_stop(self, session: Session) -> None:
        """Жёсткое прерывание хода: Esc в PTY (см. sessions.interrupt_turn).

        Ход обрывается сразу, финального ответа не будет — гасим сторожей и
        бабл здесь же, иначе индикаторы висели бы до таймаута.
        """
        try:
            self.manager.interrupt_turn(session)
        except SessionError as e:
            raise UserError(str(e)) from e
        self.turns.stop(session.name)
        await self.bubbles.close(session.name)
        self._record(session, "status", status="interrupted")
        await self.notice(session, self.t("esc_done"))

    def unblock_action(self, name: str) -> str | None:
        """Что сделает кнопка ⏭ сейчас: "kick" (Esc — прервать ожидание фона),
        "background" (Ctrl+B — свернуть идущую задачу) или None (нечего)."""
        session = self.manager.get(name)
        if session is None:
            return None
        if self._last_tool.get(name) == "TaskOutput":
            return "kick"
        if self.manager.is_busy(session):
            return "background"
        return None

    async def unblock(self, session: Session) -> None:
        """Разблокировать ввод модели, НЕ прерывая ход насмерть (для этого —
        hard_stop). Контекстно (см. unblock_action):
          * модель ждёт фоновую задачу (TaskOutput) → Esc прерывает именно
            ожидание, модель принимает новый ввод;
          * идёт долгая foreground-команда → Ctrl+B сворачивает её в фон.
        Оба случая освобождают ввод. Бабл/сторожей не гасим — ход живой."""
        action = self.unblock_action(session.name)
        try:
            if action == "kick":
                self.manager.interrupt_turn(session)  # Esc — пинок ожидания
                await self.bubbles.append(session.name, self.t("bubble_kicked"))
            else:
                # background или "нечего" — Ctrl+B безвреден как no-op.
                self.manager.background_turn(session)  # Ctrl+B — в фон
                await self.bubbles.append(session.name, self.t("bubble_backgrounded"))
        except SessionError as e:
            raise UserError(str(e)) from e

    async def slash_command(self, session: Session, cmd: str) -> None:
        """Неизвестные /команды — прямо в терминал Claude (команды Claude Code)."""
        try:
            self.manager.type_into_pty(session, cmd)
        except SessionError as e:
            raise UserError(self.t("send_fail", error=e)) from e

    async def compact(self, session: Session) -> None:
        try:
            self.manager.type_into_pty(session, "/compact")
        except SessionError as e:
            raise UserError(self.t("send_fail", error=e)) from e

    # ── ответы Claude (вызывается reply_server'ом) ──────────────

    async def handle_reply(self, data: dict) -> None:
        """Текстовый ответ или файл от Claude (тул reply_to_user)."""
        session, origin = self._parse_context(str(data.get("context_id", "")))
        if session is None:
            return
        self.manager.touch(session)  # ответ/файл = активность (таймер простоя)

        if data.get("file_path"):
            self.turns.stop(session.name)
            await self._send_file(
                session, str(data["file_path"]), str(data.get("caption", "")), origin
            )
            return

        text = str(data.get("text", ""))
        complete = bool(data.get("complete", False))
        logger.info(
            "reply сессия=%s complete=%s len=%d", session.name, complete, len(text)
        )

        if not complete:
            # Промежуточный ответ Клода — отдельным полным сообщением (💬),
            # а не обрезанной строчкой в бабле: важно видеть, что он пишет,
            # пока работает дальше.
            if text:
                await self._deliver_text(session, text, origin, intermediate=True)
                # Бабл, если он был, «застрял» бы ВЫШЕ этого 💬 (у него свой
                # message_id). Замораживаем его на месте и открываем новый —
                # дальнейшие события хода пойдут ПОД ответом модели, как бабл
                # шёл под сообщением пользователя (freeze_and_open, линейная
                # история без прыжков). Только если бабл активен.
                if self.bubbles.has(session.name):
                    await self.bubbles.freeze_and_open(session.name)
            return

        # Финал (даже с пустым текстом): гасим typing и бабл, чтобы индикатор
        # не крутился вечно. Сначала сообщение, потом чистка бабла — окна
        # «ответа ещё нет» не остаётся.
        self.turns.stop(session.name)
        self._last_tool.pop(session.name, None)  # ход завершён — сброс bg-состояния
        if text:
            await self._deliver_text(session, text, origin, intermediate=False)
        await self.bubbles.close(session.name)

    def _sendfile_roots(self, session: Session) -> list[Path]:
        """Рабочие папки, откуда Клоду разрешено отправлять файлы в чат:
        cwd проекта (или папка сессии), сама папка сессии и incoming-каталог.
        """
        return [
            self.manager.effective_cwd(session),
            session.session_dir,
            self.incoming_dir(session),
        ]

    def incoming_dir(self, session: Session) -> Path:
        """Каталог для присланных файлов: INCOMING_DIR относительный — внутри
        папки сессии, абсолютный — общий. Единый источник правды для обоих
        адаптеров и jail'а send_file (иначе куда кладут ≠ что отдают)."""
        inc = Path(self.config.incoming_dir).expanduser()
        if not inc.is_absolute():
            inc = session.session_dir / inc
        return inc

    def path_in_workspace(self, path: Path, session: Session) -> bool:
        """Лежит ли path (после resolve, со симлинками) внутри одной из рабочих
        папок сессии. Ошибки resolve/stat (битая симссылка, нет прав) → False:
        лучше отказать, чем вынести файл за пределами workspace."""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for root in self._sendfile_roots(session):
            try:
                if resolved.is_relative_to(root.resolve()):
                    return True
            except (OSError, ValueError):
                continue
        return False

    async def send_log(self, session: Session, origin: Origin | None = None) -> None:
        """Отправить полный claude.log сессии файлом — для отладки (формат Claude
        Code сменился, парсер молчит и т.п.). Лог в session_dir (внутри jail),
        доставляется документом во все адаптеры; если файла нет — notice."""
        log = session.session_dir / "claude.log"
        await self._send_file(
            session, str(log), self.t("log_caption", name=session.title), origin
        )

    async def _send_file(
        self, session: Session, file_path: str, caption: str, origin: Origin | None
    ) -> None:
        path = Path(file_path).expanduser()
        # Jail: только внутри рабочих папок сессии. Без этого промпт-инъекция
        # из чужого файла/CLAUDE.md могла бы заставить Клода вызвать
        # send_file_to_user на ~/.ssh/id_rsa и выслать секреты в чат.
        # resolve() раскрывает симлинки — escape через ссылку тоже отсекается.
        if not self.path_in_workspace(path, session):
            logger.warning("send_file отклонён вне workspace: %s", path)
            await self.notice(session, self.t("sendfile_denied", path=path))
            return
        if not path.is_file():
            await self.notice(session, self.t("sendfile_not_found", path=path))
            return
        self._record(session, "file", path=str(path), caption=caption)
        for tr in self._transports():
            try:
                await tr.deliver_file(
                    session, path, caption,
                    origin=origin if origin and origin.adapter == tr.name else None,
                )
            except Exception as e:
                logger.warning("Доставка файла через %s: %s", tr.name, e)

    # ── события хуков Claude Code ───────────────────────────────

    async def handle_tool_event(self, session_name: str, payload: dict) -> None:
        """PreToolUse-хук: вызов инструмента → строка в статус-бабле."""
        session = self.manager.get(session_name)
        if session is None:
            return
        tool = str(payload.get("tool_name") or "?")
        # Наш канальный тул — mcp__channel-<slug>__reply_to_user; сверяем по
        # ХВОСТУ имени, а не подстрокой: `"reply_to_user" in tool` ложно
        # срабатывало бы на чужом MCP-туле (mcp__notes__draft_reply_to_user),
        # ставя reply-флаг и глуша Stop-фолбэк «потерянного финала».
        is_reply = tool.endswith("__reply_to_user")
        is_file = tool.endswith("__send_file_to_user")
        # reply_to_user — единственное действие, после которого «до Stop больше
        # ничего не потерять» истинно; ЛЮБОЙ другой тул (в т.ч. send_file_to_user)
        # сбрасывает флаг — см. turn.TurnSupervisor.
        self.turns.note_tool(session.name, is_reply)
        if is_reply:
            return  # результат и так придёт сообщением — в бабле это шум
        if is_file:
            return  # тоже придёт сообщением; не считается «текстовым» ответом
        # Запоминаем последний значимый тул: TaskOutput = модель ждёт фоновую
        # задачу (заглушка-ожидание в TUI), в этом состоянии кнопка ⏬ неактивна
        # (сворачивать нечего — уже в фоне), а ⛔ прерывает именно ожидание.
        self._last_tool[session.name] = tool
        tool_input = payload.get("tool_input") or {}
        if tool == "TaskOutput":
            # Явно показываем «ждёт фон» — иначе для пользователя это выглядит
            # как молчание (эксперимент подтвердил: Bash run_in_background →
            # TaskOutput). Строка не схлопывается (tool=None).
            await self.bubbles.append(session.name, self.t("bubble_waiting_bg"))
            return
        # agent_id/agent_type — на каждом тул-вызове ВНУТРИ сабагента.
        agent_id = payload.get("agent_id")
        # Спавн сабагента (описание всегда разное) и TodoWrite (состояние
        # тудушки) не схлопываем; остальные — по (tool, agent_id).
        collapsible = tool not in AGENT_SPAWN_TOOLS and tool != "TodoWrite"
        await self.bubbles.append(
            session.name,
            tool_line(tool, tool_input, self.t),
            agent_id=str(agent_id) if agent_id else None,
            tool=tool if collapsible else None,
        )

    def _unblock_available(self, name: str) -> bool:
        """Есть ли что разблокировать (для активности кнопки ⏭): ждёт фон
        (Esc) или идёт команда (Ctrl+B). В покое — нечего."""
        return self.unblock_action(name) is not None

    async def handle_stop_event(self, session_name: str, payload: dict) -> None:
        """Stop-хук — конец хода Claude Code.

        Фолбэк против «потерянного финала»: если последним действием модели
        перед этим Stop был НЕ reply_to_user, а last_assistant_message непустой —
        этот текст канал не ретранслировал и он не долетел до пользователя.
        Шлём его сами.

        Не закрывает бабл/typing: Stop не означает «ход окончательно завершён» —
        часто это пауза перед автопродолжением (ждём CI, фоновый шелл).
        """
        session = self.manager.get(session_name)
        if session is None:
            return
        if self.turns.pop_reply_flag(session.name):
            return
        text = str(payload.get("last_assistant_message") or "").strip()
        if not text:
            return
        await self._deliver_text(session, text, None, intermediate=False)

    # ── permission relay ────────────────────────────────────────

    async def handle_permission_request(self, session_name: str, payload: dict) -> None:
        """Запрос разрешения — кнопками во все адаптеры; применяется первый ответ
        (параллельно остаётся открытым и локальный TUI-диалог)."""
        session = self.manager.get(session_name)
        if session is None:
            return
        # Диагностика: какие поля несёт запрос разрешения (ищем причину/вердикт
        # судьи, чтобы показать её пользователю). Временный INFO-лог.
        logger.info(
            "perm payload keys=%s reason-ish=%s",
            sorted(payload.keys()),
            {k: payload[k] for k in payload
             if any(w in k.lower() for w in
                    ("reason", "suggest", "rule", "denial", "message", "explan", "classif"))},
        )
        raw_preview = str(payload.get("input_preview", ""))
        if len(raw_preview) > PERM_PREVIEW_LIMIT:
            raw_preview = raw_preview[:PERM_PREVIEW_LIMIT] + " …(обрезано)"
        request = PermissionRequest(
            request_id=str(payload.get("request_id", "")),
            tool=str(payload.get("tool_name", "?")),
            description=str(payload.get("description", "")),
            preview=raw_preview,
        )
        self._pending_perms.add((session.name, request.request_id))
        self._record(
            session, "perm_request",
            request_id=request.request_id, tool=request.tool,
            description=request.description, preview=request.preview,
        )
        for tr in self._transports():
            try:
                await tr.permission_prompt(session, request)
            except Exception:
                logger.exception("permission_prompt через %s", tr.name)

    async def request_confirmation(
        self,
        session: Session,
        tool: str,
        description: str,
        preview: str,
        timeout: float = 300.0,
    ) -> bool:
        """Спросить пользователя «разрешить?» кнопками во всех адаптерах и
        дождаться ответа (для модулей: wallet и т.п. — вердикт остаётся в ядре,
        в Claude Code не уходит). Таймаут/ошибка = отказ (deny по умолчанию)."""
        request_id = f"local-{uuid.uuid4().hex[:12]}"
        key = (session.name, request_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._local_perms[key] = fut
        request = PermissionRequest(
            request_id=request_id, tool=tool, description=description,
            preview=preview[:PERM_PREVIEW_LIMIT],
        )
        self._record(
            session, "perm_request",
            request_id=request_id, tool=tool, description=description,
            preview=request.preview,
        )
        for tr in self._transports():
            try:
                await tr.permission_prompt(session, request)
            except Exception:
                logger.exception("permission_prompt (local) через %s", tr.name)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # Истёк без ответа: гасим кнопки во всех адаптерах, иначе они
            # висят вечно, а поздний клик потом молча проваливается.
            await self._broadcast_perm_resolved(session, request_id, "deny", "timeout")
            return False
        finally:
            self._local_perms.pop(key, None)

    async def _broadcast_perm_resolved(
        self, session: Session, request_id: str, behavior: str, via: str
    ) -> None:
        self._record(session, "perm_resolved", request_id=request_id, behavior=behavior)
        for tr in self._transports():
            try:
                await tr.permission_resolved(session, request_id, behavior, via)
            except Exception:
                logger.exception("permission_resolved через %s", tr.name)

    async def permission_verdict(
        self, session: Session, request_id: str, behavior: str, via: str
    ) -> bool:
        """Вердикт из адаптера via. False — запрос уже разрешён/снят (адаптеру
        стоит просто убрать кнопки: см. Transport.permission_resolved)."""
        key = (session.name, request_id)
        # Локальное подтверждение (request_confirmation): будим ожидающего,
        # в Claude Code ничего не шлём.
        local = self._local_perms.get(key)
        if local is not None:
            if local.done():
                return False  # уже отвечено/истекло — повторный клик игнорируем
            local.set_result(behavior == "allow")
            await self._broadcast_perm_resolved(session, request_id, behavior, via)
            return True
        if key not in self._pending_perms:
            return False
        try:
            await self.manager.send_permission(session, request_id, behavior)
        except Exception as e:
            logger.error("Сессия %s: не удалось передать вердикт: %s", session.name, e)
            raise UserError(self.t("perm_fail", error=e)) from e
        self._pending_perms.discard(key)
        await self._broadcast_perm_resolved(session, request_id, behavior, via)
        return True

    def _drop_pending_perms(self, session: Session) -> None:
        """Снять все ожидающие запросы разрешений сессии (close/clear/delete):
        иначе _pending_perms растёт вечно, а старая кнопка после resume била бы
        по чужому (новому) процессу с несуществующим request_id."""
        stale = [k for k in self._pending_perms if k[0] == session.name]
        for k in stale:
            self._pending_perms.discard(k)
        for k, fut in list(self._local_perms.items()):
            if k[0] == session.name and not fut.done():
                fut.set_result(False)  # разбудить ожидающего request_confirmation

    # ── статистика и справки ────────────────────────────────────

    def model_display(self, session: Session, stats: dict | None = None) -> str:
        """Имя модели для показа: реальная из транскрипта → установленная
        (алиас вроде opus) → «по умолчанию Claude Code».

        Без транскрипта (stats=None) читает его — блокирующее чтение, дёргать
        через asyncio.to_thread. Если stats уже есть — лишнего I/O нет.
        """
        model = (stats or self.manager.read_stats(session) or {}).get("model", "")
        return model or session.model or self.t("default_model")

    def stats_text(self, session: Session) -> str:
        """Блокирующее чтение транскрипта — вызывать через asyncio.to_thread."""
        stats = self.manager.read_stats(session)
        uptime = self.fmt_duration(time.time() - session.started_at)
        header = f"📊 {session.title}" + (
            "" if session.running else self.t("stats_stopped_suffix")
        )
        if stats is None:
            return self.t("stats_no_transcript", header=header, uptime=uptime)
        if stats.get("stale_schema"):
            # Транскрипт есть и валиден, но ни одного ожидаемого поля не
            # извлекли — вероятно, поменялась схема Claude Code. Показываем хвост
            # claude.log и зовём скачать полный лог (/log) для разработчиков.
            tail = self.manager.tail_log(session, 12)
            return self.t(
                "stats_stale_schema", header=header, uptime=uptime,
                tail=tail or self.t("log_empty"),
            )
        ctx = stats["context_tokens"]
        return self.t(
            "stats_body",
            header=header,
            model=self.model_display(session, stats),
            ctx=self.fmt_num(ctx),
            pct=f"{ctx / CONTEXT_WINDOW * 100:.0f}",
            out=self.fmt_num(stats["output_tokens"]),
            turns=stats["turns"],
            kb=f"{stats['transcript_bytes'] / 1024:.0f}",
            uptime=uptime,
        )

    async def usage_text(self, session: Session) -> str | None:
        """Расходы и лимиты плана: прогнать /cost в терминале Claude и разобрать.
        None — распарсить не удалось. Требует запущенной сессии (иначе
        type_into_pty бросит SessionError → UserError для адаптера)."""
        try:
            delta = await self.manager.run_and_capture(session, "/cost")
        except SessionError as e:
            raise UserError(self.t("send_fail", error=e)) from e
        data = self._parse_cost(delta)
        if not data:
            return None
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
        return "\n".join(lines)

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

    def collect_skills(self) -> list[tuple[str, str]]:
        """Скиллы профиля Claude Code (глобальные + плагины). Блокирующее I/O."""
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

    def ls_text(self, arg: str | None) -> str:
        """Листинг файлов для /ls (по умолчанию SESSIONS_DIR)."""
        target = self.config.sessions_dir
        if arg:
            target = Path(arg.strip()).expanduser().resolve()
        if not target.exists():
            return self.t("ls_not_exists", path=target)
        if not target.is_dir():
            return self.t("ls_file", path=target)
        try:
            entries = sorted(
                target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())
            )
        except OSError as e:
            return self.t("ls_no_access", error=e)
        lines = [f"📁 {target}"]
        if not entries:
            lines.append(self.t("ls_empty"))
        for entry in entries[:30]:
            icon = "📁" if entry.is_dir() else "📄"
            lines.append(f"{icon} {entry.name}{'/' if entry.is_dir() else ''}")
        if len(entries) > 30:
            lines.append(self.t("ls_more", n=len(entries) - 30))
        return "\n".join(lines)

    @staticmethod
    def parse_new_args(raw: str) -> tuple[str, str | None]:
        """Разобрать аргументы «новой сессии» → (отображаемое имя, путь-или-None).

        Поддерживает: имя с пробелами, обрамляющие кавычки, форму `/path`,
        форму `имя /path` (путь = токен, начинающийся с / или ~).
        """
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
            raw = raw[1:-1].strip()
        if not raw:
            return "", None

        def is_path(tok: str) -> bool:
            return tok.startswith("/") or tok.startswith("~")

        if is_path(raw):
            return Path(raw).name, raw
        tokens = raw.split()
        path_idx = next((i for i, tok in enumerate(tokens) if is_path(tok)), None)
        if path_idx is not None:
            project_path = " ".join(tokens[path_idx:])
            title = " ".join(tokens[:path_idx]) or Path(project_path).name
            return title, project_path
        return raw, None

    # ── /bash: постоянный терминал мимо Claude ──────────────────

    @staticmethod
    def bash_key(session: Session | None, scope: str) -> str:
        """Ключ bash-оболочки: у сессии — общий across-адаптеры не нужен,
        адаптер даёт свой scope (телеграм — топик, веб — вкладка)."""
        return f"s:{session.name}:{scope}" if session is not None else f"main:{scope}"

    def bash_cwd(self, session: Session | None) -> Path:
        """Стартовый cwd терминала: папка проекта сессии, иначе отдельный
        каталог для main-chat /bash.

        Для session=None НЕ отдаём весь SESSIONS_DIR: иначе его RW-бинд в
        песочнице открыл бы приватные дома всех сессий (.homes/*, ключи,
        ~/.wallet.json) и state-файл .sessions.json на запись. Выделяем
        нейтральный SESSIONS_DIR/.bash-main (пользователь при желании cd
        куда угодно — но по умолчанию не видит чужого).
        """
        if session is not None:
            return self.manager.effective_cwd(session)
        main = self.config.sessions_dir / ".bash-main"
        main.mkdir(parents=True, exist_ok=True)
        return main

    async def run_bash(
        self,
        key: str,
        session: Session | None,
        cmd: str,
        on_update: Callable[[str, bool], Awaitable[None]],
    ) -> None:
        """Выполнить команду в постоянном bash-терминале (та же песочница, что
        у claude). Стримит рендер (HTML) через on_update(html, done); в конце —
        код возврата. Если терминал занят — UserError.
        """
        cwd = self.bash_cwd(session)
        extra_rw = [cwd] + ([session.session_dir] if session is not None else [])
        wrapper = self.manager.sandbox_prefix(
            chdir=cwd, extra_rw=extra_rw, session=session
        )
        shell = self.bash.get_or_create(key, cwd, wrapper)
        if shell.busy:
            raise UserError(self.t("bash_busy"))
        shell.busy = True
        # Маркеры начала И конца: смещение по длине буфера ненадёжно —
        # BashSession тримит буфер до _BUF_CAP, и после переполнения
        # len(snapshot) залипает на пределе, snapshot()[start:] становится
        # пустым, а маркер конца никогда не находится (ложный таймаут на
        # каждой команде). Ищем вывод МЕЖДУ маркерами в полном снепшоте —
        # устойчиво к тримингу, пока сам вывод команды влезает в буфер.
        token = uuid.uuid4().hex
        start_marker = f"__BEG_{token}__"
        done_marker = f"__DONE_{token}__"
        interrupted = False
        try:
            # $? сразу за меткой — код возврата именно команды пользователя.
            shell.write(f"echo {start_marker}\n{cmd}\necho {done_marker} $?\n")
            out = b""
            code = None
            deadline = asyncio.get_running_loop().time() + BASH_TIMEOUT
            last_shown = ""
            beg_re = re.escape(start_marker).encode()
            done_re = re.escape(done_marker).encode() + rb"\s+(\d+)"
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(BASH_POLL_INTERVAL)
                raw = bash_clean(shell.snapshot())
                # Берём последнее вхождение маркера начала (эхо команды выше
                # тоже содержит его текст) и всё, что после.
                begs = list(re.finditer(beg_re, raw))
                region = raw[begs[-1].end():] if begs else b""
                out = region
                m = re.search(done_re, region)
                if m:
                    code = m.group(1).decode()
                    out = region[: m.start()]
                # Вымарываем эхо самих echo-маркеров из показа.
                out = b"\n".join(
                    ln for ln in out.split(b"\n")
                    if done_marker.encode() not in ln and start_marker.encode() not in ln
                )
                shown = self.bash_render(cmd, out, code)
                if shown != last_shown:
                    try:
                        await on_update(shown, code is not None)
                        last_shown = shown
                    except Exception:
                        pass  # рендер не должен ронять исполнение
                if code is not None:
                    return
            # Таймаут: прерываем убежавшую команду (Ctrl-C), чтобы она не
            # гадила в общий буфер следующему вызову. Оболочка живёт.
            interrupted = True
            shell.interrupt()
            await on_update(self.bash_render(cmd, out, None, timeout=True), True)
        except asyncio.CancelledError:
            # Клиент отвалился (веб: закрыл вкладку) — команда ещё бежит.
            # Прерываем её, иначе её вывод перемешается со следующим /bash,
            # а busy освободится под работающей командой.
            interrupted = True
            shell.interrupt()
            raise
        finally:
            if not interrupted:
                shell.busy = False
            else:
                # Дать Ctrl-C дойти и осесть, потом освободить (в фоне, чтобы
                # не держать вызывающего и не падать на отменённом таске).
                async def _release():
                    await asyncio.sleep(0.5)
                    shell.busy = False
                asyncio.ensure_future(_release())

    def bash_busy(self, key: str) -> bool:
        """Занят ли терминал (идёт команда) — адаптер спрашивает ДО поста
        статус-сообщения, чтобы не оставить висящий «⏳ Выполняю…»."""
        shell = self.bash.get(key)
        return shell is not None and shell.busy

    def bash_input(self, key: str, text: str) -> bool:
        """Досыл сырого ввода в открытый терминал (ответ на y/n). False — нет
        открытой оболочки."""
        shell = self.bash.get(key)
        if shell is None:
            return False
        shell.write(text + "\n")
        return True

    def bash_render(
        self, cmd: str, out: bytes, code: str | None, timeout: bool = False
    ) -> str:
        """HTML статуса bash: команда + вывод в <pre>, обрезка с хвоста."""
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

    # ── уведомления жизненного цикла ────────────────────────────

    async def notify_session_dead(self, session: Session, code: int | str) -> None:
        """Колбэк SessionManager: Claude умер сам по себе."""
        self.turns.stop(session.name)
        self._drop_pending_perms(session)
        await asyncio.to_thread(self.bash.close_for_session, session.name)
        await self.bubbles.close(session.name)
        text = self.t("session_died", name=session.title, code=code)
        tail = await asyncio.to_thread(self.manager.tail_log, session)
        if tail:
            text += "\n\n" + self.t("session_died_tail", tail=tail[:1500])
        await self.notice(session, text)

    async def notify_idle_closed(self, sessions: list[Session]) -> None:
        """Колбэк sweeper: сессии остановлены по простою."""
        for session in sessions:
            self.turns.stop(session.name)
            await asyncio.to_thread(self.bash.close_for_session, session.name)
            await self.bubbles.close(session.name)
            await self.notice(
                session, self.t("idle_closed", hours=f"{self.config.idle_timeout_h:g}")
            )

    async def notify_startup(self, restored: int) -> None:
        """Сообщить во все адаптеры, что оркестратор онлайн."""
        config_dir = self.config.claude_config_dir or Path.home() / ".claude"
        base_url = self.config.claude_env.get("ANTHROPIC_BASE_URL") or self.t("url_default")
        for tr in self._transports():
            try:
                await tr.notify(
                    None, self.t("startup", n=restored, config=config_dir, url=base_url)
                )
            except Exception as e:
                logger.warning("Стартовое уведомление через %s: %s", tr.name, e)

    # ── форматирование ──────────────────────────────────────────

    @staticmethod
    def fmt_num(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    def fmt_duration(self, seconds: float) -> str:
        m = int(seconds) // 60
        if m < 60:
            return self.t("min", m=m)
        return self.t("hour_min", h=m // 60, m=m % 60)
