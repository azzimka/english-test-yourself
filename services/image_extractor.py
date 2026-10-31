import io
import re
import uuid
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from PIL import Image

from config import DATA_DIR

logger = logging.getLogger(__name__)

# Directory where extracted test images are stored and served via /static/tests/
TESTS_MEDIA_DIR = DATA_DIR / "tests"
TESTS_MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Question start marker pattern (excludes sections, parts, dates, and option letters)
QUESTION_LINE_PATTERN = re.compile(
    r"^\s*(?!(?:Section|Part|Unit|Module|Grade|Page|Class|Level|Year|Раздел|Часть|Блок|Страница|Класс)\b)"
    r"(?:(?:Question|Task|Exercise|Problem|Задание|Вопрос|Item|№)\s*)?"
    r"(\d{1,3})"
    r"(?:[.)\:\-]\s*|\s+(?=[A-ZА-ЯЁ]))",
    re.IGNORECASE | re.MULTILINE
)
QUESTION_NUM_PATTERN = QUESTION_LINE_PATTERN

# Visual reference keywords in question text
VISUAL_KEYWORDS_PATTERN = re.compile(
    r"\b(picture|image|figure|diagram|illustration|photo|drawing|graph|triangle|circle|rectangle|angle|shape|shown\s+below|shown\s+in\s+the|look\s+at\s+the|рисунок|схема|чертеж|график|иллюстрация|треугольник|фигура|на\s+рисунке)\b",
    re.IGNORECASE
)


@dataclass
class ExtractedImage:
    image_url: str
    image_path: Path
    image_bytes: bytes
    question_num: Optional[int]
    page_num: int
    bbox: Optional[Tuple[float, float, float, float]] = None


def extract_images_from_pdf(
    pdf_bytes: bytes,
    test_id_prefix: Optional[str] = None
) -> List[ExtractedImage]:
    """
    Extracts images and visual diagrams from PDF bytes using PyMuPDF (fitz)
    with a graceful fallback to pypdf.
    
    Associates extracted images with specific question numbers based on
    page coordinate layout and textual visual cues.
    """
    if not pdf_bytes:
        return []

    prefix = test_id_prefix or f"doc_{hashlib.md5(pdf_bytes[:1024]).hexdigest()[:8]}"

    # Primary method: PyMuPDF (handles raster images + vector drawings + bounding boxes)
    try:
        import pymupdf
        return _extract_with_pymupdf(pdf_bytes, prefix)
    except ImportError:
        logger.warning("PyMuPDF not available, falling back to pypdf image extraction.")
    except Exception as e:
        logger.warning("PyMuPDF extraction failed: %s. Falling back to pypdf.", e)

    # Fallback method: pypdf + Pillow
    try:
        return _extract_with_pypdf(pdf_bytes, prefix)
    except Exception as e:
        logger.warning("pypdf extraction also failed: %s", e)
        return []


