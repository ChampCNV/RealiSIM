#!/bin/sh
# Reproducible install of the RealiSIM Factory runtime (idempotent). Official sources only:
#   CLI  https://downloads.factory.ai/factory-cli/releases/0.249.0/linux/<arch>/droid (+ .sha256), as used by https://app.factory.ai/cli
#   SDK  PyPI droid-sdk[mcp]==0.5.0
set -e
F=/workspace/photo2grasp/factory
VER=0.249.0
ARCH=x64; grep -q -i avx2 /proc/cpuinfo || ARCH=x64-baseline
mkdir -p "$F/bin"
if [ ! -x "$F/bin/droid" ] || [ "$("$F/bin/droid" --version 2>/dev/null)" != "$VER" ]; then
  URL="https://downloads.factory.ai/factory-cli/releases/$VER/linux/$ARCH/droid"
  curl -fsSL -o "$F/bin/droid.download" "$URL"
  EXP=$(curl -fsSL "$URL.sha256" | awk '{print $1}')
  ACT=$(sha256sum "$F/bin/droid.download" | awk '{print $1}')
  [ "$EXP" = "$ACT" ] || { echo "checksum mismatch"; rm -f "$F/bin/droid.download"; exit 1; }
  mv "$F/bin/droid.download" "$F/bin/droid"; chmod 755 "$F/bin/droid"
fi
[ -x "$F/.venv/bin/python" ] || uv venv --python 3.11 "$F/.venv"
"$F/.venv/bin/python" -c "import droid_sdk,sys; sys.exit(0 if droid_sdk.PACKAGE_VERSION=='0.5.0' else 1)" 2>/dev/null || \
  uv pip install --python "$F/.venv/bin/python" 'droid-sdk[mcp]==0.5.0'
echo "factory runtime ready: cli $("$F/bin/droid" --version) sdk $("$F/.venv/bin/python" -c 'import droid_sdk;print(droid_sdk.PACKAGE_VERSION)')"
