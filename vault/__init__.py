"""Vault — автономный кошелёк секретов (домен + демон + CLI).

Пакет БЕЗ зависимостей оркестратора (нет aiogram/Telegram): секреты, policy,
guard, редакция и транспорты живут здесь и работают без бота. Оркестратор —
клиент этого пакета через тонкий модуль-адаптер.

Пока (фаза 1 редизайна, docs/ARCHITECTURE-claude-box.md) сюда вынесен ЧИСТЫЙ
домен из orchestrator/modules/wallet: Secret+guard (secret.py), редакция вывода
(redact.py), чтение secrets.toml (store.py), правка policy (policy.py). Демон и
CLI приезжают следующими слайсами.
"""
