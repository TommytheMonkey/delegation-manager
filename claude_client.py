"""
claude_client.py
----------------
Uses Claude API to estimate page counts and complexity for construction takeoff jobs.
Two prompt modes: MTO (material takeoff) and IR (irrigation).
"""

import os
import re
import json
import logging
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"

MTO_SYSTEM_PROMPT = """You are an expert construction takeoff estimator specializing in Division 32 sitework: landscape, irrigation, hardscape, and retaining walls.

You are analyzing a sheet index from a construction drawing set. Your job is to estimate:
1. How many pages will have actual counts & measurements that a takeoff estimator needs to work through
2. The complexity/density of the project

COUNTING RULES:
- COUNT pages that contain actual plan views with countable/measurable items (planting plans, hardscape plans, sitework plans, grading plans with measurable work, irrigation plans, etc.)
- DO NOT COUNT reference-only sheets: cover sheets, overall/key plans, schedules, details pages, notes pages, legends, specifications, electrical/MEP sheets (unless specifically in scope)
- The reference sheets are still needed for context but don't count toward the page estimate

COMPLEXITY SCALE (1-5):
1 = Very simple: wide open field, mostly turf, maybe a few trees. Minimal variety.
2 = Simple: straightforward site with moderate variety. Standard residential or small commercial.
3 = Moderate: typical commercial project, reasonable density of items, multiple material types.
4 = Complex: multi-area project, many different item types, multiple plan areas or phases.
5 = Very complex: dense enlargement pages, 50+ different measurable items, requires heavy zooming, intricate details.

Consider the scope of work and any notes when making your assessment.

Return ONLY valid JSON, no markdown fences, no commentary:
{"estimated_pages": <int>, "complexity": <int 1-5>, "reasoning": "<brief explanation>"}"""

IR_SYSTEM_PROMPT = """You are an expert irrigation designer reviewing a sheet index from a construction drawing set.

You are estimating the irrigation design workload. The irrigation design is based on the planting plan — we create one irrigation plan page per planting plan page at a 30' scale.

YOUR TASKS:
1. Confirm which sheets are planting plan pages (typically LP-prefix sheets, but verify from titles)
2. Assess if any sheets appear to be at scales larger than 30' scale (look for clues in titles like "OVERALL", "KEY PLAN", or if a single sheet covers a very large area)
3. If sheets are at larger scales (e.g., 40', 50', 60'), estimate how many additional pages would be needed to redraw them at 30' scale. For example, a 60' scale sheet would need ~4 pages at 30' scale.
4. Rate the complexity of the irrigation design work

COMPLEXITY SCALE (1-5):
1 = Very simple: mostly turf, few plant types, minimal detail.
2 = Simple: standard planting with moderate variety.
3 = Moderate: typical commercial landscape, multiple plant types and zones.
4 = Complex: many plant types, multiple irrigation zones, varying water requirements.
5 = Very complex: dense mixed planting, complex topography, many micro-zones, drip and spray mix.

Consider the scope of work and any notes when making your assessment.

Return ONLY valid JSON, no markdown fences, no commentary:
{"estimated_pages": <int>, "complexity": <int 1-5>, "reasoning": "<brief explanation>"}"""


def _format_sheet_index(sheet_results: list) -> str:
    """Format sheet results into a readable table for the prompt."""
    lines = []
    lines.append(f"{'Page':>5}  {'Sheet #':<14}  {'Conf':>5}  {'Page Title'}")
    lines.append("-" * 70)
    for r in sheet_results:
        # Support both SheetResult objects and dicts
        if hasattr(r, 'page_index'):
            page_num = r.page_index + 1
            sheet = r.sheet_number or "—"
            conf = f"{r.confidence:.0%}"
            title = (r.page_title or "—")[:50]
        else:
            page_num = r.get("page_number", "?")
            sheet = r.get("sheet_number") or "—"
            conf = f"{r.get('confidence', 0):.0%}"
            title = (r.get("page_title") or "—")[:50]
        lines.append(f"{page_num:>5}  {sheet:<14}  {conf:>5}  {title}")
    return "\n".join(lines)


class ClaudeClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.client = anthropic.Anthropic(
            api_key=self.api_key,
            timeout=120.0,
        )

    def analyze_job(
        self,
        sheet_results: list,
        scope: str,
        notes: str,
        product: str,
    ) -> dict:
        """
        Analyze a job's sheet index and return page estimate + complexity.

        Args:
            sheet_results: List of SheetResult objects or dicts from sheet_extractor
            scope: Scope of work from Monday.com
            notes: Notes from Monday.com
            product: Product type ("MTO - Const.", "IR - FLD", etc.)

        Returns:
            {"estimated_pages": int, "complexity": int, "reasoning": str}
        """
        sheet_table = _format_sheet_index(sheet_results)
        total_pages = len(sheet_results)

        # Pick prompt mode
        is_irrigation = product and "IR" in product.upper() and "FLD" in product.upper()

        if is_irrigation:
            system_prompt = IR_SYSTEM_PROMPT
            # Pre-count LP sheets for context
            lp_count = sum(
                1 for r in sheet_results
                if (getattr(r, 'sheet_number', None) or (r.get('sheet_number') if isinstance(r, dict) else ''))
                and (getattr(r, 'sheet_number', '') or r.get('sheet_number', '')).upper().startswith('LP')
            )
            user_msg = (
                f"SHEET INDEX ({total_pages} total pages, {lp_count} LP-prefix sheets):\n\n"
                f"{sheet_table}\n\n"
                f"SCOPE OF WORK: {scope or '(not specified)'}\n\n"
                f"NOTES: {notes or '(none)'}"
            )
        else:
            system_prompt = MTO_SYSTEM_PROMPT
            if product and product != "MTO - Const.":
                system_prompt += f"\n\nNote: Product type is '{product}' — not a standard MTO job. Use your best judgment for page estimation."
            user_msg = (
                f"SHEET INDEX ({total_pages} total pages):\n\n"
                f"{sheet_table}\n\n"
                f"SCOPE OF WORK: {scope or '(not specified)'}\n\n"
                f"NOTES: {notes or '(none)'}"
            )

        logger.info(f"[CLAUDE] Analyzing job: {total_pages} pages, product={product or 'unknown'}")

        # Call Claude with one retry on parse failure
        for attempt in range(2):
            try:
                response = self.client.messages.create(
                    model=MODEL,
                    max_tokens=512,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_msg}],
                )

                text = response.content[0].text.strip()
                # Strip markdown fences if present
                if text.startswith("```"):
                    text = re.sub(r'^```\w*\n?', '', text)
                    text = re.sub(r'\n?```$', '', text)
                    text = text.strip()

                result = json.loads(text)

                # Validate fields
                estimated_pages = int(result.get("estimated_pages", 0))
                complexity = max(1, min(5, int(result.get("complexity", 3))))
                reasoning = str(result.get("reasoning", ""))

                logger.info(f"[CLAUDE] Result: ~{estimated_pages} pages, complexity={complexity}/5")
                return {
                    "estimated_pages": estimated_pages,
                    "complexity": complexity,
                    "reasoning": reasoning,
                }

            except (json.JSONDecodeError, KeyError, TypeError) as e:
                if attempt == 0:
                    logger.warning(f"[CLAUDE] Parse error on attempt 1, retrying: {e}")
                    continue
                logger.error(f"[CLAUDE] Parse error on attempt 2: {e}")
                return {
                    "estimated_pages": 0,
                    "complexity": 0,
                    "reasoning": f"ERROR: Could not parse Claude response — {e}",
                    "error": True,
                }

            except Exception as e:
                logger.error(f"[CLAUDE] API error: {e}")
                return {
                    "estimated_pages": 0,
                    "complexity": 0,
                    "reasoning": f"ERROR: Claude API call failed — {e}",
                    "error": True,
                }
