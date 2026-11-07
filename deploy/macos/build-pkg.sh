#!/bin/bash
# Builds DisplayCTL-<version>-<arch>.dmg containing an installer .pkg with a bundled Python.
# Usage: deploy/macos/build-pkg.sh --arch arm64|x86_64 --out DIR [--version X.Y.Z]
# Runs on any Mac (cross-arch is fine: dps is pure Python, so the wheel is unpacked, never executed, for the target).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
ARCH=""; OUT=""; VERSION=""
while [ $# -gt 0 ]; do case "$1" in --arch) ARCH=$2; shift 2;; --out) OUT=$2; shift 2;; --version) VERSION=$2; shift 2;; *) echo "bad arg $1"; exit 2;; esac; done
case "$ARCH" in arm64) PBS=aarch64;; x86_64) PBS=x86_64;; *) echo "--arch arm64|x86_64"; exit 2;; esac
[ -n "$OUT" ] || { echo "--out DIR required"; exit 2; }
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
. "$HERE/bundled-python.env"
PYPROJECT_VERSION=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$REPO/pyproject.toml")
VERSION=${VERSION:-$PYPROJECT_VERSION}
[ "$VERSION" = "$PYPROJECT_VERSION" ] || { echo "version $VERSION != pyproject $PYPROJECT_VERSION"; exit 1; }

W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
TARBALL="cpython-${PY_VERSION}+${PBS_TAG}-${PBS}-apple-darwin-install_only_stripped.tar.gz"
curl -fsSL -o "$W/py.tgz" "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${TARBALL//+/%2B}"
WANT=$(eval echo "\$SHA256_${ARCH}")
echo "$WANT  $W/py.tgz" | shasum -a 256 -c - >/dev/null || { echo "checksum mismatch for $TARBALL"; exit 1; }

R="$W/root/opt/dps"; mkdir -p "$R/bin" "$R/lib" "$R/share"
tar -xzf "$W/py.tgz" -C "$R"                                   # -> $R/python
HOSTPY=${HOSTPY:-python3}
mkdir -p "$W/src"; cp "$REPO/pyproject.toml" "$W/src/"; cp -R "$REPO/dps" "$W/src/dps"; find "$W/src" -name __pycache__ -prune -exec rm -rf {} +
"$HOSTPY" -m pip wheel --quiet --no-deps -w "$W/wheel" "$W/src"   # clean copy: never writes into the repo (build/ may be root-owned)
"$HOSTPY" -m pip install --quiet --no-deps --no-compile --target "$R/lib" "$W"/wheel/dps-*.whl
rm -rf "$R/lib/bin"
cat > "$R/bin/dps" <<'L'
#!/bin/sh
export PYTHONPATH=/opt/dps/lib PYTHONDONTWRITEBYTECODE=1
exec /opt/dps/python/bin/python3 -s -m dps "$@"
L
chmod 755 "$R/bin/dps"
sed 's|/opt/dps/venv/bin/dps|/opt/dps/bin/dps|' "$HERE/com.faire.dps.plist" > "$R/share/com.faire.dps.plist"
cp "$HERE"/{Caddyfile.tmpl,com.faire.dps-caddy.plist,install-https.sh,uninstall.sh} "$R/share/"
chmod 755 "$R"/share/*.sh

# Smoke test the payload as it will run (point the launcher's fixed /opt/dps paths at the build tree).
PYBIN="$R/python/bin/python3"; RUN=("$PYBIN")
[ "$ARCH" = "$(uname -m)" ] || RUN=(arch -x86_64 "$PYBIN")
if "${RUN[@]}" -c pass 2>/dev/null; then
  export DPS_CONFIG="$W/smoke.json" DPS_LOG_DIR="$W"
  PYTHONPATH="$R/lib" "${RUN[@]}" -s -m dps check-config >/dev/null
  GOT=$(PYTHONPATH="$R/lib" "${RUN[@]}" -s -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')
  [ "$GOT" = "$PY_VERSION" ] || { echo "bundled python is $GOT, expected $PY_VERSION"; exit 1; }
  echo "smoke test OK ($ARCH, python $GOT)"
elif [ "$ARCH" = "x86_64" ] && ! arch -x86_64 /usr/bin/true 2>/dev/null; then
  echo "smoke test SKIPPED: no Rosetta on this runner for x86_64"
else
  echo "smoke test FAILED: bundled python will not run"; exit 1
fi
rm -f "$W/smoke.json"

xattr -cr "$W/root" 2>/dev/null || true       # no ._AppleDouble files in the payload
export COPYFILE_DISABLE=1
pkgbuild --quiet --root "$W/root" --identifier com.faire.dps --version "$VERSION" --install-location / \
  --ownership recommended --scripts "$HERE/pkg/scripts" "$W/component.pkg"
cat > "$W/dist.xml" <<X
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>DisplayCTL $VERSION</title>
  <conclusion file="conclusion.html"/>
  <options customize="never" require-scripts="false" hostArchitectures="$ARCH"/>
  <domains enable_anywhere="false" enable_localSystem="true"/>
  <choices-outline><line choice="default"/></choices-outline>
  <choice id="default" title="DisplayCTL"><pkg-ref id="com.faire.dps"/></choice>
  <pkg-ref id="com.faire.dps" version="$VERSION">component.pkg</pkg-ref>
</installer-gui-script>
X
S="$W/dmg"; mkdir -p "$S"
productbuild --distribution "$W/dist.xml" --resources "$HERE/pkg/resources" --package-path "$W" "$S/DisplayCTL-$VERSION-$ARCH.pkg"
cat > "$S/README.txt" <<X
DisplayCTL $VERSION ($ARCH)
Double-click the .pkg. This build is unsigned: if macOS blocks it, right-click > Open (or allow it via MDM).
After install: sudo DPS_CONFIG=/usr/local/etc/dps/config.json dps set-pin   (first install only)
Optional HTTPS: brew install caddy && sudo /opt/dps/share/install-https.sh
Uninstall: sudo /opt/dps/share/uninstall.sh [--purge]
X
rm -f "$OUT/DisplayCTL-$VERSION-$ARCH.dmg"
hdiutil create -quiet -volname "DisplayCTL $VERSION" -srcfolder "$S" -ov -format UDZO "$OUT/DisplayCTL-$VERSION-$ARCH.dmg"
echo "built $OUT/DisplayCTL-$VERSION-$ARCH.dmg"
