#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${PIM_AUTH_USER:?请配置 PIM_AUTH_USER}"
: "${PIM_AUTH_PASS:?请配置 PIM_AUTH_PASS}"
export PIM_MI_AUTH="${PIM_MI_AUTH:-kerberos}"
# Keep the existing localhost-only browser refresh guard. Linux credentials
# are refreshed separately by the administrator (see LINUX.md).
export PIM_ALLOW_RTD_REFRESH=0
exec .venv/bin/uvicorn api:app --host "${PIM_BIND_HOST:-127.0.0.1}" --port "${PIM_PORT:-8000}" --workers 1
