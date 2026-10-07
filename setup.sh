#!/usr/bin/env bash
# Rerouter setup: installs an autoboot service that serves HTTP + HTTPS on one port.
#   ./setup.sh                 install + start (systemd; Termux:Boot / cron fallback)
#   ./setup.sh --port 8080     use a different port (default 2060)
#   ./setup.sh --no-tls        HTTP only
#   ./setup.sh --uninstall     remove the autoboot service
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="rerouter"
PORT="${PORT:-2060}"
NO_TLS=0
UNINSTALL=0
RUN_USER="${SUDO_USER:-$(id -un)}"

while [ $# -gt 0 ]; do
  case "$1" in
    --port)      PORT="${2:?--port needs a value}"; shift 2 ;;
    --user)      RUN_USER="${2:?--user needs a value}"; shift 2 ;;
    --no-tls)    NO_TLS=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
  esac
done

case "$PORT" in ''|*[!0-9]*) echo "Invalid port: $PORT" >&2; exit 1 ;; esac

have_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; }
is_termux()    { [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *com.termux* ]]; }

print_urls() {
  local ips
  ips="$(hostname -I 2>/dev/null || true)"
  echo "Rerouter is available at:"
  for ip in ${ips:-localhost}; do
    echo "  http://$ip:$PORT"
    [ "$NO_TLS" -eq 1 ] || echo "  https://$ip:$PORT   (self-signed certificate)"
  done
}

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  if have_systemd; then
    [ "$(id -u)" -eq 0 ] || exec sudo -E "$0" "$@"
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/$SERVICE.service"
    systemctl daemon-reload
    echo "Removed systemd service '$SERVICE'."
  else
    rm -f "$HOME/.termux/boot/$SERVICE.sh"
    if command -v crontab >/dev/null 2>&1; then
      (crontab -l 2>/dev/null | grep -v "$APP_DIR/server.py" || true) | crontab -
    fi
    pkill -f "$APP_DIR/server.py" 2>/dev/null || true
    echo "Removed autoboot entry."
  fi
  echo "Certificates in $APP_DIR/certs were left in place."
  exit 0
fi

# ------------------------------------------------------------------ install
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 is required but was not found." >&2; exit 1; }

if have_systemd && [ "$(id -u)" -ne 0 ]; then
  echo "Root is needed to install the systemd service - re-running with sudo..."
  exec sudo -E "$0" "$@"
fi

chmod +x "$APP_DIR/server.py"

# self-signed certificate (HTTPS)
if [ "$NO_TLS" -eq 0 ]; then
  CERT_DIR="$APP_DIR/certs"
  if [ -f "$CERT_DIR/cert.pem" ] && [ -f "$CERT_DIR/key.pem" ]; then
    echo "Using existing certificate in $CERT_DIR"
  elif command -v openssl >/dev/null 2>&1; then
    mkdir -p "$CERT_DIR"
    HOST="$(hostname)"
    SAN="DNS:$HOST,DNS:localhost,IP:127.0.0.1"
    for ip in $(hostname -I 2>/dev/null || true); do
      case "$ip" in *:*) ;; *) SAN="$SAN,IP:$ip" ;; esac   # IPv4 only
    done
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" \
      -subj "/CN=$HOST" -addext "subjectAltName=$SAN" 2>/dev/null \
    || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
         -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" -subj "/CN=$HOST" 2>/dev/null
    chmod 600 "$CERT_DIR/key.pem"
    if [ "$(id -u)" -eq 0 ]; then chown -R "$RUN_USER" "$CERT_DIR" 2>/dev/null || true; fi
    echo "Generated self-signed certificate in $CERT_DIR"
  else
    echo "WARNING: openssl not found - HTTPS will be disabled (install openssl and re-run)." >&2
  fi
fi

# password that protects adding / editing / removing entries in the web UI
run_as_user() {
  if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then sudo -u "$RUN_USER" "$@"; else "$@"; fi
}
if ! run_as_user test -w "$APP_DIR"; then
  echo "WARNING: $APP_DIR is not writable by '$RUN_USER' - editing entries from the web UI will fail." >&2
fi
if ! run_as_user "$PY" "$APP_DIR/server.py" --check-password 2>/dev/null; then
  if [ -t 0 ]; then
    echo "Set a password for adding/editing/removing entries in the web UI (stored hashed in config.json):"
    run_as_user "$PY" "$APP_DIR/server.py" --set-password || echo "Skipped - run ./server.py --set-password later."
  else
    echo "No web UI password set yet - run: ./server.py --set-password"
  fi
fi

TLS_ENV=""
[ "$NO_TLS" -eq 1 ] && TLS_ENV="Environment=NO_TLS=1"

if have_systemd; then
  cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Rerouter web viewer (HTTP + HTTPS on port $PORT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=PORT=$PORT
Environment=PYTHONUNBUFFERED=1
$TLS_ENV
ExecStart=$PY $APP_DIR/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable "$SERVICE"
  systemctl restart "$SERVICE"
  sleep 1
  systemctl --no-pager --lines=5 status "$SERVICE" || true
  echo
  echo "Autoboot enabled: systemctl status|restart|stop $SERVICE   (logs: journalctl -u $SERVICE -f)"
elif is_termux; then
  BOOT_DIR="$HOME/.termux/boot"
  mkdir -p "$BOOT_DIR"
  cat > "$BOOT_DIR/$SERVICE.sh" <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd "$APP_DIR" && PORT=$PORT NO_TLS=$NO_TLS exec "$PY" server.py >> "$APP_DIR/rerouter.log" 2>&1
BOOT
  chmod +x "$BOOT_DIR/$SERVICE.sh"
  pkill -f "$APP_DIR/server.py" 2>/dev/null || true
  (cd "$APP_DIR" && PORT=$PORT NO_TLS=$NO_TLS nohup "$PY" server.py >> rerouter.log 2>&1 &)
  echo "Termux detected: boot script written to $BOOT_DIR/$SERVICE.sh"
  echo "Install the 'Termux:Boot' app and open it once so the script runs at device boot."
else
  echo "systemd not found - falling back to cron @reboot."
  CRON_LINE="@reboot cd $APP_DIR && PORT=$PORT NO_TLS=$NO_TLS $PY server.py >> $APP_DIR/rerouter.log 2>&1"
  (crontab -l 2>/dev/null | grep -v "$APP_DIR/server.py" || true; echo "$CRON_LINE") | crontab -
  pkill -f "$APP_DIR/server.py" 2>/dev/null || true
  (cd "$APP_DIR" && PORT=$PORT NO_TLS=$NO_TLS nohup "$PY" server.py >> rerouter.log 2>&1 &)
  echo "Cron entry added."
fi

echo
print_urls
