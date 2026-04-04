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
    id                  SERIAL PRIMARY KEY,
    item_id             TEXT NOT NULL,
    item_name           TEXT,
    customer            TEXT,
    product             TEXT,
    scope               TEXT,
    total_sheets        INTEGER,
    estimated_pages     INTEGER,
    complexity          INTEGER,
    reasoning           TEXT,
    sheet_index_json    JSONB,
    pdfs_processed      INTEGER,
    had_error           BOOLEAN DEFAULT FALSE,
    actual_page_counts  INTEGER,
    ir_fl               INTEGER,
    ir_bd               INTEGER,
    std_mto             INTEGER,
    mt_mto              INTEGER,
    bt_mto              INTEGER,
    actuals_synced_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
"""

ADD_ACTUALS_COLUMNS_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='intake_results' AND column_name='actual_page_counts') THEN
        ALTER TABLE intake_results ADD COLUMN actual_page_counts INTEGER;
        ALTER TABLE intake_results ADD COLUMN ir_fl INTEGER;
        ALTER TABLE intake_results ADD COLUMN ir_bd INTEGER;
        ALTER TABLE intake_results ADD COLUMN std_mto INTEGER;
        ALTER TABLE intake_results ADD COLUMN mt_mto INTEGER;
        ALTER TABLE intake_results ADD COLUMN bt_mto INTEGER;
        ALTER TABLE intake_results ADD COLUMN actuals_synced_at TIMESTAMPTZ;
    END IF;
END $$;
"""


def _get_conn():
    """Get a psycopg2 connection from DATABASE_URL env var."""
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("NEON_DATABASE_URL or DATABASE_URL not set")
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def ensure_table():
    """Create the intake_results table if it doesn't exist, and add actuals columns if missing."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(ADD_ACTUALS_COLUMNS_SQL)
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


def _parse_int(val) -> Optional[int]:
    """Safely parse a text column to int. Returns None if empty/invalid."""
    if val is None:
        return None
    val = str(val).strip()
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def sync_actuals() -> dict:
    """
    Sync actual page counts from work_log into intake_results.
    Finds intake_results rows where actuals haven't been synced yet,
    looks up the item_id in work_log, and writes back the real numbers.

    Returns {"synced": N, "no_actuals": N, "not_found": N}
    """
    conn = _get_conn()
    stats = {"synced": 0, "no_actuals": 0, "not_found": 0}
    try:
        with conn.cursor() as cur:
            # Ensure columns exist
            cur.execute(ADD_ACTUALS_COLUMNS_SQL)

            # Find intake rows needing sync
            cur.execute("""
                SELECT id, item_id FROM intake_results
                WHERE actuals_synced_at IS NULL
            """)
            pending = cur.fetchall()

            if not pending:
                logger.info("[NEON] No intake results pending actuals sync")
                conn.commit()
                return stats

            logger.info(f"[NEON] Syncing actuals for {len(pending)} intake results")

            for row in pending:
                intake_id = row["id"]
                item_id = row["item_id"]

                # Look up in work_log
                cur.execute("""
                    SELECT actual_page_counts, ir_fl, ir_bd, std_mto, mt_mto, bt_mto
                    FROM work_log
                    WHERE item_id = %s
                    LIMIT 1
                """, (item_id,))
                wl = cur.fetchone()

                if not wl:
                    stats["not_found"] += 1
                    continue

                actual = _parse_int(wl["actual_page_counts"])

                if actual is None:
                    # Actuals not filled in yet — skip, we'll catch it next sync
                    stats["no_actuals"] += 1
                    continue

                # Write actuals back
                cur.execute("""
                    UPDATE intake_results
                    SET actual_page_counts = %s,
                        ir_fl = %s,
                        ir_bd = %s,
                        std_mto = %s,
                        mt_mto = %s,
                        bt_mto = %s,
                        actuals_synced_at = NOW()
                    WHERE id = %s
                """, (
                    actual,
                    _parse_int(wl["ir_fl"]),
                    _parse_int(wl["ir_bd"]),
                    _parse_int(wl["std_mto"]),
                    _parse_int(wl["mt_mto"]),
                    _parse_int(wl["bt_mto"]),
                    intake_id,
                ))
                stats["synced"] += 1

            conn.commit()
            logger.info(
                f"[NEON] Actuals sync complete: {stats['synced']} synced, "
                f"{stats['no_actuals']} awaiting actuals, {stats['not_found']} not in work_log"
            )
    except Exception as e:
        logger.error(f"[NEON] Actuals sync failed: {e}")
        conn.rollback()
    finally:
        conn.close()

    return stats
