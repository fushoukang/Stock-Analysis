#!/usr/bin/env bash
# One-time server setup for the Stock Analysis app on a fresh AlmaLinux /
# Rocky Linux (RHEL-family, dnf-based) VPS — we-trade.me.
#
# Run this AS ROOT, AFTER you've already copied the app code to
# /opt/stock-trading and put a real .env there (see deploy/README.md for
# both of those steps — this script does not do either).
#
#   sudo bash /opt/stock-trading/deploy/bootstrap.sh
#
# It's meant to be safe to re-run (each step checks before acting), so if
# something fails partway through, fix it and just run it again.
set -euo pipefail

APP_DIR=/opt/stock-trading
APP_USER=stockapp
UV_BIN=/usr/local/bin/uv

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (sudo bash deploy/bootstrap.sh)" >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/main.py" ]]; then
  echo "Expected the app code at $APP_DIR (main.py not found there)." >&2
  echo "Copy the code there first — see deploy/README.md — then re-run this." >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "Expected $APP_DIR/.env with your real Alpaca/SMTP settings." >&2
  echo "Copy your .env there first — see deploy/README.md — then re-run this." >&2
  exit 1
fi

echo "==> Installing base packages (firewalld, git)"
dnf install -y firewalld git

echo "==> Enabling firewalld and opening 80/443 (22 stays open by default)"
systemctl enable --now firewalld
firewall-cmd --permanent --add-service=http
firewall-cmd --permanent --add-service=https
firewall-cmd --reload

echo "==> Creating the '$APP_USER' service account (no login shell, never root)"
if ! id "$APP_USER" &>/dev/null; then
  useradd --system --no-create-home --home-dir "$APP_DIR" --shell /sbin/nologin "$APP_USER"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Installing uv (Python version/venv manager) system-wide"
if [[ ! -x "$UV_BIN" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

echo "==> Installing Python 3.13 via uv (no need for an OS package)"
sudo -u "$APP_USER" "$UV_BIN" python install 3.13

echo "==> Creating the virtualenv and installing dependencies"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$UV_BIN' venv --python 3.13 .venv && '$UV_BIN' pip install -r requirements.txt"

echo "==> Installing the systemd service"
cp "$APP_DIR/deploy/stock-trading.service" /etc/systemd/system/stock-trading.service
systemctl daemon-reload
systemctl enable --now stock-trading

echo "==> Installing Caddy (reverse proxy + automatic HTTPS)"
if ! command -v caddy &>/dev/null; then
  dnf install -y 'dnf-command(copr)'
  dnf copr enable -y @caddy/caddy
  dnf install -y caddy
fi
cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl reload caddy || systemctl restart caddy

echo
echo "==> Done. Check status with:"
echo "      sudo systemctl status stock-trading"
echo "      sudo systemctl status caddy"
echo "      sudo journalctl -u stock-trading -f"
echo
echo "If DNS for we-trade.me already points at this server, https://we-trade.me"
echo "should be live within a few seconds (Caddy requests its certificate on"
echo "first request). See deploy/README.md for DNS and troubleshooting."
