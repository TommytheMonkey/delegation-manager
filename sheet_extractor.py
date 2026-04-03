#!/usr/bin/env python3
"""
Construction Drawing Sheet Extractor
=====================================
Extracts sheet numbers and page titles from construction drawing PDFs
using a multi-layer approach (fastest → slowest):

  Layer 1: PDF page labels & OCG layer names (instant, no rendering)
  Layer 2: Title block region crop + text extraction (fast, no AI)
  Layer 3: Regex pattern matching on extracted text
  Layer 4: AI vision fallback (only for low-confidence pages)

Usage:
    python sheet_extractor.py <pdf_path> [--output json|csv|table] [--ai-fallback]

    # Algorithmic only (fast, no API calls)
    python sheet_extractor.py drawings.pdf

    # With AI fallback for problem pages
    python sheet_extractor.py drawings.pdf --ai-fallback

    # With explicit API key
    python sheet_extractor.py drawings.pdf --ai-fallback --ai-key sk-ant-...

    # Or via env var
    export ANTHROPIC_API_KEY=sk-ant-...
    python sheet_extractor.py drawings.pdf --ai-fallback

Environment:
    ANTHROPIC_API_KEY  - Required for --ai-fallback
"""

import re
import os
import sys
import json
import time
import base64
import argparse
from io import BytesIO
from pathlib import Path
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import fitz  # pymupdf
import pikepdf

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class SheetCandidate:
    value: str
    score: float
    source: str
    font_size: float = 0.0

@dataclass
class SheetResult:
    page_index: int
    sheet_number: Optional[str] = None
    page_title: Optional[str] = None
    source: str = "none"
    confidence: float = 0.0
    all_sheet_candidates: list = field(default_factory=list)
    all_title_candidates: list = field(default_factory=list)
    raw_candidates: dict = field(default_factory=dict)

    @property
    def needs_ai(self) -> bool:
        return self.confidence < 0.5


# ============================================================================
# Sheet Number Regex Patterns & Validation
# ============================================================================

SHEET_NUMBER_PATTERNS = [
    re.compile(r'\b([A-Z]{1,4}[-.\s]?\d{1,3}[-.]?\d{0,3}[A-Z]?)\b'),
    re.compile(r'\b([A-Z][-]\d{3,4})\b'),
    re.compile(r'(?:SHEET|SHT|SH)[\s#.:]*(\S+)', re.IGNORECASE),
]

VALID_SHEET_PREFIXES = {
    'A', 'AD', 'AR',           # Architectural
    'S', 'ST',                  # Structural
    'C', 'CV',                  # Civil
    'L', 'LA', 'LH', 'LD',    # Landscape / Hardscape
    'LS', 'LP', 'LL', 'LI', 'LG', 'LC',  # Landscape sub-disciplines
    'I', 'IR', 'IG',           # Irrigation
    'E', 'EL', 'EP',           # Electrical
    'M', 'ME', 'MP',           # Mechanical
    'P', 'PL', 'PB',           # Plumbing
    'F', 'FP',                  # Fire Protection
    'G', 'GN',                  # General
    'D', 'DT', 'DM',           # Details / Demo
    'T',                        # Title / TOC
    'SP', 'SK', 'AS', 'X',    # Specs / Sketch / As-built / Cross
    'MEP',                      # Combined MEP
}

SHEET_NUMBER_BLACKLIST = {
    'AS SHOWN', 'AS NOTED', 'NTS', 'N/A', 'TBD', 'TYP', 'SIM',
    'SCALE', 'DATE', 'BY', 'NO', 'REV', 'MER', 'LMC', 'MKR',
    'INC', 'LLC', 'FAX', 'TEL', 'WWW', 'COM', 'NET', 'ORG',
    'KHA', 'DWG', 'CAD', 'PDF', 'BIM',
}

PROJECT_NUMBER_PATTERNS = [
    re.compile(r'^[A-Z]{1,4}\s*\d{4,}$'),        # FP 7014, FW19486
    re.compile(r'^[A-Z]{1,4}\s*\d{3}\.\d{1}$'),   # FP 701.4
]

