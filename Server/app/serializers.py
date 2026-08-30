"""Turn `items` rows into the flat, pre-formatted JSON the firmware draws.

There's one shape: itemName / details / price / time / delivered. `price` is
the line total (unit_price * quantity); `time` is the item's static
prep_minutes - there are no timestamps to count down from.
"""
from __future__ import annotations

import json
import sqlite3

from .config import DISPLAY_DETAIL_CHARS, DISPLAY_NAME_CHARS


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: max(limit - 1, 1)].rstrip() + "…"


def display_row(row: sqlite3.Row) -> dict:
    qty = row["quantity"]
    name = f"{qty}x {row['name']}" if qty > 1 else row["name"]
    modifiers = json.loads(row["modifiers"] or "[]")
    return {
        "itemName": truncate(name, DISPLAY_NAME_CHARS),
        "details": truncate(", ".join(modifiers), DISPLAY_DETAIL_CHARS),
        "price": round(row["unit_price"] * qty, 2),
        "time": float(row["prep_minutes"]),
        "delivered": bool(row["delivered"]),
    }


def item_detail(row: sqlite3.Row) -> dict:
    detail = display_row(row)
    detail["menu_id"] = row["menu_id"]
    detail["quantity"] = row["quantity"]
    detail["modifiers"] = json.loads(row["modifiers"] or "[]")
    return detail
