"""Веб-адаптер: локальный веб-интерфейс оркестратора поверх aiohttp.

Реализует Transport (core/transport.py): вместо привязок «сессия ↔ топик»
у веба адресация по имени сессии — все доставки транслируются JSON-событиями
во все подключённые WebSocket-клиенты, а команды пользователя приходят через
REST API (/api/*). Статика (SPA на vanilla JS) отдаётся без токена, но данные
без авторизации не получить: все /api/* и WS требуют токен.

Авторизация как у Jupyter: токен из WEB_TOKEN (или сгенерированный на запуск)
печатается в лог готовой ссылкой /?token=…; заход по ней ставит HttpOnly-cookie,
дальше работают cookie, заголовок Authorization: Bearer или ?token=.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from ...config import Config
from ...core.app import OrchestratorCore, UserError
from ...core.mdrender import md_to_html
from ...core.sessions import Session
from ...core.transport import Origin, PermissionRequest

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "orch_web_token"


class WebAdapter:
    name = "web"

    def __init__(self, config: Config, core: OrchestratorCore):
        self.config = config
        self.core = core
        self.manager = core.manager
        self.t = core.t
        # Пустой WEB_TOKEN = сгенерировать на запуск (в start(), как Jupyter).
        self._token = config.web_token
        self._ws_clients: set[web.WebSocketResponse] = set()
        # ref статус-бабла — просто счётчик: клиенту важна только уникальность
        # в рамках процесса, само состояние бабла держит ядро.
        self._bubble_seq = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self.app = self._build_app()

    # ── Transport: жизненный цикл ───────────────────────────────

    async def start(self) -> None:
        if not self._token:
            self._token = secrets.token_urlsafe(18)
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.web_host, self.config.web_port)
        await site.start()
        logger.info(
            "Веб-интерфейс: http://%s:%s/?token=%s",
            self.config.web_host, self.config.web_port, self._token,
        )

    async def stop(self) -> None:
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ── Transport: привязка сессий ──────────────────────────────

    async def bind_session(self, session: Session) -> str | None:
        # У веба нет поверхности на сессию — адресация по имени, binding не нужен.
        return None

    async def unbind_session(self, session: Session, address: str) -> None:
        return None

    # ── Transport: доставка (всё — событиями в WS) ──────────────

    async def _broadcast(self, event: dict) -> None:
        """Разослать событие всем WS-клиентам. Best-effort: отвалившийся клиент
        молча выкидывается, остальных это не задерживает и не роняет."""
        for ws in list(self._ws_clients):
            try:
                await ws.send_json(event)
            except Exception:
                self._ws_clients.discard(ws)

    async def deliver_text(
        self, session: Session, text: str, *, origin: Origin | None = None,
        intermediate: bool = False,
    ) -> None:
        await self._broadcast({
            "type": "reply", "session": session.name, "text": text,
            "html": md_to_html(text), "intermediate": intermediate,
        })

    async def deliver_file(
        self, session: Session, path: Path, caption: str, *,
        origin: Origin | None = None,
    ) -> None:
        # Файл не гоняем через WS — клиент скачает по /api/…/file (jail ядра
        # проверяется и там, путь из события — только ссылка).
        await self._broadcast({
            "type": "file", "session": session.name, "path": str(path),
            "name": path.name, "caption": caption,
        })

    async def notify(self, session: Session | None, text: str) -> None:
        await self._broadcast({
            "type": "notice", "session": session.name if session else None,
            "text": text, "html": md_to_html(text),
        })

    async def typing(self, session: Session) -> bool:
        await self._broadcast({"type": "typing", "session": session.name})
        # Некому слать — циклу typing нет смысла жить ради веба.
        return bool(self._ws_clients)

    async def bubble_post(
        self, session: Session, html: str, *, stop_button: bool
    ) -> str | None:
        ref = str(next(self._bubble_seq))
        await self._broadcast({
            "type": "bubble", "session": session.name, "ref": ref,
            "html": html, "stop_button": stop_button,
        })
        return ref

    async def bubble_edit(
        self, session: Session, ref: str, html: str, *, stop_button: bool
    ) -> None:
        await self._broadcast({
            "type": "bubble", "session": session.name, "ref": ref,
            "html": html, "stop_button": stop_button,
        })

    async def bubble_finish(self, session: Session, ref: str, *, delete: bool) -> None:
        await self._broadcast({
            "type": "bubble_close", "session": session.name, "ref": ref, "delete": delete,
        })

    async def bubble_freeze(self, session: Session, ref: str) -> None:
        await self._broadcast({
            "type": "bubble_freeze", "session": session.name, "ref": ref,
        })

    async def permission_prompt(
        self, session: Session, request: PermissionRequest
    ) -> None:
        await self._broadcast({
            "type": "perm_request", "session": session.name,
            "request_id": request.request_id, "tool": request.tool,
            "description": request.description, "preview": request.preview,
        })

    async def permission_resolved(
        self, session: Session, request_id: str, behavior: str, via: str
    ) -> None:
        await self._broadcast({
            "type": "perm_resolved", "session": session.name,
            "request_id": request_id, "behavior": behavior, "via": via,
        })

    # ── HTTP: приложение и авторизация ──────────────────────────

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        r = app.router
        r.add_get("/", self.h_index)
        r.add_static("/static/", STATIC_DIR, name="static")
        r.add_get("/api/ws", self.h_ws)
        r.add_get("/api/sessions", self.h_sessions)
        r.add_post("/api/sessions", self.h_create)
        r.add_post("/api/sessions/{name}/message", self.h_message)
        r.add_post("/api/sessions/{name}/close", self.h_close)
        r.add_post("/api/sessions/{name}/clear", self.h_clear)
        r.add_post("/api/sessions/{name}/delete", self.h_delete)
        r.add_post("/api/sessions/{name}/compact", self.h_compact)
        r.add_post("/api/sessions/{name}/stop", self.h_stop)
        r.add_post("/api/sessions/{name}/interrupt", self.h_interrupt)
        r.add_post("/api/sessions/{name}/model", self.h_model)
        r.add_get("/api/sessions/{name}/stats", self.h_stats)
        r.add_get("/api/sessions/{name}/usage", self.h_usage)
        r.add_get("/api/sessions/{name}/history", self.h_history)
        r.add_post("/api/sessions/{name}/permission", self.h_permission)
        r.add_post("/api/sessions/{name}/upload", self.h_upload)
        r.add_get("/api/sessions/{name}/file", self.h_file)
        r.add_post("/api/sessions/{name}/bash", self.h_bash)
        r.add_post("/api/sessions/{name}/bash_input", self.h_bash_input)
        r.add_get("/api/ls", self.h_ls)
        r.add_get("/api/skills", self.h_skills)
        return app

    def _authorized(self, request: web.Request) -> bool:
        """Токен из cookie, Authorization: Bearer или ?token=. Сравнение
        constant-time (compare_digest) — тайминг не выдаёт префикс токена."""
        if not self._token:
            return False
        candidates = []
        if (c := request.cookies.get(COOKIE_NAME)) is not None:
            candidates.append(c)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            candidates.append(auth[len("Bearer "):])
        if (q := request.query.get("token")) is not None:
            candidates.append(q)
        return any(secrets.compare_digest(c, self._token) for c in candidates)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Статика открыта (SPA грузится без токена), данные — только /api/*
        # и только с токеном.
        if request.path.startswith("/api/") and not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def h_index(self, request: web.Request) -> web.StreamResponse:
        """SPA. Заход по /?token=<верный> ставит HttpOnly-cookie — дальше
        браузер авторизован без токена в каждой ссылке (как у Jupyter)."""
        resp = web.FileResponse(STATIC_DIR / "index.html")
        tok = request.query.get("token")
        if tok and self._token and secrets.compare_digest(tok, self._token):
            resp.set_cookie(
                COOKIE_NAME, self._token, httponly=True, samesite="Lax", path="/"
            )
        return resp

    # ── HTTP: помощники ─────────────────────────────────────────

    @staticmethod
    def _err(text: str, status: int = 400) -> web.Response:
        return web.json_response({"error": text}, status=status)

    def _session_of(self, request: web.Request) -> Session | None:
        return self.manager.get(request.match_info.get("name", ""))

    @staticmethod
    async def _json_body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _session_info(self, session: Session) -> dict:
        running = bool(session.running)
        return {
            "name": session.name,
            "title": session.title,
            "status": self.core.session_status(session),
            "model": session.model,
            "linked_path": session.linked_path,
            "running": running,
            "uptime": (
                self.core.fmt_duration(time.time() - session.started_at) if running else None
            ),
        }

    def _sessions_info(self) -> list[dict]:
        return [self._session_info(s) for s in self.manager.list_all()]

    async def _sessions_changed(self) -> None:
        # Клиенту достаточно сигнала — список он перезапрашивает сам.
        await self._broadcast({"type": "sessions_changed"})

    def _origin(self) -> Origin:
        # У веба нет reply-цитирования — токен произвольный, ядро его не трактует.
        return Origin(self.name, "0")

    # ── HTTP: WebSocket ─────────────────────────────────────────

    async def h_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        try:
            await ws.send_json({"type": "hello", "sessions": self._sessions_info()})
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
                # Входящие сообщения клиент не шлёт (всё через REST) — игнор.
        finally:
            self._ws_clients.discard(ws)
        return ws

    # ── HTTP: сессии ────────────────────────────────────────────

    async def h_sessions(self, request: web.Request) -> web.Response:
        return web.json_response(self._sessions_info())

    async def h_create(self, request: web.Request) -> web.Response:
        data = await self._json_body(request)
        title = str(data.get("title", "")).strip()
        path = str(data.get("path", "")).strip() or None
        if not title:
            return self._err("title required")
        try:
            session = await self.core.create_session(title, path)
        except UserError as e:
            return self._err(str(e))
        await self._sessions_changed()
        return web.json_response(self._session_info(session))

    async def h_message(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        data = await self._json_body(request)
        text = str(data.get("text", ""))
        if not text.strip():
            return self._err("text required")
        try:
            state = await self.core.ensure_running(session)
            if text.lstrip().startswith("/"):
                # Паритет с Telegram on_slash: неизвестные /команды — прямо
                # в терминал Claude (команды Claude Code).
                await self.core.slash_command(session, text.strip().splitlines()[0])
                slash = True
            else:
                await self.core.user_message(session, text, self._origin())
                slash = False
        except UserError as e:
            return self._err(str(e))
        if state != "running":
            await self._sessions_changed()  # resume — статус в списке поменялся
        return web.json_response({"ok": True, "slash": slash})

    async def h_close(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        await self.core.close_session(session)
        await self._sessions_changed()
        return web.json_response({"ok": True})

    async def h_clear(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        try:
            await self.core.clear_session(session)
        except UserError as e:
            return self._err(str(e))
        await self._sessions_changed()
        return web.json_response({"ok": True})

    async def h_delete(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        await self.core.delete_session(session)
        await self._sessions_changed()
        return web.json_response({"ok": True})

    async def h_compact(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        try:
            await self.core.compact(session)
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"ok": True})

    async def h_stop(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        try:
            await self.core.soft_stop(session, self._origin())
        except Exception as e:
            logger.error("Сессия %s: не удалось отправить стоп: %s", session.name, e)
            return self._err(str(e))
        return web.json_response({"ok": True})

    async def h_interrupt(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        try:
            await self.core.hard_stop(session)
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"ok": True})

    async def h_model(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        data = await self._json_body(request)
        model = str(data.get("model", "")).strip()
        if not model:
            return self._err("model required")
        try:
            resumed = await self.core.switch_model(session, model)
        except UserError as e:
            return self._err(str(e))
        await self._sessions_changed()
        return web.json_response({"resumed": resumed})

    async def h_stats(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        # Блокирующее чтение транскрипта — в поток, event loop не стопорим.
        text = await asyncio.to_thread(self.core.stats_text, session)
        return web.json_response({"text": text})

    async def h_usage(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        text = await self.core.usage_text(session)
        return web.json_response({"text": text})  # null — распарсить не удалось

    async def h_history(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        items = []
        for ev in self.core.history(session.name):
            ev = dict(ev)
            if ev.get("kind") in ("reply", "intermediate", "notice"):
                ev["html"] = md_to_html(str(ev.get("text", "")))
            items.append(ev)
        return web.json_response(items)

    async def h_permission(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        data = await self._json_body(request)
        request_id = str(data.get("request_id", ""))
        behavior = str(data.get("behavior", ""))
        if not request_id or behavior not in ("allow", "deny"):
            return self._err("request_id and behavior allow|deny required")
        try:
            handled = await self.core.permission_verdict(
                session, request_id, behavior, via=self.name
            )
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"handled": handled})

    # ── HTTP: файлы ─────────────────────────────────────────────

    def _incoming_dir(self, session: Session) -> Path:
        """INCOMING_DIR: относительный — внутри папки сессии, абсолютный —
        общий для всех сессий (та же логика, что в Telegram on_file)."""
        incoming = Path(self.config.incoming_dir).expanduser()
        if not incoming.is_absolute():
            incoming = session.session_dir / incoming
        return incoming

    async def h_upload(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        try:
            reader = await request.multipart()
        except Exception:
            return self._err("multipart body required")
        caption, dest = "", None
        incoming = self._incoming_dir(session)
        async for part in reader:
            if part.name == "caption":
                caption = (await part.text()).strip()
            elif part.name == "file" and part.filename:
                # .name отрезает возможные ../ из имени файла клиента.
                fname = Path(part.filename).name or f"file_{int(time.time())}"
                incoming.mkdir(parents=True, exist_ok=True)
                dest = incoming / fname
                with dest.open("wb") as f:
                    while chunk := await part.read_chunk(65536):
                        f.write(chunk)
        if dest is None:
            return self._err("file field required")
        text = self.t("file_received", path=dest)
        if caption:
            text += "\n" + self.t("file_caption", caption=caption)
        try:
            await self.core.ensure_running(session)
            await self.core.user_message(session, text, self._origin())
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"ok": True, "path": str(dest)})

    async def h_file(self, request: web.Request) -> web.StreamResponse:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        raw = request.query.get("path", "")
        if not raw:
            return self._err("path required")
        path = Path(raw).expanduser()
        # Jail ядра: наружу отдаются только файлы рабочих папок сессии —
        # иначе ссылкой можно было бы выкачать любой файл хоста.
        if not self.core.path_in_workspace(path, session):
            return self._err("path outside session workspace", 403)
        if not path.is_file():
            return self._err("file not found", 404)
        return web.FileResponse(path)

    # ── HTTP: bash-терминал ─────────────────────────────────────

    async def h_bash(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        data = await self._json_body(request)
        cmd = str(data.get("cmd", "")).strip()
        if not cmd:
            return self._err("cmd required")
        key = self.core.bash_key(session, "web")
        sname = session.name

        async def on_update(html_text: str, done: bool) -> None:
            await self._broadcast(
                {"type": "bash", "session": sname, "html": html_text, "done": done}
            )

        try:
            # Держим запрос до конца команды: busy-конфликт отдаётся кодом 400,
            # а стрим вывода клиент всё равно получает по WS.
            await self.core.run_bash(key, session, cmd, on_update)
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"ok": True})

    async def h_bash_input(self, request: web.Request) -> web.Response:
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        data = await self._json_body(request)
        text = str(data.get("text", ""))
        key = self.core.bash_key(session, "web")
        if not self.core.bash_input(key, text):
            return self._err(self.t("bash_not_running"))
        return web.json_response({"ok": True})

    # ── HTTP: справки ───────────────────────────────────────────

    async def h_ls(self, request: web.Request) -> web.Response:
        path = request.query.get("path") or None
        return web.json_response({"text": self.core.ls_text(path)})

    async def h_skills(self, request: web.Request) -> web.Response:
        skills = await asyncio.to_thread(self.core.collect_skills)
        return web.json_response(
            [{"name": name, "description": desc} for name, desc in skills]
        )
