"""Меню команд Telegram не показывает то, что при текущей конфигурации не работает.

- `/wallet` — только когда кошелёк подключён (вне bwrap он не включается);
- `/stats` — не под agent-vm: транскрипт Claude живёт ВНУТРИ microVM, а
  оркестратор читает его на хосте, так что цифр не будет никогда;
- `/bash` — показываем ВСЕГДА: в топике сессии под agent-vm он отказывает, но
  в главном чате это операторский терминал на хосте, и он работает.

Запуск: .venv/bin/python tests/command_menu_test.py
"""
import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.adapters.telegram.adapter import TelegramAdapter  # noqa: E402
from orchestrator.config import Config  # noqa: E402
from orchestrator.core.texts import get_texts  # noqa: E402


class _FakeBot:
    """Ловит set_my_commands, ничего не шлёт в сеть."""

    def __init__(self):
        self.menus: list[list] = []

    async def set_my_commands(self, commands, scope=None):
        self.menus.append([c.command for c in commands])

    async def delete_my_commands(self):
        self.menus.append([])


def _menu_for(config) -> list[str]:
    """Собрать меню тем же кодом, что и адаптер, но без поллинга и сети."""
    from orchestrator.core.app import OrchestratorCore

    core = OrchestratorCore.__new__(OrchestratorCore)
    core.config = config
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.config = config
    adapter.core = core
    texts = get_texts(config.bot_lang)
    adapter.t = lambda key, **kw: texts[key].format(**kw)
    bot = _FakeBot()
    adapter.bot = bot
    asyncio.run(TelegramAdapter._set_command_menu(adapter))
    return bot.menus[0] if bot.menus else []


def test_bwrap_with_wallet_shows_everything():
    cfg = replace(Config.from_env(), sandbox="bwrap", modules=("wallet",))
    menu = _menu_for(cfg)
    assert "wallet" in menu, menu
    assert "stats" in menu, menu
    assert "bash" in menu, menu
    print("OK bwrap+кошелёк: /wallet, /stats, /bash в меню")


def test_wallet_hidden_when_module_off():
    cfg = replace(Config.from_env(), sandbox="bwrap", modules=())
    menu = _menu_for(cfg)
    assert "wallet" not in menu, menu
    assert "stats" in menu, menu
    print("OK кошелёк выключен → /wallet скрыт")


def test_agentvm_hides_wallet_and_stats_but_keeps_bash():
    cfg = replace(Config.from_env(), sandbox="agent-vm", modules=())
    menu = _menu_for(cfg)
    assert "wallet" not in menu, menu
    assert "stats" not in menu, menu
    assert "bash" in menu, "/bash работает в главном чате — прятать нельзя"
    print("OK agent-vm: /wallet и /stats скрыты, /bash остался")


# ── /help: те же правила, что и для меню ─────────────────────────────────


def _help_for(config) -> str:
    """Справка тем же кодом ядра, но без поднятия ядра целиком."""
    from orchestrator.core.app import OrchestratorCore

    core = OrchestratorCore.__new__(OrchestratorCore)
    core.config = config
    texts = get_texts(config.bot_lang)
    core.t = lambda key, **kw: texts[key].format(**kw)
    return OrchestratorCore.help_text(core)


def test_help_hides_disabled_commands():
    on = _help_for(replace(Config.from_env(), sandbox="bwrap", modules=("wallet",)))
    assert "/wallet" in on and "/stats" in on, "при включённых фичах строки на месте"

    vm = _help_for(replace(Config.from_env(), sandbox="agent-vm", modules=()))
    assert "/wallet" not in vm, "выключенный кошелёк не должен светиться в справке"
    assert "/stats" not in vm, "недоступная статистика не должна светиться в справке"
    # Остальная справка цела — вырезаны строки, а не всё подряд.
    assert "/new" in vm and "/list" in vm, vm[:200]
    print("OK /help не показывает недоступные команды")


def main():
    test_bwrap_with_wallet_shows_everything()
    test_wallet_hidden_when_module_off()
    test_agentvm_hides_wallet_and_stats_but_keeps_bash()
    test_help_hides_disabled_commands()
    print("ALL COMMAND-MENU OK")


if __name__ == "__main__":
    main()
