#!/bin/bash

# Build yabai from upstream at a PINNED commit plus the local patches in
# ~/.config/yabai/patches/, install it as its own Homebrew keg, and switch to it.
#
#   yabai_build_patched.sh               # build, sudoers line, install, restart
#   yabai_build_patched.sh --build-only  # stop after the build (prints the binary path)
#   yabai_build_patched.sh --allow-adhoc # install even if the build could only be
#                                        # ad-hoc signed (see step 2)
#
# The flags combine in any order.
#
# Why a patched build: upstream cannot adopt a window that a running app shows late,
# on another space, or below the normal window level (upstream #2833) -- see
# patches/adopt-windows.patch and the README ("Patched yabai"). To move to a newer
# upstream, bump COMMIT, run this, and fix the patch if it no longer applies.
#
# `brew upgrade` can relink an UNPATCHED yabai over this one: the formula is still
# installed as far as Homebrew knows, and a new release replaces the bin/opt symlinks
# this script points at the patched keg (with a binary whose hash the sudoers line does
# not match). `brew pin yabai` prevents that; run it once by hand.
#
# What it does, in order -- arranged so that a failed or cancelled run leaves the
# machine on the yabai it was already running:
#   1. clone upstream at COMMIT, `git apply` every patches/*.patch, `make install`
#   2. sign the binary with the Apple Development identity in the login keychain, so
#      the Accessibility grant (tied to the signature) survives the NEXT rebuild. If
#      that fails the binary is signed ad-hoc, and the run STOPS when the installed
#      yabai is identity-signed: an ad-hoc binary loses the Accessibility grant, yabai
#      does not come back, and nothing says why. --allow-adhoc installs it anyway (the
#      grant must then be re-added after every build).
#   3. rewrite /private/etc/sudoers.d/yabai for the new binary's hash (ONE sudo = one
#      Touch ID), validated with `visudo -cf` first. This comes BEFORE the binary is
#      switched: a cancelled Touch ID then changes nothing at all, where the old order
#      left a new binary with a stale sudoers hash (the scripting addition failed to
#      load at the next restart).
#   4. keep the binary being replaced as <keg>/bin/yabai.prev, then install into
#      $(brew --prefix)/Cellar/yabai/<version>-patched and repoint the bin/opt/linked
#      symlinks at it (kegs of other commits are kept as well)
#   5. check that sudo accepts the installed binary, restart the service
#
# If yabai does not come back after step 5, the signature changed: System Settings ->
# Privacy & Security -> Accessibility, remove `yabai` with "-" and add
# /opt/homebrew/bin/yabai with "+", then `yabai --restart-service`. The script also
# prints the exact commands that put yabai.prev and its sudoers line back.

set -euo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

COMMIT="${YABAI_BUILD_COMMIT:-b2dc8b4c892d74738a6e865d73048ed2dc3c9ed1}"
REPO="https://github.com/koekeishiya/yabai.git"
PATCH_DIR="${YABAI_PATCH_DIR:-$HOME/.config/yabai/patches}"
BUILD_ROOT="${YABAI_BUILD_ROOT:-$HOME/Library/Caches/yabai-build}"
BUILD_ONLY=0
ALLOW_ADHOC=0
while [ $# -gt 0 ]; do
  case "$1" in
    --build-only)  BUILD_ONLY=1 ;;
    --allow-adhoc) ALLOW_ADHOC=1 ;;
    *) echo "usage: $0 [--build-only] [--allow-adhoc]" >&2; exit 64 ;;
  esac
  shift
done
PREFIX="$(brew --prefix)"
KEG="$PREFIX/Cellar/yabai/HEAD-${COMMIT:0:7}-patched"

say() { printf '==> %s\n' "$*"; }

# `codesign -dvv` of $1, or nothing for an unsigned or missing file. Captured into a
# variable by the callers rather than piped into `grep -q`: under pipefail a grep that
# exits at its first match can fail the whole pipeline with SIGPIPE.
sig_info() { codesign -dvv "$1" 2>&1 || true; }
is_adhoc() { printf '%s\n' "$1" | grep -q '^Signature=adhoc'; }
# Identity-signed: has a certificate chain or a team, and is not ad-hoc.
is_identity() {
  is_adhoc "$1" && return 1
  printf '%s\n' "$1" | grep -Eq '^Authority=|^TeamIdentifier=[A-Z0-9]+$'
}

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
  codesign -fs - "$bin" >/dev/null 2>&1 || { echo "codesign failed (identity and ad-hoc)" >&2; exit 1; }
  say "signed AD-HOC (no usable signing identity): Accessibility must be re-added after this build"
fi
"$bin" --version

if [ "$BUILD_ONLY" = 1 ]; then
  say "built: $bin"
  exit 0
fi

yabai_path="$PREFIX/bin/yabai"

# Never trade an identity-signed yabai for an ad-hoc one by accident. The signing step
# above falls back to ad-hoc when the identity is unusable (expired certificate, locked
# keychain), and installing that drops the Accessibility grant: the restart at the end
# then leaves the machine with no window manager.
if is_adhoc "$(sig_info "$bin")" && is_identity "$(sig_info "$yabai_path")" && [ "$ALLOW_ADHOC" = 0 ]; then
  cat >&2 <<EOF
The build is AD-HOC signed, but the installed $yabai_path is signed with an
identity. Installing it would lose the Accessibility grant. Nothing was changed.
  - fix the signing identity (security find-identity -v -p codesigning) and re-run, or
  - re-run with --allow-adhoc and re-add yabai under Accessibility afterwards.
