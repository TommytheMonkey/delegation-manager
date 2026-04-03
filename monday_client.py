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