TITLE_BLOCK_LABELS = {
    'sheet number', 'sheet no', 'sht no', 'sheet #', 'dwg no', 'drawing no',
    'project', 'project name', 'project no', 'project number',
    'date', 'scale', 'drawn by', 'designed by', 'checked by', 'approved by',
    'revision', 'revisions', 'rev', 'no.', 'description',
    'phone', 'fax', 'www', 'email', 'address',
    'registered', 'licensed', 'engineer', 'architect',
    'copyright', 'all rights reserved', 'kha project',
}

TITLE_SKIP_PATTERNS = [
    'SCALE:', 'DATE:', 'DRAWN', 'CHECKED', 'DESIGNED',
    'PROJECT NO', 'KHA PROJECT', 'PHONE:', 'FAX:',
    'WWW.', '.COM', '.NET', 'INC.', 'LLC', 'ASSOCIATES',
    'REGISTERED', 'FIRM F-', '©', 'COPYRIGHT',
    'REVISIONS', 'REVISION', 'PARKWAY', 'SUITE',
    'MATCHLINE', 'MATCH LINE', 'REF.', 'REF:',
    'SEE SHEET', 'SEE DWG',
    'ISSUED FOR', 'NOT FOR CONSTRUCTION',
    'PRELIMINARY', 'FOR REVIEW', 'ALL RIGHTS',
]


def is_likely_project_number(candidate: str) -> bool:
    c = candidate.upper().strip()
    for pattern in PROJECT_NUMBER_PATTERNS:
        if pattern.match(c):
            return True
    digits = re.sub(r'[^0-9]', '', c)
    if len(digits) >= 5:
        return True
    return False


def is_valid_sheet_number(candidate: str) -> bool:
    c = candidate.upper().strip()
    if c in SHEET_NUMBER_BLACKLIST:
        return False
    if len(c) < 2 or len(c) > 12:
        return False
    if not c[0].isalpha():
        return False
    if not any(ch.isdigit() for ch in c):
        return False
    prefix = re.match(r'^([A-Z]+)', c)
    if prefix and prefix.group(1) in VALID_SHEET_PREFIXES:
        return True
    if re.match(r'^[A-Z]{1,3}[-.\s]?\d', c):
        return True
    return False


def score_sheet_number(candidate: str) -> float:
    c = candidate.upper().strip()
    score = 0.0
    prefix = re.match(r'^([A-Z]+)', c)
    if prefix and prefix.group(1) in VALID_SHEET_PREFIXES:
        score += 0.5
    else:
        score += 0.2
    if re.match(r'^[A-Z]{1,4}[-.\s]\d{1,3}([-.]\d{1,3})?[A-Z]?$', c):
        score += 0.4
    elif re.match(r'^[A-Z]{1,4}\d{1,3}[-.]\d{1,3}[A-Z]?$', c):
        score += 0.4
    elif re.match(r'^[A-Z]{1,4}\d{1,4}$', c):
        score += 0.3
    if 3 <= len(c) <= 8:
        score += 0.1
    if is_likely_project_number(c):
        score -= 0.35
    return max(min(score, 1.0), 0.05)


# ============================================================================
# Layer 1: PDF Page Labels & OCG Layer Names
# ============================================================================

def extract_from_page_labels(pdf_path: str) -> dict[int, dict]:
    results = {}
    try:
        with pikepdf.open(pdf_path) as pdf:
            if '/PageLabels' in pdf.Root:
                labels = pdf.Root['/PageLabels']
                if '/Nums' in labels:
                    nums = list(labels['/Nums'])
                    for i in range(0, len(nums), 2):
                        page_idx = int(nums[i])
                        label_dict = nums[i + 1]
                        prefix = str(label_dict.get('/P', ''))
                        if prefix and is_valid_sheet_number(prefix):
                            results[page_idx] = {
                                'sheet_number': prefix,
                                'source': 'page_label',
                                'confidence': score_sheet_number(prefix),
                            }
    except Exception:
        pass
    return results


def extract_from_ocg_layers(pdf_path: str) -> dict[int, dict]:
    results = {}
    try:
        doc = fitz.open(pdf_path)
        ocgs = doc.get_ocgs()
        if ocgs:
            for xref, info in ocgs.items():
                name = info.get('name', '')
                if not name:
                    continue
                parts = re.split(r'\s*[-–—]\s*', name, maxsplit=1)
                if len(parts) >= 1:
                    candidate = parts[0].strip()
                    if is_valid_sheet_number(candidate):
                        title = parts[1].strip() if len(parts) > 1 else None
                        results[f'ocg_{xref}'] = {
                            'sheet_number': candidate,
                            'page_title': title,
                            'source': 'ocg',
                            'confidence': score_sheet_number(candidate),
                        }
        doc.close()
    except Exception:
        pass
    return results


