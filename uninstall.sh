#!/usr/bin/env bash
# mytool uninstaller — removes auto-run, CLI symlink; --purge also wipes
# the code dir and ~/.my_ai_tool data (backups included!).
set -euo pipefail

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

echo "== mytool uninstaller =="

# 1. cron
if command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v "# mytool-cron" || true ) | crontab - || true
    echo "cron   : mytool entry removed"
fi

# 2. systemd units
for u in mytool-tick.timer mytool-tick.service mytool-daemon.service; do
    systemctl --user disable --now "$u" >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/$u"
done
systemctl --user daemon-reload >/dev/null 2>&1 || true
echo "systemd: units removed"

# 3. symlink
rm -f "$HOME/.local/bin/mytool"
echo "cli    : symlink removed"

# 4. dirs
if [ "$PURGE" = "1" ]; then
    read -r -p "Delete CODE ($HOME/my_ai_tool_app) AND ALL DATA ($HOME/.my_ai_tool)? [y/N] " a
    if [ "${a:-n}" = "y" ]; then
        rm -rf "$HOME/my_ai_tool_app" "$HOME/.my_ai_tool"
        echo "purge  : code + data deleted"
    else
        echo "purge  : skipped"
    fi
else
    echo "kept   : $HOME/my_ai_tool_app (code) and $HOME/.my_ai_tool (data)"
    echo "         delete manually, ya sab kuch hatane ke liye: ./uninstall.sh --purge"
fi
echo "done."
