#!/bin/sh
# Builds dist/salvaguarda_<versão>_all.deb. Needs only dpkg-deb, which every
# Debian-based distribution already has, and works without internet.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/salvaguarda/__init__.py")
APP_ID=io.github.franciscovazquezreis.Salvaguarda
NAME=salvaguarda_${VERSION}_all
OUT="$ROOT/dist"
WORK=$(mktemp -d)
PKG="$WORK/$NAME"
trap 'rm -rf "$WORK"' EXIT

# Reproducible timestamps (date of the last commit when available).
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}
export SOURCE_DATE_EPOCH

install -d "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/share/salvaguarda" \
    "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps" \
    "$PKG/usr/share/metainfo" "$PKG/usr/share/doc/salvaguarda" "$PKG/usr/share/man/man1"

# Application code (without caches)
tar -C "$ROOT" --exclude='__pycache__' --exclude='*.pyc' -cf - salvaguarda | tar -C "$PKG/usr/share/salvaguarda" -xf -
install -m 0755 "$ROOT/packaging/salvaguarda.launcher" "$PKG/usr/bin/salvaguarda"

# Desktop integration
install -m 0644 "$ROOT/data/$APP_ID.desktop" "$PKG/usr/share/applications/"
install -m 0644 "$ROOT/salvaguarda/data/$APP_ID.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$ROOT/data/$APP_ID.metainfo.xml" "$PKG/usr/share/metainfo/"

# Documentation
install -m 0644 "$ROOT/packaging/debian/copyright" "$PKG/usr/share/doc/salvaguarda/copyright"
gzip -9n -c "$ROOT/packaging/debian/changelog" > "$PKG/usr/share/doc/salvaguarda/changelog.gz"
gzip -9n -c "$ROOT/packaging/salvaguarda.1" > "$PKG/usr/share/man/man1/salvaguarda.1.gz"

install -m 0755 "$ROOT/packaging/debian/postinst" "$PKG/DEBIAN/postinst"
install -m 0755 "$ROOT/packaging/debian/prerm" "$PKG/DEBIAN/prerm"

find "$PKG/usr" -type d -exec chmod 0755 {} +
find "$PKG/usr/share" -type f -exec chmod 0644 {} +
chmod 0755 "$PKG/usr/bin/salvaguarda"

SIZE=$(du -sk "$PKG/usr" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$ROOT/packaging/debian/control.in" > "$PKG/DEBIAN/control"

find "$PKG" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

mkdir -p "$OUT"
rm -f "$OUT"/salvaguarda_*_all.deb
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$OUT/$NAME.deb" >/dev/null
echo "$OUT/$NAME.deb"
