#!/bin/sh
# Installs hwdash as a systemd user service that runs from this directory.
#   ./install.sh               install / update and start
#   ./install.sh --uninstall   stop and remove the service (data and settings stay)
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/hwdash.service"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now hwdash.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "hwdash service removed. Data: ~/.local/share/hwdash, settings: ~/.config/hwdash"
    exit 0
fi

PY=$(command -v python3) || { echo "python3 not found" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "hwdash needs Python 3.11+" >&2; exit 1; }

mkdir -p "$UNIT_DIR"
sed -e "s|@PYTHON@|$PY|" -e "s|@DIR@|$DIR|" "$DIR/systemd/hwdash.service.in" > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable hwdash.service
systemctl --user restart hwdash.service
sleep 2
systemctl --user --no-pager --lines=3 status hwdash.service || true

cat <<MSG

hwdash is running: http://$(hostname):8090 (port is set in ~/.config/hwdash/config.toml)

Optional, needs sudo:
  sudo loginctl enable-linger "$USER"   # keep running without an active login session
  # open the port for your LAN only, e.g. with ufw:
  sudo ufw allow from 192.168.0.0/16 to any port 8090 proto tcp comment hwdash
MSG
