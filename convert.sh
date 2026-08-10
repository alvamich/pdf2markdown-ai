#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$PROJECT_DIR/.venv/bin:$PATH"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /path/to/document.pdf [extra options]"
  exit 2
fi

PDF_PATH="$1"
shift

exec "$PROJECT_DIR/.venv/bin/pdf2md-ai" "$PDF_PATH" "$@"
