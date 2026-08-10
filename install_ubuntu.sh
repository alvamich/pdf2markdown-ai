#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing Ubuntu system packages..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip libgl1 libglib2.0-0 poppler-utils curl pandoc unzip

echo "Creating the private Python environment..."
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/python" -m pip install --upgrade pip uv
"$PROJECT_DIR/.venv/bin/uv" pip install --python "$PROJECT_DIR/.venv/bin/python" -e "$PROJECT_DIR[mineru]"

echo
echo "Installation finished. MinerU downloads its free models on first use."
echo "For a reboot-safe MinerU service, next run:"
echo "  chmod +x $PROJECT_DIR/install_service.sh $PROJECT_DIR/convert_epub.sh"
echo "  $PROJECT_DIR/install_service.sh"
echo
echo "Convert a PDF with:"
echo "  $PROJECT_DIR/convert.sh /path/to/book.pdf"
echo
echo "After conversion, create an EPUB with embedded images:"
echo "  $PROJECT_DIR/convert_epub.sh '/path/to/Detected Book Title.md'"
