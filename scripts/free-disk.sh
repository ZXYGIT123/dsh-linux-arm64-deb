#!/usr/bin/env bash
# Reclaim runner disk before a multi-gigabyte Electron build.
# The arm64 Ubuntu image ships large SDKs the packaging run never uses.
set -euo pipefail

echo "== disk before =="
df -h /

for path in \
  /usr/share/dotnet \
  /opt/ghc \
  /opt/hostedtoolcache/CodeQL \
  /usr/local/lib/android \
  /usr/local/share/boost \
  /usr/share/swift \
  /opt/az \
  /usr/local/aws-cli \
  /usr/local/aws-sam-cli \
  /imagegeneration; do
  if [ -e "$path" ]; then
    echo "removing $path"
    sudo rm -rf "$path" || true
  fi
done

# Preinstalled Node versions and container images are re-fetched on demand.
if [ -d /opt/hostedtoolcache/node ]; then
  echo "trimming /opt/hostedtoolcache/node"
  sudo find /opt/hostedtoolcache/node -mindepth 1 -maxdepth 1 -type d -print -exec rm -rf {} + || true
fi

sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/* || true

echo "== disk after =="
df -h /
