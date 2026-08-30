"""SQLite persistence layer.

One file, WAL mode, a process-wide connection guarded by a lock. Order volume
for a single restaurant is tiny, so this stays comfortably fast while keeping
the deployment to a single container with one mounted volume.

No timestamps: a table's `time` on the display is always the item's static
`prep_minutes`, not a countdown. `tables.version` is the only thing that
changes on its own, and only when the *current* order changes - the e-paper
client uses it to decide whether a redraw is necessary.

Every table has at most one "current" order (what the kitchen is making) and
one "pending" order (staged, awaiting `/confirm`). Both are rows in `items`,
distinguished by `kind`, ordered by `position` - `position` is the stable id
a client uses in `/table/{table}/{item_id}`, so it never changes except when
the whole bucket is replaced via createOrder/createPending.
"""
from __future__ import annotations

import json
import sqlite3
import threading

from .config import DB_PATH, MAX_DEVICES

SCHEMA = """
CREATE TABLE IF NOT EXISTS tables (
    table_number  INTEGER PRIMARY KEY,
    version       INTEGER NOT NULL DEFAULT 0,
    order_id      INTEGER
);

-- A bare autoincrement sequence: every createOrder call takes the next id,
-- so order ids are unique and increasing across the whole restaurant, not
-- just within one table.
CREATE TABLE IF NOT EXISTS order_ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    table_number  INTEGER NOT NULL,
    kind          TEXT    NOT NULL,               -- 'current' | 'pending'
    position      INTEGER NOT NULL,               -- 0-based, stable within (table_number, kind)
    menu_id       TEXT,
    name          TEXT    NOT NULL,
    unit_price    REAL    NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 1,
    modifiers     TEXT    NOT NULL DEFAULT '[]',  -- JSON array of strings
    prep_minutes  INTEGER NOT NULL DEFAULT 0,
    delivered     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS devices (
    device_number INTEGER PRIMARY KEY,
    table_number  INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_slot
    ON items(table_number, kind, position);
"""

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
            _conn.commit()
            _seed_devices(_conn)
        return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table already existed on disk."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tables)")}
    if "order_id" not in columns:
        conn.execute("ALTER TABLE tables ADD COLUMN order_id INTEGER")


def _seed_devices(conn: sqlite3.Connection) -> None:
    """Default device N -> table N, so a fresh install answers /device/N immediately."""
    with _lock:
        for device_number in range(1, MAX_DEVICES + 1):
            conn.execute(
                "INSERT OR IGNORE INTO devices (device_number, table_number) VALUES (?, ?)",
                (device_number, device_number),
            )
        conn.commit()


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


class NotFound(LookupError):
    pass


# --------------------------------------------------------------------- tables
def list_tables() -> list[sqlite3.Row]:
    """Every table that's ever had an order - not just ones with a device pointed at them."""
    conn = connect()
    with _lock:
        return conn.execute("SELECT * FROM tables ORDER BY table_number").fetchall()


def ensure_table(table_number: int) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO tables (table_number, version) VALUES (?, 0)",
            (table_number,),
        )
        conn.commit()


def get_version(table_number: int) -> int:
    conn = connect()
    with _lock:
        row = conn.execute(
            "SELECT version FROM tables WHERE table_number = ?", (table_number,)
        ).fetchone()
    return row["version"] if row else 0


def bump_version(table_number: int) -> int:
    ensure_table(table_number)
    conn = connect()
    with _lock:
        conn.execute(
            "UPDATE tables SET version = version + 1 WHERE table_number = ?",
            (table_number,),
        )
        conn.commit()
    return get_version(table_number)


def get_order_id(table_number: int) -> int | None:
    conn = connect()
    with _lock:
        row = conn.execute(
            "SELECT order_id FROM tables WHERE table_number = ?", (table_number,)
        ).fetchone()
    return row["order_id"] if row else None


def new_order_id(table_number: int) -> int:
    """Mint a fresh, globally-increasing order id and attach it to this table.

    Called once per createOrder - confirming pending items or toggling
    delivered keeps the same order id, since they change an existing order
    rather than starting a new one.
    """
    ensure_table(table_number)
    conn = connect()
    with _lock:
        cur = conn.execute("INSERT INTO order_ids DEFAULT VALUES")
        order_id = cur.lastrowid
        conn.execute(
            "UPDATE tables SET order_id = ? WHERE table_number = ?", (order_id, table_number)
        )
        conn.commit()
    return order_id


