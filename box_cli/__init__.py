"""box_cli — приложение `claude-box` (Слой 2 редизайна, docs/ARCHITECTURE-claude-box.md §5).

Это НЕ библиотека `box/` и НЕ часть её: `box/` — автономный pure-пакет
(ноль импортов оркестратора, см. tests/box_autonomy_test.py). CLI же — app
поверх слоёв: он ИМПОРТИТ и `box` (Слой 2, PTY-запуск), и Engine
(`orchestrator.runners`, Слой 0, изоляция bwrap|off). Поэтому он живёт ОТДЕЛЬНЫМ
пакетом вне `box/`, чтобы не тянуть Engine в автономный `box/`.

Физическое место Engine (`orchestrator/runners/`) — временный варт: по плану он
переедет в свой пакет позже (Engine-extract). Использовать его из Слоя 2 сейчас
допустимо — это Слой 0.

Первый срез (§5.1): `claude-box [--engine bwrap|off] [-- <аргументы claude>]` —
запустить claude (или любую команду) в песочнице через `box.launch` и отдать
терминал (PTY-relay stdin↔процесс). Кошелёк/vault/профили/VM/`-p`/init/connect —
следующие срезы (пока честные заглушки «не реализовано», см. cli.py).
"""
