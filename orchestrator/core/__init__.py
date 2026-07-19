"""Ядро оркестратора: транспорт-независимая логика.

app.py — OrchestratorCore (команды, маршрутизация, события), transport.py —
контракт адаптеров, sessions.py — процессы Claude Code, остальное — сервисы
и чистые утилиты. Адаптеры и модули живут в orchestrator/adapters и
orchestrator/modules.
"""
