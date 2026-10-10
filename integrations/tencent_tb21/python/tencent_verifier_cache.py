"""Supply the existing uv 0.9.5 archive without changing verifier commands."""
from pathlib import Path

ARCHIVE = Path('/data2/liyishan/tbench2-openclaw-min/cache/uv/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz')
SANDBOX_ARCHIVE = '/opt/tbench-cache/uv/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz'

SHIM = r'''#!/bin/sh
set -eu
emit_installer() {
cat <<'INSTALLER'
#!/bin/sh
set -eu
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
tar -xzf /opt/tbench-cache/uv/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz -C "$tmp"
dest="${UV_INSTALL_DIR:-$HOME/.local/bin}"
mkdir -p "$dest"
for binary in uv uvx; do
    staged=$(mktemp "$dest/.$binary.XXXXXX")
    cp "$tmp/uv-x86_64-unknown-linux-gnu/$binary" "$staged"
    chmod +x "$staged"
    mv -f "$staged" "$dest/$binary"
done
printf 'export PATH="%s:$PATH"\n' "$dest" > "$dest/env"
echo 'using cached uv 0.9.5 x86_64-unknown-linux-gnu'
INSTALLER
}
matched=0
output=''
next_output=0
for arg in "$@"; do
    if [ "$next_output" = 1 ]; then output="$arg"; next_output=0; continue; fi
    case "$arg" in
        https://astral.sh/uv/0.9.5/install.sh) matched=1 ;;
        -o|--output) next_output=1 ;;
        --output=*) output=${arg#--output=} ;;
        -o?*) output=${arg#-o} ;;
    esac
done
if [ "$matched" = 1 ] && [ "$(uname -m)" = x86_64 ] &&
   [ -r /opt/tbench-cache/uv/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz ]; then
    if [ -n "$output" ] && [ "$output" != '-' ]; then
        emit_installer > "$output"
    else
        emit_installer
    fi
    exit 0
fi
exec /usr/bin/curl "$@"
'''


async def configure(sandbox):
    if not ARCHIVE.is_file():
        return False
    await sandbox.files.write_files([
        {'path': SANDBOX_ARCHIVE, 'data': ARCHIVE.read_bytes()},
        {'path': '/usr/local/bin/curl', 'data': SHIM},
    ], user='root', request_timeout=120)
    return True
