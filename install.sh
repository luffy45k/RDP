#!/usr/bin/env bash
# ============================================================================
# mytool installer (Linux)
#   - code  -> git repo (~/my_ai_tool_app by default, or this folder if it
#              already is a git clone) — self-updates via git pull
#   - data  -> ~/.my_ai_tool/  (database.db, config.json, logs, backups)
#   - auto-run -> cron job (default) | --systemd timer | --daemon-service
#
# usage:
#   ./install.sh                       # cron every 15 min
#   MYTOOL_INTERVAL_MIN=30 ./install.sh
#   ./install.sh --interval 20
#   ./install.sh --systemd             # user systemd timer instead of cron
#   ./install.sh --daemon-service      # always-on systemd user service
# ============================================================================
set -euo pipefail

INTERVAL="${MYTOOL_INTERVAL_MIN:-15}"
MODE="cron"
while [ $# -gt 0 ]; do
    case "$1" in
        --interval)        INTERVAL="$2"; shift 2 ;;
        --systemd)         MODE="systemd"; shift ;;
        --daemon-service)  MODE="daemon"; shift ;;
        -h|--help)         sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

echo "== mytool installer =="

# ---------------------------------------------------------------- 1. deps
command -v git >/dev/null 2>&1 || { echo "ERROR: git is required (sudo apt install git)"; exit 1; }
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required (sudo apt install python3 python3-venv)"; exit 1
fi
PYTHON3="$(command -v python3)"

# ---------------------------------------------------------------- 2. code
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="${MYTOOL_CODE_DIR:-$HOME/my_ai_tool_app}"

if [ -d "$SCRIPT_DIR/.git" ]; then
    CODE_DIR="$SCRIPT_DIR"          # installing straight from a git clone
    echo "code  : using this clone at $CODE_DIR"
elif [ -d "$CODE_DIR/.git" ]; then
    echo "code  : existing install at $CODE_DIR (git pull)"
    git -C "$CODE_DIR" pull --rebase --autostash || true
else
    REMOTE="${MYTOOL_REPO_URL:-https://github.com/luffy45k/RDP.git}"
    echo "code  : cloning $REMOTE -> $CODE_DIR"
    git clone "$REMOTE" "$CODE_DIR"
fi

# ---------------------------------------------------------------- 3. venv
DATA_DIR="$HOME/.my_ai_tool"
mkdir -p "$DATA_DIR"
if [ ! -x "$DATA_DIR/venv/bin/python" ]; then
    echo "venv  : creating $DATA_DIR/venv (isolated, zero pip deps needed)"
    if ! "$PYTHON3" -m venv "$DATA_DIR/venv" 2>/dev/null; then
        echo "venv  : venv module unavailable — will use system python3 (OK)"
    fi
fi

# ---------------------------------------------------------------- 4. CLI
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
ln -sf "$CODE_DIR/bin/mytool" "$BIN_DIR/mytool"
chmod +x "$CODE_DIR/bin/mytool"
MYTOOL="$BIN_DIR/mytool"

"$MYTOOL" init
"$MYTOOL" selftest --core || true

# ---------------------------------------------------------------- 5. auto-run
CRON_MARK="# mytool-cron"
install_cron() {
    if ! command -v crontab >/dev/null 2>&1; then
        echo "cron  : crontab not found — skipping (use --systemd or run daemon manually)"
        return 0
    fi
    ( crontab -l 2>/dev/null | grep -v "$CRON_MARK" || true
      echo "*/$INTERVAL * * * * $MYTOOL cron-tick --quiet >> $DATA_DIR/logs/cron.log 2>&1 $CRON_MARK"
    ) | crontab -
    echo "cron  : installed — runs every $INTERVAL min (pick divisors of 60: 5/10/15/20/30)"
}

install_systemd_timer() {
    local UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/mytool-tick.service" <<EOF
[Unit]
Description=mytool background tick (update + heal + queued tasks)

[Service]
Type=oneshot
ExecStart=$MYTOOL cron-tick --quiet
EOF
    cat > "$UNIT_DIR/mytool-tick.timer" <<EOF
[Unit]
Description=Run mytool tick every $INTERVAL minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=${INTERVAL}min
Unit=mytool-tick.service

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now mytool-tick.timer
    echo "systemd: tick timer enabled (every $INTERVAL min)"
}

install_daemon_service() {
    local UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/mytool-daemon.service" <<EOF
[Unit]
Description=mytool always-on AI daemon (update + heal + tasks)

[Service]
ExecStart=$MYTOOL daemon --interval $INTERVAL
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now mytool-daemon.service
    echo "systemd: always-on daemon service enabled"
}

case "$MODE" in
    cron)    install_cron ;;
    systemd) install_systemd_timer ;;
    daemon)  install_daemon_service ;;
esac

# ---------------------------------------------------------------- 6. brain
if curl -s --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "brain : Ollama detected ✔"
else
    cat <<'HINT'

brain  : Ollama NOT detected on localhost:11434.
         1) install : curl -fsSL https://ollama.com/install.sh | sh
         2) model   : ollama pull hermes3:3b      # Nous Research Hermes (chhote server)
                      # 7GB+ RAM ho toh: ollama pull hermes3:8b
         3) ya auto : mytool setup-ollama         # sab khud kar deta hai
         4) test    : mytool status
         (testing bina server ke: mytool config set provider mock)
HINT
fi

echo
echo "== install complete =="
echo "  PATH mein add karo (agar ~/.local/bin nahi hai):"
echo "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
echo "  try:"
echo "      mytool status"
echo "      mytool do-task 'list files larger than 10MB in home'"
echo "      mytool crash-test --demo    # self-healing dekhne ke liye"
