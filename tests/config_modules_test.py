"""Дефолт модулей: кошелёк включён по умолчанию при песочнице bwrap.

MODULES не задан → wallet при bwrap (host-like работа с секретами без выдачи их
модели), пусто при off/agent-vm. Явный MODULES (в т.ч. пустой) уважается.

Запуск: .venv/bin/python tests/config_modules_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.config import Config  # noqa: E402


def test_default_modules():
    # MODULES не задан → wallet при bwrap, пусто иначе
    assert Config._default_modules(None, "bwrap") == ("wallet",)
    assert Config._default_modules(None, "off") == ()
    assert Config._default_modules(None, "agent-vm") == ()
    # явный MODULES уважаем как есть (в т.ч. пустой — осознанное отключение)
    assert Config._default_modules("", "bwrap") == ()
    assert Config._default_modules("wallet", "off") == ("wallet",)
    assert Config._default_modules("wallet", "bwrap") == ("wallet",)
    print("OK _default_modules: bwrap+не задан→wallet; off→пусто; явный уважается")


def main():
    test_default_modules()
    print("ALL CONFIG-MODULES OK")


if __name__ == "__main__":
    main()
