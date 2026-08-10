#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 /path/to/book.md [/path/to/book.epub]"
  exit 2
fi

if ! command -v pandoc >/dev/null 2>&1; then
  echo "Pandoc is required. Install it with: sudo apt-get install -y pandoc"
  exit 1
fi

if [[ ! -f "$1" ]]; then
  echo "Markdown file not found: $1"
  exit 1
fi
MD_PATH="$(realpath "$1")"

MD_DIR="$(dirname "$MD_PATH")"
MD_STEM="$(basename "${MD_PATH%.md}")"
ASSET_DIR="$MD_DIR/${MD_STEM}_assets"

if [[ $# -eq 2 ]]; then
  EPUB_PATH="$(realpath -m "$2")"
else
  EPUB_PATH="$MD_DIR/$MD_STEM.epub"
fi

TITLE="$(awk '/^#[[:space:]]+/ {sub(/^#[[:space:]]+/, ""); print; exit}' "$MD_PATH")"
if [[ -z "$TITLE" ]]; then
  TITLE="$MD_STEM"
fi

if grep -Eq '!\[[^]]*\]\([^)]+\)|<img[[:space:]][^>]*src=' "$MD_PATH"; then
  echo "Image references found in Markdown."
else
  echo "WARNING: No image references were found in the Markdown."
fi

if [[ -d "$ASSET_DIR" ]]; then
  IMAGE_COUNT="$(find "$ASSET_DIR" -type f \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.gif' -o -iname '*.webp' -o -iname '*.svg' \) | wc -l)"
  echo "Companion images: $IMAGE_COUNT file(s) in $ASSET_DIR"
else
  echo "WARNING: Expected assets folder not found: $ASSET_DIR"
fi

echo "Creating self-contained EPUB..."
PANDOC_ARGS=(
  "$MD_PATH"
  --from=gfm+raw_html
  --to=epub3
  --standalone
  --toc
  --toc-depth=3
  --resource-path="$MD_DIR:$ASSET_DIR"
  --metadata "title=$TITLE"
  --output="$EPUB_PATH"
)

if [[ -f "$ASSET_DIR/cover.png" ]]; then
  PANDOC_ARGS+=(--epub-cover-image="$ASSET_DIR/cover.png")
  echo "EPUB cover: $ASSET_DIR/cover.png"
fi

pandoc "${PANDOC_ARGS[@]}"

if command -v unzip >/dev/null 2>&1; then
  EMBEDDED_IMAGES="$(unzip -Z1 "$EPUB_PATH" | grep -Eic '\.(png|jpe?g|gif|webp|svg)$' || true)"
  echo "Embedded images in EPUB: $EMBEDDED_IMAGES"
fi

echo "Finished: $EPUB_PATH"
echo "Add this EPUB file to Calibre; do not add only the Markdown file."
