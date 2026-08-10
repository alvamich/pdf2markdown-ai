from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter


IMAGE_MD_RE = re.compile(r"(!\[[^\]]*\]\()([^\s)]+)([^)]*\))")
IMAGE_HTML_RE = re.compile(r"(<img\b[^>]*?\bsrc=[\"'])([^\"']+)([\"'])", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\([^)]+\)")
MARKDOWN_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
GENERIC_PDF_TITLES = {
    "untitled",
    "document",
    "microsoft word",
    "adobe acrobat",
    "pdf document",
}


class InfrastructureError(RuntimeError):
    """A machine, executable, or API problem that page splitting cannot fix."""


class MinerUProcessError(RuntimeError):
    """MinerU ran but could not parse the supplied page range."""


def resolve_mineru_command(requested: str = "mineru") -> str:
    """Find MinerU globally or beside the active virtual-environment Python."""

    requested_path = Path(requested).expanduser()
    if requested_path.parent != Path(".") or requested_path.is_absolute():
        if requested_path.is_file():
            return str(requested_path.resolve())
        raise InfrastructureError(f"MinerU executable not found: {requested_path}")

    executable_name = "mineru.exe" if os.name == "nt" else "mineru"
    sibling = Path(sys.executable).resolve().parent / executable_name
    if sibling.is_file():
        return str(sibling)
    discovered = shutil.which(requested)
    if discovered:
        return discovered
    raise InfrastructureError(
        "MinerU is installed incorrectly or is not on PATH. Expected it at "
        f"{sibling}. Run the installer or install 'mineru[pipeline]' in this virtual environment."
    )


def api_health_url(api_url: str) -> str:
    return api_url.rstrip("/") + "/health"