# ============================================================================
# Layer 2: Title Block Region Text Extraction
# ============================================================================

def get_title_block_region(page: fitz.Page) -> list[fitz.Rect]:
    w, h = page.rect.width, page.rect.height
    regions = []
    is_landscape = w > h
    if is_landscape:
        regions.append(fitz.Rect(w * 0.78, 0, w, h))
        regions.append(fitz.Rect(w * 0.65, h * 0.50, w, h))
        regions.append(fitz.Rect(w * 0.55, h * 0.80, w, h))
        regions.append(fitz.Rect(0, h * 0.85, w, h))
    else:
        regions.append(fitz.Rect(0, h * 0.85, w, h))
        regions.append(fitz.Rect(w * 0.75, 0, w, h))
        regions.append(fitz.Rect(w * 0.55, h * 0.85, w, h))
    return regions


def get_title_block_render_region(page: fitz.Page) -> fitz.Rect:
    """Get a single region suitable for rendering to image (for AI fallback)."""
    w, h = page.rect.width, page.rect.height
    is_landscape = w > h
    if is_landscape:
        return fitz.Rect(w * 0.55, h * 0.40, w, h)
    else:
        return fitz.Rect(0, h * 0.70, w, h)


def extract_text_blocks_from_region(page: fitz.Page, clip: fitz.Rect) -> list[dict]:
    blocks = []
    text_dict = page.get_text("dict", clip=clip, flags=fitz.TEXT_PRESERVE_WHITESPACE)
    for block in text_dict.get("blocks", []):
        if block["type"] != 0:
            continue
        for line in block.get("lines", []):
            text_parts = []
            max_font_size = 0
            for span in line.get("spans", []):
                t = span["text"].strip()
                if t:
                    text_parts.append(t)
                    max_font_size = max(max_font_size, span["size"])
            full_text = " ".join(text_parts).strip()
            if full_text and len(full_text) > 1:
                blocks.append({
                    "text": full_text,
                    "bbox": block["bbox"],
                    "font_size": max_font_size,
                    "y": block["bbox"][1],
                    "x": block["bbox"][0],
                })
    return blocks


def extract_full_page_text_blocks(page: fitz.Page) -> list[dict]:
    blocks = []
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    for block in text_dict.get("blocks", []):
        if block["type"] != 0:
            continue
        for line in block.get("lines", []):
            text_parts = []
            max_font_size = 0
            for span in line.get("spans", []):
                t = span["text"].strip()
                if t:
                    text_parts.append(t)
                    max_font_size = max(max_font_size, span["size"])
            full_text = " ".join(text_parts).strip()
            if full_text and len(full_text) > 1:
                blocks.append({
                    "text": full_text,
                    "bbox": block["bbox"],
                    "font_size": max_font_size,
                    "y": block["bbox"][1],
                    "x": block["bbox"][0],
                })
    return blocks


def find_unclipped_text(clipped_text: str, clipped_y: float, full_blocks: list[dict],
                        font_size: float = 0, y_tolerance: float = 50.0) -> Optional[str]:
    if not clipped_text or len(clipped_text) < 3:
        return None
    ct = clipped_text.strip()
    ct_upper = ct.upper()
    candidates = []
    for fb in full_blocks:
        ft = fb["text"].strip()
        ft_upper = ft.upper()
        if len(ft) <= len(ct):
            continue
        if ft_upper.endswith(ct_upper):
            score = len(ft)
            if abs(fb["y"] - clipped_y) < y_tolerance:
                score += 100
            if font_size > 0 and abs(fb["font_size"] - font_size) < 1.0:
                score += 50
            candidates.append((ft, score))
            continue
        if ct_upper in ft_upper and len(ct) >= 4:
            score = len(ft)
            if abs(fb["y"] - clipped_y) < y_tolerance:
                score += 80
            if font_size > 0 and abs(fb["font_size"] - font_size) < 1.0:
                score += 50
            candidates.append((ft, score))
            continue
        min_suffix = min(len(ct), len(ft), 8)
        if min_suffix >= 4 and ft_upper[-min_suffix:] == ct_upper[-min_suffix:]:
            score = len(ft)
            if abs(fb["y"] - clipped_y) < y_tolerance:
                score += 60
            candidates.append((ft, score))
    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]
    return None


