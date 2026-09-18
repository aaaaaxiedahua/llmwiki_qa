"""PDF extraction via opendataloader-pdf.

Shared module used by both the hosted OCR service and the local processor.
No server-specific dependencies (no asyncpg, S3, httpx).
"""

import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

EXTRACT_TIMEOUT_SECONDS = 180
MAX_EXTRACT_JSON_BYTES = 128 * 1024 * 1024
MAX_EXTRACT_PAGES = 10_000
MAX_EXTRACT_ELEMENTS = 100_000

_EXTRACT_SCRIPT = (
    "import sys, opendataloader_pdf\n"
    "opendataloader_pdf.convert("
    "input_path=sys.argv[1], output_dir=sys.argv[2], format='json', "
    "image_output='embedded', quiet=True)\n"
)


def _element_to_markdown(el: dict) -> str:
    """Convert a single JSON element to markdown."""
    t = el.get("type", "")
    content = el.get("content", "")

    if t == "heading":
        level = max(1, min(el.get("heading level", 1), 6))
        prefix = "#" * level
        return f"{prefix} {content}"

    if t == "paragraph":
        return content

    if t == "list":
        lines = []
        for item in el.get("list items", []):
            lines.append(f"- {item.get('content', '')}")
            for child in item.get("kids", []):
                lines.append(f"  - {child.get('content', '')}")
        return "\n".join(lines)

    if t == "image":
        # Don't include the data URI in markdown — images are stored separately
        return ""

    if t == "caption":
        return f"*{content}*" if content else ""

    # Skip headers, footers, and unknown types
    return ""


def _parse_data_uri(data_uri: str) -> tuple[bytes, str] | None:
    """Parse a data URI into (bytes, format). Returns None on failure."""
    if not data_uri.startswith("data:"):
        return None
    try:
        header, b64 = data_uri.split(",", 1)
        fmt = "png"
        if "jpeg" in header or "jpg" in header:
            fmt = "jpeg"
        return base64.b64decode(b64), fmt
    except Exception:
        return None


def _elements_to_pages(
    elements: list[dict], total_pages: int,
) -> list[tuple[int, str, list[dict]]]:
    """Group JSON elements by page number and reconstruct markdown per page.

    Returns a list of (page_num, markdown, images) for every page up to total_pages.
    Each image dict has: {"id": str, "bytes": bytes, "format": str}
    """
    if not isinstance(total_pages, int) or total_pages < 0:
        total_pages = 0
    total_pages = min(total_pages, MAX_EXTRACT_PAGES)
    if len(elements) > MAX_EXTRACT_ELEMENTS:
        raise RuntimeError(
            f"PDF extraction produced too many elements ({len(elements):,})"
        )

    page_elements: dict[int, list[dict]] = defaultdict(list)

    for el in elements:
        page_num = el.get("page number")
        if page_num is None or el.get("type") in ("header", "footer"):
            continue
        page_elements[page_num].append(el)

    pages = []
    img_counter = 0
    for page_num in range(1, total_pages + 1):
        parts = []
        images = []
        for el in page_elements.get(page_num, []):
            if el.get("type") == "image":
                src = el.get("source", "")
                parsed = _parse_data_uri(src) if src else None
                if parsed:
                    img_bytes, fmt = parsed
                    img_id = f"img_{page_num}_{img_counter}.{fmt}"
                    img_counter += 1
                    images.append({"id": img_id, "bytes": img_bytes, "format": fmt})
                continue
            md = _element_to_markdown(el)
            if md:
                parts.append(md)
        pages.append((page_num, "\n\n".join(parts), images))

    return pages


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate the extractor and the JVM/processes it spawned."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - exercised on Windows installations
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if proc.poll() is None:
                proc.kill()
    except ProcessLookupError:
        pass
    finally:
        proc.wait()


def _run_extractor(pdf_path: str, output_dir: str) -> None:
    """Run opendataloader in a bounded child process group."""
    popen_options: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
    }
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):  # pragma: no cover - Windows
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [sys.executable, "-c", _EXTRACT_SCRIPT, pdf_path, output_dir],
        **popen_options,
    )
    try:
        _, stderr = proc.communicate(timeout=EXTRACT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _kill_process_tree(proc)
        raise TimeoutError(
            f"PDF extraction timed out after {EXTRACT_TIMEOUT_SECONDS} seconds"
        ) from error

    if proc.returncode != 0:
        detail = (stderr or b"").decode(errors="replace")[:500]
        raise RuntimeError(f"opendataloader-pdf failed: {detail}")


def extract_pdf(pdf_path: str) -> list[tuple[int, str, list[dict]]]:
    """Run opendataloader-pdf and return per-page markdown with images.

    Returns list of (page_num, markdown, images) where images is a list of
    {"id": str, "bytes": bytes, "format": str} dicts.

    Raises RuntimeError if extraction fails (Java missing, corrupt PDF, etc.).
    """
    try:
        with tempfile.TemporaryDirectory() as extract_dir:
            _run_extractor(pdf_path, extract_dir)

            json_files = list(Path(extract_dir).glob("*.json"))
            if not json_files:
                raise RuntimeError("opendataloader-pdf produced no output")

            json_path = json_files[0]
            json_size = json_path.stat().st_size
            if json_size > MAX_EXTRACT_JSON_BYTES:
                raise RuntimeError(
                    "PDF extraction output exceeds the "
                    f"{MAX_EXTRACT_JSON_BYTES // (1024 * 1024)} MiB limit"
                )

            with json_path.open(encoding="utf-8") as f:
                data = json.load(f)

        if not isinstance(data, dict):
            raise RuntimeError("opendataloader-pdf returned invalid JSON")
        total_pages = data.get("number of pages", 0)
        elements = data.get("kids", [])
        if not isinstance(elements, list) or not all(isinstance(el, dict) for el in elements):
            raise RuntimeError("opendataloader-pdf returned invalid elements")
        return _elements_to_pages(elements, total_pages)
    except (RuntimeError, TimeoutError):
        raise
    except Exception as e:
        raise RuntimeError(f"PDF extraction failed: {e}") from e
