"""claude-orchestrator: оркестратор параллельных сессий Claude Code.

Ядро (core/) + транспорт-адаптеры (adapters/: telegram, web) + раннеры
изоляции (runners/: bwrap, agent-vm) + модули (modules/: wallet).
Запуск: python -m orchestrator (см. __main__.py). Карта модулей — в README.
"""