def is_title_block_label(text: str) -> bool:
    t = text.lower().strip().rstrip(':')
    if t in TITLE_BLOCK_LABELS:
        return True
    for label in TITLE_BLOCK_LABELS:
        if t == label or t.startswith(label + ' ') or t.startswith(label + ':'):
            return True
    return False


def extract_from_title_block(page: fitz.Page, page_idx: int) -> SheetResult:
    result = SheetResult(page_index=page_idx)
    regions = get_title_block_region(page)

    all_blocks = []
    for region in regions:
        blocks = extract_text_blocks_from_region(page, region)
        all_blocks.extend(blocks)

    if not all_blocks:
        return result

    # Deduplicate
    seen = set()
    unique_blocks = []
    for b in all_blocks:
        if b["text"] not in seen:
            seen.add(b["text"])
            unique_blocks.append(b)
    all_blocks = unique_blocks

    result.raw_candidates["text_blocks"] = [
        {"text": b["text"], "font_size": round(b["font_size"], 1)}
        for b in all_blocks[:20]
    ]

    # --- Find Sheet Number ---
    sheet_candidates = []
    for block in all_blocks:
        text = block["text"].upper().strip()
        if 'SHEET' in text and ('NUMBER' in text or 'NO' in text or '#' in text):
            after_label = re.sub(
                r'SHEET\s*(NUMBER|NO\.?|#)\s*[:.]?\s*', '', text
            ).strip()
            if after_label and is_valid_sheet_number(after_label):
                sheet_candidates.append(SheetCandidate(
                    value=after_label,
                    score=score_sheet_number(after_label) + 0.15,
                    source="sheet_label_adjacent",
                    font_size=block["font_size"],
                ))
        for pattern in SHEET_NUMBER_PATTERNS:
            for m in pattern.finditer(text):
                candidate = m.group(1) if m.lastindex else m.group(0)
                candidate = candidate.strip()
                if is_valid_sheet_number(candidate):
                    score = score_sheet_number(candidate)
                    sheet_candidates.append(SheetCandidate(
                        value=candidate, score=score, source="regex",
                        font_size=block["font_size"],
                    ))

    seen_candidates = {}
    for sc in sheet_candidates:
        key = sc.value.upper()
        if key not in seen_candidates or sc.score > seen_candidates[key].score:
            seen_candidates[key] = sc
    sheet_candidates = sorted(seen_candidates.values(), key=lambda x: x.score, reverse=True)

    result.all_sheet_candidates = [
        {"value": sc.value, "score": round(sc.score, 3), "source": sc.source}
        for sc in sheet_candidates
    ]

    if sheet_candidates:
        best = sheet_candidates[0]
        result.sheet_number = best.value
        result.confidence = best.score
        result.source = "title_block_text"

    # --- Find Page Title ---
    full_page_blocks = None
    title_candidates = []
    for block in all_blocks:
        text = block["text"].strip()
        text_upper = text.upper()
        if is_title_block_label(text):
            continue
        if result.sheet_number and text_upper == result.sheet_number.upper():
            continue
        if len(text) < 3:
            continue
        if any(skip in text_upper for skip in TITLE_SKIP_PATTERNS):
            continue
        if re.match(r'^\d{1,2}/\d{1,2}/\d{2,4}$', text):
            continue
        if re.match(r'^[\d./-]+$', text):
            continue
        if re.match(r'^[A-Z]{2,3}$', text):
            continue
        if re.match(r'^\d+/[A-Z]', text):
            continue
        if re.match(r'^[A-Z]{1,4}\d{4,}$', text_upper):
            continue
        if is_likely_project_number(text_upper):
            continue
        if is_valid_sheet_number(text) and score_sheet_number(text) > 0.5:
            continue

        font_score = min(block["font_size"] / 20.0, 1.0)
        len_score = min(len(text) / 30.0, 0.5)
        caps_bonus = 0.15 if text == text_upper and len(text) > 4 else 0
        total = font_score + len_score + caps_bonus
        title_candidates.append({
            "text": text, "score": total, "font_size": block["font_size"],
            "y": block["y"],
        })

    result.all_title_candidates = sorted(title_candidates, key=lambda x: x["score"], reverse=True)

    if result.all_title_candidates:
        best = result.all_title_candidates[0]
        title_text = best["text"]
        if full_page_blocks is None:
            full_page_blocks = extract_full_page_text_blocks(page)
        recovered = find_unclipped_text(
            title_text, best["y"], full_page_blocks, font_size=best.get("font_size", 0)
        )
        if recovered and len(recovered) > len(title_text):
            rec_upper = recovered.upper()
            if not any(skip in rec_upper for skip in TITLE_SKIP_PATTERNS):
                title_text = recovered
        result.page_title = title_text
        if not result.source or result.source == "none":
            result.source = "title_block_text"
        result.confidence = max(result.confidence, 0.4)

    return result


