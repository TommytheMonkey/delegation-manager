# Plan Indexer Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a job processing pipeline that runs the sheet extractor on incoming PDFs, pulls metadata from Monday.com, estimates page counts and complexity via Claude, and writes results back into the job brief.

**Architecture:** Single-process polling loop. Four new modules (`monday_client.py`, `claude_client.py`, `job_processor.py`, `main.py`) that orchestrate around the existing `sheet_extractor.py`. Local folder watcher for dev; Drive watcher later.

**Tech Stack:** Python 3.13, pymupdf, pikepdf, httpx, anthropic SDK

**Spec:** `docs/superpowers/specs/2026-04-03-plan-indexer-pipeline-design.md`

---

### Task 1: Update requirements.txt and project setup

**Files:**
- Modify: `requirements.txt`
- Create: `.gitignore`
- Create: `Procfile`

- [ ] **Step 1: Update requirements.txt**

Replace the current `requirements.txt` with the actual dependencies this project needs:

```
pymupdf>=1.27
pikepdf>=10.0
httpx>=0.27
anthropic>=0.40
```

- [ ] **Step 2: Create .gitignore**

```
__pycache__/
*.pyc
venv/
.venv/
.env
watch/
.DS_Store
*.egg-info/
```

- [ ] **Step 3: Create Procfile**

```
worker: python main.py
```

- [ ] **Step 4: Install new deps**

Run: `source venv/bin/activate && pip install httpx anthropic`

- [ ] **Step 5: Commit**

```bash
git add requirements.txt .gitignore Procfile
git commit -m "chore: update deps, add gitignore and Procfile"
```

---

### Task 2: Monday.com Client

**Files:**
- Create: `monday_client.py`

- [ ] **Step 1: Create monday_client.py**

```python
"""
monday_client.py
----------------
Minimal Monday.com GraphQL client for fetching item columns.
Follows patterns from takeo-reviewer/reviewer/monday_client.py.
"""

import os
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MONDAY_API_URL = "https://api.monday.com/v2"

# Column IDs on board 3874058084
COL_SCOPE = "dropdown35"
COL_NOTES = "long_text_mkqwc9v8"
COL_PRODUCT = "color"

FETCH_ITEM_QUERY = """
query ($ids: [ID!]!) {
  items(ids: $ids) {
    id
    name
    column_values {
      id
      text
      value
    }
  }
}
"""


class MondayClient:
    def __init__(self, api_token: Optional[str] = None):
        self.token = api_token or os.environ["MONDAY_API_TOKEN"]
        self.headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
            "API-Version": "2024-10",
        }

    def _query(self, query: str, variables: dict = None) -> dict:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = httpx.post(
            MONDAY_API_URL, json=payload, headers=self.headers, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise ValueError(f"Monday API error: {data['errors']}")
        return data["data"]

    def fetch_item(self, item_id: str) -> dict:
        """Fetch a single item by ID with all column values."""
        logger.info(f"[MONDAY] Fetching item {item_id}")
        data = self._query(FETCH_ITEM_QUERY, {"ids": [str(item_id)]})
        items = data.get("items", [])
        if not items:
            raise ValueError(f"[MONDAY] Item {item_id} not found")
        logger.info(f"[MONDAY] Found item: {items[0]['name']}")
        return items[0]

    def extract_columns(self, item_data: dict) -> dict:
        """
        Extract the columns we care about from a Monday item.
        Returns {"scope": str, "notes": str, "product": str, "item_name": str}
        """
        columns = {col["id"]: col.get("text", "") or "" for col in item_data.get("column_values", [])}

        product = columns.get(COL_PRODUCT, "")
        if not product:
            logger.warning(f"[MONDAY] Product column '{COL_PRODUCT}' is empty — will use generic estimation")

        return {
            "item_name": item_data.get("name", ""),
            "scope": columns.get(COL_SCOPE, ""),
            "notes": columns.get(COL_NOTES, ""),
            "product": product,
        }
```

- [ ] **Step 2: Smoke test (manual)**