The built binary is at: $bin
EOF
  exit 1
fi

# Stage everything that needs no privileges BEFORE the sudo, so that the binary switch
# after it is two renames and a few symlinks. Nothing staged here is used by anything
# until then.
say "stage: $KEG"
mkdir -p "$KEG/bin" "$KEG/share/man/man1"
rm -f "$KEG/bin/yabai.new" "$KEG/bin/yabai.prev.new"
cp "$bin" "$KEG/bin/yabai.new"
# The binary being replaced, wherever the symlink points now. The keg name depends only
# on COMMIT, so a rebuild with a changed patch lands on the SAME path and used to
# overwrite the only working patched binary. Copied into this keg (not left in place)
# so the rollback is the same one rename whichever keg it came from.
prev_sha=""
if [ -x "$yabai_path" ]; then
  cp -p "$yabai_path" "$KEG/bin/yabai.prev.new"
  prev_sha=$(shasum -a 256 "$KEG/bin/yabai.prev.new" | cut -d' ' -f1)
fi

sudoers_line() { printf '%s ALL=(root) NOPASSWD: sha256:%s %s --load-sa\n' "$(id -un)" "$1" "$yabai_path"; }

# Printed whenever a step after the sudoers rewrite fails.
rollback_hint() {
  if [ -n "$prev_sha" ] && [ -e "$KEG/bin/yabai.prev" ]; then
    cat >&2 <<EOF
To go back to the previous binary (the symlinks already point at this keg):
  cp -p "$KEG/bin/yabai.prev" "$KEG/bin/yabai.new" && mv -f "$KEG/bin/yabai.new" "$KEG/bin/yabai"
  printf '%s\n' '$(sudoers_line "$prev_sha")' >"$BUILD_ROOT/sudoers.yabai"
  visudo -cf "$BUILD_ROOT/sudoers.yabai" && sudo install -m 0440 -o root -g wheel "$BUILD_ROOT/sudoers.yabai" /private/etc/sudoers.d/yabai
  yabai --restart-service
EOF
  else
    echo "There was no previous binary to keep, so there is nothing to roll back to." >&2
  fi
}

# The scripting addition is loaded by `sudo -n yabai --load-sa` (yabairc), allowed by a
# sudoers line pinned to the binary's sha256 -- stale after every build. Hash the BUILT
# binary: the installed copy is byte-identical, and the line names the final path.
tmp="$BUILD_ROOT/sudoers.yabai"
sudoers_line "$(shasum -a 256 "$bin" | cut -d' ' -f1)" >"$tmp"
visudo -cf "$tmp" >/dev/null
say "sudoers (Touch ID / password prompt): /private/etc/sudoers.d/yabai"
if ! sudo install -m 0440 -o root -g wheel "$tmp" /private/etc/sudoers.d/yabai; then
  rm -f "$tmp" "$KEG/bin/yabai.new" "$KEG/bin/yabai.prev.new"
  echo "sudoers was not updated (cancelled?). Nothing was changed; the installed yabai is untouched." >&2
  exit 1
fi
rm -f "$tmp"

# From here until the rename below, the sudoers line matches the NEW hash while the OLD
# binary is still installed: a yabai restart in that window would fail to load the
# scripting addition. So the switch follows immediately, with nothing that can prompt,
# block or fail slowly in between.
say "install: $KEG"
[ -e "$KEG/bin/yabai.prev.new" ] && mv -f "$KEG/bin/yabai.prev.new" "$KEG/bin/yabai.prev"
# Replace, don't overwrite in place: the running yabai has the old file mapped.
mv -f "$KEG/bin/yabai.new" "$KEG/bin/yabai"
ln -sfn "../Cellar/yabai/$(basename "$KEG")/bin/yabai" "$PREFIX/bin/yabai"
ln -sfn "../Cellar/yabai/$(basename "$KEG")" "$PREFIX/opt/yabai"
ln -sfn "../../../Cellar/yabai/$(basename "$KEG")" "$PREFIX/var/homebrew/linked/yabai"
cp "$src/doc/yabai.1" "$KEG/share/man/man1/yabai.1" 2>/dev/null || true
[ -f "$KEG/share/man/man1/yabai.1" ] && ln -sfn "../../../Cellar/yabai/$(basename "$KEG")/share/man/man1/yabai.1" "$PREFIX/share/man/man1/yabai.1"

# An explicit `if`: as `sudo ... && say ...` a failure here was the left side of an &&
# list, which `set -e` ignores, so a mismatched line went by without a word.
# Compared by hash, not `sudo -n -l <cmd>`: that asks whether ANY rule allows the
# command, and an admin's blanket rule always does (it "passed" for /bin/ls here), so
# it could never fail. What must hold is that the line installed above names the bytes
# now at $yabai_path.
installed_line=$(sudo -n cat /private/etc/sudoers.d/yabai 2>/dev/null || true)
if [ "$installed_line" = "$(sudoers_line "$(shasum -a 256 "$yabai_path" | cut -d' ' -f1)")" ]; then
  say "sudoers ok"
else
  cat >&2 <<EOF
sudo does NOT accept "$yabai_path --load-sa": the sudoers line does not match the
installed binary, so the scripting addition will not load. yabai was NOT restarted and
is still running the previous binary from memory.
EOF
  rollback_hint
  exit 1
fi

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
rollback_hint
exit 1
