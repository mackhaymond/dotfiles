#!/bin/sh
# Apply iCloud / TimeMachine exclusion xattrs to the ~/Desktop/coding symlink.
#
# Why: ~/Desktop is a CLOUDDESKTOP sync root, but ~/code (the symlink target) is
# not. macOS iCloud Drive generally does not recurse through symlinks, but these
# xattrs are defense-in-depth so the File Provider extension explicitly skips
# the symlink entry and Time Machine ignores it.
#
# Changing the xattr values below triggers chezmoi to re-run this script.
# Current values:
#   com.apple.fileprovider.ignore#P=1
#   com.apple.metadata:com_apple_backup_excludeItem=com.apple.backupd

set -eu

LINK="$HOME/Desktop/coding"

# Bail if the link doesn't exist yet (e.g. first apply on a new machine where
# ~/Desktop is missing). chezmoi creates the symlink itself before this script.
[ -L "$LINK" ] || { echo "skip: $LINK is not a symlink"; exit 0; }

# -s = act on the symlink itself, not the target
xattr -ws 'com.apple.fileprovider.ignore#P' 1 "$LINK"
xattr -ws 'com.apple.metadata:com_apple_backup_excludeItem' 'com.apple.backupd' "$LINK"

echo "set iCloud-exclude xattrs on $LINK"