# ============================================================================
# Layer 4: AI Vision Fallback
# ============================================================================

def render_title_block_image(page: fitz.Page, dpi: int = 150) -> bytes:
    """Render the title block region of a page as a PNG image."""
    clip = get_title_block_render_region(page)
    # Render just the clipped region
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
    return pix.tobytes("png")


AI_PROMPT = """You are analyzing a construction drawing title block. Extract ONLY these two fields:

1. **Sheet Number** - The unique identifier for this sheet (e.g., A-101, L1.01, LH 2.01, C-001, MEP-3.2, S-102).
   It typically appears in its own box, often near the bottom of the title block, with a label like "SHEET NUMBER" or "SHEET NO" or "SHT".
   It follows a pattern of 1-4 letter prefix + numbers (with optional dots/dashes).
   Do NOT confuse with project numbers, permit numbers, or page counts.

2. **Page Title** - The descriptive name of this drawing sheet (e.g., "LANDSCAPE PLANTING PLAN", "HARDSCAPE ENLARGEMENT", "FLOOR PLAN - LEVEL 2").
   It is typically the LARGEST text in the title block, often in the center.
   Do NOT include project names, company names, addresses, or metadata.

Respond with ONLY valid JSON, no other text:
{"sheet_number": "XX-NNN", "page_title": "DESCRIPTIVE TITLE"}

If you cannot determine a field, use null for that field."""


def extract_with_ai(pages_to_process: list[tuple[int, fitz.Page]],
                    api_key: str) -> dict[int, dict]:
    """
    Send title block images to Claude API for extraction.
    Only processes the specific pages that need AI help.
    Returns dict of page_index -> {"sheet_number": ..., "page_title": ...}
    """
    if not HAS_HTTPX:
        print("  AI fallback requires 'httpx': pip install httpx", file=sys.stderr)
        return {}

    results = {}
    total = len(pages_to_process)
    print(f"  AI fallback: processing {total} pages...", file=sys.stderr)

    client = httpx.Client(timeout=30.0)
    headers = {
        "x-api-key": api_key,
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    for i, (page_idx, page) in enumerate(pages_to_process):
        try:
            # Render title block region as small PNG
            img_bytes = render_title_block_image(page, dpi=150)
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")

            payload = {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 256,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": img_b64,
                            },
                        },
                        {"type": "text", "text": AI_PROMPT},
                    ],
                }],
            }

            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

            # Parse response
            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")

            # Extract JSON from response (handle markdown fences)
            text = text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```\w*\n?', '', text)
                text = re.sub(r'\n?```$', '', text)
            text = text.strip()

            parsed = json.loads(text)
            results[page_idx] = {
                "sheet_number": parsed.get("sheet_number"),
                "page_title": parsed.get("page_title"),
            }
            print(f"    Page {page_idx + 1}: {parsed.get('sheet_number', '?')} - "
                  f"{parsed.get('page_title', '?')}", file=sys.stderr)

        except Exception as e:
            print(f"    Page {page_idx + 1}: AI error - {e}", file=sys.stderr)
            results[page_idx] = {"sheet_number": None, "page_title": None}

    client.close()
    return results


