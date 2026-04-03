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
