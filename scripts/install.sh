#!/bin/sh
# WorkBoard installer for macOS and Linux (x64 and arm64).
#
#   curl -fsSL https://github.com/Paliverse/workboard/releases/latest/download/install.sh | sh
#
# Installs the self-contained release binary into ${XDG_DATA_HOME:-~/.local/share}/workboard
# and links `workboard` and `wb` into ~/.local/bin. It never runs `workboard setup` and never
# edits shell profiles.
#
# Environment:
#   WORKBOARD_VERSION            release to install, e.g. 0.1.0 (default: latest)
#   WORKBOARD_INSTALL_BASE_URL   URL holding the release assets and SHA256SUMS
set -eu

die() {
    printf 'workboard install: %s\n' "$*" >&2
    exit 1
}

case "$(uname -s)" in
    Darwin) os=macos ;;
    Linux) os=linux ;;
    *) die "unsupported OS $(uname -s); on Windows use install.ps1" ;;
esac
case "$(uname -m)" in
    x86_64 | amd64) arch=x64 ;;
    arm64 | aarch64) arch=arm64 ;;
    *) die "unsupported CPU $(uname -m); release binaries exist for x64 and arm64" ;;
esac
# An x64 shell running under Rosetta on Apple silicon still gets the native build.
if [ "$os" = macos ] && [ "$arch" = x64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" = 1 ]; then
    arch=arm64
fi
if [ "$os" = linux ] && ldd --version 2>&1 | grep -qi musl; then
    die "musl-based Linux (e.g. Alpine) cannot run the release binaries; run from source instead: https://github.com/Paliverse/workboard/blob/main/.github/CONTRIBUTING.md#development-setup"
fi

asset="workboard-$os-$arch.tar.gz"
version="${WORKBOARD_VERSION:-}"
version="${version#v}"
if [ -n "${WORKBOARD_INSTALL_BASE_URL:-}" ]; then
    base="${WORKBOARD_INSTALL_BASE_URL%/}"
elif [ -n "$version" ]; then
    base="https://github.com/Paliverse/workboard/releases/download/v$version"
else
    base="https://github.com/Paliverse/workboard/releases/latest/download"
fi

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
else
    die "curl or wget is required"
fi
if command -v sha256sum >/dev/null 2>&1; then
    sha256() { sha256sum "$1" | cut -d ' ' -f 1; }
elif command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$1" | cut -d ' ' -f 1; }
else
    die "sha256sum or shasum is required"
fi

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
install_dir="$data_home/workboard"
bin_dir="$HOME/.local/bin"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
trap 'exit 1' HUP INT TERM

printf 'Downloading %s\n' "$base/$asset"
fetch "$base/$asset" "$tmp/$asset" || die "download failed: $base/$asset"
fetch "$base/SHA256SUMS" "$tmp/SHA256SUMS" || die "download failed: $base/SHA256SUMS"
expected="$(awk -v name="$asset" '{ file = $2; sub(/^\*/, "", file); if (file == name) print $1 }' "$tmp/SHA256SUMS")"
[ -n "$expected" ] || die "SHA256SUMS has no entry for $asset"
actual="$(sha256 "$tmp/$asset")"
[ "$actual" = "$expected" ] || die "checksum mismatch for $asset: expected $expected, got $actual"

tar -xzf "$tmp/$asset" -C "$tmp"
[ -x "$tmp/workboard/workboard" ] || die "$asset has no workboard/workboard executable"
mkdir -p "$data_home" "$bin_dir"
# Stage next to the target so the final swap is a same-filesystem rename.
rm -rf "$install_dir.new"
mv "$tmp/workboard" "$install_dir.new"
rm -rf "$install_dir"
mv "$install_dir.new" "$install_dir"
ln -sf "$install_dir/workboard" "$bin_dir/workboard"
ln -sf "$install_dir/workboard" "$bin_dir/wb"

installed="$("$install_dir/workboard" --version)" || die "the installed binary does not run: $install_dir/workboard"
installed="${installed#workboard }"
json_string() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
printf '{"channel": "script", "version": "%s", "installedAt": "%s", "installDir": "%s"}\n' \
    "$(json_string "$installed")" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(json_string "$install_dir")" \
    >"$install_dir/install-receipt.json"

printf 'WorkBoard %s installed to %s\n' "$installed" "$install_dir"
case ":${PATH}:" in
    *":$bin_dir:"*) ;;
    *) printf '%s is not on PATH; add it, e.g. in your shell profile: export PATH="$HOME/.local/bin:$PATH"\n' "$bin_dir" ;;
esac
printf 'Run: workboard setup\n'
