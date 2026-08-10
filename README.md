# PDF2Markdown AI

A beginner-friendly Python wrapper around [MinerU](https://github.com/opendatalab/MinerU) for converting small or very large PDFs into Markdown on a Linux machine.

It uses free, locally downloaded document-AI models. No paid API or document upload is required. On a machine with little or no GPU memory, the default `pipeline` backend runs on the CPU. If you later run MinerU on a more powerful computer, this program can connect to that MinerU API instead.

## Why this project exists

PDF is a visual page format, while Markdown is a reading-order format. A basic text extractor cannot reliably decide which column comes first, recognize a scanned page, reconstruct a table, or turn a formula into LaTeX. MinerU combines PDF text extraction, OCR, layout analysis, reading-order detection, formula recognition, and table recognition.

MinerU now includes sliding-window and streaming improvements for long documents. This wrapper adds another layer of safety for book-sized files:

- Tries the complete PDF first so cross-page paragraphs, tables, and headings keep their context.
- Saves a checkpoint after every completed range.
- Resumes automatically if the VM stops or the process is interrupted.
- Retries failed ranges, then divides them into smaller ranges automatically.
- Merges the Markdown and copies/relinks extracted images.
- Keeps a plain-text log and a JSON state file for diagnosis.

Current MinerU uses sliding windows and streaming writes for long documents, so the default is now `--chunk-pages 0`: process the complete book first. If that attempt fails, this wrapper automatically divides the document into halves and continues recursively down to individual pages. You can still choose a fixed size such as `--chunk-pages 25` when testing an older MinerU release or a particularly constrained VM.

## Recommended VM

Start with:

- Ubuntu 22.04 or 24.04, 64-bit
- 4 CPU cores (2 works, more is faster)
- 16 GB RAM minimum; 32 GB is more comfortable
- 30 GB free SSD space, plus room for source PDFs and results
- GPU optional
- Python 3.10–3.12 recommended

CPU conversion can be slow. That is expected: a scanned book can take hours. The checkpoints mean slow processing is safe.

## Installation on Ubuntu

### 1. Copy the project to the VM

Place this whole `pdf2markdown-ai` folder somewhere convenient, for example in your home folder.

### 2. Open a Terminal in the project folder

```bash
cd ~/pdf2markdown-ai
```

### 3. Allow the helper files to run

```bash
chmod +x install_ubuntu.sh install_service.sh convert.sh convert_epub.sh
```

### 4. Install the program and MinerU

```bash
./install_ubuntu.sh
```

The script asks for your Linux password to install system packages. It then creates an isolated `.venv` folder and installs this program plus MinerU. MinerU's free model files are downloaded on first use, so the first conversion starts more slowly and requires internet access. Later conversions can run locally.

### 5. Install the reboot-safe MinerU service

On a low-resource machine MinerU can need several minutes to start. Install its persistent local API once:

```bash
./install_service.sh
```

The service starts automatically after reboot and restarts after a failure. Check it with:

```bash
sudo systemctl status mineru-api
sudo journalctl -u mineru-api -f
curl --fail http://127.0.0.1:8000/health
```

The converter waits up to ten minutes for this service after boot and prints a message every ten seconds while waiting.

## First conversion

Put a PDF on the VM, then run:

```bash
./convert.sh "/home/your-name/Documents/my-book.pdf" \
  --backend pipeline \
  --api-url http://127.0.0.1:8000
```

The result will be:

```text
/home/your-name/Documents/Detected Book Title.md
/home/your-name/Documents/Detected Book Title_assets/
/home/your-name/Documents/Detected Book Title.quality.json
```

The program looks for the book title in `--title`, the PDF metadata, MinerU's first main heading, and the first page text—in that order. It removes filename characters that are unsafe on Windows or Linux and shortens an excessively long subtitle. Use `--output preferred-name.md` when you want to choose the filename yourself.

The complete first PDF page is rendered into the assets folder as `cover.png` and placed directly below the title in the Markdown. This preserves cover artwork even when it is stored as a page background that normal PDF image extraction would miss.

The `.quality.json` file is a proofreading checklist. It reports heading counts, image references, source-PDF versus Markdown links, suspicious standalone page numbers, repeated lines that may be running headers or footers, unbalanced code fences, and unusually short output. It identifies likely problems but does not silently delete source content.

The hidden checkpoint folder is next to the result:

```text
/home/your-name/Documents/.my-book.pdf2md-work/
```

If conversion is interrupted, run the exact same command. Completed ranges will be skipped.

## Progress shown in the terminal

The converter now displays:

- API readiness and startup waiting time.
- The active page range and attempt number.
- MinerU output in real time while also saving it to `process.log`.
- A heartbeat every ten seconds when MinerU is quiet.
- Completed pages and overall percentage after each recoverable range.

Whole-document mode cannot always provide an exact within-page percentage because MinerU's API does not expose one consistently. The heartbeat confirms that the process is still alive; any batch progress MinerU emits is streamed immediately. Use `--chunk-pages 5` during testing when you want percentage updates after every five pages.

## Useful commands

English OCR hint:

```bash
./convert.sh book.pdf --language en
```

Spanish OCR hint:

```bash
./convert.sh book.pdf --language es
```

Use smaller chunks on a low-memory machine:

```bash
./convert.sh book.pdf --chunk-pages 10
```

Whole-document processing is already the default. You can state it explicitly with:

```bash
./convert.sh book.pdf --chunk-pages 0
```

Let MinerU select an accelerated backend when a suitable GPU is installed:

```bash
./convert.sh book.pdf --backend auto
```

Connect to a MinerU API that you run on another machine:

```bash
./convert.sh book.pdf --backend auto --api-url http://192.168.1.50:8000
```

Start again and discard the existing checkpoint:

```bash
./convert.sh book.pdf --fresh
```

Add invisible source page-range comments to help later proofreading:

```bash
./convert.sh book.pdf --page-markers
```

Choose the title and therefore the automatic filename:

```bash
./convert.sh book.pdf --title "My Preferred Book Title"
```

Disable the front-page cover image if a particular PDF does not use page 1 as its cover:

```bash
./convert.sh book.pdf --no-cover
```

See every option:

```bash
.venv/bin/pdf2md-ai --help
```

## Understanding the output

MinerU may produce:

- Markdown headings and paragraphs in inferred reading order
- LaTeX formulas
- HTML tables when Markdown tables cannot represent the layout
- Image links pointing to the generated `_assets` folder

HTML tables are valid inside Markdown and are usually safer for a later EPUB conversion than forcing a complex table into Markdown pipe syntax.

Always inspect a few difficult areas before making the EPUB: the table of contents, multi-column pages, footnotes, tables, equations, and scanned or rotated pages. AI document parsing is not perfectly reliable and can occasionally omit, reorder, or misread content.

## Preparing for EPUB

Markdown files contain image links, not the binary image data. Keep the generated `.md` file and its matching `_assets` folder together. After proofreading, create a self-contained EPUB with:

```bash
./convert_epub.sh "/path/to/Detected Book Title.md"
```

The script uses [Pandoc](https://pandoc.org/) to find the referenced files, embed them inside the EPUB, and report how many images the EPUB contains. Add the resulting `.epub` file to Calibre rather than adding only the Markdown file.

You can choose an output filename:

```bash
./convert_epub.sh "Detected Book Title.md" "My Short Book Title.epub"
```

To check whether extraction produced image references before conversion:

```bash
grep -nE '!\[|<img' "Detected Book Title.md" | head
find "Detected Book Title_assets" -type f | head
```

For a polished book you will normally also add metadata, a cover image, language, chapter breaks, and EPUB CSS. Those are deliberately separate from extraction so mistakes are easier to find.

## Choosing the engine

| Engine | Low-GPU VM | Quality on difficult pages | Why it was or was not selected |
|---|---:|---:|---|
| MinerU `pipeline` | Yes, CPU supported | Good | Selected default; local, free models, OCR/layout/formula/table support |
| MinerU VLM/hybrid | Needs supported GPU or remote server | Higher on complex layouts | Supported through `--backend auto` or `--api-url` |
| Docling | CPU supported | Good | Strong fallback candidate, but adding two large engines makes first installation and support harder |
| Marker | Usually benefits from GPU | Good | Useful alternative, but its model stack is less attractive for this first low-resource build |
| olmOCR | At least 12 GB GPU for local inference | Very good on difficult pages | Not suitable as the local default; it can use a remote OpenAI-compatible inference server |

## Limits and next improvements

This is a reliable orchestration layer, not a fork of MinerU's model code. The next useful improvements should be driven by real PDFs:

1. Add an optional second-pass VLM only for pages flagged by the quality report.
2. Preserve bookmarks and PDF metadata as EPUB chapter metadata.
3. Offer reviewed removal of repeated headers/footers that survive extraction.
4. Add an optional Docling fallback when one-page MinerU retries still fail.

Keeping the first version focused makes it easier to install, understand, and diagnose.

## Troubleshooting

### The terminal says `Killed`

Linux probably stopped the process because it ran out of memory. Run the same command with a smaller range:

```bash
./convert.sh book.pdf --chunk-pages 5
```

Existing completed ranges remain usable.

### It is taking a very long time

CPU-only OCR and layout analysis are slow. Check the timestamped progress in the terminal or open:

```text
.book.pdf2md-work/conversion.log
```

### A particular range keeps failing

The program automatically divides it until it reaches one page. The detailed MinerU output is in:

```text
.book.pdf2md-work/mineru-output/<page-range>/process.log
```

### Images are missing

Keep the generated Markdown file and its `_assets` folder together. Relative links depend on that folder.

### MinerU changed a command-line option

MinerU is developing quickly. Check `mineru --help` inside the environment and compare it with the command recorded in the range's `process.log`. This wrapper targets the current `mineru -p ... -o ... -b pipeline` interface.

### `No such file or directory: 'mineru'`

Version 0.2 resolves MinerU beside the active virtual-environment Python and `convert.sh` adds `.venv/bin` to `PATH`. Re-run `./install_ubuntu.sh` if `.venv/bin/mineru` genuinely does not exist.

### `No module named 'six'`

Version 0.2.1 installs `six` explicitly because some MinerU pipeline components import it even when the dependency resolver does not install it transitively. Upgrade an existing environment with:

```bash
.venv/bin/python -m pip install -U "six>=1.16,<2"
sudo systemctl restart mineru-api
```

### The API is unavailable after reboot

Install or repair the service:

```bash
chmod +x install_service.sh
./install_service.sh
```

When `--api-url` is supplied, the converter checks `/health` before splitting the PDF. Missing executables, connection failures, and API startup timeouts are classified as infrastructure failures and stop immediately; they no longer cause pointless recursive page splitting.

## Project license

This wrapper is MIT-licensed. MinerU and its models are separate projects with their own current license terms; review those terms before redistributing a packaged system or offering it as a service.
