"""
neon_client.py
--------------
Logs intake results to the Neon PostgreSQL database (worklog_iii_live).
Same DB as takeo-reviewer's review_results — linked via item_id.
"""

import os
import json
import logging
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS intake_results (
    id                SERIAL PRIMARY KEY,
    item_id           TEXT NOT NULL,
    item_name         TEXT,
    customer          TEXT,
    product           TEXT,
    scope             TEXT,
    total_sheets      INTEGER,
    estimated_pages   INTEGER,
    complexity        INTEGER,
    reasoning         TEXT,
    sheet_index_json  JSONB,
    pdfs_processed    INTEGER,
    had_error         BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
"""


def _get_conn():
    """Get a psycopg2 connection from DATABASE_URL env var."""
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("NEON_DATABASE_URL or DATABASE_URL not set")
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def ensure_table():
    """Create the intake_results table if it doesn't exist."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        logger.info("[NEON] intake_results table ensured")
    finally:
        conn.close()


def log_intake_result(
    item_id: str,
    item_name: str = "",
    customer: str = "",
    product: str = "",
    scope: str = "",
    total_sheets: int = 0,
    estimated_pages: int = 0,
    complexity: int = 0,
    reasoning: str = "",
    sheet_index_json: Optional[list] = None,
    pdfs_processed: int = 0,
    had_error: bool = False,
) -> Optional[int]:
    """
    Log an intake result to the database. Returns the inserted row ID.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(
                """
                INSERT INTO intake_results
                    (item_id, item_name, customer, product, scope,
                     total_sheets, estimated_pages, complexity, reasoning,
                     sheet_index_json, pdfs_processed, had_error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    item_id,
                    item_name,
                    customer,
                    product,
                    scope,
                    total_sheets,
                    estimated_pages,
                    complexity,
                    reasoning,
                    json.dumps(sheet_index_json) if sheet_index_json else None,
                    pdfs_processed,
                    had_error,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            row_id = row["id"] if row else None
            logger.info(f"[NEON] Logged intake result: id={row_id}, item_id={item_id}")
            return row_id
    except Exception as e:
        logger.error(f"[NEON] Failed to log intake result: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()
