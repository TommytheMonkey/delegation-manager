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
    """Find the job brief file in a folder. Prefer the original (non-.md) version."""
    candidates = []
    for f in folder.iterdir():
        if f.is_file() and "job brief" in f.name.lower():
            candidates.append(f)
    if not candidates:
        return None
    # Prefer non-.md files (original brief from n8n)
    non_md = [f for f in candidates if f.suffix.lower() != ".md"]
    return non_md[0] if non_md else candidates[0]


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


def parse_brief_sections(text: str) -> dict:
    """
    Parse the raw job brief into structured sections.
    Splits on ======== dividers and extracts key-value fields.
    """
    sections = re.split(r'={3,}', text)
    parsed = {
        "monday_id": "",
        "project_name": "",
        "date": "",
        "due_date": "",
        "sender": "",
        "product_type": "",
        "scope": "",
        "instructions": "",
        "updates": "",
    }

    # First section: header (Monday ID, project name, date, due date)
    if sections:
        header = sections[0].strip()
        id_match = MONDAY_ID_PATTERN.search(header)
        if id_match:
            parsed["monday_id"] = id_match.group(1)

        header_lines = [l.strip() for l in header.split("\n") if l.strip()]
        for line in header_lines:
            if MONDAY_ID_PATTERN.search(line):
                continue
            # Skip lines that look like previously-injected estimates
            if line.startswith("Estimated Page Count:") or line.startswith("Complexity:"):
                continue
            if line.startswith("**Due Date:"):
                parsed["due_date"] = line.replace("**Due Date:**", "").strip()
            elif not parsed["project_name"]:
                parsed["project_name"] = line
            elif not parsed["date"]:
                # Verify it looks like a date (contains a month name or digits with slashes/dashes)
                if re.search(r'(?:January|February|March|April|May|June|July|August|September|October|November|December|\d{1,2}[/\-])', line, re.IGNORECASE):
                    parsed["date"] = line

    # Second section: contact/product/scope
    if len(sections) > 1:
        meta = sections[1].strip()
        for line in meta.split("\n"):
            line = line.strip()
            if line.upper().startswith("SENDER/CONTACT:"):
                parsed["sender"] = line.split(":", 1)[1].strip()
            elif line.upper().startswith("PRODUCT TYPE:"):
                parsed["product_type"] = line.split(":", 1)[1].strip()
            elif line.upper().startswith("SCOPE:"):
                parsed["scope"] = line.split(":", 1)[1].strip()

    # Third section: instructions
    if len(sections) > 2:
        instr = sections[2].strip()
        # Strip the "INSTRUCTIONS:" label
        instr = re.sub(r'^INSTRUCTIONS\s*:\s*', '', instr, flags=re.IGNORECASE).strip()
        parsed["instructions"] = instr

    # Fourth+ sections: updates/comments
    if len(sections) > 3:
        updates_parts = []
        for s in sections[3:]:
            s = s.strip()
            # Strip the "UPDATES/COMMENTS:" label if present
            s = re.sub(r'^UPDATES/COMMENTS\s*:\s*', '', s, flags=re.IGNORECASE).strip()
            # Skip empty sections and previously-appended sheet index sections
            if not s or s.startswith("SHEET INDEX:"):
                continue
            updates_parts.append(s)
        parsed["updates"] = "\n\n".join(updates_parts)

    return parsed


def build_markdown_brief(parsed: dict, estimated_pages: int, complexity: int,
                         sheet_table: str, reasoning: str = "",
                         monday_scope: str = "", had_error: bool = False) -> str:
    """Build a clean markdown job brief from parsed sections + analysis results."""
    lines = []

    # Header
    lines.append(f"# {parsed['project_name'] or 'Job Brief'}")
    lines.append("")
    lines.append(f"**Monday Item ID:** {parsed['monday_id']}")
    if parsed["date"]:
        lines.append(f"**Date:** {parsed['date']}")
    if parsed["due_date"]:
        lines.append(f"**Due Date:** {parsed['due_date']}")
    lines.append("")

    # Estimation results
    lines.append("---")
    lines.append("")
    if had_error:
        lines.append("**Estimated Page Count:** ERROR — could not complete analysis")
        lines.append("**Complexity:** ERROR")
    else:
        lines.append(f"**Estimated Page Count:** ~{estimated_pages}")
        lines.append(f"**Complexity:** {complexity}/5")
        if reasoning:
            lines.append(f"**Reasoning:** {reasoning}")
    lines.append("")

    # Job details
    lines.append("---")
    lines.append("")
    lines.append("## Job Details")
    lines.append("")
    if parsed["sender"]:
        lines.append(f"**Sender/Contact:** {parsed['sender']}")
    if parsed["product_type"]:
        lines.append(f"**Product Type:** {parsed['product_type']}")
    scope_display = monday_scope or parsed["scope"]
    if scope_display:
        lines.append(f"**Scope:** {scope_display}")
    lines.append("")

    # Instructions
    if parsed["instructions"]:
        lines.append("## Instructions")
        lines.append("")
        lines.append(parsed["instructions"])
        lines.append("")

    # Updates/Comments
    if parsed["updates"]:
        lines.append("## Updates/Comments")
        lines.append("")
        lines.append(parsed["updates"])
        lines.append("")

    # Sheet Index
    lines.append("---")
    lines.append("")
    lines.append("## Sheet Index")
    lines.append("")
    lines.append("```")
    lines.append(sheet_table)
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def update_brief(brief_path: Path, estimated_pages: int, complexity: int,
                 sheet_table: str, reasoning: str = "", monday_scope: str = "",
                 had_error: bool = False) -> Path:
    """
    Parse the original job brief, reformat everything as a clean .md file.
    Returns the path to the new .md file.
    """
    text = brief_path.read_text(encoding="utf-8", errors="replace")
    parsed = parse_brief_sections(text)

    md_content = build_markdown_brief(
        parsed, estimated_pages, complexity, sheet_table,
        reasoning=reasoning, monday_scope=monday_scope, had_error=had_error,
    )

    # Write as .md file with same name
    md_path = brief_path.parent / (brief_path.stem + ".md")
    md_path.write_text(md_content, encoding="utf-8")
    logger.info(f"[PROCESSOR] Created markdown brief: {md_path.name}")
    return md_path


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

    # 7. Update the brief → output as .md
    had_error = claude_result is None or claude_result.get("error")
    md_path = update_brief(
        brief_path,
        estimated_pages=claude_result["estimated_pages"] if claude_result else 0,
        complexity=claude_result["complexity"] if claude_result else 0,
        sheet_table=sheet_table,
        reasoning=claude_result["reasoning"] if claude_result else "",
        monday_scope=monday_data["scope"] if monday_data else "",
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
