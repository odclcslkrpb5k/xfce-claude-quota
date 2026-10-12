#!/usr/bin/env bash
#
# install.sh — set up the Claude Quota XFCE panel widget.
#
# This is intentionally conservative: it checks dependencies, makes the script
# executable, writes a default config, and installs an application-menu launcher
# for the detailed view. It does NOT modify your panel automatically (poking
# xfconf to inject panel items is fragile); instead it prints the exact 4-click
# genmon setup at the end.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN="$HERE/xfce-claude-quota.py"

echo "==> Claude Quota installer"
echo "    plugin dir: $HERE"

# --- 1. Dependency check ----------------------------------------------------
missing_pkgs=()

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# genmon plugin (the panel host)
if ! ls /usr/share/xfce4/panel/plugins/genmon.desktop >/dev/null 2>&1; then
    missing_pkgs+=("xfce4-genmon-plugin")
fi

# Python 3
if ! need_cmd python3; then
    echo "ERROR: python3 not found." >&2; exit 1
fi

# pycairo (panel image) + PyGObject/GTK3 (detail + prefs windows)
python3 - <<'PY' || PYDEPS_FAIL=1
import sys
ok = True
try:
    import cairo  # noqa
except Exception:
    print("  - missing: python3 pycairo (python3-cairo)"); ok = False
try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # noqa
except Exception:
    print("  - missing: PyGObject + GTK3 (python3-gi gir1.2-gtk-3.0)"); ok = False
sys.exit(0 if ok else 3)
PY
if [ "${PYDEPS_FAIL:-0}" = "1" ]; then
    missing_pkgs+=("python3-gi" "gir1.2-gtk-3.0" "python3-cairo" "python3-gi-cairo")
fi

if [ "${#missing_pkgs[@]}" -gt 0 ]; then
    # de-duplicate
    uniq_pkgs="$(printf '%s\n' "${missing_pkgs[@]}" | sort -u | tr '\n' ' ')"
    echo
    echo "==> Missing dependencies. Install them with:"
    echo "      sudo apt install $uniq_pkgs"
    echo
    echo "    Then re-run this script."
    exit 1
fi
echo "    dependencies: OK"

# --- 2. Make executable -----------------------------------------------------
chmod +x "$MAIN"

# --- 3. Default config ------------------------------------------------------
echo "==> Writing default configuration (if absent) and indexing your data..."
python3 "$MAIN" --self-test >/dev/null 2>&1 || true
# Touch the config by opening/saving defaults non-interactively:
python3 - "$MAIN" <<'PY'
import importlib.util, sys, os
spec = importlib.util.spec_from_file_location("m", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
if not os.path.exists(m.CONFIG_FILE):
    m.save_config(m.load_config())
print("    config: " + m.CONFIG_FILE)
PY

# --- 4. Application-menu launcher for the detailed view ---------------------
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS_DIR"
LAUNCHER="$APPS_DIR/xfce-claude-quota.desktop"
cat > "$LAUNCHER" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Claude Quota — Detailed Usage
Comment=Detailed Claude Code usage: graph over time and per-message events
Exec=python3 "$MAIN" --detail
Icon=utilities-system-monitor
Terminal=false
Categories=Utility;System;Monitor;
Keywords=claude;usage;quota;tokens;
EOF
update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
echo "    menu launcher: $LAUNCHER"

# --- 5. Final instructions --------------------------------------------------
cat <<EOF

==> Done. Add it to your panel (4 clicks):

  1. Right-click the panel → Panel → Add New Items…
  2. Choose "Generic Monitor" → Add.
  3. Right-click the new item → Properties.
  4. Set:
        Command : python3 "$MAIN"
        Period  : 15  (seconds — this is your refresh interval)
        Label   : (clear it / untick "Display label")

  The widget then shows usage bars with the percentages over them. Left-click
  it to open the detailed view; "Preferences…" there sets which bars are shown,
  their colours and direction, the number font, and the refresh intervals.

  Quick check from a terminal:   python3 "$MAIN" --self-test
EOF