Run: `source venv/bin/activate && python -c "from monday_client import MondayClient; print('import OK')"`
Expected: `import OK`

- [ ] **Step 3: Commit**

```bash
git add monday_client.py
git commit -m "feat: add Monday.com client for fetching item columns"
```

---

### Task 3: Claude Client

**Files:**
- Create: `claude_client.py`

- [ ] **Step 1: Create claude_client.py**

```python
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
```

- [ ] **Step 2: Smoke test**

Run: `source venv/bin/activate && python -c "from claude_client import ClaudeClient, _format_sheet_index; print(_format_sheet_index([{'page_number': 1, 'sheet_number': 'LP 1.01', 'confidence': 0.95, 'page_title': 'PLANTING PLAN'}]))"`

Expected: A formatted table row.

- [ ] **Step 3: Commit**

```bash
git add claude_client.py
git commit -m "feat: add Claude client for page estimation and complexity scoring"
```

---

### Task 4: Job Processor (Orchestrator)

**Files:**
- Create: `job_processor.py`

- [ ] **Step 1: Create job_processor.py**

```python
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
```

- [ ] **Step 2: Smoke test**

Run: `source venv/bin/activate && python -c "from job_processor import parse_monday_id, find_pdfs; from pathlib import Path; print(parse_monday_id(Path('tests/Franklin Park Terrace - Job Brief')))"`

Expected: `11607590263`

- [ ] **Step 3: Commit**

```bash
git add job_processor.py
git commit -m "feat: add job processor orchestrator"
```

---

### Task 5: Local Folder Watcher (main.py)

**Files:**
- Create: `main.py`

- [ ] **Step 1: Create main.py**

```python
"""
main.py
-------
Entry point: polls a local watch directory for new job folders and processes them.
Designed to be swapped for a Google Drive watcher later.

Usage:
    python main.py                    # Watch ./watch with 30s interval
    python main.py --watch-dir /path  # Custom watch directory
    python main.py --once             # Single scan, no loop (for testing)
"""

import os
import sys
import json
import time
import logging
import argparse
import re
from pathlib import Path

from job_processor import process_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def load_processed(watch_dir: Path) -> set:
    """Load the set of already-processed folder paths."""
    processed_file = watch_dir / ".processed.json"
    if processed_file.exists():
        try:
            return set(json.loads(processed_file.read_text()))
        except (json.JSONDecodeError, TypeError):
            return set()
    return set()


def save_processed(watch_dir: Path, processed: set) -> None:
    """Save the set of processed folder paths."""
    processed_file = watch_dir / ".processed.json"
    processed_file.write_text(json.dumps(sorted(processed), indent=2))


def scan_for_jobs(watch_dir: Path) -> list[dict]:
    """
    Scan for ready job folders.
    Structure: watch_dir/YYYY-MM-DD/customer_name/job_name/
    A job is ready when it has at least 1 PDF and a job brief file.
    """
    jobs = []

    if not watch_dir.exists():
        return jobs

    for date_dir in watch_dir.iterdir():
        if not date_dir.is_dir() or not DATE_PATTERN.match(date_dir.name):
            continue
        for customer_dir in date_dir.iterdir():
            if not customer_dir.is_dir():
                continue
            for job_dir in customer_dir.iterdir():
                if not job_dir.is_dir():
                    continue

                # Check for PDFs
                pdfs = [f for f in job_dir.iterdir() if f.is_file() and f.suffix.lower() == ".pdf"]
                if not pdfs:
                    continue

                # Check for job brief
                brief = None
                for f in job_dir.iterdir():
                    if f.is_file() and "job brief" in f.name.lower():
                        brief = f
                        break
                if not brief:
                    continue

                jobs.append({
                    "folder": str(job_dir),
                    "pdfs": [str(p) for p in pdfs],
                    "brief": str(brief),
                })

    return jobs


def run_watcher(watch_dir: Path, poll_interval: int, once: bool = False) -> None:
    """Main watcher loop."""
    logger.info(f"Watching: {watch_dir.resolve()}")
    logger.info(f"Poll interval: {poll_interval}s")

    watch_dir.mkdir(parents=True, exist_ok=True)
    processed = load_processed(watch_dir)

    while True:
        jobs = scan_for_jobs(watch_dir)

        new_jobs = [j for j in jobs if j["folder"] not in processed]

        if new_jobs:
            logger.info(f"Found {len(new_jobs)} new job(s)")

        for job in new_jobs:
            folder = job["folder"]
            try:
                logger.info(f"Processing: {Path(folder).name}")
                result = process_job(folder)

                if result.get("had_error"):
                    logger.warning(f"Completed with errors: {Path(folder).name}")
                else:
                    logger.info(
                        f"Done: {Path(folder).name} — "
                        f"~{result['estimated_pages']} pages, "
                        f"complexity {result['complexity']}/5"
                    )

                processed.add(folder)
                save_processed(watch_dir, processed)

            except Exception as e:
                logger.error(f"Failed to process {Path(folder).name}: {e}", exc_info=True)

        if once:
            break

        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="Plan Indexer — Local Folder Watcher")
    parser.add_argument("--watch-dir", default=os.environ.get("WATCH_DIR", "./watch"),
                        help="Directory to watch (default: ./watch)")
    parser.add_argument("--interval", type=int,
                        default=int(os.environ.get("POLL_INTERVAL", "30")),
                        help="Poll interval in seconds (default: 30)")
    parser.add_argument("--once", action="store_true",
                        help="Run a single scan then exit (for testing)")
    args = parser.parse_args()

    run_watcher(Path(args.watch_dir), args.interval, once=args.once)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke test**

Run: `source venv/bin/activate && python main.py --once --watch-dir ./watch`

Expected: Starts, scans, finds nothing (no job folders yet), exits cleanly.

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add local folder watcher entry point"
```

