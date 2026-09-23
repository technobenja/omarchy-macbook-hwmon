#!/usr/bin/env bash
# hwmon install / uninstall (spec A11). No root. Idempotent.
#
# Install layout:
#   ~/.local/bin/hwmon                 thin launcher (bin/hwmon)
#   ~/.local/share/hwmon/lib/hwmon/    the Python package (stdlib only)
#   ~/.local/share/hwmon/hwmon.db      SQLite history (kept across reinstall,
#                                      removed only by --uninstall --purge)
#   ~/.config/systemd/user/hwmon.service
#   ~/.config/omarchy/plugins/techno.hwmon/   real copy, never a symlink
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="techno.hwmon"
PLUGIN_SRC="$REPO_DIR/plugin/$PLUGIN_ID"
PLUGIN_DEST_DIR="$HOME/.config/omarchy/plugins"
PLUGIN_DEST="$PLUGIN_DEST_DIR/$PLUGIN_ID"
BIN_DEST="$HOME/.local/bin/hwmon"
LIB_DEST="$HOME/.local/share/hwmon/lib/hwmon"
DB_PATH="$HOME/.local/share/hwmon/hwmon.db"
SHELL_JSON="$HOME/.config/omarchy/shell.json"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="hwmon.service"

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: install.sh [--uninstall [--purge]]

  (no args)     install/update hwmon: CLI, systemd user service, and the
                omarchy-shell bar widget. Safe to re-run.
  --uninstall   disable the widget, stop the service, remove the CLI,
                package and plugin copy. Keeps hwmon.db unless --purge.
  --purge       only valid together with --uninstall: also delete hwmon.db.
EOF
}

do_install() {
    command -v omarchy >/dev/null 2>&1 || die "omarchy CLI not found on PATH"
    command -v omarchy-shell >/dev/null 2>&1 || die "omarchy-shell CLI not found on PATH"
    command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"
    command -v systemctl >/dev/null 2>&1 || die "systemctl not found on PATH"
    [ -d "$PLUGIN_SRC" ] || die "plugin source not found: $PLUGIN_SRC"

    log "validating plugin"
    omarchy plugin validate "$PLUGIN_SRC"

    log "installing CLI to $BIN_DEST"
    mkdir -p "$(dirname "$BIN_DEST")"
    install -m 0755 "$REPO_DIR/bin/hwmon" "$BIN_DEST"

    log "installing package to $LIB_DEST"
    mkdir -p "$(dirname "$LIB_DEST")"
    rm -rf "$LIB_DEST"
    cp -rL "$REPO_DIR/hwmon" "$LIB_DEST"

    log "staging plugin copy (real copy, never a symlink)"
    mkdir -p "$PLUGIN_DEST_DIR"
    stage_dir="$(mktemp -d "$PLUGIN_DEST_DIR/.stage-${PLUGIN_ID}.XXXXXX")"
    trap 'rm -rf "$stage_dir"' EXIT
    cp -rL "$PLUGIN_SRC" "$stage_dir/$PLUGIN_ID"
    rm -rf "$PLUGIN_DEST"
    mv "$stage_dir/$PLUGIN_ID" "$PLUGIN_DEST"
    trap - EXIT
    rm -rf "$stage_dir"

    log "installing systemd user unit"
    mkdir -p "$SYSTEMD_USER_DIR"
    install -m 0644 "$REPO_DIR/systemd/$SERVICE_NAME" "$SYSTEMD_USER_DIR/$SERVICE_NAME"
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"

    if [ -f "$SHELL_JSON" ]; then
        backup="$SHELL_JSON.bak.$(date +%Y%m%dT%H%M%S)"
        log "backing up shell.json to $backup"
        cp -p "$SHELL_JSON" "$backup"
    else
        log "no existing shell.json to back up yet"
    fi

    log "rescanning plugins"
    omarchy-shell shell rescanPlugins

    log "enabling widget in the bar (right, before omarchy.power)"
    omarchy plugin enable "$PLUGIN_ID" --section right --before omarchy.power

    log "install complete"
    log "on an UPDATE: run 'systemctl --user restart $SERVICE_NAME' and 'omarchy restart shell'"
    log "(the shell keeps cached popup QML; a running collector keeps old code)"
}

do_uninstall() {
    purge="$1"

    if command -v omarchy >/dev/null 2>&1; then
        log "disabling widget"
        omarchy plugin disable "$PLUGIN_ID" || true
    fi

    if command -v systemctl >/dev/null 2>&1; then
        log "stopping service"
        systemctl --user disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
        rm -f "$SYSTEMD_USER_DIR/$SERVICE_NAME"
        systemctl --user daemon-reload || true
    fi

    log "removing plugin copy"
    rm -rf "$PLUGIN_DEST"

    log "removing CLI and package"
    rm -f "$BIN_DEST"
    rm -rf "$(dirname "$LIB_DEST")"

    if [ "$purge" = "1" ]; then
        log "purging $DB_PATH"
        rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"
    else
        log "keeping $DB_PATH (pass --purge to delete it)"
    fi

    log "uninstall complete"
}

purge=0
uninstall=0
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) uninstall=1 ;;
        --purge) purge=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

if [ "$purge" = "1" ] && [ "$uninstall" != "1" ]; then
    die "--purge is only valid together with --uninstall"
fi

if [ "$uninstall" = "1" ]; then
    do_uninstall "$purge"
else
    do_install
fi
