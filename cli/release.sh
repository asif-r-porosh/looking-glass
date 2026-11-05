#!/usr/bin/env bash
# Usage: ./release.sh [patch|minor|major]
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v gh >/dev/null; then
  echo "❌ Error: GitHub CLI (gh) is required." >&2
  exit 1
fi

BUMP="${1:-patch}"

# 1. Safety checks
if ! git diff-index --quiet HEAD --; then
  echo "❌ Error: Working directory has uncommitted changes. Commit or stash them first." >&2
  exit 1
fi

echo "Checking remote repository status..."
git fetch origin

LOCAL_BRANCH=$(git branch --show-current)
LOCAL_SHA=$(git rev-parse @)
REMOTE_SHA=$(git rev-parse "origin/$LOCAL_BRANCH")
BASE_SHA=$(git merge-base @ "origin/$LOCAL_BRANCH")

if [ "$LOCAL_SHA" = "$BASE_SHA" ] && [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
  echo "❌ Error: Your local branch is behind origin/$LOCAL_BRANCH. Please run 'git pull' first." >&2
  exit 1
fi

echo "Running go test ./... to ensure stability..."
go test ./...

# 2. Determine new version tag
git fetch --tags
LATEST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
VERSION_NUM=${LATEST_TAG#v}

IFS='.' read -r major minor patch <<< "$VERSION_NUM"
case "$BUMP" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
  *) echo "Usage: $0 [patch|minor|major]" >&2; exit 2 ;;
esac

NEXT_VERSION="v$major.$minor.$patch"
echo "🚀 Preparing release: $LATEST_TAG ➔ $NEXT_VERSION"

# 3. Clean and build using build.sh
./build.sh clean
export VERSION="$NEXT_VERSION"
./build.sh build

# 4. Archive folders into .tar.gz assets from within the build folder
echo "Packaging directories into .tar.gz archives..."
(
  cd build
  for dir in looking-glass-*; do
    if [ -d "$dir" ]; then
      tar --owner=0 --group=0 -czf "$dir.tar.gz" "$dir"
      # Optional: Clean up the directory so only archives remain locally
      rm -rf "$dir"
    fi
  done
)

# 5. Push the commit only. The tag is not created here: if the release below fails, no tag is left public.
git push origin "$LOCAL_BRANCH"

# 6. Create the GitHub Release. gh creates the tag $NEXT_VERSION on the remote, on this exact commit, together
#    with the release and the uploaded archives. Then bring the new tag home.
echo "Uploading archives to GitHub Releases..."
gh release create "$NEXT_VERSION" \
  build/looking-glass-*.tar.gz \
  --target "$LOCAL_SHA" \
  --title "Release $NEXT_VERSION" \
  --generate-notes
git fetch --tags

echo "🎉 Successfully released $NEXT_VERSION!"
unset VERSION
