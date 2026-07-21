"""Набор модулей: кошелёк требует песочницу bwrap и вне её НЕ включается.

MODULES — реестр расширений (кошелёк — первый из них, не единственный
возможный). Модуль может требовать конкретную песочницу: кошелёк работает
ТОЛЬКО под bwrap, потому что его провода (шимы в PATH, env-маркеры) — это
окружение процесса claude на ХОСТЕ. Под agent-vm claude живёт в госте, env
туда не течёт и домашний каталог сессии не монтируется, поэтому включённый
кошелёк был бы тихим no-op: демон поднят, а в сессии его нет.

Поэтому wallet отфильтровывается вне bwrap ДАЖЕ при явном MODULES=wallet —
но громко (WARNING в лог), а не молча.

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
    # явный пустой MODULES — осознанное отключение, уважаем
    assert Config._default_modules("", "bwrap") == ()
    assert Config._default_modules("wallet", "bwrap") == ("wallet",)
    print("OK _default_modules: bwrap+не задан→wallet; off/agent-vm→пусто")


def test_wallet_requires_bwrap_even_if_explicit():
    """Явный MODULES=wallet вне bwrap НЕ включает кошелёк (был тихий no-op).

    Под agent-vm: демон поднимался на хосте, шимы и .wallet.json ложились в
    домашний каталог сессии, но в гостя не попадали ни они, ни env-маркеры —
    кошелёк «включён» и бесполезен. Теперь он просто не включается.
    """
    assert Config._default_modules("wallet", "agent-vm") == ()
    assert Config._default_modules("wallet", "off") == ()
    print("OK явный MODULES=wallet вне bwrap отфильтрован (не тихий no-op)")


def test_unknown_module_still_rejected():
    """Неизвестное имя модуля — по-прежнему ошибка запуска, а не тихий пропуск."""
    try:
        Config._default_modules("nosuchmodule", "bwrap")
    except SystemExit:
        print("OK неизвестный модуль → SystemExit")
        return
    raise AssertionError("неизвестный модуль должен падать SystemExit")


def main():
    test_default_modules()
    test_wallet_requires_bwrap_even_if_explicit()
    test_unknown_module_still_rejected()
    print("ALL CONFIG-MODULES OK")


if __name__ == "__main__":
    main()