def wait_for_api_ready(
    api_url: str,
    timeout: int = 600,
    heartbeat: int = 10,
    reporter=print,
) -> dict:
    """Wait for a persistent MinerU API, giving useful startup feedback."""

    health_url = api_health_url(api_url)
    started = time.monotonic()
    deadline = started + timeout
    last_report = 0.0
    last_error = "not contacted"
    while True:
        try:
            with urllib.request.urlopen(health_url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if 200 <= response.status < 300:
                    return payload
                last_error = f"HTTP {response.status}"
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = str(error)

        now = time.monotonic()
        if now >= deadline:
            raise InfrastructureError(
                f"MinerU API did not become ready within {timeout} seconds at {health_url}. "
                f"Last error: {last_error}. Check: sudo systemctl status mineru-api"
            )
        if now - last_report >= max(1, heartbeat):
            elapsed = int(now - started)
            reporter(f"Waiting for MinerU API at {health_url} ({elapsed}s elapsed; last error: {last_error})")
            last_report = now
        time.sleep(1)


def looks_like_infrastructure_failure(output: str) -> bool:
    lowered = output.casefold()
    markers = (
        "connection refused",
        "connecterror",
        "all connection attempts failed",
        "failed to connect",
        "server disconnected",
        "remoteprotocolerror",
        "connection reset",
        "service unavailable",
        "api did not become ready",
        "timed out waiting for local mineru-api",
        "no such file or directory",
        "no module named",
    )
    if any(marker in lowered for marker in markers):
        return True
    return "started local mineru-api" in lowered and "shutting down" in lowered


@dataclass(frozen=True, order=True)
class PageRange:
    """A zero-based, end-exclusive PDF page range."""

    start: int
    end: int

    @property
    def count(self) -> int:
        return self.end - self.start

    @property
    def key(self) -> str:
        return f"{self.start + 1:06d}-{self.end:06d}"

    @property
    def label(self) -> str:
        return f"pages {self.start + 1}-{self.end}"


def make_ranges(total_pages: int, chunk_pages: int) -> list[PageRange]:
    if total_pages < 1:
        raise ValueError("The PDF contains no pages.")
    if chunk_pages <= 0:
        return [PageRange(0, total_pages)]
    return [
        PageRange(start, min(start + chunk_pages, total_pages))
        for start in range(0, total_pages, chunk_pages)
    ]


def split_range(page_range: PageRange) -> tuple[PageRange, PageRange]:
    if page_range.count < 2:
        raise ValueError("A one-page range cannot be divided.")
    middle = page_range.start + page_range.count // 2
    return PageRange(page_range.start, middle), PageRange(middle, page_range.end)


def write_pdf_range(source: Path, destination: Path, page_range: PageRange) -> None:
    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page_number in range(page_range.start, page_range.end):
        writer.add_page(reader.pages[page_number])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output_file:
        writer.write(output_file)


def find_markdown(output_dir: Path) -> Path:
    candidates = [path for path in output_dir.rglob("*.md") if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"MinerU produced no Markdown file in {output_dir}")
    return max(candidates, key=lambda path: (path.stat().st_size, path.stat().st_mtime))


def clean_title(value: object) -> str | None:
    """Return a plausible single-line book title, or None for generic metadata."""

    if not isinstance(value, str):
        return None
    title = re.sub(r"\s+", " ", value).strip(" .-_\t\r\n")
    if len(title) < 2 or title.casefold() in GENERIC_PDF_TITLES:
        return None
    return title


def title_from_markdown(markdown: str) -> str | None:
    match = MARKDOWN_H1_RE.search(markdown)
    if not match:
        return None
    candidate = re.sub(r"[*_`~]", "", match.group(1))
    candidate = re.sub(r"<[^>]+>", "", candidate)
    return clean_title(candidate)


def safe_title_filename(title: str, max_length: int = 72) -> str:
    """Create a readable, cross-platform filename from a book title."""

    name = re.sub(r"\s+", " ", title).strip(" .-")
    if len(name) > max_length:
        for separator in (":", " — ", " – ", " - "):
            short = name.split(separator, 1)[0].strip(" .-")
            if 3 <= len(short) <= max_length:
                name = short
                break
    name = INVALID_FILENAME_RE.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    if len(name) > max_length:
        name = name[: max_length + 1].rsplit(" ", 1)[0].strip(" .-")
    if not name:
        name = "Converted book"
    if name.casefold() in {"con", "prn", "aux", "nul", "com1", "lpt1"}:
        name += " book"
    return name


def render_cover(pdf_path: Path, destination: Path, dpi: int = 160) -> None:
    """Render the complete first PDF page so cover artwork is never lost."""

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        if len(document) < 1:
            raise ValueError("The PDF contains no pages.")
        page = document[0]
        try:
            bitmap = page.render(scale=dpi / 72)
            destination.parent.mkdir(parents=True, exist_ok=True)
            bitmap.to_pil().convert("RGB").save(destination, format="PNG", optimize=True)
        finally:
            page.close()
    finally:
        document.close()


def count_pdf_links(reader: PdfReader) -> int:
    count = 0
    for page in reader.pages:
        for annotation in page.get("/Annots", []):
            try:
                count += annotation.get_object().get("/Subtype") == "/Link"
            except Exception:
                continue
    return count


def _is_local_asset(target: str) -> bool:
    lowered = target.lower()
    return not (
        lowered.startswith(("http://", "https://", "data:", "mailto:"))
        or target.startswith("#")
        or target.startswith("/")
    )


def copy_and_rewrite_assets(
    markdown: str,
    source_markdown: Path,
    asset_dir: Path,
    asset_prefix: str,
    output_markdown_parent: Path,
) -> str:
    """Copy locally referenced images and rewrite their paths for the merged file."""

    asset_dir.mkdir(parents=True, exist_ok=True)
    replacements: dict[str, str] = {}

    def relocate(target: str) -> str:
        if target in replacements:
            return replacements[target]
        if not _is_local_asset(target):
            return target

        decoded = unquote(target)
        source_asset = (source_markdown.parent / decoded).resolve()
        if not source_asset.is_file():
            return target

        digest = hashlib.sha1(str(source_asset).encode("utf-8")).hexdigest()[:8]
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source_asset.name)
        destination = asset_dir / f"{asset_prefix}_{digest}_{safe_name}"
        if not destination.exists():
            shutil.copy2(source_asset, destination)
        relative = os.path.relpath(destination, output_markdown_parent).replace(os.sep, "/")
        relative = quote(relative, safe="/._-")
        replacements[target] = relative
        return relative

    markdown = IMAGE_MD_RE.sub(
        lambda match: f"{match.group(1)}{relocate(match.group(2))}{match.group(3)}",
        markdown,
    )
    markdown = IMAGE_HTML_RE.sub(
        lambda match: f"{match.group(1)}{relocate(match.group(2))}{match.group(3)}",
        markdown,
    )
    return markdown


