# Plan Indexer Pipeline — Design Spec

## Goal

Automate construction drawing intake: when PDFs land in a job folder, run the sheet extractor, pull job metadata from Monday.com, use Claude to estimate page counts and complexity, and write the results back into the job brief.

## Architecture

Single-process polling loop (Approach A). Four new modules plus an entry point:

```
main.py (entry point, local folder watcher)
  ↓
job_processor.py (orchestrator)
  ├── sheet_extractor.py (existing — extract sheet index from PDFs)
  ├── monday_client.py (fetch item columns via GraphQL)
  └── claude_client.py (page estimation + complexity scoring)
```

### File Layout

```
plan_indexer_plain/
├── main.py                # Entry point: local watcher loop
├── job_processor.py       # Orchestrator
├── monday_client.py       # Monday.com GraphQL client
├── claude_client.py       # Claude API for estimation
├── sheet_extractor.py     # Existing — no changes
├── requirements.txt       # Updated with new deps
├── Procfile               # Heroku worker
├── watch/                 # Local test directory (gitignored)
│   └── YYYY-MM-DD/customer/job/  # Mimics Drive structure
└── tests/
    └── Franklin Park Terrace - Job Brief  # Sample brief
```

## Components

### 1. main.py — Local Folder Watcher

Polls `watch/` directory every 30 seconds.

**Job detection logic:**
- Scan for `watch/YYYY-MM-DD/{customer}/{job}/` folders
- A job is "ready" when the folder contains at least 1 `.pdf` file AND a file matching `*Job Brief*` or `*job brief*`
- Track processed folders in `watch/.processed.json` (set of folder paths)
- On detection: call `job_processor.process_job(folder_path)`

**Interface** (so Drive watcher can replace it later):
```python
def scan_for_jobs(watch_dir: str) -> list[dict]:
    """Returns list of {"folder": path, "pdfs": [paths], "brief": path}"""

def mark_processed(folder: str) -> None:
    """Add folder to processed set"""
```

**Env vars:** `WATCH_DIR` (default: `./watch`), `POLL_INTERVAL` (default: `30`)

### 2. monday_client.py — Monday.com Integration

Reuses patterns from takeo-reviewer: same board ID `3874058084`, same auth approach.

**Public API:**
```python
class MondayClient:
    def __init__(self, api_token: str):
        """Auth: Authorization header with raw token (no Bearer prefix), API-Version: 2024-10"""

    def fetch_item(self, item_id: str) -> dict:
        """Fetch single item by ID with column values."""

    def extract_columns(self, item_data: dict) -> dict:
        """Returns {
            "scope": str,         # dropdown35
            "notes": str,         # long_text_mkqwc9v8
            "product": str,       # color (e.g. "MTO - Const." or "IR - FLD")
        }"""
```

**GraphQL query:**
```graphql
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
```

**Env var:** `MONDAY_API_TOKEN`

### 3. claude_client.py — Page Estimation & Complexity

