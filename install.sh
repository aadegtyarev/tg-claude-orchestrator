#!/usr/bin/env bash
# Установка tg-claude-orchestrator: venv, зависимости, systemd user-сервис.
set -euo pipefail
cd "$(dirname "$0")"
DIR=$(pwd)
SERVICE=tg-claude-orchestrator

echo "==> Проверки"
command -v python3 >/dev/null || { echo "Нужен python3"; exit 1; }
command -v claude >/dev/null || echo "ВНИМАНИЕ: claude не найден в PATH — установи Claude Code и залогинься"

echo "==> venv и зависимости"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> Создан .env — заполни TELEGRAM_BOT_TOKEN и ALLOWED_USER_IDS"
fi

echo "==> systemd user-сервис"
UNIT_DIR=$HOME/.config/systemd/user
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$SERVICE.service" <<EOF
[Unit]
Description=tg-claude-orchestrator — Telegram wrapper for Claude Code
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/launcher.py
Restart=on-failure
RestartSec=5
Environment=PATH=$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

# Linger: user-сервис продолжает работать после разлогина / без SSH-сессии.
echo "==> loginctl enable-linger (сервис переживает разлогин)"
if ! loginctl enable-linger "$USER" 2>/dev/null; then
    echo "Не удалось включить linger без прав root, выполни вручную:"
    echo "  sudo loginctl enable-linger $USER"
fi

cat <<EOF

Готово. Дальше:
  1. Отредактируй $DIR/.env (TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, TELEGRAM_CHAT_ID)
  2. Запусти:            systemctl --user enable --now $SERVICE
  3. Логи:               journalctl --user -u $SERVICE -f
  4. Перезапуск:         systemctl --user restart $SERVICE
EOF
