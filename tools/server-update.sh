#!/usr/bin/env bash
# inventree-xero-update - update the Xero plugin on an InvenTree server (package installer)
#
# Install once on the server:
#   sudo curl -fsSL -o /usr/local/bin/inventree-xero-update \
#        https://raw.githubusercontent.com/SXSLYDA/inventree-xero/main/tools/server-update.sh
#   sudo chmod +x /usr/local/bin/inventree-xero-update
#
# Use:
#   sudo inventree-xero-update                 # newest release tag on GitHub
#   sudo inventree-xero-update v1.4.2          # a specific release
#   sudo inventree-xero-update --check-only    # show installed / pinned / newest, change nothing
#   sudo inventree-xero-update v1.4.1 --allow-downgrade   # roll back (read the warning)
#
# What it does:
#   1. Finds plugins.txt (from /etc/inventree/config.yaml) and the release tag to install.
#   2. Backs up the database (inventree run invoke backup) and plugins.txt.
#   3. Points the inventree-xero line in plugins.txt at that tag (adds it if missing).
#   4. Runs "inventree run invoke update" (installs it, runs migrations, static files).
#   5. Restarts InvenTree and confirms the installed version matches the tag.
#   6. Runs the plugin's read-only configuration check against Xero.
# If the update fails, plugins.txt is put back and it says how to reinstall the previous version.
#
# Options: --no-backup  --no-check  --allow-downgrade  --check-only
#          --repo URL (default: the GitHub repo)  --plugins-file PATH  --app-home PATH

set -euo pipefail

REPO="${XERO_PLUGIN_REPO:-https://github.com/SXSLYDA/inventree-xero.git}"
PACKAGE="inventree-xero"
APP_HOME="${INVENTREE_APP_HOME:-/opt/inventree}"
CONFIG="${INVENTREE_CONFIG_FILE:-/etc/inventree/config.yaml}"
PLUGINS_FILE=""
TAG=""
BACKUP=1
CHECK=1
ALLOW_DOWNGRADE=0
CHECK_ONLY=0

say()  { printf '\n\033[36m=== %s ===\033[0m\n' "$*"; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mSTOPPED: %s\033[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        v[0-9]*.[0-9]*.[0-9]*) TAG="$1" ;;
        --no-backup) BACKUP=0 ;;
        --no-check) CHECK=0 ;;
        --allow-downgrade) ALLOW_DOWNGRADE=1 ;;
        --check-only) CHECK_ONLY=1 ;;
        --repo) REPO="$2"; shift ;;
        --plugins-file) PLUGINS_FILE="$2"; shift ;;
        --app-home) APP_HOME="$2"; shift ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *) die "unknown option '$1' (try --help)" ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || die "run with sudo (the InvenTree service commands need root)."
command -v inventree >/dev/null || die "the 'inventree' command isn't here - this script is for package-installer servers."
command -v git >/dev/null || die "git isn't installed (apt install git)."

vercmp() {  # prints -1, 0 or 1 comparing X.Y.Z strings
    if [ "$1" = "$2" ]; then echo 0; return; fi
    if [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]; then echo -1; else echo 1; fi
}

installed_version() {
    inventree run python3 -c "import importlib.metadata as m; print(m.version('$PACKAGE'))" 2>/dev/null | tail -1 || true
}

# ------------------------------------------------------------------ 1. where and what
if [ -z "$PLUGINS_FILE" ]; then
    if [ -f "$CONFIG" ]; then
        PLUGINS_FILE="$(sed -n 's/^[[:space:]]*plugin_file:[[:space:]]*//p' "$CONFIG" | head -1 | tr -d "\"'" | sed 's/[[:space:]]*#.*$//')"
    fi
    PLUGINS_FILE="${PLUGINS_FILE:-/etc/inventree/plugins.txt}"
fi
[ -f "$PLUGINS_FILE" ] || die "plugins file '$PLUGINS_FILE' not found (use --plugins-file)."

say "Release check"
TAGS="$(git ls-remote --tags --refs "$REPO" 2>/dev/null | sed -n 's#.*refs/tags/\(v[0-9]*\.[0-9]*\.[0-9]*\)$#\1#p' | sort -V)" \
    || die "couldn't list release tags from $REPO (network? repo private?)."
[ -n "$TAGS" ] || die "no vX.Y.Z release tags found in $REPO."
NEWEST="$(printf '%s\n' "$TAGS" | tail -1)"
PIN_LINE="$(grep -E "^[[:space:]]*${PACKAGE}([[:space:]]|@|$)" "$PLUGINS_FILE" | head -1 || true)"
PINNED="$(printf '%s' "$PIN_LINE" | sed -n 's#.*@\(v[0-9]*\.[0-9]*\.[0-9]*\)[[:space:]]*$#\1#p')"
INSTALLED="$(installed_version)"