def build_quality_report(
    markdown: str,
    title: str,
    total_pages: int,
    cover_path: Path | None,
    source_links: int = 0,
) -> dict:
    """Create simple, explainable warnings for a human proofreading pass."""

    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    line_counts = Counter(
        line
        for line in lines
        if 25 <= len(line) <= 140
        and not line.startswith(("#", "!", "<!--", "```", "|"))
    )
    repeated_lines = [
        {"count": count, "text": line}
        for line, count in line_counts.most_common(20)
        if count >= 5
    ]
    standalone_page_numbers = sum(bool(re.fullmatch(r"\d{1,4}", line)) for line in lines)
    heading_counts = {
        f"h{level}": len(re.findall(rf"^{'#' * level}\s+", markdown, re.MULTILINE))
        for level in range(1, 7)
    }
    code_fences = len(re.findall(r"^```", markdown, re.MULTILINE))
    markdown_links = len(MARKDOWN_LINK_RE.findall(markdown))
    warnings: list[str] = []
    if len(markdown) < max(500, total_pages * 100):
        warnings.append("The output contains unusually little text for the PDF page count.")
    if heading_counts["h1"] == 0:
        warnings.append("No top-level Markdown heading was found.")
    if heading_counts["h1"] > 1:
        warnings.append("Multiple top-level headings were found; check title and chapter hierarchy.")
    if code_fences % 2:
        warnings.append("The number of fenced-code markers is odd; a code block may be unclosed.")
    if standalone_page_numbers > max(3, total_pages // 20):
        warnings.append("Many standalone page numbers remain and may need removal before EPUB conversion.")
    if repeated_lines:
        warnings.append("Repeated lines remain; inspect them for running headers or footers.")
    if cover_path is not None and not cover_path.is_file():
        warnings.append("The requested cover image was not created.")
    if source_links and not markdown_links:
        warnings.append("The PDF contains links, but no ordinary Markdown links were preserved.")

    return {
        "title": title,
        "source_pages": total_pages,
        "markdown_characters": len(markdown),
        "markdown_lines": len(lines),
        "headings": heading_counts,
        "image_references": len(IMAGE_MD_RE.findall(markdown)) + len(IMAGE_HTML_RE.findall(markdown)),
        "source_pdf_link_annotations": source_links,
        "markdown_links": markdown_links,
        "standalone_page_numbers": standalone_page_numbers,
        "repeated_lines": repeated_lines,
        "warnings": warnings,
        "manual_checks": [
            "Compare the cover, table of contents, callout boxes, numbered procedures, and command blocks.",
            "Verify that running headers and printed page numbers were removed.",
            "Verify chapter hierarchy and links before converting to EPUB.",
        ],
    }


class Converter:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.source = args.pdf.resolve()
        self.destination = args.output.resolve()
        self.work_dir = args.work_dir.resolve()
        self.chunks_dir = self.work_dir / "chunks"
        self.runs_dir = self.work_dir / "mineru-output"
        self.segments_dir = self.work_dir / "segments"
        self.state_path = self.work_dir / "state.json"
        self.log_path = self.work_dir / "conversion.log"
        self.state = self._load_state()

    def _choose_title(self, ordered_ranges: list[PageRange]) -> str:
        explicit = clean_title(self.args.title)
        if explicit:
            return explicit

        reader = PdfReader(str(self.source))
        metadata_title = clean_title(getattr(reader.metadata, "title", None))
        if metadata_title:
            return metadata_title

        for page_range in ordered_ranges:
            segment = self.segments_dir / f"{page_range.key}.md"
            detected = title_from_markdown(segment.read_text(encoding="utf-8"))
            if detected:
                return detected

        try:
            first_page_text = reader.pages[0].extract_text() or ""
            for line in first_page_text.splitlines():
                detected = clean_title(line)
                if detected and len(detected) <= 160:
                    return detected
        except Exception:
            pass
        return clean_title(self.source.stem.replace("_", " ").replace("-", " ")) or "Converted book"

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"source": str(self.source), "completed": {}, "failures": {}}

    def _save_state(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _log(self, message: str) -> None:
        timestamped = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(timestamped, flush=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(timestamped + "\n")

    def _completed_pages(self) -> int:
        completed_pages = set()
        for item in self.state.get("completed", {}).values():
            page_range = item.get("range", {})
            completed_pages.update(range(int(page_range.get("start", 0)), int(page_range.get("end", 0))))
        return len(completed_pages)

    def _progress_text(self, page_range: PageRange, started: float) -> str:
        total = max(1, int(self.state.get("total_pages", page_range.end)))
        completed = self._completed_pages()
        percent = completed * 100 / total
        elapsed = int(time.monotonic() - started)
        return (
            f"MinerU is processing {page_range.label} | elapsed {elapsed}s | "
            f"completed {completed}/{total} pages ({percent:.1f}%)"
        )

    def _run_mineru(self, chunk: Path, run_dir: Path, page_range: PageRange) -> None:
        mineru_command = resolve_mineru_command(self.args.mineru_command)
        command = [mineru_command, "-p", str(chunk), "-o", str(run_dir)]
        if self.args.backend != "auto":
            command.extend(["-b", self.args.backend])
        if self.args.api_url:
            command.extend(["--api-url", self.args.api_url])
        if self.args.language:
            command.extend(["-l", self.args.language])
        run_dir.mkdir(parents=True, exist_ok=True)
        process_log_path = run_dir / "process.log"
        output_queue: queue.Queue[str | None] = queue.Queue()
        recent_output: deque[str] = deque(maxlen=30)
        started = time.monotonic()
        deadline = started + self.args.timeout if self.args.timeout > 0 else None

        with process_log_path.open("w", encoding="utf-8") as process_log:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            def read_output() -> None:
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        output_queue.put(line)
                finally:
                    output_queue.put(None)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            last_heartbeat = 0.0
            stream_closed = False
            while not stream_closed or process.poll() is None:
                try:
                    line = output_queue.get(timeout=1)
                    if line is None:
                        stream_closed = True
                    else:
                        process_log.write(line)
                        process_log.flush()
                        recent_output.append(line.rstrip())
                        print(f"[MinerU] {line}", end="", flush=True)
                except queue.Empty:
                    pass

                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(command, self.args.timeout)
                if now - last_heartbeat >= max(1, self.args.heartbeat):
                    self._log(self._progress_text(page_range, started))
                    last_heartbeat = now

            return_code = process.wait()

        if return_code != 0:
            tail = "\n".join(recent_output)
            message = (
                f"MinerU exited with code {return_code}. See {process_log_path}."
                + (f" Last output: {recent_output[-1]}" if recent_output else "")
            )
            if looks_like_infrastructure_failure(tail):
                raise InfrastructureError(message)
            raise MinerUProcessError(message)

    def _process_range(self, page_range: PageRange) -> list[PageRange]:
        segment_path = self.segments_dir / f"{page_range.key}.md"
        completed = self.state["completed"].get(page_range.key)
        if completed and segment_path.exists():
            self._log(f"Skipping completed {page_range.label}")
            return [page_range]

        chunk_path = self.chunks_dir / f"{page_range.key}.pdf"
        run_dir = self.runs_dir / page_range.key
        total_pages = max(1, int(self.state.get("total_pages", page_range.end)))
        completed_pages = self._completed_pages()
        self._log(
            f"Converting {page_range.label} | completed {completed_pages}/{total_pages} "
            f"pages ({completed_pages * 100 / total_pages:.1f}%)"
        )
        write_pdf_range(self.source, chunk_path, page_range)

        last_error: Exception | None = None
        for attempt in range(1, self.args.retries + 2):
            try:
                if run_dir.exists():
                    shutil.rmtree(run_dir)
                self._log(f"Attempt {attempt}/{self.args.retries + 1} for {page_range.label}")
                self._run_mineru(chunk_path, run_dir, page_range)
                mineru_markdown = find_markdown(run_dir)
                segment_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(mineru_markdown, segment_path)
                self.state["completed"][page_range.key] = {
                    "range": asdict(page_range),
                    "segment": str(segment_path),
                }
                self.state["failures"].pop(page_range.key, None)
                self._save_state()
                if not self.args.keep_chunks:
                    chunk_path.unlink(missing_ok=True)
                completed_pages = self._completed_pages()
                self._log(
                    f"Completed {page_range.label} | {completed_pages}/{total_pages} pages "
                    f"({completed_pages * 100 / total_pages:.1f}%)"
                )
                return [page_range]
            except InfrastructureError as error:
                self.state["failures"][page_range.key] = str(error)
                self._save_state()
                self._log(f"Infrastructure failure: {error}")
                self._log("Stopping without page splitting because smaller ranges cannot fix this problem.")
                raise
            except (OSError, MinerUProcessError, subprocess.TimeoutExpired) as error:
                last_error = error
                self._log(f"Attempt {attempt} failed for {page_range.label}: {error}")

        self.state["failures"][page_range.key] = str(last_error)
        self._save_state()
        if page_range.count > self.args.min_chunk_pages:
            left, right = split_range(page_range)
            self._log(f"Dividing {page_range.label} into smaller ranges")
            return self._process_range(left) + self._process_range(right)
        raise RuntimeError(f"Could not convert {page_range.label}: {last_error}")

    def _merge(self, completed_ranges: Iterable[PageRange]) -> None:
        ordered = sorted(completed_ranges)
        title = self._choose_title(ordered)
        if self.args.output_is_default:
            filename = safe_title_filename(title) + ".md"
            self.destination = self.destination.parent / filename
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        asset_dir = self.destination.parent / f"{self.destination.stem}_assets"
        sections: list[str] = []
        for page_range in ordered:
            segment_path = self.segments_dir / f"{page_range.key}.md"
            markdown = segment_path.read_text(encoding="utf-8").strip()
            source_markdown = find_markdown(self.runs_dir / page_range.key)
            markdown = copy_and_rewrite_assets(
                markdown,
                source_markdown,
                asset_dir,
                page_range.key,
                self.destination.parent,
            )
            if self.args.page_markers:
                markdown = f"<!-- Source PDF {page_range.label} -->\n\n{markdown}"
            sections.append(markdown)

        body = "\n\n".join(section for section in sections if section)
        if body and not re.match(r"^\s*#\s+", body):
            body = f"# {title}\n\n{body}"
        cover_path: Path | None = None
        if not self.args.no_cover:
            cover_path = asset_dir / "cover.png"
            render_cover(self.source, cover_path, self.args.cover_dpi)
            cover_relative = os.path.relpath(cover_path, self.destination.parent).replace(os.sep, "/")
            cover_relative = quote(cover_relative, safe="/._-")
            cover_markdown = f"![Cover of {title}]({cover_relative})"
            heading = re.match(r"^(#\s+[^\n]+\n)", body)
            if heading:
                body = body[: heading.end()] + "\n" + cover_markdown + "\n" + body[heading.end():].lstrip("\n")
            else:
                body = f"# {title}\n\n{cover_markdown}\n\n{body}"
        body = body.rstrip() + "\n"
        self.destination.write_text(body, encoding="utf-8")
        quality_path: Path | None = None
        if not self.args.no_quality_report:
            quality_path = self.destination.with_suffix(".quality.json")
            report = build_quality_report(
                body,
                title,
                int(self.state.get("total_pages", 0)),
                cover_path,
                int(self.state.get("source_links", 0)),
            )
            quality_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.state["output"] = str(self.destination)
        self.state["title"] = title
        self.state["quality_report"] = str(quality_path) if quality_path else None
        self._save_state()

    def convert(self) -> None:
        if not self.source.is_file():
            raise FileNotFoundError(f"Input PDF not found: {self.source}")
        if self.source.suffix.lower() != ".pdf":
            raise ValueError("Input must be a PDF file.")
        if self.state.get("source") not in (None, str(self.source)) and not self.args.fresh:
            raise RuntimeError("The work directory belongs to a different PDF. Use --fresh or another --work-dir.")
        if self.args.fresh and self.work_dir.exists():
            shutil.rmtree(self.work_dir)
            self.state = {"source": str(self.source), "completed": {}, "failures": {}}

        reader = PdfReader(str(self.source))
        total_pages = len(reader.pages)
        self.state.update(
            {"source": str(self.source), "total_pages": total_pages, "source_links": count_pdf_links(reader)}
        )
        self._save_state()
        self._log(
            f"Starting {self.source.name}: {total_pages} pages, backend={self.args.backend}, "
            f"chunk size={self.args.chunk_pages or total_pages}"
        )
        if self.args.api_url:
            self._log(f"Checking MinerU API: {api_health_url(self.args.api_url)}")
            health = wait_for_api_ready(
                self.args.api_url,
                timeout=self.args.api_wait,
                heartbeat=self.args.heartbeat,
                reporter=self._log,
            )
            protocol = health.get("protocol_version", "unknown")
            self._log(f"MinerU API is ready (protocol {protocol})")
        else:
            self._log(
                "No --api-url supplied; MinerU will start a temporary API. "
                "For slow machines, install the persistent service and use http://127.0.0.1:8000."
            )

        completed: list[PageRange] = []
        for page_range in make_ranges(total_pages, self.args.chunk_pages):
            completed.extend(self._process_range(page_range))
        self._merge(completed)
        self._log(f"Finished: {self.destination}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2md-ai",
        description="Convert a PDF to Markdown with MinerU, safely and resumably.",
    )
    parser.add_argument("pdf", type=Path, help="PDF file to convert")
    parser.add_argument("-o", "--output", type=Path, help="Final Markdown file")
    parser.add_argument(
        "--backend",
        default="pipeline",
        help="MinerU backend. 'pipeline' is CPU-friendly; 'auto' lets MinerU choose (default: pipeline)",
    )
    parser.add_argument("--api-url", help="Optional URL of a local or remote MinerU API service")
    parser.add_argument(
        "--api-wait",
        type=int,
        default=600,
        help="Seconds to wait for a persistent MinerU API after boot (default: 600)",
    )
    parser.add_argument("--language", help="OCR language code, for example en, es, or ch")
    parser.add_argument(
        "--chunk-pages",
        type=int,
        default=0,
        help="Pages per initial chunk; 0 tries the whole PDF then divides automatically on failure (default: 0)",
    )
    parser.add_argument("--min-chunk-pages", type=int, default=1, help="Smallest range used after a failure")
    parser.add_argument("--retries", type=int, default=1, help="Retries before dividing a failed range")
    parser.add_argument("--timeout", type=int, default=0, help="Seconds allowed per chunk; 0 means unlimited")
    parser.add_argument(
        "--heartbeat",
        type=int,
        default=10,
        help="Seconds between progress messages while MinerU is quiet (default: 10)",
    )
    parser.add_argument("--work-dir", type=Path, help="Checkpoint and temporary-file directory")
    parser.add_argument("--title", help="Title inserted if MinerU finds no top-level heading")
    parser.add_argument("--no-cover", action="store_true", help="Do not render the first PDF page as a cover image")
    parser.add_argument("--cover-dpi", type=int, default=160, help="Cover rendering resolution (default: 160 DPI)")
    parser.add_argument("--no-quality-report", action="store_true", help="Do not create the JSON proofreading report")
    parser.add_argument("--page-markers", action="store_true", help="Add invisible page-range comments to Markdown")
    parser.add_argument("--keep-chunks", action="store_true", help="Keep temporary split PDF files")
    parser.add_argument("--fresh", action="store_true", help="Discard checkpoints and start again")
    parser.add_argument("--mineru-command", default="mineru", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.pdf = args.pdf.expanduser()
    args.output_is_default = args.output is None
    if args.output is None:
        args.output = args.pdf.with_suffix(".md")
    else:
        args.output = args.output.expanduser()
    if args.work_dir is None:
        args.work_dir = args.output.parent / f".{args.output.stem}.pdf2md-work"
    else:
        args.work_dir = args.work_dir.expanduser()
    if (
        args.chunk_pages < 0
        or args.min_chunk_pages < 1
        or args.retries < 0
        or args.cover_dpi < 72
        or args.api_wait < 1
        or args.heartbeat < 1
    ):
        parser.error(
            "chunk/retry values must be non-negative; --min-chunk-pages, --api-wait, and "
            "--heartbeat must be at least 1"
        )

    try:
        Converter(args).convert()
    except KeyboardInterrupt:
        print("\nStopped safely. Run the same command to resume.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