---

### Task 6: End-to-End Integration Test with Sample Data

**Files:**
- No new files — uses existing `tests/Franklin Park Terrace - Job Brief` and `Sample_1.pdf`

- [ ] **Step 1: Set up a test job folder**

Create the watch directory structure with the sample data:

```bash
mkdir -p "watch/2026-04-03/Exscape Group/Franklin Park Terrace"
cp Sample_1.pdf "watch/2026-04-03/Exscape Group/Franklin Park Terrace/"
cp "tests/Franklin Park Terrace - Job Brief" "watch/2026-04-03/Exscape Group/Franklin Park Terrace/"
```

- [ ] **Step 2: Run the pipeline in single-scan mode**

Run: `source venv/bin/activate && MONDAY_API_TOKEN=<your-token> ANTHROPIC_API_KEY=<your-key> python main.py --once`

Expected output:
- Logs showing sheet extraction (53 pages from Sample_1.pdf)
- Monday.com fetch for item 11607590263
- Claude analysis returning page estimate and complexity
- Brief updated with new lines

- [ ] **Step 3: Verify the updated job brief**

Check: `cat "watch/2026-04-03/Exscape Group/Franklin Park Terrace/Franklin Park Terrace - Job Brief"`

Expected: The brief now contains:
- `Estimated Page Count: ~{N}` after the Monday ID line
- `Complexity: {X}/5` after the page count line
- `SHEET INDEX:` section appended at the bottom with the full table

- [ ] **Step 4: Verify idempotency**

Run: `source venv/bin/activate && MONDAY_API_TOKEN=<your-token> ANTHROPIC_API_KEY=<your-key> python main.py --once`

Expected: No jobs processed (folder already in `.processed.json`).

- [ ] **Step 5: Commit**

```bash
git commit -m "test: verify end-to-end pipeline with sample data"
```

---

### Task 7: Clean up and final commit

- [ ] **Step 1: Remove test watch directory**

```bash
rm -rf watch/
```

- [ ] **Step 2: Final commit with all files**

```bash
git add -A
git status
git commit -m "feat: complete plan indexer pipeline — local watcher, Monday client, Claude estimator, job processor"
```
