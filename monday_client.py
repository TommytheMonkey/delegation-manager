"""
monday_client.py
----------------
Monday.com GraphQL client for fetching and updating item columns.
Follows patterns from takeo-reviewer/reviewer/monday_client.py.
"""

import os
import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MONDAY_API_URL = "https://api.monday.com/v2"

# Column IDs on board 3874058084
COL_SCOPE = "dropdown35"
COL_NOTES = "long_text_mkqwc9v8"
COL_PRODUCT = "color"
COL_DUE_DATE = "date"
COL_EST_PAGES = "numeric_mkqgngp7"

BOARD_ID = 3874058084

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

CHANGE_COLUMN_VALUES_MUTATION = """
mutation ($boardId: ID!, $itemId: ID!, $columnValues: JSON!) {
  change_multiple_column_values(
    board_id: $boardId
    item_id: $itemId
    column_values: $columnValues
  ) {
    id
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
        Returns {"scope": str, "notes": str, "product": str, "item_name": str, "due_date": str}
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
            "due_date": columns.get(COL_DUE_DATE, ""),
        }

    def update_columns(self, item_id: str, column_values: dict) -> None:
        """
        Update column values on a Monday item.

        Args:
            item_id: The Monday item ID
            column_values: Dict of {column_id: value} — values should be
                           Monday-formatted (e.g. {"date": "2025-06-15"} for date columns)
        """
        logger.info(f"[MONDAY] Updating item {item_id}: {list(column_values.keys())}")
        self._query(
            CHANGE_COLUMN_VALUES_MUTATION,
            {
                "boardId": str(BOARD_ID),
                "itemId": str(item_id),
                "columnValues": json.dumps(column_values),
            },
        )
        logger.info(f"[MONDAY] Updated item {item_id} successfully")
