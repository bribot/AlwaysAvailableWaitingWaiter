"""End-to-end tests against a throwaway SQLite file and a temp menu CSV."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="restaurant-test-"))
shutil.copy(ROOT / "data" / "menu.csv", _TMP / "menu.csv")
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DB_PATH"] = str(_TMP / "test.db")
os.environ["MENU_CSV"] = str(_TMP / "menu.csv")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
        # wipe state between tests
        from app import db

        conn = db.connect()
        for table in ("items", "tables", "devices"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        db._seed_devices(conn)


def test_health_and_menu(client):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["menu_items"] > 0

    items = client.get("/menu").json()
    assert any(i["id"] == "BUR01" for i in items)
    assert all(i["available"] for i in items)

    cats = client.get("/menu/categories").json()["categories"]
    assert "main" in cats and "dessert" in cats

    assert client.get("/menu/BUR01").json()["name"] == "Classic Burger"
    assert client.get("/menu/NOPE99").status_code == 404


# --------------------------------------------------------------------- device
def test_device_defaults_to_matching_table(client):
    d = client.get("/device/1").json()
    assert d == {"device_number": 1, "table_number": 1, "order_id": None, "order": []}


def test_device_out_of_range_is_rejected(client):
    assert client.get("/device/0").status_code == 422
    assert client.get("/device/4").status_code == 422  # only 3 devices for now


def test_list_devices(client):
    devices = client.get("/devices").json()
    assert [d["device_number"] for d in devices] == [1, 2, 3]
    assert [d["table_number"] for d in devices] == [1, 2, 3]  # default 1:1 mapping


def test_device_reassignment(client):
    client.post("/table/9/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    r = client.put("/device/2", json={"table_number": 9})
    assert r.status_code == 200
    d = client.get("/device/2").json()
    assert d["table_number"] == 9
    assert d["order"][0]["itemName"] == "Classic Burger"


def test_order_id_is_new_per_create_order_only(client):
    client.put("/device/2", json={"table_number": 10})
    assert client.get("/device/2").json()["order_id"] is None  # no order yet

    client.post("/table/10/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    first_id = client.get("/device/2").json()["order_id"]
    assert first_id is not None

    # confirming pending items and toggling delivered don't start a new order
    client.post("/table/10/createPending", json={"items": [{"menu_id": "DRK01"}]})
    client.post("/table/10/confirm")
    client.post("/table/10/0/delivered")
    assert client.get("/device/2").json()["order_id"] == first_id

    # a fresh createOrder replaces it with a new id
    client.post("/table/10/createOrder", json={"items": [{"menu_id": "DRK01"}]})
    second_id = client.get("/device/2").json()["order_id"]
    assert second_id != first_id


def test_clear_table(client):
    client.put("/device/2", json={"table_number": 11})
    client.post("/table/11/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    assert client.get("/device/2").json()["order_id"] is not None

    r = client.post("/table/11/clearTable")
    assert r.status_code == 200
    assert r.json() == []
    assert client.get("/table/11").json() == []
    assert client.get("/device/2").json()["order_id"] == 0


def test_clear_table_leaves_pending_alone(client):
    client.post("/table/12/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    client.post("/table/12/createPending", json={"items": [{"menu_id": "DRK01"}]})

    client.post("/table/12/clearTable")
    assert client.get("/table/12").json() == []
    assert len(client.get("/table/12/pending").json()) == 1


# ----------------------------------------------------------------------- table
def test_list_tables(client):
    client.post("/table/40/createOrder", json={"items": [{"menu_id": "BUR01"}, {"menu_id": "DRK01"}]})
    client.post("/table/40/createPending", json={"items": [{"menu_id": "DES01"}]})

    tables = client.get("/tables").json()
    row = next(t for t in tables if t["table_number"] == 40)
    assert row["item_count"] == 2
    assert row["pending_count"] == 1
    assert row["order_id"] is not None
    assert row["version"] >= 1


def test_create_order_and_read_table(client):
    r = client.post(
        "/table/5/createOrder",
        json={"items": [{"menu_id": "STK01", "quantity": 1, "modifiers": ["-Onions", "+Medium rare"]}]},
    )
    assert r.status_code == 200
    row = r.json()[0]
    assert row["itemName"] == "Ribeye Steak"
    assert row["price"] == pytest.approx(28.00)
    assert row["details"] == "-Onions, +Medium rare"
    assert row["delivered"] is False
    assert row["time"] == pytest.approx(25.0)  # menu prep_minutes, no countdown

    d = client.get("/table/5").json()
    assert d == r.json()


def test_create_order_replaces_the_old_one(client):
    client.post("/table/6/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    r = client.post("/table/6/createOrder", json={"items": [{"menu_id": "DRK01"}]})
    assert len(r.json()) == 1
    assert r.json()[0]["itemName"] == "Soda"


def test_create_order_by_fuzzy_name(client):
    r = client.post("/table/3/createOrder", json={"items": [{"name": "classic burger"}]})
    assert r.status_code == 200
    assert r.json()[0]["itemName"] == "Classic Burger"

    assert client.post("/table/3/createOrder", json={"items": [{"name": "unicorn stew"}]}).status_code == 404
    assert client.post("/table/3/createOrder", json={"items": [{}]}).status_code == 422


def test_unavailable_item_rejected(client):
    csv_path = Path(os.environ["MENU_CSV"])
    original = csv_path.read_text(encoding="utf-8")
    try:
        csv_path.write_text(original + "OFF01,Sold Out Dish,main,10.00,5,0\n", encoding="utf-8")
        client.post("/menu/reload")
        r = client.post("/table/41/createOrder", json={"items": [{"menu_id": "OFF01"}]})
        assert r.status_code == 409
    finally:
        csv_path.write_text(original, encoding="utf-8")
        client.post("/menu/reload")


def test_empty_table_returns_empty_list(client):
    assert client.get("/table/88").json() == []


def test_bad_table_number(client):
    assert client.get("/table/0").status_code == 422
    assert client.post("/table/0/createOrder", json={"items": []}).status_code == 422


def test_etag_returns_304(client):
    client.post("/table/6/createOrder", json={"items": [{"menu_id": "PIZ02"}]})
    r1 = client.get("/table/6")
    etag = r1.headers["etag"]
    r2 = client.get("/table/6", headers={"If-None-Match": etag})
    assert r2.status_code == 304

    client.post("/table/6/createOrder", json={"items": [{"menu_id": "DRK01"}]})
    r3 = client.get("/table/6", headers={"If-None-Match": etag})
    assert r3.status_code == 200
    assert r3.headers["etag"] != etag


def test_rows_query_param_clips(client):
    ids = ["BRU01", "BRU02", "SOU01", "SAL01", "SAL02", "BUR01", "PAS01", "PIZ01"]
    client.post("/table/4/createOrder", json={"items": [{"menu_id": i} for i in ids]})
    assert len(client.get("/table/4").json()) == 6  # DISPLAY_ROWS
    assert len(client.get("/table/4?rows=3").json()) == 3


# ------------------------------------------------------------------- pending
def test_pending_is_separate_from_current(client):
    client.post("/table/12/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    client.post("/table/12/createPending", json={"items": [{"menu_id": "DRK01"}, {"menu_id": "DES01"}]})

    assert len(client.get("/table/12").json()) == 1
    pending = client.get("/table/12/pending").json()
    assert len(pending) == 2
    assert {p["itemName"] for p in pending} == {"Soda", "Cheesecake"}


def test_check_pending(client):
    assert client.get("/table/19/checkPending").json() is False
    client.post("/table/19/createPending", json={"items": [{"menu_id": "DRK01"}]})
    assert client.get("/table/19/checkPending").json() is True
    client.post("/table/19/confirm")
    assert client.get("/table/19/checkPending").json() is False


def test_create_pending_replaces_old_pending(client):
    client.post("/table/13/createPending", json={"items": [{"menu_id": "BUR01"}]})
    client.post("/table/13/createPending", json={"items": [{"menu_id": "DRK01"}]})
    pending = client.get("/table/13/pending").json()
    assert len(pending) == 1
    assert pending[0]["itemName"] == "Soda"


def test_confirm_merges_pending_into_current(client):
    client.post("/table/14/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    client.post("/table/14/createPending", json={"items": [{"menu_id": "DRK01"}]})

    r = client.post("/table/14/confirm")
    assert r.status_code == 200
    names = [i["itemName"] for i in r.json()]
    assert names == ["Classic Burger", "Soda"]
    assert client.get("/table/14/pending").json() == []


def test_confirm_with_no_pending_is_a_noop(client):
    client.post("/table/15/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    r = client.post("/table/15/confirm")
    assert len(r.json()) == 1


def test_create_order_skips_pending_and_clears_it(client):
    client.post("/table/16/createPending", json={"items": [{"menu_id": "DRK01"}]})
    client.post("/table/16/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    assert client.get("/table/16/pending").json() == []
    assert client.get("/table/16").json()[0]["itemName"] == "Classic Burger"


# ---------------------------------------------------------------------- items
def test_read_single_item(client):
    client.post(
        "/table/17/createOrder",
        json={"items": [{"menu_id": "BUR01"}, {"menu_id": "DRK01", "quantity": 2}]},
    )
    item = client.get("/table/17/1").json()
    assert item["itemName"] == "2x Soda"
    assert item["menu_id"] == "DRK01"
    assert item["quantity"] == 2
    assert item["delivered"] is False

    assert client.get("/table/17/99").status_code == 404


def test_toggle_delivered(client):
    client.post("/table/18/createOrder", json={"items": [{"menu_id": "BUR01"}]})
    r = client.post("/table/18/0/delivered")
    assert r.status_code == 200
    assert r.json()["delivered"] is True
    assert client.get("/table/18").json()[0]["delivered"] is True

    r2 = client.post("/table/18/0/delivered")
    assert r2.json()["delivered"] is False

    assert client.post("/table/18/99/delivered").status_code == 404


# ------------------------------------------------------------------ websocket
def test_websocket_snapshot_and_push(client):
    with client.websocket_connect("/ws/tables/21") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        assert snap["display"] == []

        client.post("/table/21/createOrder", json={"items": [{"menu_id": "BUR01"}]})
        msg = ws.receive_json()
        assert msg["type"] == "order.updated"
        assert msg["display"][0]["itemName"] == "Classic Burger"


def test_websocket_isolated_per_table(client):
    with client.websocket_connect("/ws/tables/30") as ws30:
        ws30.receive_json()  # snapshot
        client.post("/table/31/createOrder", json={"items": [{"menu_id": "DRK01"}]})
        client.post("/table/30/createOrder", json={"items": [{"menu_id": "DES03"}]})
        msg = ws30.receive_json()
        assert msg["table"] == 30
        assert msg["display"][0]["itemName"] == "Ice Cream"


def test_menu_reload_picks_up_csv_edits(client):
    csv_path = Path(os.environ["MENU_CSV"])
    original = csv_path.read_text(encoding="utf-8")
    try:
        csv_path.write_text(original + "TST99,Test Dish,main,42.00,5,1\n", encoding="utf-8")
        assert client.post("/menu/reload").json()["loaded"] > 0
        assert client.get("/menu/TST99").json()["price"] == 42.0

        r = client.post("/table/40/createOrder", json={"items": [{"menu_id": "TST99"}]})
        assert r.json()[0]["price"] == pytest.approx(42.00)
    finally:
        csv_path.write_text(original, encoding="utf-8")
        client.post("/menu/reload")


def test_bad_menu_csv_is_rejected(client, tmp_path):
    from app.menu import Menu, MenuError

    bad = tmp_path / "bad.csv"
    bad.write_text("id,name\nX1,Thing\n", encoding="utf-8")
    with pytest.raises(MenuError):
        Menu(bad).load()

    dupe = tmp_path / "dupe.csv"
    dupe.write_text("id,name,price\nX1,A,1\nX1,B,2\n", encoding="utf-8")
    with pytest.raises(MenuError):
        Menu(dupe).load()
