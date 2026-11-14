#!/bin/sh
# Installs ubo-lpa for the current user and sets up its daily update:
#
#   curl -fsSL https://raw.githubusercontent.com/ojura/ubo-lpa/main/install.sh | sh
#   wget -qO- https://raw.githubusercontent.com/ojura/ubo-lpa/main/install.sh | sh
#
# The repository is kept in $XDG_DATA_HOME/ubo-lpa-source (by default
# ~/.local/share/ubo-lpa-source): a git checkout when git is installed,
# otherwise the main branch's archive. The daily timer runs ubo-lpa.py from
# there, so the directory has to stay. Running this script again updates
# ubo-lpa, then uBO.
#
# UBO_LPA_REPO selects another repository, for testing a fork.
#
# Everything runs from main() at the last line, so a download that is cut
# short does not run a partial script.

set -eu

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

download() {  # download URL FILE
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1"
    else
        fail "neither curl nor wget is installed"
    fi
}

fetch_archive() {  # fetch_archive URL DIR
    parent=$(dirname "$2")
    mkdir -p "$parent"
    staging=$(mktemp -d "$2.new.XXXXXX")
    if ! download "$1" "$staging/main.tar.gz" \
        || ! tar -xzf "$staging/main.tar.gz" -C "$staging" --strip-components=1; then
        rm -rf "$staging"
        fail "could not download and unpack $1"
    fi
    rm -f "$staging/main.tar.gz"
    rm -rf "$2"
    mv "$staging" "$2"
}

main() {
    repo=${UBO_LPA_REPO:-https://github.com/ojura/ubo-lpa}
    src=${XDG_DATA_HOME:-$HOME/.local/share}/ubo-lpa-source

    command -v python3 >/dev/null 2>&1 || fail "python3 is not installed"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
        || fail "ubo-lpa needs Python 3.9 or newer, and python3 is $(python3 -V 2>&1)"

    if [ -d "$src/.git" ]; then
        say "Updating $src"
        git -C "$src" pull --ff-only --quiet
    elif [ ! -e "$src" ] && command -v git >/dev/null 2>&1; then
        say "Cloning $repo into $src"
        git clone --quiet "$repo" "$src"
    else
        say "Downloading $repo into $src"
        fetch_archive "$repo/archive/refs/heads/main.tar.gz" "$src"
    fi

    # Through a pipe, stdin is the rest of this script, so the installer
    # gets /dev/null instead.
    status=0
    python3 "$src/ubo-lpa.py" install --non-interactive </dev/null || status=$?
    python3 "$src/ubo-lpa.py" timer </dev/null
    if [ "$status" -ne 0 ]; then
        say ""
        say "install exited with status $status. Once the problem it reported is"
        say "fixed (usually by closing Chrome), finish with:"
        say "  python3 $src/ubo-lpa.py install"
        exit "$status"
    fi
}

main "$@"
