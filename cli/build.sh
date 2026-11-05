#!/usr/bin/env bash
# Usage: ./build.sh [build|clean]
set -euo pipefail
cd "$(dirname "$0")"

NAME=looking-glass
VERSION="${VERSION:-development}"

case "${1:-build}" in
clean)
  rm -rf build
  ;;
build)
  mkdir -p build
  
  for target in darwin/arm64 darwin/amd64 linux/amd64 linux/arm64 windows/amd64 windows/arm64; do
    os=${target%/*}
    arch=${target#*/}
    
    # Define subdirectory and native binary name
    target_dir="build/$NAME-$os-$arch"
    ext=""
    [ "$os" = windows ] && ext=".exe"
    binary_name="$NAME$ext"
    binary_path="$target_dir/$binary_name"
    
    mkdir -p "$target_dir"
    echo "building $binary_path ($VERSION)"
    
    # Compile the static binary into its dedicated os-arch folder
    CGO_ENABLED=0 GOOS=$os GOARCH=$arch go build \
      -trimpath \
      -ldflags "-s -w -X main.version=$VERSION" \
      -o "$binary_path" .
      
    # Make executable (for macOS and Linux platforms)
    [ "$os" != windows ] && chmod +x "$binary_path"
    
    # Generate sidecar checksum file inside the subdirectory
    (
      cd "$target_dir"
      if command -v sha256sum >/dev/null; then 
        sha256sum "$binary_name" > "$NAME.sha256"
      else 
        shasum -a 256 "$binary_name" > "$NAME.sha256"
      fi
    )
  done
  echo "done: local build artifact tree structured under build/"
  ;;
*)
  echo "usage: $0 [build|clean]" >&2
  exit 2
  ;;
esac
