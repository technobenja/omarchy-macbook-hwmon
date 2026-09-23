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

    log "installing systemd user unit"
    mkdir -p "$SYSTEMD_USER_DIR"
    install -m 0644 "$REPO_DIR/systemd/$SERVICE_NAME" "$SYSTEMD_USER_DIR/$SERVICE_NAME"
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"

    # B3 (advisor, measured): `enable --now` alone does not restart an
    # ALREADY-ACTIVE unit -- on an update this left the collector running
    # the OLD package with the OLD schema. Always `restart` explicitly, so
    # a fresh install and an update behave the same way.
    log "restarting $SERVICE_NAME to pick up the installed package"
    systemctl --user restart "$SERVICE_NAME"

    # B3: only stage the plugin once the collector has proven it is
    # actually running the new schema -- a stale collector writing schema 1
    # under a plugin built for schema 2 is exactly what A9 staleness
    # handling is NOT meant to paper over.
    #
    # S3 (advisor): wait at least 10s, not 3 -- 3s cut it too close against
    # a cold-cache Python import + first-tick sensor discovery. The
    # `got_schema=... || true` guards the command-substitution assignment
    # so a non-zero exit from the python probe (a transient read racing the
    # collector's atomic rename, for instance) can't abort this script
    # under `set -e` mid-loop -- an empty/mismatched `$got_schema` already
    # falls through to another `sleep` iteration on its own.
    log "waiting up to 10s for $SERVICE_NAME to report the current schema"
    runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    latest_json="$runtime_dir/hwmon/latest.json"
    want_schema="$(python3 -c "import sys; sys.path.insert(0, '$REPO_DIR'); from hwmon.snapshot import SCHEMA_VERSION; print(SCHEMA_VERSION)")"
    schema_ok=0
    for _ in $(seq 1 20); do
        if [ -f "$latest_json" ]; then
            got_schema="$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get('schema'))
except Exception:
    print('')
" "$latest_json" 2>/dev/null)" || true
            if [ -n "$got_schema" ] && [ "$got_schema" = "$want_schema" ]; then
                schema_ok=1
                break
            fi
        fi
        sleep 0.5
    done
    if [ "$schema_ok" != "1" ]; then
        die "$SERVICE_NAME did not report schema $want_schema within 10s after restart -- the machine is now HALF-UPGRADED (package and service replaced, but the bar plugin was NOT staged, so it still expects the old schema; once the collector does catch up it will show 'hwmon —' in the bar per A9 until the plugin is updated too). Check 'journalctl --user -u $SERVICE_NAME', then re-run install.sh."
    fi

    log "staging plugin copy (real copy, never a symlink)"
    mkdir -p "$PLUGIN_DEST_DIR"
    stage_dir="$(mktemp -d "$PLUGIN_DEST_DIR/.stage-${PLUGIN_ID}.XXXXXX")"
    trap 'rm -rf "$stage_dir"' EXIT
    cp -rL "$PLUGIN_SRC" "$stage_dir/$PLUGIN_ID"
    rm -rf "$PLUGIN_DEST"
    mv "$stage_dir/$PLUGIN_ID" "$PLUGIN_DEST"
    trap - EXIT
    rm -rf "$stage_dir"

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
    log "remember: run 'omarchy restart shell' -- Quickshell keeps cached popup QML across updates"
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
