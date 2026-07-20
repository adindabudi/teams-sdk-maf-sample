#!/usr/bin/env bash
set -euo pipefail

uv run python scripts/package_app.py "$@"
