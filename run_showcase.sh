#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
mkdir -p local-tools/cache/temp local-tools/cache/uv local-tools/cache/npm
export TEMP="$PWD/local-tools/cache/temp" TMP="$PWD/local-tools/cache/temp"
export UV_CACHE_DIR="$PWD/local-tools/cache/uv" npm_config_cache="$PWD/local-tools/cache/npm"
docker compose -f compose.showcase.yaml up -d --build --wait
printf '%s\n' 'Offline synthetic showcase: http://127.0.0.1:4182/#chart-reports'
