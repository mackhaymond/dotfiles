#!/bin/bash

# Build yabai from upstream at a PINNED commit plus the local patches in
# ~/.config/yabai/patches/, install it as its own Homebrew keg, and switch to it.
#
#   yabai_build_patched.sh              # build, install, sudoers line, restart
#   yabai_build_patched.sh --build-only # stop after the build (prints the binary path)
#
# Why a patched build: upstream cannot adopt a window that a running app shows late,
# on another space, or below the normal window level (upstream #2833) -- see
# patches/adopt-windows.patch and the README ("Patched yabai"). To move to a newer
# upstream, bump COMMIT, run this, and fix the patch if it no longer applies.
#
# What it does, in order:
#   1. clone upstream at COMMIT, `git apply` every patches/*.patch, `make install`
#   2. sign the binary with the Apple Development identity in the login keychain, so
#      the Accessibility grant (tied to the signature) survives the NEXT rebuild; falls
#      back to an ad-hoc signature (grant must then be re-added after every build)
#   3. install into $(brew --prefix)/Cellar/yabai/<version>-patched and repoint the
#      bin/opt/linked symlinks at it (older kegs are kept for rollback: re-run the
#      `ln -sfn` lines against one of them, then the sudoers step)
#   4. rewrite /private/etc/sudoers.d/yabai for the new binary hash (ONE sudo = one
#      Touch ID), validated with `visudo -cf` first
#   5. restart the service
#
# If yabai does not come back after step 5, the signature changed: System Settings ->
# Privacy & Security -> Accessibility, remove `yabai` with "-" and add
# /opt/homebrew/bin/yabai with "+", then `yabai --restart-service`.

set -euo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

COMMIT="${YABAI_BUILD_COMMIT:-b2dc8b4c892d74738a6e865d73048ed2dc3c9ed1}"
REPO="https://github.com/koekeishiya/yabai.git"
PATCH_DIR="${YABAI_PATCH_DIR:-$HOME/.config/yabai/patches}"
BUILD_ROOT="${YABAI_BUILD_ROOT:-$HOME/Library/Caches/yabai-build}"
PREFIX="$(brew --prefix)"
KEG="$PREFIX/Cellar/yabai/HEAD-${COMMIT:0:7}-patched"
BUILD_ONLY=0
[ "${1:-}" = "--build-only" ] && BUILD_ONLY=1

say() { printf '==> %s\n' "$*"; }

ls "$PATCH_DIR"/*.patch >/dev/null 2>&1 || { echo "no patches in $PATCH_DIR" >&2; exit 1; }

say "source: $REPO @ ${COMMIT:0:7}"
src="$BUILD_ROOT/src"
rm -rf "$src"
mkdir -p "$BUILD_ROOT"
git clone -q "$REPO" "$src"
git -C "$src" checkout -q "$COMMIT"
for p in "$PATCH_DIR"/*.patch; do
  say "patch: $(basename "$p")"
  git -C "$src" apply --index "$p"
done

say "build"
make -C "$src" install >"$BUILD_ROOT/build.log" 2>&1 || { tail -n 40 "$BUILD_ROOT/build.log" >&2; exit 1; }
bin="$src/bin/yabai"
[ -x "$bin" ] || { echo "build produced no binary" >&2; exit 1; }

identity=$(security find-identity -v -p codesigning 2>/dev/null \
  | sed -n 's/^ *[0-9]*) \([0-9A-F]\{40\}\) "Apple Development:.*/\1/p' | head -n 1)
if [ -n "$identity" ] && codesign -fs "$identity" "$bin" >/dev/null 2>&1; then
  say "signed with Apple Development identity $identity"
else
  codesign -fs - "$bin" >/dev/null 2>&1
  say "signed AD-HOC (no usable signing identity): Accessibility must be re-added after this build"
fi
"$bin" --version

if [ "$BUILD_ONLY" = 1 ]; then
  say "built: $bin"
  exit 0
fi

say "install: $KEG"
mkdir -p "$KEG/bin" "$KEG/share/man/man1"
# Replace, don't overwrite in place: the running yabai has the old file mapped.
cp "$bin" "$KEG/bin/yabai.new" && mv -f "$KEG/bin/yabai.new" "$KEG/bin/yabai"
cp "$src/doc/yabai.1" "$KEG/share/man/man1/yabai.1" 2>/dev/null || true
ln -sfn "../Cellar/yabai/$(basename "$KEG")/bin/yabai" "$PREFIX/bin/yabai"
ln -sfn "../Cellar/yabai/$(basename "$KEG")" "$PREFIX/opt/yabai"
ln -sfn "../../../Cellar/yabai/$(basename "$KEG")" "$PREFIX/var/homebrew/linked/yabai"
[ -f "$KEG/share/man/man1/yabai.1" ] && ln -sfn "../../../Cellar/yabai/$(basename "$KEG")/share/man/man1/yabai.1" "$PREFIX/share/man/man1/yabai.1"

# The scripting addition is loaded by `sudo -n yabai --load-sa` (yabairc), allowed by a
# sudoers line pinned to the binary's sha256 -- stale after every build.
yabai_path="$PREFIX/bin/yabai"
line="$(id -un) ALL=(root) NOPASSWD: sha256:$(shasum -a 256 "$yabai_path" | cut -d' ' -f1) $yabai_path --load-sa"
tmp="$BUILD_ROOT/sudoers.yabai"
printf '%s\n' "$line" >"$tmp"
visudo -cf "$tmp" >/dev/null
say "sudoers (Touch ID / password prompt): /private/etc/sudoers.d/yabai"
sudo install -m 0440 -o root -g wheel "$tmp" /private/etc/sudoers.d/yabai
rm -f "$tmp"
sudo -n -l "$yabai_path" --load-sa >/dev/null && say "sudoers ok"

say "restart"
yabai --restart-service || true
for _ in $(seq 20); do
  sleep 0.5
  yabai -m query --spaces --space >/dev/null 2>&1 && { say "yabai is up: $(yabai --version) ($(basename "$KEG"))"; exit 0; }
done
cat >&2 <<EOF
yabai did not come back. The usual cause is the Accessibility grant:
  System Settings -> Privacy & Security -> Accessibility: remove "yabai" (-),
  add $yabai_path (+), then run: yabai --restart-service
EOF
exit 1
