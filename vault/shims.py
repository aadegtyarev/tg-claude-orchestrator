"""Обёртки-«шимы» кошелька: PATH-шлюз, чтобы модель звала git/gh/curl как обычно.

Смысл: секрет живёт на хосте, а команда бежит в песочнице. Чтобы модель не
дёргала `wallet exec` руками (и не изобретала обходы), в PATH песочницы первым
ставится каталог крошечных `/bin/sh`-обёрток: `gh` → `wallet exec gh "$@"`,
и демон уже на хосте подберёт секрет по команде. `git` — особый случай: сетевые
подкоманды (GIT_NETWORK) идут через кошелёк, локальные (status/commit/log) —
настоящим git прямо в песочнице (быстро, без хостового раунд-трипа).

Почему здесь, а не в модуле оркестратора. Потребителей два: оркестраторный
адаптер (per-session каталог в приватном $HOME сессии) и standalone CLI
claude-box (`--wallet` на host/inject-секрет, каталог во временной папке).
box_cli — Слой 2 редизайна, ему нельзя импортировать orchestrator.*, а две копии
генератора разошлись бы (git-шим — вещь, которую чинят один раз). Модуль
stdlib-only, как весь vault/.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable

from .secret import GIT_NETWORK

# Имя каталога обёрток. Оркестратор кладёт его в приватный $HOME сессии (это же
# имя знает ядро — session_home), claude-box — во временный каталог, который
# биндится в песочницу. Ставится ПЕРВЫМ в PATH, поэтому обёртки побеждают
# настоящие бинари.
SHIM_DIRNAME = ".wallet-bin"


def tool_names(patterns: Iterable[str]) -> set[str]:
    """Голые имена инструментов, которые надо завернуть, из шаблонов `commands`.

    Берём первый токен шаблона (`curl https://api/*` → curl) и его basename;
    чистые глобы (`*`, `sub?cmd`) пропускаем — это не имя бинаря. Дубликаты
    схлопываются (set).
    """
    tools: set[str] = set()
    for pat in patterns:
        parts = pat.split()
        tool = os.path.basename(parts[0]) if parts else ""
        if tool and not any(c in tool for c in "*?["):
            tools.add(tool)
    return tools


def git_shim(real_git: str | None = None) -> str:
    """Обёртка git: сетевые подкоманды → на хост через кошелёк, локальные →
    настоящий git в песочнице. Путь настоящего git резолвим на хосте (/usr у
    песочницы — тот же RO-бинд, поэтому путь совпадает)."""
    real = real_git or shutil.which("git") or "/usr/bin/git"
    nets = "|".join(GIT_NETWORK)
    return (
        "#!/bin/sh\n"
        "# Обёртка кошелька (генерируется): сетевые git-подкоманды идут на\n"
        "# хост через `wallet exec` (креды хоста), локальные — настоящим git.\n"
        f'case "${{1:-}}" in\n'
        f'  {nets}) exec wallet exec git "$@" ;;\n'
        "esac\n"
        f'exec {real} "$@"\n'
    )


def tool_shim(tool: str) -> str:
    """Обёртка обычного инструмента: заворачиваем вызов целиком в кошелёк."""
    return f'#!/bin/sh\nexec wallet exec {tool} "$@"\n'


def shim_script(tool: str) -> str:
    """Скрипт обёртки для инструмента (git — особый случай)."""
    return git_shim() if tool == "git" else tool_shim(tool)


def cli_shim(cli_path: Path) -> str:
    """Обёртка самого клиента `wallet`: он лежит в репозитории (bin/wallet), а
    репозиторий RO-биндится в песочницу тем же путём — значит из песочницы файл
    достижим, не хватает только имени в PATH. Если бит исполнения снят (чекаут
    без прав), зовём через python3 — клиент stdlib-only."""
    if os.access(cli_path, os.X_OK):
        return f'#!/bin/sh\nexec {cli_path} "$@"\n'
    return f'#!/bin/sh\nexec python3 {cli_path} "$@"\n'


def write_shims(shim_dir: Path, tools: Iterable[str]) -> list[str]:
    """Полная перегенерация каталога обёрток: каталог 0700, файлы 0755.

    Сначала сносим прежние файлы (отозвали секрет/команду — обёртка не должна
    пережить перегенерацию), потом пишем текущие. Пустой набор инструментов →
    ничего не создаём (каталог остаётся пустым/отсутствующим) и возвращаем [].
    Возвращает отсортированный список завёрнутых имён — для прозрачного
    сообщения оператору.
    """
    if shim_dir.exists():
        for old in shim_dir.iterdir():
            if old.is_file() or old.is_symlink():
                old.unlink()
    names = sorted(set(tools))
    if not names:
        return []
    shim_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(shim_dir, 0o700)
    for tool in names:
        path = shim_dir / tool
        path.write_text(shim_script(tool))
        os.chmod(path, 0o755)
    return names
