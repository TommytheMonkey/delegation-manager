"""
job_processor.py
----------------
Orchestrates the full job processing pipeline:
1. Parse job brief → extract Monday item ID
2. Run sheet extractor on all PDFs
3. Fetch metadata from Monday.com
4. Estimate pages + complexity via Claude
5. Update the job brief with results
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional

from sheet_extractor import extract_sheets, format_table
from monday_client import MondayClient
from claude_client import ClaudeClient

logger = logging.getLogger(__name__)

MONDAY_ID_PATTERN = re.compile(r'MONDAY\s*ITEM\s*ID\s*:\s*(\d+)', re.IGNORECASE)


def find_brief(folder: Path) -> Optional[Path]:
    """Find the job brief file in a folder."""
    for f in folder.iterdir():
        if f.is_file() and "job brief" in f.name.lower():
            return f
    return None


def find_pdfs(folder: Path) -> list[Path]:
    """Find all PDF files in a folder."""
    return sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".pdf")


def parse_monday_id(brief_path: Path) -> Optional[str]:
    """Extract the Monday item ID from the job brief."""
    text = brief_path.read_text(encoding="utf-8", errors="replace")
    match = MONDAY_ID_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


def run_sheet_extractor(pdf_paths: list[Path]) -> list:
    """Run sheet extractor on all PDFs, merge results with sequential page numbering."""
    all_results = []
    offset = 0
    for pdf_path in pdf_paths:
        logger.info(f"[PROCESSOR] Extracting sheets from: {pdf_path.name}")
        results = extract_sheets(str(pdf_path), ai_fallback=False)
        # Offset page indices if merging multiple PDFs
        if offset > 0:
            for r in results:
                r.page_index += offset
        all_results.extend(results)
        offset += len(results)
    return all_results


def update_brief(brief_path: Path, estimated_pages: int, complexity: int,
                 sheet_table: str, had_error: bool = False) -> None:
    """
    Update the job brief with estimation results and sheet index.
    - Insert page count and complexity after the Monday ID line
    - Append sheet index at the end
    """
    text = brief_path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")

    # Find the Monday ID line and insert after it
    insert_idx = None
    for i, line in enumerate(lines):
        if MONDAY_ID_PATTERN.search(line):
            insert_idx = i + 1
            break

    if insert_idx is not None:
        new_lines = []
        if had_error:
            new_lines.append(f"Estimated Page Count: ERROR — could not complete analysis")
            new_lines.append(f"Complexity: ERROR")
        else:
            new_lines.append(f"Estimated Page Count: ~{estimated_pages}")
            new_lines.append(f"Complexity: {complexity}/5")

        for nl in reversed(new_lines):
            lines.insert(insert_idx, nl)

    # Append sheet index at the end
    lines.append("")
    lines.append("========================================")
    lines.append("")
    lines.append("SHEET INDEX:")
    lines.append("")
    lines.append(sheet_table)

    brief_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"[PROCESSOR] Updated brief: {brief_path.name}")


def process_job(folder_path: str) -> dict:
    """
    Run the full pipeline on a job folder.

    Args:
        folder_path: Path to the job folder containing PDFs and a job brief

    Returns:
        dict with processing results including estimated_pages, complexity, etc.
    """
    folder = Path(folder_path)
    logger.info(f"[PROCESSOR] Processing job folder: {folder}")

    # 1. Find job brief
    brief_path = find_brief(folder)
    if not brief_path:
        raise FileNotFoundError(f"No job brief found in {folder}")
    logger.info(f"[PROCESSOR] Found brief: {brief_path.name}")

    # 2. Parse Monday item ID
    item_id = parse_monday_id(brief_path)
    if not item_id:
        raise ValueError(f"No Monday Item ID found in {brief_path.name}")
    logger.info(f"[PROCESSOR] Monday Item ID: {item_id}")

    # 3. Find PDFs
    pdf_paths = find_pdfs(folder)
    if not pdf_paths:
        raise FileNotFoundError(f"No PDF files found in {folder}")
    logger.info(f"[PROCESSOR] Found {len(pdf_paths)} PDF(s)")

    # 4. Run sheet extractor
    sheet_results = run_sheet_extractor(pdf_paths)
    sheet_table = format_table(sheet_results)
    logger.info(f"[PROCESSOR] Extracted {len(sheet_results)} sheets")

    # 5. Fetch Monday.com data
    monday_data = None
    try:
        monday = MondayClient()
        item_data = monday.fetch_item(item_id)
        monday_data = monday.extract_columns(item_data)
        logger.info(f"[PROCESSOR] Monday data: product={monday_data['product']}, scope={monday_data['scope']}")
    except Exception as e:
        logger.error(f"[PROCESSOR] Monday.com failed: {e}")

    # 6. Run Claude analysis
    claude_result = None
    try:
        claude = ClaudeClient()
        claude_result = claude.analyze_job(
            sheet_results=sheet_results,
            scope=monday_data["scope"] if monday_data else "",
            notes=monday_data["notes"] if monday_data else "",
            product=monday_data["product"] if monday_data else "",
        )
        logger.info(
            f"[PROCESSOR] Claude estimate: ~{claude_result['estimated_pages']} pages, "
            f"complexity={claude_result['complexity']}/5"
        )
    except Exception as e:
        logger.error(f"[PROCESSOR] Claude analysis failed: {e}")

    # 7. Update the brief
    had_error = claude_result is None or claude_result.get("error")
    update_brief(
        brief_path,
        estimated_pages=claude_result["estimated_pages"] if claude_result else 0,
        complexity=claude_result["complexity"] if claude_result else 0,
        sheet_table=sheet_table,
        had_error=had_error,
    )

    # 8. Return result summary
    result = {
        "folder": str(folder),
        "item_id": item_id,
        "total_sheets": len(sheet_results),
        "pdfs_processed": len(pdf_paths),
        "monday_data": monday_data,
        "estimated_pages": claude_result["estimated_pages"] if claude_result else None,
        "complexity": claude_result["complexity"] if claude_result else None,
        "reasoning": claude_result["reasoning"] if claude_result else None,
        "had_error": had_error,
    }

    logger.info(f"[PROCESSOR] Done: {folder.name}")
    return result
