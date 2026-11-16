#!/usr/bin/env bash
# Actualiza el agente trader en el servidor con la última versión de la rama y lo reinicia.
# No toca la configuración (config/estrategia.toml) ni las llaves (.env).
# Uso, como root:  curl -fsSL https://raw.githubusercontent.com/t1salas/mails/claude/blissful-lamport-h4j2no/trader/deploy/actualizar.sh | bash
set -euo pipefail
RAMA="${RAMA:-claude/blissful-lamport-h4j2no}"
DESTINO="${DESTINO:-/opt/trader}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone -q --depth 1 --branch "$RAMA" https://github.com/t1salas/mails.git "$TMP/mails"
cp -r "$TMP/mails/trader/bot_trader/." "$DESTINO/bot_trader/"
cp -r "$TMP/mails/trader/deploy/." "$DESTINO/deploy/"
cp "$TMP/mails/trader/pyproject.toml" "$DESTINO/pyproject.toml"
chown -R trader: "$DESTINO"
sudo -u trader bash -c "cd '$DESTINO' && .venv/bin/pip install -q -e ."
systemctl restart bot-trader
sleep 3
echo "Versión instalada: $(git -C "$TMP/mails" log -1 --format='%h %s')"
echo "Servicio: $(systemctl is-active bot-trader)"