# ============================================================================
# Post-Processing: Dedup & Project Number Detection
# ============================================================================

def postprocess_results(results: list[SheetResult]) -> list[SheetResult]:
    """
    Post-processing pass:
    If the same "sheet number" appears on >25% of pages (min 3), it's likely
    a project/permit number. Demote and promote next-best candidate.
    """
    total_pages = len(results)
    threshold = max(3, int(total_pages * 0.25))

    sheet_counts = Counter()
    for r in results:
        if r.sheet_number:
            sheet_counts[r.sheet_number.upper()] += 1

    suspect_numbers = {sn for sn, count in sheet_counts.items() if count >= threshold}

    if suspect_numbers:
        print(f"  Detected likely project/permit numbers (appear {threshold}+ times): "
              f"{', '.join(suspect_numbers)}", file=sys.stderr)

    for r in results:
        if not r.sheet_number:
            continue
        if r.sheet_number.upper() not in suspect_numbers:
            continue

        promoted = False
        for candidate in r.all_sheet_candidates:
            alt = candidate["value"].upper()
            if alt == r.sheet_number.upper():
                continue
            if alt in suspect_numbers:
                continue
            if is_likely_project_number(candidate["value"]):
                continue
            old = r.sheet_number
            r.sheet_number = candidate["value"]
            r.confidence = candidate["score"]
            r.source = f"title_block_text (demoted {old})"
            promoted = True
            break

        if not promoted:
            r.confidence = min(r.confidence, 0.3)
            r.source = f"suspect_project_num ({sheet_counts[r.sheet_number.upper()]}x)"

    return results


# ============================================================================
# Main Pipeline
# ============================================================================

def extract_sheets(pdf_path: str, ai_fallback: bool = False,
                   api_key: Optional[str] = None) -> list[SheetResult]:
    """Run the full extraction pipeline on a PDF."""
    pdf_path = str(Path(pdf_path).resolve())
    results = []
    t0 = time.time()

    # --- Layer 1: Page labels ---
    page_label_results = extract_from_page_labels(pdf_path)
    ocg_results = extract_from_ocg_layers(pdf_path)

    # --- Layer 2+3: Title block extraction with regex ---
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    print(f"Processing {total_pages} pages from: {Path(pdf_path).name}", file=sys.stderr)

    for page_idx in range(total_pages):
        page = doc[page_idx]

        if page_idx in page_label_results:
            pl = page_label_results[page_idx]
            result = SheetResult(
                page_index=page_idx,
                sheet_number=pl['sheet_number'],
                source=pl['source'],
                confidence=pl['confidence'],
            )
        else:
            result = SheetResult(page_index=page_idx)

        tb_result = extract_from_title_block(page, page_idx)

        if tb_result.sheet_number:
            if not result.sheet_number or tb_result.confidence > result.confidence:
                result.sheet_number = tb_result.sheet_number
                result.confidence = max(result.confidence, tb_result.confidence)
                result.source = tb_result.source

        if tb_result.page_title:
            result.page_title = tb_result.page_title

        result.raw_candidates = tb_result.raw_candidates
        result.all_sheet_candidates = tb_result.all_sheet_candidates
        result.all_title_candidates = tb_result.all_title_candidates
        results.append(result)

        if total_pages > 20 and (page_idx + 1) % 25 == 0:
            elapsed = time.time() - t0
            rate = (page_idx + 1) / elapsed
            remaining = (total_pages - page_idx - 1) / rate
            print(f"  ...{page_idx + 1}/{total_pages} pages "
                  f"({elapsed:.1f}s elapsed, ~{remaining:.0f}s remaining)", file=sys.stderr)

    # --- Post-processing ---
    results = postprocess_results(results)

    # --- Layer 4: AI fallback for low-confidence pages ---
    ai_needed = [(r.page_index, doc[r.page_index]) for r in results if r.needs_ai]

    if ai_needed and ai_fallback:
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            ai_results = extract_with_ai(ai_needed, api_key)
            for r in results:
                if r.page_index in ai_results:
                    ai = ai_results[r.page_index]
                    if ai.get("sheet_number"):
                        r.sheet_number = ai["sheet_number"]
                        r.confidence = 0.85
                        r.source = "ai_vision"
                    if ai.get("page_title"):
                        r.page_title = ai["page_title"]
                        if not r.sheet_number:
                            r.source = "ai_vision"
                        r.confidence = max(r.confidence, 0.85)
        else:
            print("  AI fallback requested but no API key found. "
                  "Set ANTHROPIC_API_KEY or use --ai-key.", file=sys.stderr)

    doc.close()

    elapsed = time.time() - t0
    extracted = sum(1 for r in results if r.sheet_number and r.confidence >= 0.5)
    low_conf = sum(1 for r in results if r.needs_ai)
    print(f"\nDone: {total_pages} pages in {elapsed:.2f}s "
          f"({total_pages / elapsed:.0f} pages/sec)", file=sys.stderr)
    print(f"  Sheet numbers found (>=50% conf): {extracted}/{total_pages}", file=sys.stderr)
    print(f"  Low confidence (AI recommended): {low_conf}/{total_pages}", file=sys.stderr)

    return results