Uses Anthropic SDK (like takeo-reviewer's ai_reviewer pattern).

**Public API:**
```python
class ClaudeClient:
    def __init__(self, api_key: str):
        """anthropic.Anthropic client, model claude-sonnet-4-20250514"""

    def analyze_job(self, sheet_index: list[SheetResult], scope: str,
                    notes: str, product: str) -> dict:
        """Returns {
            "estimated_pages": int,
            "complexity": int,      # 1-5
            "reasoning": str,       # brief explanation
        }"""
```

**Two prompt modes based on product:**

**MTO - Const. prompt:**
Sends the sheet index table + scope + notes. Asks Claude to:
- Identify which pages will have actual counts & measurements (plan pages with material to take off)
- Exclude reference-only sheets: overalls, schedules, details, notes, cover sheets, legends
- But note those reference sheets are still needed for context
- Return estimated count of countable/measurable plan pages
- Rate complexity 1-5:
  - 1 = simple open field, turf, few trees
  - 2 = straightforward site with moderate variety
  - 3 = typical commercial project, reasonable density
  - 4 = complex multi-area project, many item types
  - 5 = dense enlargement pages, 50+ measurable items, heavy zoom

**IR - FLD prompt:**
Algorithmic first pass: count sheets with `LP` prefix (planting sheets).
Then send to Claude with scope + notes to:
- Confirm planting sheet identification
- Assess if any sheets appear to be at scales larger than 30' (from title/name clues)
- If so, estimate additional pages needed to achieve 30' scale legibility (baseline: 1:1 landscape-to-irrigation ratio)
- Rate complexity 1-5 (same scale, applied to irrigation context)

**Response format (enforced via prompt):**
```json
{
  "estimated_pages": 12,
  "complexity": 3,
  "reasoning": "24 LP sheets identified, 12 are plan pages with countable material..."
}
```

**Unknown product type fallback:** If `product` is empty, null, or an unrecognized value, use the MTO prompt as a generic default and add a note in the reasoning: "Unknown product type — used generic estimation."

**Error handling:** If JSON parsing fails, retry once. If still fails, return defaults with error flag.

**Env var:** `ANTHROPIC_API_KEY`

### 4. job_processor.py — Orchestrator

**Public API:**
```python
def process_job(folder_path: str) -> dict:
    """Run the full pipeline on a job folder. Returns processing result."""
```

**Flow:**
1. Find the job brief file in folder
2. Parse first line → extract `MONDAY ITEM ID: {id}`
3. Find all `.pdf` files in folder
4. Run `sheet_extractor.extract_sheets()` on each PDF. If multiple PDFs, merge results with sequential page numbering (offset by prior PDF's page count). Track source PDF filename per result.
5. Call `monday_client.fetch_item(item_id)` → `extract_columns()`
6. Call `claude_client.analyze_job()` with sheet index + scope + notes + product
7. Update the job brief:
   - Insert `Estimated Page Count: ~{N}` after the Monday ID line
   - Insert `Complexity: {X}/5` after the page count line
   - Append `## Sheet Index` section with table output at the bottom
8. Log result summary to stderr

**Brief parsing:**
- Line 1: `MONDAY ITEM ID: 11607590263` → extract via `r'MONDAY\s*ITEM\s*ID\s*:\s*(\d+)'` (case-insensitive, handles whitespace variations)
- Insert new lines after line 1 (before the project name)
- Append sheet index after the last `========` section or at EOF

**Error handling:** If Monday or Claude fails, still write the sheet index (the indexer result is the most valuable part). Add a note like `Estimated Page Count: ERROR — could not reach Monday/Claude` so the brief isn't silently incomplete.

## Data Flow

```
watch/2026-04-03/Acme Corp/Franklin Park/
  ├── plans.pdf
  └── Franklin Park - Job Brief
          ↓
  job_processor.process_job()
          ↓
  ┌─────────────────────────────────────────┐
  │ 1. Parse brief → item_id = 11607590263  │
  │ 2. extract_sheets("plans.pdf")          │
  │ 3. monday_client → scope, notes, color  │
  │ 4. claude_client → pages=12, complex=3  │
  │ 5. Update brief with estimates + index  │
  └─────────────────────────────────────────┘
          ↓
  Franklin Park - Job Brief (updated)
    MONDAY ITEM ID: 11607590263
    Estimated Page Count: ~12
    Complexity: 3/5
    ...existing content...
    ========
    ## Sheet Index
    Page  Sheet #     Confidence  Source              Page Title
    ---------------------------------------------------------------
    1     LC 1.02     ✅  100%    title_block_text    material
    2     LG 1.01     ✅  100%    title_block_text    ATTACHED GREEN GRADING PLAN
    ...
```

## Dependencies (full set for requirements.txt)

```
pymupdf>=1.27
pikepdf>=10.0
httpx>=0.27
anthropic>=0.40
```

`pymupdf` + `pikepdf` for sheet extractor. `httpx` for Monday.com GraphQL. `anthropic` SDK for Claude.

**Note:** `pymupdf` requires native PDF libraries. On Heroku, use the default Python buildpack (mupdf wheels are pre-built for Linux).

## Env Vars

```bash
MONDAY_API_TOKEN=       # Monday.com API token
ANTHROPIC_API_KEY=      # Claude API key
WATCH_DIR=./watch       # Local watch directory (default)
POLL_INTERVAL=30        # Seconds between polls (default)
```

## Heroku Deployment

```
Procfile: worker: python main.py
```

Single worker dyno. When Drive watcher replaces local watcher, same entry point — just swap the scanning logic.

## Design Notes

- **AI fallback in sheet extractor:** The orchestrator runs `extract_sheets()` with `ai_fallback=False` by default. The Claude analysis step works from the algorithmic results. This avoids double API calls per job.
- **Logging:** Use Python `logging` module consistently across all new modules (prefix: `[PROCESSOR]`, `[MONDAY]`, `[CLAUDE]`). Heroku captures stdout/stderr.
- **Column ID `color`:** Needs verification against the live board before first run. If `color` returns empty, log a warning and fall back to MTO generic prompt.
- **Training data:** Results are structured to support future storage in a training table (estimated vs actual page counts/complexity). Not implemented yet but the output dict from `process_job()` includes all fields needed for logging.

## Out of Scope (for now)

- Google Drive watcher (stub with local watcher)
- Team assignment routing
- US team visualization dashboard
- Writing results back to Monday.com columns
- Database storage / training pipeline (design outputs to support it later)
- Heroku deployment (blocked on Drive watcher; Procfile included as placeholder)
