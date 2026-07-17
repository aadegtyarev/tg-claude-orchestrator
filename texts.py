"""Тексты бота на ru/en. Язык выбирается переменной BOT_LANG в .env."""

from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    "ru": {
        "help": (
            "🤖 <b>Claude Code Orchestrator</b>\n\n"
            "В основном чате:\n"
            "• <code>/new &lt;имя&gt;</code> — новая сессия; имя можно с пробелами/эмодзи\n"
            "• <code>/new /путь</code> — сессия в директории (создастся, если нет)\n"
            "• <code>/new имя /путь</code> — то же, со своим именем\n"
            "• <code>/list</code> — сессии со статусами и кнопками\n"
            "• <code>/ls [путь]</code> — показать файлы\n"
            "• <code>/skills</code> — список скиллов\n"
            "• <code>/chat_id</code> — ID чата и привязка бота\n\n"
            "В топике сессии:\n"
            "• текст — отправить Claude (остановленная сессия возобновится сама)\n"
            "• фото/файл — сохранится в папку сессии, Claude получит путь\n"
            "• <code>/stats</code> — контекст и статистика\n"
            "• <code>/usage</code> — расходы и лимиты плана\n"
            "• <code>/model</code> — модель: fable/opus/sonnet/haiku или точное имя\n"
            "• <code>/compact</code> — сжать контекст\n"
            "• <code>/clear</code> — очистить контекст (топик остаётся)\n"
            "• <code>/close_session</code> — остановить; продолжение — сообщением\n"
            "• <code>/delete_session</code> — удалить сессию вместе с топиком\n"
            "• другие <code>/команды</code> — уходят в терминал Claude Code\n\n"
            "Пока Claude работает, в топике живёт статус-бабл: вызовы\n"
            "инструментов, сабагенты и промежуточные ответы, кнопка ⏹ Стоп.\n"
            "Файлы Claude присылает сам (тул send_file_to_telegram).\n"
            "Запросы разрешений приходят кнопками ✅/❌ (permission relay)."
        ),
        "only_main_chat": "Команда работает только в основном чате.",
        "only_topic": "Команда работает в топике сессии.",
        "new_usage": "Укажи имя или путь:\n/new my-project\n/new /home/user/project",
        "name_exists": "Сессия «{name}» уже существует.",
        "limit_reached": "Достигнут лимит сессий ({limit}).",
        "creating": "🔄 Создаю сессию…",
        "create_fail": "❌ {error}",
        "created": "✅ «{name}» готов. Пиши в топик «{name}».",
        "list_empty": "📭 Нет активных сессий.",
        "st_working": "🔄 работает",
        "st_waiting": "🟢 ожидает",
        "st_stopped": "⏸ остановлена",
        "uptime": "аптайм {uptime}",
        "min": "{m} мин",
        "hour_min": "{h} ч {m} мин",
        "ls_not_exists": "❌ Путь не существует: {path}",
        "ls_file": "📄 {path}",
        "ls_no_access": "❌ Нет доступа: {error}",
        "ls_empty": "(пусто)",
        "ls_more": "… и ещё {n}",
        "stats_stopped_suffix": " (остановлена)",
        "stats_no_transcript": "{header} — {uptime}.\nТранскрипт ещё не создан.",
        "stats_body": (
            "{header}\n"
            "Модель: {model}\n"
            "Контекст: ~{ctx} токенов (~{pct}% от 200k)\n"
            "Сгенерировано за сессию: {out} токенов\n"
            "Сообщений пользователя: {turns}\n"
            "Транскрипт: {kb} КБ\n"
            "Аптайм: {uptime}"
        ),
        "usage_collecting": "💰 Собираю расходы и лимиты…",
        "usage_failed": "Не удалось разобрать вывод /cost (возможно, другая версия Claude Code). Данные — в claude.log.",
        "usage_title": "💰 Расходы и лимиты — {name}",
        "usage_cost": "Стоимость сессии: ${cost}",
        "usage_session": "Лимит сессии (5ч): {pct}%{reset}",
        "usage_week": "Неделя, все модели: {pct}%{reset}",
        "usage_model": "Неделя, {model}: {pct}%",
        "default_model": "по умолчанию Claude Code",
        "model_prompt": (
            "Текущая модель: {model}\n"
            "Выбери синоним (маппинг на конкретную версию делает Claude Code) "
            "или задай точное имя: /model claude-opus-4-8"
        ),
        "model_switching_btn": "Переключаю на {model}…",
        "model_switching": "🔀 Переключаю «{name}» на {model} (перезапуск, контекст через resume)…",
        "model_fail": "❌ Не удалось переключить на {model}: {error}\nМодель осталась прежней.",
        "model_done": "🔀 Модель: {model}. Сессия готова.",
        "model_ctx_lost": "\n⚠️ Прежний контекст восстановить не удалось.",
        "skills_none": "Скиллы не найдены.",
        "skills_header": "🧩 Скиллы ({n}). Вызываются просто текстом задачи:",
        "compact_sent": "🗜 Отправил /compact — контекст сессии будет сжат.",
        "send_fail": "❌ Не удалось отправить: {error}",
        "clear_progress": "🧹 Перезапускаю с чистым контекстом…",
        "clear_fail": "❌ Не удалось перезапустить: {error}\nСессия остановлена — создай заново через /new.",
        "clear_done": "🧹 Контекст очищен, сессия готова.",
        "close_done": "⏸ Сессия остановлена, топик сохранён.\nНапиши сюда — продолжу диалог (resume).",
        "resume_progress": "🔁 Возобновляю сессию…",
        "resume_fail": "❌ Не удалось возобновить: {error}",
        "resume_ok": "🔁 Сессия возобновлена, контекст продолжен.",
        "resume_fresh": "🔁 Сессия перезапущена (прежний контекст восстановить не удалось).",
        "forward_fail": "❌ Не удалось передать сообщение в Claude: {error}",
        "file_dl_fail": "❌ Не удалось скачать файл: {error}",
        "file_received": "Пользователь прислал файл, он сохранён в {path}",
        "file_caption": "Подпись к файлу: {caption}",
        "slash_sent": (
            "⌨️ Отправил в терминал Claude: {cmd}\n"
            "Вывод команды — в claude.log; в чат придёт только то, что Claude отправит сам."
        ),
        "bubble_working": "⏳ Работаю…",
        "bubble_stop": "⏹ Стоп",
        "bubble_stop_requested": "⏹ Запрошена остановка",
        "stop_requested": "Попросил Claude остановиться…",
        "stop_not_active": "Сессия уже не активна.",
        "stop_fail": "Не удалось связаться с сессией.",
        "stop_message": (
            "[system] Пользователь нажал Стоп. Немедленно прекрати текущую работу, "
            "не начинай новых действий и вызови reply_to_telegram с complete=true "
            "и коротким итогом: что успел сделать."
        ),
        "subagent": "🤖 Сабагент {agent}",
        "session_died": (
            "💀 Сессия «{name}»: Claude завершился (код {code}).\n"
            "Напиши в топик — перезапущу сессию."
        ),
        "session_died_tail": "Хвост лога:\n{tail}",
        "idle_closed": "😴 Сессия остановлена по простою (> {hours} ч). Напиши — возобновлю.",
        "startup": "🟢 Бот онлайн. Восстановлено сессий: {n} (возобновятся по сообщению).",
        "sendfile_not_found": "❌ Не удалось отправить: файл не найден: {path}",
        "sendfile_too_big": "❌ Не удалось отправить: файл больше 50 МБ (лимит Telegram): {path}",
        "sendfile_fail": "❌ Не удалось отправить файл: {error}",
        "session_not_found": "Сессия не найдена.",
        "topic_delete_fail": "Сессия удалена, но топик удалить не удалось: {error}",
        "sess_closed": "«{name}» остановлена.",
        "perm_request": "🔐 <b>Запрос разрешения</b>\n{tool}: {desc}\n<pre>{preview}</pre>",
        "perm_allow": "✅ Разрешить",
        "perm_deny": "❌ Отклонить",
        "perm_allowed": "✅ Разрешено: {tool}",
        "perm_denied": "❌ Отклонено: {tool}",
        "perm_fail": "Не удалось передать ответ: {error}",
        "menu_new": "Новая сессия (имя или /путь)",
        "menu_list": "Список сессий",
        "menu_ls": "Показать файлы",
        "menu_stats": "Контекст и статистика сессии",
        "menu_usage": "Расходы и лимиты плана",
        "menu_model": "Модель сессии (fable/opus/sonnet/haiku)",
        "menu_skills": "Список скиллов",
        "menu_compact": "Сжать контекст сессии",
        "menu_clear": "Очистить контекст сессии",
        "menu_close": "Остановить сессию (возобновляемо)",
        "menu_delete": "Удалить сессию и топик",
        "menu_help": "Справка",
        "menu_chat_id": "ID чата (для TELEGRAM_CHAT_ID)",
        "chat_id_bound_now": (
            "✅ Чат привязан к боту.\n"
            "ID: <code>{id}</code>\n"
            "Зафиксируй в .env, чтобы привязка пережила рестарт:\n"
            "<code>TELEGRAM_CHAT_ID={id}</code>"
        ),
        "chat_id_current": (
            "ID этого чата: <code>{id}</code> — бот привязан к нему.\n"
            "В .env: <code>TELEGRAM_CHAT_ID={id}</code>"
        ),
        "chat_id_other": (
            "ID этого чата: <code>{id}</code>.\n"
            "⚠️ Бот привязан к другому чату (<code>{bound}</code>) — "
            "чтобы работать здесь, поменяй TELEGRAM_CHAT_ID в .env и перезапусти."
        ),
    },
    "en": {
        "help": (
            "🤖 <b>Claude Code Orchestrator</b>\n\n"
            "In the main chat:\n"
            "• <code>/new &lt;name&gt;</code> — new session; the name may contain spaces/emoji\n"
            "• <code>/new /path</code> — session in a directory (created if missing)\n"
            "• <code>/new name /path</code> — same, with a custom name\n"
            "• <code>/list</code> — sessions with statuses and buttons\n"
            "• <code>/ls [path]</code> — list files\n"
            "• <code>/skills</code> — list skills\n"
            "• <code>/chat_id</code> — chat ID and bot binding\n\n"
            "In a session topic:\n"
            "• text — send to Claude (a stopped session resumes automatically)\n"
            "• photo/file — saved into the session folder, Claude gets the path\n"
            "• <code>/stats</code> — context and usage stats\n"
            "• <code>/usage</code> — cost and plan limits\n"
            "• <code>/model</code> — model: fable/opus/sonnet/haiku or an exact name\n"
            "• <code>/compact</code> — compact the context\n"
            "• <code>/clear</code> — fresh context (topic stays)\n"
            "• <code>/close_session</code> — stop; continue by sending a message\n"
            "• <code>/delete_session</code> — delete the session and its topic\n"
            "• other <code>/commands</code> — typed into the Claude Code terminal\n\n"
            "While Claude works, a status bubble lives in the topic: tool calls,\n"
            "subagents, intermediate replies, and a ⏹ Stop button.\n"
            "Claude can send files back (send_file_to_telegram tool).\n"
            "Permission prompts arrive as ✅/❌ buttons (permission relay)."
        ),
        "only_main_chat": "This command works only in the main chat.",
        "only_topic": "This command works in a session topic.",
        "new_usage": "Provide a name or a path:\n/new my-project\n/new /home/user/project",
        "name_exists": "Session “{name}” already exists.",
        "limit_reached": "Session limit reached ({limit}).",
        "creating": "🔄 Creating session…",
        "create_fail": "❌ {error}",
        "created": "✅ “{name}” is ready. Write in the “{name}” topic.",
        "list_empty": "📭 No active sessions.",
        "st_working": "🔄 working",
        "st_waiting": "🟢 idle",
        "st_stopped": "⏸ stopped",
        "uptime": "uptime {uptime}",
        "min": "{m} min",
        "hour_min": "{h} h {m} min",
        "ls_not_exists": "❌ Path does not exist: {path}",
        "ls_file": "📄 {path}",
        "ls_no_access": "❌ Access denied: {error}",
        "ls_empty": "(empty)",
        "ls_more": "… and {n} more",
        "stats_stopped_suffix": " (stopped)",
        "stats_no_transcript": "{header} — {uptime}.\nNo transcript yet.",
        "stats_body": (
            "{header}\n"
            "Model: {model}\n"
            "Context: ~{ctx} tokens (~{pct}% of 200k)\n"
            "Generated this session: {out} tokens\n"
            "User messages: {turns}\n"
            "Transcript: {kb} KB\n"
            "Uptime: {uptime}"
        ),
        "usage_collecting": "💰 Collecting cost and limits…",
        "usage_failed": "Could not parse /cost output (maybe a different Claude Code version). See claude.log.",
        "usage_title": "💰 Cost and limits — {name}",
        "usage_cost": "Session cost: ${cost}",
        "usage_session": "Session limit (5h): {pct}%{reset}",
        "usage_week": "Week, all models: {pct}%{reset}",
        "usage_model": "Week, {model}: {pct}%",
        "default_model": "Claude Code default",
        "model_prompt": (
            "Current model: {model}\n"
            "Pick an alias (Claude Code maps it to a concrete version) "
            "or set an exact name: /model claude-opus-4-8"
        ),
        "model_switching_btn": "Switching to {model}…",
        "model_switching": "🔀 Switching “{name}” to {model} (restart, context via resume)…",
        "model_fail": "❌ Failed to switch to {model}: {error}\nModel unchanged.",
        "model_done": "🔀 Model: {model}. Session ready.",
        "model_ctx_lost": "\n⚠️ Previous context could not be restored.",
        "skills_none": "No skills found.",
        "skills_header": "🧩 Skills ({n}). Invoke them with a plain task message:",
        "compact_sent": "🗜 Sent /compact — the session context will be compacted.",
        "send_fail": "❌ Failed to send: {error}",
        "clear_progress": "🧹 Restarting with a fresh context…",
        "clear_fail": "❌ Restart failed: {error}\nSession stopped — create it again with /new.",
        "clear_done": "🧹 Context cleared, session ready.",
        "close_done": "⏸ Session stopped, topic kept.\nSend a message here to continue (resume).",
        "resume_progress": "🔁 Resuming session…",
        "resume_fail": "❌ Resume failed: {error}",
        "resume_ok": "🔁 Session resumed, context continued.",
        "resume_fresh": "🔁 Session restarted (previous context could not be restored).",
        "forward_fail": "❌ Failed to deliver the message to Claude: {error}",
        "file_dl_fail": "❌ Failed to download the file: {error}",
        "file_received": "The user sent a file, saved at {path}",
        "file_caption": "File caption: {caption}",
        "slash_sent": (
            "⌨️ Typed into the Claude terminal: {cmd}\n"
            "Command output stays in claude.log; only what Claude sends arrives here."
        ),
        "bubble_working": "⏳ Working…",
        "bubble_stop": "⏹ Stop",
        "bubble_stop_requested": "⏹ Stop requested",
        "stop_requested": "Asked Claude to stop…",
        "stop_not_active": "Session is no longer active.",
        "stop_fail": "Could not reach the session.",
        "stop_message": (
            "[system] The user pressed Stop. Immediately stop the current work, "
            "start nothing new, and call reply_to_telegram with complete=true "
            "and a short summary of what you managed to do."
        ),
        "subagent": "🤖 Subagent {agent}",
        "session_died": (
            "💀 Session “{name}”: Claude exited (code {code}).\n"
            "Send a message to the topic to restart the session."
        ),
        "session_died_tail": "Log tail:\n{tail}",
        "idle_closed": "😴 Session stopped after being idle (> {hours} h). Send a message to resume.",
        "startup": "🟢 Bot online. Restored sessions: {n} (resume on message).",
        "sendfile_not_found": "❌ Cannot send: file not found: {path}",
        "sendfile_too_big": "❌ Cannot send: file exceeds 50 MB (Telegram limit): {path}",
        "sendfile_fail": "❌ Failed to send the file: {error}",
        "session_not_found": "Session not found.",
        "topic_delete_fail": "Session deleted, but the topic could not be removed: {error}",
        "sess_closed": "“{name}” stopped.",
        "perm_request": "🔐 <b>Permission request</b>\n{tool}: {desc}\n<pre>{preview}</pre>",
        "perm_allow": "✅ Allow",
        "perm_deny": "❌ Deny",
        "perm_allowed": "✅ Allowed: {tool}",
        "perm_denied": "❌ Denied: {tool}",
        "perm_fail": "Failed to deliver the verdict: {error}",
        "menu_new": "New session (name or /path)",
        "menu_list": "List sessions",
        "menu_ls": "List files",
        "menu_stats": "Session context and stats",
        "menu_usage": "Cost and plan limits",
        "menu_model": "Session model (fable/opus/sonnet/haiku)",
        "menu_skills": "List skills",
        "menu_compact": "Compact session context",
        "menu_clear": "Clear session context",
        "menu_close": "Stop session (resumable)",
        "menu_delete": "Delete session and topic",
        "menu_help": "Help",
        "menu_chat_id": "Chat ID (for TELEGRAM_CHAT_ID)",
        "chat_id_bound_now": (
            "✅ Chat bound to the bot.\n"
            "ID: <code>{id}</code>\n"
            "Pin it in .env so the binding survives restarts:\n"
            "<code>TELEGRAM_CHAT_ID={id}</code>"
        ),
        "chat_id_current": (
            "This chat ID: <code>{id}</code> — the bot is bound to it.\n"
            "In .env: <code>TELEGRAM_CHAT_ID={id}</code>"
        ),
        "chat_id_other": (
            "This chat ID: <code>{id}</code>.\n"
            "⚠️ The bot is bound to another chat (<code>{bound}</code>) — "
            "to use it here, change TELEGRAM_CHAT_ID in .env and restart."
        ),
    },
}


def get_texts(lang: str) -> dict[str, str]:
    return MESSAGES.get(lang, MESSAGES["ru"])