def _extract_with_pymupdf(pdf_bytes: bytes, prefix: str) -> List[ExtractedImage]:
    """PyMuPDF implementation: extracts embedded raster images and vector diagrams."""
    import pymupdf

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    extracted_images: List[ExtractedImage] = []
    saved_hashes = set()

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1

        # 1. Map questions and their vertical boundaries on this page
        # List of (q_num, y_start, y_end)
        questions_on_page = _locate_questions_on_pymupdf_page(page)

        # 2. Extract embedded raster images
        page_images = page.get_images(full=True)
        for img_idx, img_info in enumerate(page_images):
            xref = img_info[0]
            try:
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                rect = rects[0]

                # Filter out small decorative icons / bullets / hairline borders
                if rect.width < 35 or rect.height < 35:
                    continue

                base_image = doc.extract_image(xref)
                img_bytes = base_image.get("image")
                if not img_bytes:
                    continue

                # Deduplicate identical images on the same page
                img_hash = hashlib.md5(img_bytes).hexdigest()
                if img_hash in saved_hashes:
                    continue
                saved_hashes.add(img_hash)

                # Correlate with question
                q_num = _match_rect_to_question(rect, questions_on_page)

                # Save image
                ext = base_image.get("ext", "png")
                filename = f"{prefix}_p{page_num}_q{q_num or 'img'}_{img_idx}.{ext}"
                out_path = TESTS_MEDIA_DIR / filename
                out_path.write_bytes(img_bytes)

                image_url = f"/static/tests/{filename}"
                extracted_images.append(
                    ExtractedImage(
                        image_url=image_url,
                        image_path=out_path,
                        image_bytes=img_bytes,
                        question_num=q_num,
                        page_num=page_num,
                        bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                    )
                )
                logger.info(
                    "Extracted raster image on page %d (q=%s, %dx%d) -> %s",
                    page_num, q_num, int(rect.width), int(rect.height), image_url
                )
            except Exception as e:
                logger.debug("Failed to extract raster image xref %d: %s", xref, e)

        # 3. Extract vector diagrams/drawings if no raster image matched questions
        # Many Olympiad geometry & math problems are drawn as vector paths
        try:
            drawings = page.get_drawings()
            if drawings:
                diagram_rects = _cluster_drawings_into_diagrams(drawings, page.rect)
                for d_idx, d_rect in enumerate(diagram_rects):
                    # Check if this drawing overlaps with any question that doesn't already have an image
                    q_num = _match_rect_to_question(d_rect, questions_on_page)
                    already_has_image = any(
                        img.question_num == q_num for img in extracted_images if q_num is not None
                    )
                    if already_has_image:
                        continue

                    # Render the drawing region as a crisp PNG pixmap
                    pad = 10
                    crop_rect = pymupdf.Rect(
                        max(0, d_rect.x0 - pad),
                        max(0, d_rect.y0 - pad),
                        min(page.rect.width, d_rect.x1 + pad),
                        min(page.rect.height, d_rect.y1 + pad)
                    )
                    pix = page.get_pixmap(clip=crop_rect, dpi=150)
                    png_bytes = pix.tobytes("png")

                    if len(png_bytes) < 100:
                        continue

                    img_hash = hashlib.md5(png_bytes).hexdigest()
                    if img_hash in saved_hashes:
                        continue
                    saved_hashes.add(img_hash)

                    filename = f"{prefix}_p{page_num}_q{q_num or 'diag'}_v{d_idx}.png"
                    out_path = TESTS_MEDIA_DIR / filename
                    out_path.write_bytes(png_bytes)

                    image_url = f"/static/tests/{filename}"
                    extracted_images.append(
                        ExtractedImage(
                            image_url=image_url,
                            image_path=out_path,
                            image_bytes=png_bytes,
                            question_num=q_num,
                            page_num=page_num,
                            bbox=(d_rect.x0, d_rect.y0, d_rect.x1, d_rect.y1),
                        )
                    )
                    logger.info(
                        "Extracted vector diagram on page %d (q=%s) -> %s",
                        page_num, q_num, image_url
                    )
        except Exception as e:
            logger.debug("Failed vector diagram extraction on page %d: %s", page_num, e)

    return extracted_images


def _locate_questions_on_pymupdf_page(page) -> List[Dict[str, Any]]:
    """
    Finds questions and their vertical boundaries on a PyMuPDF page.
    Returns list of dicts: {'num': int, 'y0': float, 'y1': float, 'text': str, 'y_end': float}
    """
    questions: List[Dict[str, Any]] = []
    seen_nums = set()

    # Attempt 1: Line-level precision using page.get_text("dict")
    try:
        page_dict = page.get_text("dict")
        for b in page_dict.get("blocks", []):
            if "lines" in b:
                for l in b["lines"]:
                    line_text = "".join(s.get("text", "") for s in l.get("spans", [])).strip()
                    m = QUESTION_LINE_PATTERN.match(line_text)
                    if m:
                        q_num = int(m.group(1))
                        if 0 < q_num <= 200 and q_num not in seen_nums:
                            seen_nums.add(q_num)
                            bbox = l.get("bbox", (0, 0, 0, 0))
                            questions.append({
                                "num": q_num,
                                "y0": float(bbox[1]),
                                "y1": float(bbox[3]),
                                "text": line_text,
                            })
    except Exception as e:
        logger.debug("Dict-based text extraction failed on page: %s", e)

    # Attempt 2: Fallback to blocks if dict found nothing
    if not questions:
        blocks = page.get_text("blocks")
        for b in blocks:
            if len(b) < 5:
                continue
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
            lines = text.split("\n")
            for l_idx, line in enumerate(lines):
                m = QUESTION_LINE_PATTERN.match(line.strip())
                if m:
                    q_num = int(m.group(1))
                    if 0 < q_num <= 200 and q_num not in seen_nums:
                        seen_nums.add(q_num)
                        line_offset = ((y1 - y0) / max(len(lines), 1)) * l_idx
                        questions.append({
                            "num": q_num,
                            "y0": float(y0 + line_offset),
                            "y1": float(y0 + line_offset + 15),
                            "text": line.strip(),
                        })

    # Sort questions by vertical position y0
    questions.sort(key=lambda q: q["y0"])

    # Establish full question vertical range [y0, next_q.y0]
    for i in range(len(questions)):
        if i + 1 < len(questions):
            questions[i]["y_end"] = questions[i + 1]["y0"]
        else:
            questions[i]["y_end"] = float(page.rect.height)

    return questions


