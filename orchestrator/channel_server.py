#!/usr/bin/env python3
"""MCP channel-сервер для Claude Code — raw JSON-RPC 2.0 на stdio.

Запускается самим Claude Code как stdio-подпроцесс (через .mcp.json).
Raw JSON-RPC, а не MCP SDK: SDK не умеет push-уведомления вне запроса,
а channels-режим построен именно на них.

Потоки данных:
  launcher ──HTTP POST /notify──> этот процесс ──JSON-RPC push──> Claude
  Claude ──tools/call reply_to_user──> этот процесс ──HTTP POST /reply──> launcher
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import time
import urllib.request

import aiohttp
from aiohttp import web

CHANNEL_PORT = int(os.environ.get("CHANNEL_PORT", "18761"))
SESSION_NAME = os.environ.get("SESSION_NAME", "default")
ORCH_HOST = os.environ.get("ORCH_HOST", "127.0.0.1")
ORCH_PORT = int(os.environ.get("ORCH_PORT", "18080"))
ORCH_URL = f"http://{ORCH_HOST}:{ORCH_PORT}/reply"
# Общий секрет с оркестратором (REVIEW.md S1). Приходят из env, который
# sessions.py кладёт в .mcp.json; пустой = оркестратор без токена (старый режим).
ORCH_TOKEN = os.environ.get("ORCH_TOKEN", "")

# Минимальный интервал между push-уведомлениями в Claude.
PUSH_INTERVAL = 0.5

logger = logging.getLogger("ch-srv")

# Контракт каналов: https://code.claude.com/docs/en/channels-reference
# capability строго {"experimental": {"claude/channel": {}}}, tools: {} — для
# reply-тула, claude/channel/permission — приём запросов разрешений (relay).
CAPABILITIES = {
    "experimental": {
        "claude/channel": {},
        "claude/channel/permission": {},
    },
    "tools": {},
}

# Попадает в системный промпт Claude (поле instructions в initialize).
INSTRUCTIONS = (
    f'Messages from the user arrive as <channel source="channel-{SESSION_NAME}" '
    'context_id="...">text</channel>. The channel connects you to the user\'s '
    "chat (Telegram, web console, ...). Always reply with the reply_to_user "
    "tool, passing the context_id attribute exactly as received. Use "
    "complete=false for short intermediate progress updates while you work and "
    "complete=true exactly once with the final answer.\n"
    "CRITICAL: plain text you write is INVISIBLE to the user — this channel "
    "only relays explicit reply_to_user tool calls, nothing else. If you "
    "stop working (end your turn) without having called reply_to_user since "
    "your last one, whatever you were thinking never reaches the user, even if "
    "it reads like a message to them (\"I'll continue once CI is green\", \"done "
    "for now\"). Before ending ANY turn — including turns that end while you're "
    "waiting on a background command, a CI run, or another async condition — "
    "call reply_to_user one more time (complete=false if you'll keep going "
    "when it resolves, complete=true if you're truly done) to state where things "
    "stand. Never let a turn end on bare text.\n"
    "The user only sees your tool calls and reply_to_user messages — nothing "
    "else. A long silent stretch (reading files, thinking, running a slow "
    "command) looks identical to being stuck from their side. So: right after "
    "you receive a message, send one short complete=false line — what you "
    "understand the task to be and what you're about to do first — before you "
    "start working. Then keep sending short complete=false updates (one "
    "sentence — what you're doing / what you found) whenever you're about to do "
    "something that takes a while, and again after any noteworthy finding — "
    "don't wait for the whole task to finish. Err toward more of these, not "
    "fewer.\n"
    "Keep these updates and any status check-in answers (e.g. \"how's it "
    "going?\", \"что как?\") brief — one or two sentences. The user wants a "
    "quick pulse, not an essay; save detail for the final answer.\n"
    "For any task with more than one step, FIRST send a numbered plan via "
    "reply_to_user (complete=false) before starting — in the user's language, "
    "e.g.:\n"
    "План работ:\n1. …\n2. …\n3. …\n"
    "Then, as you finish each item, send a short updated status marking what is "
    "done (✅) and what is in progress (🔄), e.g. '1. ✅ … 2. 🔄 … 3. …'. This "
    "lets the operator follow progress point by point. Keep it terse; don't "
    "repeat the full plan every time unless it changed.\n"
    "send_file_to_user is ONLY for when the user explicitly asks you to send "
    "them a file, image, or artifact. Files you create or edit while working stay "
    "in the project — do NOT push them to the chat automatically. When you write "
    "code or files, just report what you did via reply_to_user; attach a file "
    "only on explicit request (\"send me the file\", \"скинь файл\").\n"
    "You have NO interactive terminal — the user is only reachable through this "
    "channel. Never use plan mode, ExitPlanMode, or interactive question tools "
    "like AskUserQuestion: they would block invisibly. When you need a decision "
    "or clarification, ask the user via reply_to_user (list options as "
    "numbered choices) and stop your turn; the user's answer arrives as the next "
    "channel message. Permission prompts are handled automatically — just proceed."
)

# Авто-подсказка про кошелёк секретов: добавляется в системный промпт ТОЛЬКО
# когда сессии выдан ~/.wallet.json (MODULES=wallet + сессия в чьей-то policy).
# Смысл — прозрачный шлюз: обёртки в PATH (.wallet-bin) заворачивают gh/git/curl/
# ssh в кошелёк, поэтому модель просто зовёт инструмент как обычно, а токен
# подставляется на хосте. Промпт лишь называет, ЧТО доступно (из policy). Под
# bwrap $HOME подменён приватным домом сессии, файл лежит ровно по ~/.wallet.json.
# Значений секретов тут нет — только факт наличия; конкретику — из `wallet ls`.
def _wallet_catalog() -> str | None:
    """Каталог доступных сессии секретов ИЗ POLICY (демон /secrets), а не хардкод.

    Возвращает готовый текст «доступно: <секрет> — команды …» либо None, если
    кошелёк сессии не выдан. Список строится из secrets.toml (что оператор
    разрешил, то и в подсказке) — не зашиваем gh/git/ssh руками. Ошибка запроса
    → общий текст (кошелёк есть, конкретику возьми из `wallet ls`)."""
    wf = os.path.expanduser("~/.wallet.json")
    if not os.path.exists(wf):
        return None
    try:
        cfg = json.loads(open(wf, encoding="utf-8").read())
        req = urllib.request.Request(
            cfg["url"] + "/secrets",
            headers={"Authorization": "Bearer " + cfg["token"]},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            secrets_list = json.load(r)
    except Exception:
        return "Run `wallet ls` to see which secrets and commands you may use."
    if not secrets_list:
        return "Run `wallet ls` — this session currently has no wallet secrets."
    lines = []
    any_inject = any_shared = False
    for s in secrets_list:
        desc = f" ({s['description']})" if s.get("description") else ""
        mode = s.get("mode")
        if mode == "shared":
            any_shared = True
            how = f"`wallet get {s['name']}`"
            if s.get("env"):
                how += f" or `wallet env {s['name']}` (${s['env']})"
            lines.append(f"  • `{s['name']}`{desc} — SHARED value, get it with {how}")
            continue
        cmds = ", ".join(s.get("commands", [])) or "—"
        if mode == "inject" and s.get("env"):
            any_inject = True
            inj = f" [inject: use ${s['env']} in the command; ${s['env']}_FILE for a file]"
        else:
            inj = ""
        lines.append(f"  • `{s['name']}`{desc} — {cmds}{inj}")
    out = (
        "These tools are wired through the wallet TRANSPARENTLY — just run them "
        "NORMALLY (e.g. `gh pr create`, `git push`, `curl ...`); a wrapper on your "
        "PATH routes the call to the host with the right credential attached, and you "
        "never handle the token. They run on the host IN YOUR PROJECT DIRECTORY, so a "
        "`cd` in your shell does not move them. Use each tool's own watch (e.g. `gh "
        "run watch`) — do NOT wrap calls in a `while`/poll loop. `wallet ls` lists "
        "this again; `wallet help` has the full reference.\n"
        + "\n".join(lines)
    )
    if any_shared:
        out += (
            "\nShared secrets are values you MAY read and use (a dev API key for a "
            "service you build, a login/password to enter somewhere): `wallet get "
            "<name>` prints the value, `wallet env <name>` prints `export VAR=value` "
            "for `eval \"$(wallet env <name>)\"`. They live in the wallet so they "
            "aren't pasted in chat or committed — don't echo them into the chat."
        )
    if any_inject:
        # Inject-секрет отдаётся привычной env-переменной $ИМЯ (в песочнице там
        # маркер, реальное значение подставляется на хосте). Модель просто
        # использует $ИМЯ; значение читать/выводить не надо.
        out += (
            "\nInject secrets appear as an env var — use it as usual: the tool reads "
            "$NAME itself (gh/aws/kubectl), or you drop $NAME into the command "
            "(curl -H 'Authorization: Bearer $OPENAI_KEY'); use $NAME_FILE where a "
            "FILE path is needed (ssh -i $DEPLOY_KEY_FILE). In the sandbox $NAME "
            "holds only a marker — the real value is filled in on the host, you never "
            "see it. Don't try to print or transform the value."
        )
    return out


_WALLET_CATALOG = _wallet_catalog()
if _WALLET_CATALOG is not None:
    INSTRUCTIONS += (
        "\nSECRETS WALLET: some tools are wired to run with a host-side credential you "
        "cannot see. For the tools listed below, just RUN THEM NORMALLY — a wrapper on "
        "your PATH forwards the call to the host with the right secret attached, and any "
        "secret value is auto-redacted from the output. " + _WALLET_CATALOG + "\n"
        "For git this covers the network subcommands (push/fetch/pull/clone/…); local git "
        "(status/commit/diff/log) just runs in place. A secret's value reaches a command "
        "through its env var $NAME — the tool reads it (gh/aws/kubectl), or you put $NAME "
        "in the command (curl -H 'Authorization: Bearer $OPENAI_KEY'), or $NAME_FILE "
        "where a file is needed (ssh -i $DEPLOY_KEY_FILE). Keep these commands "
        "non-interactive (ssh `-o BatchMode=yes -o StrictHostKeyChecking=accept-new`).\n"
        "To force a specific secret, `wallet run <name> -- <cmd>` picks it by name; "
        "`wallet ls`/`wallet help` show what's available. Use the wallet only for a "
        "credential you do NOT have — one you already have (the user gave it to you, or "
        "you generated your own for a NEW resource) you just use directly. If the wallet "
        "can't do it (secret not listed, denied, or it keeps failing), don't improvise a "
        "workaround — tell the operator via reply_to_user with the exact command and the "
        "error.\n"
        "The wallet hides values by design: don't try to print or transform a token "
        "to see its value — if you think you need the raw value, you don't."
    )

TOOLS = [
    {
        "name": "reply_to_user",
        "description": (
            "Send the user a reply for a message received via this channel. "
            "Use complete=false liberally for short intermediate progress "
            "updates while you work — each one is sent as its own message, so "
            "the user can see you're alive and what you're doing/finding, not "
            "just a spinning status. When the task is done, call it exactly "
            "once with complete=true and the final answer — that is what the "
            "user keeps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context_id": {
                    "type": "string",
                    "description": (
                        "Context ID from the incoming notification "
                        "(e.g. telegram:demo:123:45:6). REQUIRED — pass it exactly as received."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Reply text to send to the user.",
                },
                "complete": {
                    "type": "boolean",
                    "description": (
                        "true — final answer (regular message), "
                        "false — intermediate progress (status bubble)."
                    ),
                    "default": False,
                },
            },
            "required": ["context_id", "text"],
        },
    },
    {
        "name": "send_file_to_user",
        "description": (
            "Send a local file (document, image, archive, ...) to the user. "
            "Use ONLY when the user explicitly asks to receive a "
            "file — do not push files you created while working. Max 50 MB."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context_id": {
                    "type": "string",
                    "description": "Context ID from the incoming notification, exactly as received.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to an existing local file.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional short caption for the file.",
                },
            },
            "required": ["context_id", "file_path"],
        },
    },
]


class ChannelServer:
    def __init__(self) -> None:
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._initialized = asyncio.Event()
        self._stopped = asyncio.Event()
        self._last_push = 0.0
        # Ссылки на фоновые задачи: event loop держит task слабой ссылкой,
        # без сохранения задача может быть собрана GC до завершения.
        self._tasks: set[asyncio.Task] = set()
        # Общий HTTP-пул к оркестратору (REVIEW.md E1).
        self._http: aiohttp.ClientSession | None = None

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {ORCH_TOKEN}"} if ORCH_TOKEN else {}

    def _http_session(self) -> aiohttp.ClientSession:
        # Общий пул к оркестратору (keep-alive) — без сессии на запрос (REVIEW E1).
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._http

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        reader = await self._open_stdio()
        http_runner = await self._start_http()
        try:
            await self._serve_rpc(reader)
        finally:
            self._stopped.set()
            await http_runner.cleanup()
            if self._http is not None and not self._http.closed:
                await self._http.close()

    # ── stdio / JSON-RPC ────────────────────────────────────────

    async def _open_stdio(self) -> asyncio.StreamReader:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        transport, protocol = await loop.connect_write_pipe(
            lambda: asyncio.streams.FlowControlMixin(), sys.stdout
        )
        self._writer = asyncio.StreamWriter(transport, protocol, None, loop)
        return reader

    async def _read_message(self, reader: asyncio.StreamReader) -> dict | None:
        """Одно JSON-RPC-сообщение (newline-delimited). None = поток закрыт.

        Без таймаута: канал обязан жить, пока жив Claude, даже если
        сообщений нет часами. Битая строка не роняет цикл.
        """
        while True:
            raw = await reader.readline()
            if not raw:
                return None
            # errors="replace": один битый байт не должен ронять весь RPC-цикл.
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Пропускаю битую JSON-RPC строку: %s", e)

    async def _write_message(self, msg: dict) -> None:
        assert self._writer is not None
        payload = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._writer.write(payload)
            await self._writer.drain()

    async def _serve_rpc(self, reader: asyncio.StreamReader) -> None:
        # Handshake: initialize -> notifications/initialized
        init = await self._read_message(reader)
        if init is None or init.get("method") != "initialize":
            logger.error("Ожидался 'initialize', получено: %s", init)
            return
        await self._write_message({
            "jsonrpc": "2.0",
            "id": init.get("id", 0),
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": CAPABILITIES,
                "serverInfo": {"name": f"channel-{SESSION_NAME}", "version": "2.1.0"},
                "instructions": INSTRUCTIONS,
            },
        })

        # Клиент может прислать запрос (с id) ещё до notifications/initialized —
        # обслуживаем его и продолжаем ждать нотификацию, иначе он повиснет на
        # этом id (раньше сообщение молча глоталось).
        while True:
            notif = await self._read_message(reader)
            if notif is None:
                logger.warning("Соединение закрыто до notifications/initialized")
                return
            if notif.get("method") == "notifications/initialized":
                break
            if notif.get("id") is not None:
                await self._handle_request(
                    notif["id"], notif.get("method"), notif.get("params") or {}
                )
            else:
                logger.debug("До initialized пришло уведомление: %s", notif.get("method"))
        self._initialized.set()
        logger.info("Handshake завершён, канал '%s' активен", SESSION_NAME)

        while True:
            msg = await self._read_message(reader)
            if msg is None:
                logger.info("Claude закрыл соединение")
                return
            if msg.get("id") is not None:
                await self._handle_request(msg["id"], msg.get("method"), msg.get("params") or {})
            elif msg.get("method") == "notifications/claude/channel/permission_request":
                # Запрос разрешения от Claude Code — переслать в Telegram.
                self._spawn(self._relay_permission(msg.get("params") or {}))
            else:
                logger.debug("Уведомление: %s", msg.get("method"))

    async def _handle_request(self, msg_id: int, method: str | None, params: dict) -> None:
        if method == "tools/list":
            await self._write_message({
                "jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS},
            })
        elif method == "tools/call" and params.get("name") == "reply_to_user":
            # В фоне: пока оркестратор отвечает, read-loop должен читать stdin
            # (иначе блокируются permission_request и следующие запросы).
            args = params.get("arguments") or {}
            self._spawn(self._forward_to_orchestrator(msg_id, {
                "context_id": args.get("context_id", ""),
                "text": args.get("text", ""),
                "complete": bool(args.get("complete", False)),
            }, ok_text="Reply sent"))
        elif method == "tools/call" and params.get("name") == "send_file_to_user":
            args = params.get("arguments") or {}
            self._spawn(self._forward_to_orchestrator(msg_id, {
                "context_id": args.get("context_id", ""),
                "file_path": args.get("file_path", ""),
                "caption": args.get("caption", ""),
            }, ok_text="File sent"))
        elif method == "tools/call":
            await self._write_message({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Unknown tool: {params.get('name')}"}],
                },
            })
        else:
            await self._write_message({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })

    async def _forward_to_orchestrator(self, msg_id: int, payload: dict, ok_text: str) -> None:
        try:
            http = self._http_session()
            async with http.post(ORCH_URL, json=payload, headers=self._auth_headers) as resp:
                resp.raise_for_status()
            result = {"content": [{"type": "text", "text": f"{ok_text} (ctx={payload.get('context_id')})"}]}
        except Exception as e:
            logger.error("Не удалось передать оркестратору: %s", e)
            result = {
                "isError": True,
                "content": [{"type": "text", "text": f"Failed: {e}"}],
            }
        await self._write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def _relay_permission(self, params: dict) -> None:
        """Переслать permission_request оркестратору (POST /permission/<имя>)."""
        url = f"http://{ORCH_HOST}:{ORCH_PORT}/permission/{SESSION_NAME}"
        try:
            http = self._http_session()
            async with http.post(url, json=params,
                                 timeout=aiohttp.ClientTimeout(total=10),
                                 headers=self._auth_headers) as resp:
                resp.raise_for_status()
        except Exception as e:
            logger.error("Не удалось переслать permission_request: %s", e)

    # ── HTTP: приём push от оркестратора ────────────────────────

    async def _start_http(self) -> web.AppRunner:
        # Auth на /notify /permission /ping тем же ORCH_TOKEN (симметрично
        # оркестратору): без него локальный процесс мог бы POST /notify и вбросить
        # промпт в Claude или POST /permission behavior=allow и авто-разрешить
        # запрос. Пустой ORCH_TOKEN (тестовый запуск без .env) = режим открыт —
        # в проде config всегда генерирует/читает токен.
        expected = (f"Bearer {ORCH_TOKEN}").encode() if ORCH_TOKEN else b""
        if not ORCH_TOKEN:
            logger.warning(
                "ORCH_TOKEN пуст — channel-сервер в ОТКРЫТОМ режиме (любой "
                "локальный процесс может /notify//permission). Только для тестов!"
            )

        @web.middleware
        async def _auth(request: web.Request, handler):
            if expected:
                sent = request.headers.get("Authorization", "").encode("utf-8", "replace")
                if not secrets.compare_digest(sent, expected):
                    return web.Response(status=401, text="unauthorized")
            return await handler(request)

        app = web.Application(middlewares=[_auth])
        app.router.add_get("/ping", self._http_ping)
        app.router.add_post("/notify", self._http_notify)
        app.router.add_post("/permission", self._http_permission)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="127.0.0.1", port=CHANNEL_PORT)
        await site.start()
        logger.info("HTTP push-сервер на порту %d", CHANNEL_PORT)
        return runner

    async def _http_ping(self, request: web.Request) -> web.Response:
        return web.Response(text="OK")

    async def _http_permission(self, request: web.Request) -> web.Response:
        """Вердикт пользователя — уведомлением в Claude Code."""
        try:
            data = await request.json()
        except ValueError:
            return web.Response(status=400, text="invalid json")
        await self._write_message({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel/permission",
            "params": {
                "request_id": str(data.get("request_id", "")).lower(),
                "behavior": "allow" if data.get("behavior") == "allow" else "deny",
            },
        })
        return web.Response(text="OK")

    async def _http_notify(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return web.Response(status=400, text="invalid json")

        try:
            await asyncio.wait_for(self._initialized.wait(), timeout=30)
        except asyncio.TimeoutError:
            return web.Response(status=503, text="MCP handshake not completed")

        # Троттлинг: не чаще одного push в PUSH_INTERVAL. Слот резервируем
        # сразу — конкурентные /notify не проснутся одновременно.
        now = time.monotonic()
        slot = max(now, self._last_push + PUSH_INTERVAL)
        self._last_push = slot
        if slot > now:
            await asyncio.sleep(slot - now)

        # Формат по контракту: {content, meta}. meta-ключи становятся
        # атрибутами тега <channel>.
        await self._write_message({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "content": str(data.get("content", "")),
                "meta": {"context_id": str(data.get("context_id", ""))},
            },
        })
        return web.Response(text="OK")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(levelname)s] ch-srv({SESSION_NAME}): %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(ChannelServer().run())
    except KeyboardInterrupt:
        pass
    logger.info("Channel-сервер остановлен")


if __name__ == "__main__":
    main()