echo "Repository:      $REPO"
echo "plugins.txt:     $PLUGINS_FILE"
echo "Pinned now:      ${PINNED:-${PIN_LINE:-(not in plugins.txt)}}"
echo "Installed now:   ${INSTALLED:-(not installed)}"
echo "Newest release:  $NEWEST"
[ "$CHECK_ONLY" -eq 1 ] && exit 0

TAG="${TAG:-$NEWEST}"
printf '%s\n' "$TAGS" | grep -qx "$TAG" || die "$TAG isn't a release tag in $REPO. Releases: $(printf '%s ' $TAGS)"
TARGET="${TAG#v}"

if [ -n "$INSTALLED" ]; then
    case "$(vercmp "$TARGET" "$INSTALLED")" in
        0)  if [ "$PINNED" = "$TAG" ]; then ok "Already on $TAG - nothing to do."; exit 0; fi ;;
        -1) [ "$ALLOW_DOWNGRADE" -eq 1 ] || die "$TAG is older than the installed $INSTALLED. To roll back, add --allow-downgrade (and read its warning)."
            warn "ROLLING BACK $INSTALLED -> $TARGET. Database changes (migrations) made by the newer version are NOT undone;"
            warn "only roll back to a version with the same migrations, or restore the database backup made below." ;;
    esac
fi

# ------------------------------------------------------------------ 2. backups
STAMP="$(date +%Y%m%d-%H%M%S)"
say "Backing up"
cp -p "$PLUGINS_FILE" "$PLUGINS_FILE.bak-$STAMP"
echo "plugins.txt -> $PLUGINS_FILE.bak-$STAMP"
if [ "$BACKUP" -eq 1 ]; then
    inventree run invoke backup || die "the database backup failed - not updating. (Use --no-backup to skip it.)"
    ok "Database backed up (invoke backup)."
else
    warn "Database backup skipped (--no-backup)."
fi

restore_pin() {
    cp -p "$PLUGINS_FILE.bak-$STAMP" "$PLUGINS_FILE"
    warn "plugins.txt restored from $PLUGINS_FILE.bak-$STAMP."
    warn "To reinstall the previous version:  sudo inventree run invoke update && sudo inventree restart"
}

# ------------------------------------------------------------------ 3. pin
say "Pinning $PACKAGE to $TAG"
NEW_LINE="$PACKAGE @ git+$REPO@$TAG"
TMP="$(mktemp)"
if [ -n "$PIN_LINE" ]; then
    awk -v pkg="$PACKAGE" -v line="$NEW_LINE" '
        BEGIN { done = 0 }
        { if (!done && $0 ~ "^[[:space:]]*" pkg "([[:space:]]|@|$)") { print line; done = 1 } else { print } }
    ' "$PLUGINS_FILE" > "$TMP"
else
    cat "$PLUGINS_FILE" > "$TMP"
    [ -z "$(tail -c1 "$PLUGINS_FILE")" ] || echo >> "$TMP"
    echo "$NEW_LINE" >> "$TMP"
fi
cat "$TMP" > "$PLUGINS_FILE"   # keep the file's owner and permissions
rm -f "$TMP"
echo "$NEW_LINE"

# ------------------------------------------------------------------ 4. update
say "Updating InvenTree (install, migrate, static files)"
if ! inventree run invoke update; then
    restore_pin
    die "'invoke update' failed (output above). Nothing else was changed."
fi

# ------------------------------------------------------------------ 5. restart and verify
say "Restarting"
if systemctl list-unit-files 2>/dev/null | grep -q '^inventree\.service'; then
    systemctl restart inventree
else
    inventree restart
fi
NOW="$(installed_version)"
if [ "$NOW" = "$TARGET" ]; then
    ok "inventree-xero $NOW is installed."
else
    restore_pin
    die "expected $TARGET to be installed but found '${NOW:-nothing}'."
fi

# ------------------------------------------------------------------ 6. configuration check
if [ "$CHECK" -eq 1 ]; then
    say "Configuration check (read-only)"
    MANAGE="$APP_HOME/src/backend/InvenTree/manage.py"
    if [ -f "$MANAGE" ]; then
        sleep 5
        inventree run python3 "$MANAGE" xero_check || warn "The configuration check reported problems (above) - look at them before the next sync."
    else
        warn "manage.py not found at $MANAGE - skipping the check (use --app-home)."
    fi
fi

echo
ok "Done: $PACKAGE $TAG. Hard-refresh InvenTree in your browser (Ctrl+F5) to load the new panels."
echo "Backup of the old plugins.txt: $PLUGINS_FILE.bak-$STAMP"
