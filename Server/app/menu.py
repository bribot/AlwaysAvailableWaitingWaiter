"""CSV-backed menu catalog.

The menu lives in a plain CSV so staff can edit it in Excel / Google Sheets and
drop it back into the mounted `data/` volume. `POST /menu/reload` re-reads
the file without restarting the container.

Columns: id,name,category,price,prep_minutes,available
"""
from __future__ import annotations

import csv
import difflib
import threading
from dataclasses import dataclass, asdict
from pathlib import Path

from .config import MENU_CSV

REQUIRED_COLUMNS = {"id", "name", "price"}


@dataclass(frozen=True)
class MenuItem:
    id: str
    name: str
    category: str
    price: float
    prep_minutes: int
    available: bool

    def dict(self) -> dict:
        return asdict(self)


class MenuError(ValueError):
    """Raised when the CSV is missing or malformed."""


class Menu:
    """Thread-safe in-memory view of the menu CSV."""

    def __init__(self, path: Path = MENU_CSV):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items: dict[str, MenuItem] = {}
        self._mtime: float | None = None

    # ---------------------------------------------------------------- loading
    def load(self) -> int:
        """(Re)read the CSV. Returns the number of items loaded."""
        if not self.path.exists():
            raise MenuError(f"menu CSV not found at {self.path}")

        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
            missing = REQUIRED_COLUMNS - headers
            if missing:
                raise MenuError(
                    f"menu CSV is missing required column(s): {', '.join(sorted(missing))}"
                )

            items: dict[str, MenuItem] = {}
            for line_no, raw in enumerate(reader, start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
                if not row.get("id") or not row.get("name"):
                    continue  # tolerate blank padding rows
                item_id = row["id"].upper()
                if item_id in items:
                    raise MenuError(f"duplicate menu id '{item_id}' on line {line_no}")
                try:
                    price = round(float(row["price"]), 2)
                except ValueError as exc:
                    raise MenuError(f"bad price on line {line_no}: {row['price']!r}") from exc
                try:
                    prep = int(float(row.get("prep_minutes") or 0))
                except ValueError as exc:
                    raise MenuError(
                        f"bad prep_minutes on line {line_no}: {row.get('prep_minutes')!r}"
                    ) from exc
                items[item_id] = MenuItem(
                    id=item_id,
                    name=row["name"],
                    category=row.get("category") or "other",
                    price=price,
                    prep_minutes=max(prep, 0),
                    available=_as_bool(row.get("available", "1")),
                )

        if not items:
            raise MenuError("menu CSV contains no usable rows")

        with self._lock:
            self._items = items
            self._mtime = self.path.stat().st_mtime
        return len(items)

    def load_if_changed(self) -> bool:
        """Hot-reload when the file on disk was edited. Returns True if reloaded."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if self._mtime is not None and mtime <= self._mtime:
            return False
        self.load()
        return True

    # ---------------------------------------------------------------- reading
    def all(self, include_unavailable: bool = False) -> list[MenuItem]:
        with self._lock:
            items = list(self._items.values())
        if not include_unavailable:
            items = [i for i in items if i.available]
        return sorted(items, key=lambda i: (i.category, i.name))

    def get(self, item_id: str) -> MenuItem | None:
        with self._lock:
            return self._items.get((item_id or "").strip().upper())

    def match_by_name(self, name: str) -> MenuItem | None:
        """Exact match, then substring, then fuzzy - for a hand-typed or spoken dish name."""
        candidates = self.all()
        lowered = name.strip().lower()
        for item in candidates:
            if item.name.lower() == lowered:
                return item
        for item in candidates:
            if lowered in item.name.lower():
                return item
        close = difflib.get_close_matches(
            lowered, [i.name.lower() for i in candidates], n=1, cutoff=0.7
        )
        if close:
            return next(i for i in candidates if i.name.lower() == close[0])
        return None

    def categories(self) -> list[str]:
        return sorted({i.category for i in self.all()})

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() not in {"0", "false", "no", "n", "off", ""}


menu = Menu()