def _match_rect_to_question(rect, questions_on_page: List[Dict[str, Any]]) -> Optional[int]:
    """Matches a bounding rectangle of an image/drawing to the appropriate question."""
    if not questions_on_page:
        return None

    rect_mid_y = (rect.y0 + rect.y1) / 2.0

    # 1. Primary check: does the midpoint fall into the question's vertical range?
    for q in questions_on_page:
        if q["y0"] <= rect_mid_y < q.get("y_end", 9999.0):
            return q["num"]

    # 2. Check if rect significantly overlaps with the question bounds
    best_overlap = 0.0
    best_q = None
    for q in questions_on_page:
        y_top = max(rect.y0, q["y0"])
        y_bot = min(rect.y1, q.get("y_end", 9999.0))
        overlap = max(0.0, y_bot - y_top)
        if overlap > best_overlap:
            best_overlap = overlap
            best_q = q["num"]

    if best_q is not None and best_overlap > 10.0:
        return best_q

    # 3. If there is only one question on this page, map to it
    if len(questions_on_page) == 1:
        return questions_on_page[0]["num"]

    # 4. Keyword affinity check: find nearest question that explicitly mentions visual cues
    visual_qs = [q for q in questions_on_page if VISUAL_KEYWORDS_PATTERN.search(q["text"])]
    if visual_qs:
        nearest_visual = min(
            visual_qs,
            key=lambda q: min(abs(q["y0"] - rect_mid_y), abs(q.get("y_end", q["y0"]) - rect_mid_y))
        )
        return nearest_visual["num"]

    # 5. Final fallback: nearest question by vertical center distance
    nearest_q = min(questions_on_page, key=lambda q: abs(q["y0"] - rect_mid_y))
    return nearest_q["num"]


def _cluster_drawings_into_diagrams(drawings, page_rect) -> List[Any]:
    """
    Clusters vector drawings on a page into cohesive diagram bounding rectangles.
    Filters out simple horizontal dividing rules or tiny bullets.
    """
    import pymupdf

    diagram_rects = []
    min_size = 35.0  # minimum width & height to be considered a diagram

    for d in drawings:
        rect = d.get("rect")
        if not rect:
            continue
        # Filter out full-width page dividing lines or tiny accents
        if rect.width > page_rect.width * 0.95 and rect.height < 5.0:
            continue
        if rect.width < 10.0 and rect.height < 10.0:
            continue

        # Try to merge with an existing cluster if close (within 25px)
        merged = False
        for i, existing in enumerate(diagram_rects):
            # Proximity check
            if (
                abs(existing.y0 - rect.y0) < 30
                or abs(existing.y1 - rect.y1) < 30
                or existing.intersects(rect)
            ):
                diagram_rects[i] = existing | rect
                merged = True
                break
        if not merged:
            diagram_rects.append(pymupdf.Rect(rect))

    # Return only diagrams that meet size threshold
    return [r for r in diagram_rects if r.width >= min_size and r.height >= min_size]


def _extract_with_pypdf(pdf_bytes: bytes, prefix: str) -> List[ExtractedImage]:
    """Fallback implementation using pypdf."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    extracted = []
    saved_hashes = set()

    for page_idx, page in enumerate(reader.pages):
        page_num = page_idx + 1
        page_text = page.extract_text() or ""
        q_matches = list(QUESTION_NUM_PATTERN.finditer(page_text))
        page_q_nums = [int(m.group(1)) for m in q_matches if 0 < int(m.group(1)) <= 200]

        for img_idx, img in enumerate(page.images):
            try:
                img_bytes = img.data
                img_hash = hashlib.md5(img_bytes).hexdigest()
                if img_hash in saved_hashes:
                    continue
                saved_hashes.add(img_hash)

                # Open with Pillow to verify and get dimensions
                pil_img = Image.open(io.BytesIO(img_bytes))
                if pil_img.width < 40 or pil_img.height < 40:
                    continue

                q_num = page_q_nums[0] if len(page_q_nums) == 1 else None
                if not q_num and page_q_nums:
                    # Look for visual keywords in question context
                    for qn in page_q_nums:
                        if VISUAL_KEYWORDS_PATTERN.search(page_text):
                            q_num = qn
                            break

                filename = f"{prefix}_p{page_num}_q{q_num or 'img'}_{img_idx}.png"
                out_path = TESTS_MEDIA_DIR / filename

                # Save as PNG
                pil_img.save(out_path, format="PNG")

                extracted.append(
                    ExtractedImage(
                        image_url=f"/static/tests/{filename}",
                        image_path=out_path,
                        image_bytes=img_bytes,
                        question_num=q_num,
                        page_num=page_num,
                    )
                )
            except Exception as e:
                logger.debug("pypdf failed to extract image %d on page %d: %s", img_idx, page_num, e)

    return extracted