# ============================================================================
# Output Formatters
# ============================================================================

def format_table(results: list[SheetResult]) -> str:
    lines = []
    lines.append(f"{'Page':>5}  {'Sheet #':<14}  {'Confidence':>10}  {'Source':<35}  {'Page Title'}")
    lines.append("-" * 115)
    for r in results:
        conf_str = f"{r.confidence:.0%}"
        conf_icon = "✅" if r.confidence >= 0.7 else "⚠️" if r.confidence >= 0.4 else "❌"
        sheet = r.sheet_number or "—"
        title = (r.page_title or "—")[:45]
        source = (r.source or "none")[:35]
        lines.append(
            f"{r.page_index + 1:>5}  {sheet:<14}  {conf_icon} {conf_str:>7}  "
            f"{source:<35}  {title}"
        )
    return "\n".join(lines)


def format_json(results: list[SheetResult]) -> str:
    output = []
    for r in results:
        d = {
            "page_number": r.page_index + 1,
            "sheet_number": r.sheet_number,
            "page_title": r.page_title,
            "source": r.source,
            "confidence": round(r.confidence, 3),
            "alt_candidates": r.all_sheet_candidates[:5],
        }
        output.append(d)
    return json.dumps(output, indent=2)


def format_csv(results: list[SheetResult]) -> str:
    lines = ["page_number,sheet_number,page_title,confidence,source"]
    for r in results:
        title = (r.page_title or "").replace('"', '""')
        sheet = r.sheet_number or ""
        lines.append(f'{r.page_index + 1},"{sheet}","{title}",{r.confidence:.2f},{r.source}')
    return "\n".join(lines)


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Extract sheet numbers and page titles from construction drawing PDFs"
    )
    parser.add_argument("pdf_path", help="Path to the PDF file")
    parser.add_argument(
        "--output", "-o", choices=["table", "json", "csv"], default="table",
        help="Output format (default: table)"
    )
    parser.add_argument("--ai-fallback", action="store_true",
                        help="Enable AI vision fallback for low-confidence pages")
    parser.add_argument("--ai-key", default=None,
                        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    parser.add_argument("--debug", action="store_true",
                        help="Show raw text block candidates")
    args = parser.parse_args()

    if not Path(args.pdf_path).exists():
        print(f"Error: File not found: {args.pdf_path}", file=sys.stderr)
        sys.exit(1)

    results = extract_sheets(
        args.pdf_path,
        ai_fallback=args.ai_fallback,
        api_key=args.ai_key,
    )

    if args.output == "table":
        print("\n" + format_table(results))
    elif args.output == "json":
        print(format_json(results))
    elif args.output == "csv":
        print(format_csv(results))

    if args.debug:
        print("\n\n=== DEBUG: Raw candidates per page ===")
        for r in results:
            print(f"\nPage {r.page_index + 1}:")
            if r.all_sheet_candidates:
                print(f"  Sheet candidates:")
                for sc in r.all_sheet_candidates[:5]:
                    print(f"    [{sc['score']:.3f}] {sc['value']} ({sc['source']})")
            if r.raw_candidates.get("text_blocks"):
                print(f"  Text blocks:")
                for tb in r.raw_candidates["text_blocks"]:
                    print(f"    [{tb['font_size']:>5.1f}pt] {tb['text'][:80]}")


if __name__ == "__main__":
    main()