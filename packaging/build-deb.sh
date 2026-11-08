#!/bin/sh
# Builds dist/salvaguarda_<versão>_all.deb. Needs only dpkg-deb, which every
# Debian-based distribution already has, and works without internet.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/salvaguarda/__init__.py")
APP_ID=io.github.franciscovazquezreis.Salvaguarda
NAME=salvaguarda_${VERSION}_all
CHANGELOG_VERSION=$(sed -n '1s/^salvaguarda (\([^)]*\)).*/\1/p' "$ROOT/packaging/debian/changelog")
if [ "$CHANGELOG_VERSION" != "$VERSION" ]; then
    echo "VERSION ($VERSION) e packaging/debian/changelog ($CHANGELOG_VERSION) não coincidem" >&2
    exit 1
fi
if ! grep -q "<release version=\"$VERSION\"" "$ROOT/data/$APP_ID.metainfo.xml"; then
    echo "Falta <release version=\"$VERSION\"> em data/$APP_ID.metainfo.xml" >&2
    exit 1
fi
OUT="$ROOT/dist"
WORK=$(mktemp -d)
PKG="$WORK/$NAME"
trap 'rm -rf "$WORK"' EXIT

# Reproducible timestamps (date of the last commit when available).
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}
export SOURCE_DATE_EPOCH

install -d "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/share/salvaguarda" \
    "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps" \
    "$PKG/usr/share/metainfo" "$PKG/usr/share/doc/salvaguarda" "$PKG/usr/share/man/man1" \
    "$PKG/usr/share/python3/runtime.d"

# Application code (without caches)
tar -C "$ROOT" --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' -cf - salvaguarda | tar -C "$PKG/usr/share/salvaguarda" -xf -
install -m 0755 "$ROOT/packaging/salvaguarda.launcher" "$PKG/usr/bin/salvaguarda"

# Desktop integration
install -m 0644 "$ROOT/data/$APP_ID.desktop" "$PKG/usr/share/applications/"
install -m 0644 "$ROOT/salvaguarda/data/$APP_ID.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$ROOT/data/$APP_ID.metainfo.xml" "$PKG/usr/share/metainfo/"

# Documentation
install -m 0644 "$ROOT/packaging/debian/copyright" "$PKG/usr/share/doc/salvaguarda/copyright"
gzip -9n -c "$ROOT/packaging/debian/changelog" > "$PKG/usr/share/doc/salvaguarda/changelog.gz"
gzip -9n -c "$ROOT/packaging/salvaguarda.1" > "$PKG/usr/share/man/man1/salvaguarda.1.gz"

for script in preinst postinst prerm; do
    install -m 0755 "$ROOT/packaging/debian/$script" "$PKG/DEBIAN/$script"
done

find "$PKG/usr" -type d -exec chmod 0755 {} +
find "$PKG/usr/share" -type f -exec chmod 0644 {} +
chmod 0755 "$PKG/usr/bin/salvaguarda"
install -m 0755 "$ROOT/packaging/salvaguarda.rtupdate" "$PKG/usr/share/python3/runtime.d/salvaguarda.rtupdate"

SIZE=$(du -sk "$PKG/usr" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$ROOT/packaging/debian/control.in" > "$PKG/DEBIAN/control"

find "$PKG" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

# Checksums for "dpkg -V" (dpkg would otherwise compute them at install time).
(cd "$PKG" && find usr -type f -print0 | LC_ALL=C sort -z | xargs -0 md5sum) > "$PKG/DEBIAN/md5sums"
chmod 0644 "$PKG/DEBIAN/md5sums"
touch -h -d "@$SOURCE_DATE_EPOCH" "$PKG/DEBIAN/md5sums"

mkdir -p "$OUT"
# Older .debs are kept: they are the way back if a release goes wrong.
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$OUT/$NAME.deb" >/dev/null
echo "$OUT/$NAME.deb"
