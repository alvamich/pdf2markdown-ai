import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from pypdf import PdfWriter

from pdf2markdown_ai.cli import (
    InfrastructureError,
    PageRange,
    Converter,
    api_health_url,
    build_quality_report,
    build_parser,
    copy_and_rewrite_assets,
    looks_like_infrastructure_failure,
    make_ranges,
    resolve_mineru_command,
    safe_title_filename,
    split_range,
    title_from_markdown,
)


class CoreTests(unittest.TestCase):
    def test_make_ranges_covers_every_page_once(self):
        self.assertEqual(
            make_ranges(53, 25),
            [PageRange(0, 25), PageRange(25, 50), PageRange(50, 53)],
        )

    def test_zero_chunk_size_means_whole_document(self):
        self.assertEqual(make_ranges(200, 0), [PageRange(0, 200)])

    def test_whole_document_is_the_default(self):
        args = build_parser().parse_args(["book.pdf"])
        self.assertEqual(args.chunk_pages, 0)
        self.assertEqual(args.api_wait, 600)
        self.assertEqual(args.heartbeat, 10)

    def test_api_health_url(self):
        self.assertEqual(api_health_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000/health")

    def test_infrastructure_failures_are_recognized(self):
        self.assertTrue(looks_like_infrastructure_failure("Connection refused by API"))
        self.assertTrue(looks_like_infrastructure_failure("No module named 'six'"))
        self.assertTrue(
            looks_like_infrastructure_failure("Started local mineru-api\nINFO: Shutting down")
        )
        self.assertFalse(looks_like_infrastructure_failure("Could not parse a malformed table"))

    def test_missing_explicit_mineru_command_has_helpful_error(self):
        with self.assertRaises(InfrastructureError):
            resolve_mineru_command("/definitely/missing/mineru")

    def test_quality_report_flags_repeated_running_header(self):
        header = "Red Hat Enterprise Linux 8 Security hardening"
        markdown = "# Book\n\n" + "\n\n".join(f"{header}\n\nPage {number}" for number in range(8))
        report = build_quality_report(markdown, "Book", 8, None, source_links=3)
        self.assertEqual(report["repeated_lines"][0], {"count": 8, "text": header})
        self.assertTrue(any("Repeated lines" in warning for warning in report["warnings"]))
        self.assertTrue(any("no ordinary Markdown links" in warning for warning in report["warnings"]))

    def test_split_range_has_no_gap_or_overlap(self):
        left, right = split_range(PageRange(10, 25))
        self.assertEqual(left, PageRange(10, 17))
        self.assertEqual(right, PageRange(17, 25))

    def test_title_detection_and_safe_short_filename(self):
        self.assertEqual(title_from_markdown("# **The Great Book**\n\nText"), "The Great Book")
        long_title = "A Short Book Title: " + "A very long explanatory subtitle " * 4
        self.assertEqual(safe_title_filename(long_title), "A Short Book Title")
        self.assertEqual(safe_title_filename('A Book: With / Unsafe * Characters'), "A Book- With - Unsafe - Characters")

    def test_assets_are_copied_and_markdown_is_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            mineru_dir = tmp_path / "run"
            images = mineru_dir / "images"
            images.mkdir(parents=True)
            source_md = mineru_dir / "result.md"
            source_md.write_text("![figure](images/figure 1.png)", encoding="utf-8")
            (images / "figure 1.png").write_bytes(b"png")

            output_parent = tmp_path / "final"
            assets = output_parent / "book_assets"
            output_parent.mkdir()
            rewritten = copy_and_rewrite_assets(
                "![figure](images/figure%201.png)",
                source_md,
                assets,
                "000001-000010",
                output_parent,
            )

            self.assertIn("book_assets/", rewritten)
            self.assertEqual(len(list(assets.iterdir())), 1)

    def test_remote_images_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            source_md = tmp_path / "source.md"
            source_md.write_text("", encoding="utf-8")
            markdown = "![remote](https://example.com/a.png)"
            self.assertEqual(
                copy_and_rewrite_assets(markdown, source_md, tmp_path / "assets", "x", tmp_path),
                markdown,
            )

    def test_end_to_end_split_merge_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            pdf_path = tmp_path / "sample.pdf"
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=612, height=792)
            with pdf_path.open("wb") as pdf_file:
                writer.write(pdf_file)

            args = Namespace(
                pdf=pdf_path,
                output=tmp_path / "sample.md",
                work_dir=tmp_path / "work",
                backend="pipeline",
                api_url=None,
                api_wait=10,
                language="en",
                chunk_pages=2,
                min_chunk_pages=1,
                retries=0,
                timeout=0,
                heartbeat=1,
                title="Sample Book",
                output_is_default=True,
                page_markers=True,
                no_cover=False,
                cover_dpi=72,
                no_quality_report=False,
                keep_chunks=False,
                fresh=False,
                mineru_command="mineru",
            )
            converter = Converter(args)
            run_count = 0

            def fake_mineru(chunk: Path, run_dir: Path, page_range: PageRange) -> None:
                nonlocal run_count
                run_count += 1
                images = run_dir / "images"
                images.mkdir(parents=True, exist_ok=True)
                (images / "figure.png").write_bytes(b"png")
                (run_dir / "result.md").write_text(
                    f"## Chunk {run_count}\n\n![figure](images/figure.png)", encoding="utf-8"
                )

            converter._run_mineru = fake_mineru  # type: ignore[method-assign]
            converter.convert()
            self.assertEqual(converter.destination.name, "Sample Book.md")
            result = converter.destination.read_text(encoding="utf-8")

            self.assertEqual(run_count, 2)
            self.assertIn("# Sample Book", result)
            self.assertIn("![Cover of Sample Book](Sample%20Book_assets/cover.png)", result)
            self.assertIn("Source PDF pages 1-2", result)
            self.assertIn("Source PDF pages 3-3", result)
            self.assertEqual(len(list((tmp_path / "Sample Book_assets").iterdir())), 3)
            quality = json.loads(converter.destination.with_suffix(".quality.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["title"], "Sample Book")

            converter.convert()
            self.assertEqual(run_count, 2, "a second run should reuse completed ranges")


if __name__ == "__main__":
    unittest.main()
