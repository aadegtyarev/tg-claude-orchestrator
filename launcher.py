#!/usr/bin/env python3
"""Совместимость: точка входа переехала в пакет (python -m orchestrator).

Шим оставлен, чтобы существующие systemd-юниты с ExecStart=... launcher.py
продолжали работать. Новые установки используют `python -m orchestrator`.
"""

import asyncio

from orchestrator.__main__ import main

if __name__ == "__main__":
    asyncio.run(main())