# ---------------------------------------------------------------------- items
def list_items(table_number: int, kind: str) -> list[sqlite3.Row]:
    conn = connect()
    with _lock:
        return conn.execute(
            "SELECT * FROM items WHERE table_number = ? AND kind = ? ORDER BY position ASC",
            (table_number, kind),
        ).fetchall()


def get_item(table_number: int, kind: str, position: int) -> sqlite3.Row | None:
    conn = connect()
    with _lock:
        return conn.execute(
            "SELECT * FROM items WHERE table_number = ? AND kind = ? AND position = ?",
            (table_number, kind, position),
        ).fetchone()


def replace_items(table_number: int, kind: str, items: list[dict]) -> None:
    """Wipe one bucket ('current' or 'pending') and write these in its place.

    `items` entries: menu_id, name, unit_price, quantity, modifiers (list),
    prep_minutes. Position is assigned from list order. For the 'current'
    bucket this is createOrder - it bumps the version and mints a fresh
    order id, since replacing it wholesale starts a new order.
    """
    ensure_table(table_number)
    conn = connect()
    with _lock:
        conn.execute(
            "DELETE FROM items WHERE table_number = ? AND kind = ?", (table_number, kind)
        )
        for position, item in enumerate(items):
            conn.execute(
                "INSERT INTO items (table_number, kind, position, menu_id, name,"
                " unit_price, quantity, modifiers, prep_minutes, delivered)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    table_number,
                    kind,
                    position,
                    item.get("menu_id"),
                    item["name"],
                    item["unit_price"],
                    item.get("quantity", 1),
                    json.dumps(item.get("modifiers") or []),
                    item.get("prep_minutes", 0),
                ),
            )
        conn.commit()
    if kind == "current":
        new_order_id(table_number)
        bump_version(table_number)


def clear_current(table_number: int) -> None:
    """Wipe the current order and reset order_id to 0.

    Distinct from a table that's never had an order (order_id stays NULL
    until the first createOrder) - 0 means "explicitly cleared".
    """
    ensure_table(table_number)
    conn = connect()
    with _lock:
        conn.execute(
            "DELETE FROM items WHERE table_number = ? AND kind = 'current'", (table_number,)
        )
        conn.execute("UPDATE tables SET order_id = 0 WHERE table_number = ?", (table_number,))
        conn.commit()
    bump_version(table_number)


def merge_pending_into_current(table_number: int) -> None:
    """Append the pending bucket onto the end of current, then clear pending."""
    conn = connect()
    with _lock:
        pending = conn.execute(
            "SELECT * FROM items WHERE table_number = ? AND kind = 'pending' ORDER BY position ASC",
            (table_number,),
        ).fetchall()
        if not pending:
            return
        current_count = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE table_number = ? AND kind = 'current'",
            (table_number,),
        ).fetchone()["n"]
        for offset, row in enumerate(pending):
            conn.execute(
                "UPDATE items SET kind = 'current', position = ? WHERE id = ?",
                (current_count + offset, row["id"]),
            )
        conn.commit()
    bump_version(table_number)


def set_delivered(table_number: int, position: int, delivered: bool) -> sqlite3.Row:
    conn = connect()
    with _lock:
        cur = conn.execute(
            "UPDATE items SET delivered = ? WHERE table_number = ? AND kind = 'current' AND position = ?",
            (1 if delivered else 0, table_number, position),
        )
        if cur.rowcount == 0:
            raise NotFound(f"table {table_number} has no item at position {position}")
        conn.commit()
    bump_version(table_number)
    return get_item(table_number, "current", position)


# ------------------------------------------------------------------- devices
def list_devices() -> list[sqlite3.Row]:
    conn = connect()
    with _lock:
        return conn.execute("SELECT * FROM devices ORDER BY device_number").fetchall()


def get_device(device_number: int) -> sqlite3.Row | None:
    conn = connect()
    with _lock:
        return conn.execute(
            "SELECT * FROM devices WHERE device_number = ?", (device_number,)
        ).fetchone()


def assign_device(device_number: int, table_number: int) -> sqlite3.Row:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO devices (device_number, table_number) VALUES (?, ?)"
            " ON CONFLICT(device_number) DO UPDATE SET table_number = excluded.table_number",
            (device_number, table_number),
        )
        conn.commit()
    return get_device(device_number)
