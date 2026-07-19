"""Общий pytest-конфиг тестов.

Тесты — офлайновые скрипты (каждый гоняется и как `python tests/x_test.py`
через run_all.sh, и как pytest-функция). Чтобы `async def test_*` работали
без внешнего pytest-asyncio, обрабатываем корутины сами через хук
pytest_pyfunc_call — 10 строк вместо зависимости.
"""

from __future__ import annotations

import asyncio
import inspect


def pytest_pyfunc_call(pyfuncitem):
    func = pyfuncitem.obj
    if inspect.iscoroutinefunction(func):
        kwargs = {
            name: pyfuncitem.funcargs[name]
            for name in pyfuncitem._fixtureinfo.argnames
        }
        asyncio.run(func(**kwargs))
        return True  # мы выполнили тест сами
    return None
