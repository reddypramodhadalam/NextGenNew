"""
service/jira_attachment_reader.py

Optional image-attachment evidence extraction for a single Jira issue.

Design goals (strict, no-hallucination):
  - Never invent or guess image content. Only text actually produced by a
    real OCR pass on the real downloaded bytes is ever included.
  - If OCR tooling (pytesseract / the Tesseract binary) is unavailable, or
    a download fails, skip that image and say so in the log — never
    substitute a placeholder or synthesized description.
  - Bounded cost: caps on attachment count and byte size, so a single
    story with many/large images cannot stall or blow up a survey run.
  - Scoped by design: callers should only invoke this for a single,
    explicitly requested issue (not a bulk project-wide read), because it
    performs real network downloads per image.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Optional

import urllib.request

logger = logging.getLogger(__name__)

MAX_IMAGES_PER_ISSUE = 10
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB per image
DOWNLOAD_TIMEOUT_SECONDS = 20

# Logged once per process so repeated calls don't spam the log when the
# optional OCR stack simply is not installed in this environment.
_ocr_unavailable_logged = False


@dataclass
class ImageAttachment:
    filename: str
    mime_type: str
    size: int
    content_url: str


def parse_image_attachments(raw_attachments: Optional[list]) -> list[ImageAttachment]:
    """
    Filter a Jira 'attachment' field payload down to image/* entries only.
    Pure parsing — no network calls, so always safe to call.
    """
    out: list[ImageAttachment] = []
    for a in raw_attachments or []:
        mime = str(a.get("mimeType") or "")
        if not mime.startswith("image/"):
            continue
        content_url = a.get("content") or ""
        if not content_url:
            continue
        out.append(
            ImageAttachment(
                filename=a.get("filename") or "unnamed",
                mime_type=mime,
                size=int(a.get("size") or 0),
                content_url=content_url,
            )
        )
    return out


def _download_bytes(url: str, auth_header: str, max_bytes: int) -> Optional[bytes]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": auth_header},
    )
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
            data = resp.read(max_bytes + 1)
    except Exception as exc:
        logger.warning("Image attachment download failed for %s: %s", url, exc)
        return None

    if len(data) > max_bytes:
        logger.info("Image attachment at %s exceeds %d bytes cap — skipping OCR", url, max_bytes)
        return None
    return data


def _ocr_extract_text(image_bytes: bytes) -> tuple[str, bool]:
    """
    Run OCR on real downloaded image bytes.

    Returns (text, ocr_ran). text is '' when OCR ran but found no text —
    that is a genuine result, not a failure. ocr_ran is False only when the
    OCR stack itself could not run (missing library / missing Tesseract
    binary / decode error), so callers can distinguish "nothing to say"
    from "could not check".
    """
    global _ocr_unavailable_logged
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        if not _ocr_unavailable_logged:
            logger.warning(
                "OCR libraries not available (%s) — image attachments will be "
                "listed by name only, with no content analysis.", exc,
            )
            _ocr_unavailable_logged = True
        return "", False

    try:
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image)
        return (text or "").strip(), True
    except pytesseract.TesseractNotFoundError as exc:
        if not _ocr_unavailable_logged:
            logger.warning(
                "Tesseract OCR binary not found (%s) — image attachments will be "
                "listed by name only, with no content analysis.", exc,
            )
            _ocr_unavailable_logged = True
        return "", False
    except Exception as exc:
        logger.warning("OCR failed for one image attachment: %s", exc)
        return "", False


def build_image_evidence_text(
    issue_key: str,
    raw_attachments: Optional[list],
    auth_header: str,
) -> str:
    """
    Build a factual, bounded evidence block for one issue's image
    attachments. Returns '' when there is nothing genuine to report.

    Every line in the returned text traces to either:
      - a filename Jira actually reported for this issue, or
      - text OCR actually extracted from the actual downloaded image bytes.
    Nothing here is inferred or guessed.
    """
    images = parse_image_attachments(raw_attachments)
    if not images:
        return ""

    considered = images[:MAX_IMAGES_PER_ISSUE]
    if len(images) > MAX_IMAGES_PER_ISSUE:
        logger.info(
            "Issue %s has %d image attachment(s) — processing first %d (cap)",
            issue_key, len(images), MAX_IMAGES_PER_ISSUE,
        )

    ocr_lines: list[str] = []
    name_only_lines: list[str] = []
    any_ocr_ran = False

    for img in considered:
        if img.size > MAX_IMAGE_BYTES:
            logger.info(
                "Issue %s attachment %s (%d bytes) exceeds cap — listing name only",
                issue_key, img.filename, img.size,
            )
            name_only_lines.append(img.filename)
            continue

        data = _download_bytes(img.content_url, auth_header, MAX_IMAGE_BYTES)
        if data is None:
            name_only_lines.append(img.filename)
            continue

        text, ocr_ran = _ocr_extract_text(data)
        if ocr_ran:
            any_ocr_ran = True
        if text:
            ocr_lines.append(f"- {img.filename}: {text}")
        else:
            name_only_lines.append(img.filename)

    logger.info(
        "Issue %s image evidence: %d image(s) considered, %d with extracted text, "
        "%d listed by name only, ocr_available=%s",
        issue_key, len(considered), len(ocr_lines), len(name_only_lines), any_ocr_ran,
    )

    if not ocr_lines and not name_only_lines:
        return ""

    lines = ["Image Attachments:"]
    lines.extend(ocr_lines)
    if name_only_lines:
        lines.append(
            "Not content-analyzed (name only — no OCR text available): "
            + ", ".join(name_only_lines)
        )
    return "\n".join(lines)
