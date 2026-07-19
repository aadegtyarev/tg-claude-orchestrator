"""Реестр транспорт-адаптеров: имя из ADAPTERS → фабрика.

Новый адаптер = подпакет здесь + Transport-реализация + запись в make_adapters
+ имя в config._parse_adapters. Ядро (core/app.py) о конкретных адаптерах
не знает.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config
    from ..core.app import OrchestratorCore
    from ..core.transport import Transport


def make_adapters(config: "Config", core: "OrchestratorCore") -> "list[Transport]":
    out = []
    for name in config.adapters:
        if name == "telegram":
            from .telegram.adapter import TelegramAdapter

            out.append(TelegramAdapter(config, core))
        elif name == "web":
            from .web.adapter import WebAdapter

            out.append(WebAdapter(config, core))
    return out
