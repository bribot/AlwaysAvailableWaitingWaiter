"""Restaurant order API.

Three device classes talk to this service:

* the e-paper ESP32 - either polls `GET /table/{table}` directly, or looks
  itself up via `GET /device/{device}` (which resolves device -> table for it)
  - and subscribes to `WS /ws/tables/{table}` for a push on change.
* the mic board - holds `WS /table/{table}/voice` open and streams audio only
  while its button is held; the server transcribes it and stages the parsed
  items in that table's pending order.
* anything that adds food (tablet, POS, phone) - stages an order with
  `POST /table/{table}/createPending`, or writes the kitchen's order directly
  with `POST /table/{table}/createOrder`.

Every table has at most one *current* order (what the kitchen is making) and
one *pending* order (staged, awaiting `POST /table/{table}/confirm`). Mutating
the current order bumps its version and pushes the freshly formatted display
array to that table's websocket subscribers.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db, serializers, voice
from .config import API_KEY, DISPLAY_ROWS, MAX_DEVICES, SAMPLE_RATE
from .events import broker, table_topic
from .menu import MenuError, menu
from .schemas import (
    AssignDeviceRequest,
    CreateOrderRequest,
    DeviceOut,
    DisplayItemOut,
    ItemDetailOut,
    MenuItemOut,
    OrderItemIn,
    TableSummaryOut,
)

log = logging.getLogger("restaurant")

MAX_TABLE = 999
PING_SECONDS = 25
MIN_UTTERANCE_SECONDS = 0.4
MAX_UTTERANCE_SECONDS = 30.0


# ----------------------------------------------------------------------- auth
def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def check_table(table: int) -> int:
    if table < 1 or table > MAX_TABLE:
        raise HTTPException(status_code=422, detail=f"table must be between 1 and {MAX_TABLE}")
    return table


def check_device(device: int) -> int:
    if device < 1 or device > MAX_DEVICES:
        raise HTTPException(
            status_code=422, detail=f"device must be between 1 and {MAX_DEVICES}"
        )
    return device


# ------------------------------------------------------------------ lifecycle
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    broker.bind_loop(asyncio.get_running_loop())
    try:
        count = menu.load()
        log.info("loaded %s menu items from %s", count, menu.path)
    except MenuError as exc:
        log.error("menu failed to load: %s", exc)
    yield
    db.close()


app = FastAPI(
    title="Restaurant Order API",
    version="2.0.0",
    description="Order backend for the e-paper table display and companion devices.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# The admin dashboard - a static page, no auth of its own (the API calls it
# makes still go through require_api_key like anything else).
app.mount("/admin", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="admin")

api = APIRouter(dependencies=[Depends(require_api_key)])


# ------------------------------------------------------------------ broadcast
def broadcast(table: int) -> list[dict]:
    """Publish the current display array for a table's websocket subscribers."""
    order = _current_display(table)
    broker.publish(
        table_topic(table),
        {"type": "order.updated", "table": table, "version": db.get_version(table), "display": order},
    )
    return order


def _current_display(table: int) -> list[dict]:
    return [serializers.display_row(r) for r in db.list_items(table, "current")]


# ----------------------------------------------------------------------- menu
@api.get("/menu", response_model=list[MenuItemOut], tags=["menu"])
def get_menu(
    category: str | None = None,
    include_unavailable: bool = False,
) -> list[dict]:
    menu.load_if_changed()  # pick up edits to the mounted CSV automatically
    items = menu.all(include_unavailable=include_unavailable)
    if category:
        items = [i for i in items if i.category.lower() == category.lower()]
    return [i.dict() for i in items]


@api.get("/menu/categories", tags=["menu"])
def get_categories() -> dict:
    menu.load_if_changed()
    return {"categories": menu.categories()}


@api.post("/menu/reload", tags=["menu"])
def reload_menu() -> dict:
    try:
        count = menu.load()
    except MenuError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"loaded": count, "source": str(menu.path)}


@api.get("/menu/{menu_id}", response_model=MenuItemOut, tags=["menu"])
def get_menu_item(menu_id: str) -> dict:
    item = menu.get(menu_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no menu item '{menu_id}'")
    return item.dict()


# -------------------------------------------------------------------- devices
def _device_payload(device_number: int, table: int) -> dict:
    return {
        "device_number": device_number,
        "table_number": table,
        "order_id": db.get_order_id(table),
        "order": _current_display(table),
    }


@api.get("/devices", response_model=list[DeviceOut], tags=["device"])
def list_devices() -> list[dict]:
    return [_device_payload(row["device_number"], row["table_number"]) for row in db.list_devices()]


@api.get("/device/{device_number}", response_model=DeviceOut, tags=["device"])
def read_device(device_number: int) -> dict:
    check_device(device_number)
    row = db.get_device(device_number)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"device {device_number} is not assigned to a table"
        )
    return _device_payload(device_number, row["table_number"])


@api.put("/device/{device_number}", response_model=DeviceOut, tags=["device"])
def assign_device(device_number: int, body: AssignDeviceRequest) -> dict:
    check_device(device_number)
    check_table(body.table_number)
    db.assign_device(device_number, body.table_number)
    return _device_payload(device_number, body.table_number)


# ---------------------------------------------------------------------- table
@api.get("/tables", response_model=list[TableSummaryOut], tags=["table"])
def list_tables() -> list[dict]:
    """Every table that's ever had an order - counts only, for a dashboard list."""
    return [
        {
            "table_number": row["table_number"],
            "version": row["version"],
            "order_id": row["order_id"],
            "item_count": len(db.list_items(row["table_number"], "current")),
            "pending_count": len(db.list_items(row["table_number"], "pending")),
        }
        for row in db.list_tables()
    ]


@api.get("/table/{table_id}", tags=["table"])
def read_table(
    table_id: int,
    response: Response,
    if_none_match: str | None = Header(default=None),
    rows: int = Query(default=DISPLAY_ROWS, ge=1, le=20),
) -> Any:
    """Panel-ready payload: a flat list of item rows. Returns 304 when unchanged."""
    check_table(table_id)
    version = db.get_version(table_id)
    etag = f'W/"{table_id}-{version}"'
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return _current_display(table_id)[:rows]


@api.get("/table/{table_id}/pending", response_model=list[DisplayItemOut], tags=["table"])
def read_pending(table_id: int) -> list[dict]:
    check_table(table_id)
    return [serializers.display_row(r) for r in db.list_items(table_id, "pending")]


@api.get("/table/{table_id}/checkPending", response_model=bool, tags=["table"])
def check_pending(table_id: int) -> bool:
    check_table(table_id)
    return bool(db.list_items(table_id, "pending"))


@api.post("/table/{table_id}/confirm", response_model=list[DisplayItemOut], tags=["table"])
def confirm_pending(table_id: int) -> list[dict]:
    """Merge the staged (pending) order onto the end of the current one."""
    check_table(table_id)
    db.merge_pending_into_current(table_id)
    return broadcast(table_id)


@api.post("/table/{table_id}/createOrder", response_model=list[DisplayItemOut], tags=["table"])
def create_order(table_id: int, body: CreateOrderRequest) -> list[dict]:
    """Replace the table's current order wholesale. Skips pending confirmation."""
    check_table(table_id)
    resolved = _resolve_items(body.items)
    db.replace_items(table_id, "current", resolved)
    db.replace_items(table_id, "pending", [])
    return broadcast(table_id)


@api.post("/table/{table_id}/createPending", response_model=list[DisplayItemOut], tags=["table"])
def create_pending(table_id: int, body: CreateOrderRequest) -> list[dict]:
    """Stage a replacement order for the table, awaiting `/confirm`."""
    check_table(table_id)
    resolved = _resolve_items(body.items)
    db.replace_items(table_id, "pending", resolved)
    return [serializers.display_row(r) for r in db.list_items(table_id, "pending")]


@api.post("/table/{table_id}/clearTable", response_model=list[DisplayItemOut], tags=["table"])
def clear_table(table_id: int) -> list[dict]:
    """Erase the current order and reset order_id to 0. Pending is untouched."""
    check_table(table_id)
    db.clear_current(table_id)
    return broadcast(table_id)


@api.post("/table/{table_id}/voice", tags=["table"])
def upload_voice(table_id: int, file: UploadFile = File(...), device: str = Query(default="mic")) -> dict:
    """Test the voice pipeline without a mic: upload a WAV (or raw PCM) clip.

    Skips the min/max-duration guardrails the websocket protocol enforces -
    this is a dev/test convenience, not a hardened public endpoint.
    """
    check_table(table_id)
    raw = file.file.read()
    wav_bytes = raw if voice.is_wav(raw) else voice.pcm_to_wav(raw, SAMPLE_RATE)
    try:
        return voice.process_utterance(table_id, wav_bytes, device)
    except voice.VoiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@api.get("/table/{table_id}/{item_id}", response_model=ItemDetailOut, tags=["table"])
def read_item(table_id: int, item_id: int) -> dict:
    check_table(table_id)
    row = db.get_item(table_id, "current", item_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"table {table_id} has no item at position {item_id}"
        )
    return serializers.item_detail(row)


@api.post("/table/{table_id}/{item_id}/delivered", response_model=ItemDetailOut, tags=["table"])
def toggle_delivered(table_id: int, item_id: int) -> dict:
    check_table(table_id)
    row = db.get_item(table_id, "current", item_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"table {table_id} has no item at position {item_id}"
        )
    updated = db.set_delivered(table_id, item_id, not bool(row["delivered"]))
    broadcast(table_id)
    return serializers.item_detail(updated)


# ------------------------------------------------------------------ websocket
@app.websocket("/ws/tables/{table}")
async def table_socket(websocket: WebSocket, table: int, api_key: str | None = Query(default=None)):
    """Live feed for one table.

    Sends the current display immediately on connect (so a rebooting ESP32 gets
    a full picture without a second HTTP call), then one message per change.
    Keepalive pings every PING_SECONDS keep NAT/router state alive.
    """
    if API_KEY and api_key != API_KEY:
        await websocket.close(code=4401, reason="invalid api key")
        return
    if table < 1 or table > MAX_TABLE:
        await websocket.close(code=4400, reason="bad table number")
        return

    await websocket.accept()
    topic = table_topic(table)
    queue = broker.subscribe(topic)
    try:
        await websocket.send_json(
            {
                "type": "snapshot",
                "table": table,
                "version": db.get_version(table),
                "display": _current_display(table),
            }
        )

        receiver = asyncio.create_task(_drain(websocket))
        try:
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "ping"})
                    continue
                if receiver.done():
                    break
                await websocket.send_json(message)
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - never let one socket kill the server
        log.warning("table %s socket closed: %s", table, exc)
    finally:
        broker.unsubscribe(topic, queue)


async def _drain(websocket: WebSocket) -> None:
    """Consume inbound frames so client pongs/pings don't fill the buffer."""
    try:
        while True:
            await websocket.receive_text()
    except Exception:  # noqa: BLE001 - disconnect is the expected exit
        return


@app.websocket("/table/{table_id}/voice")
async def voice_socket(
    websocket: WebSocket,
    table_id: int,
    device: str = Query(default="mic"),
    api_key: str | None = Query(default=None),
):
    """Push-to-talk audio ingest for the mic board.

    Protocol, from the ESP32's side:
      -> {"type":"start"}            button pressed
      -> <binary frames>              raw 16-bit mono PCM @ SAMPLE_RATE, little-endian
      -> {"type":"stop"}              button released; server transcribes and stages
      -> {"type":"cancel"}            throw the buffer away, no Gemini call

    The server answers with `ready`, `listening`, `processing`, then either
    `result` (parsed items, staged into `pending`) or `error`.
    """
    if API_KEY and api_key != API_KEY:
        await websocket.close(code=4401, reason="invalid api key")
        return
    if table_id < 1 or table_id > MAX_TABLE:
        await websocket.close(code=4400, reason="bad table number")
        return

    await websocket.accept()
    max_bytes = int(MAX_UTTERANCE_SECONDS * SAMPLE_RATE * voice.SAMPLE_WIDTH)
    chunks: list[bytes] = []
    buffered = 0
    capturing = False
    overflowed = False

    try:
        await websocket.send_json(
            {
                "type": "ready",
                "table": table_id,
                "sample_rate": SAMPLE_RATE,
                "max_seconds": MAX_UTTERANCE_SECONDS,
                "enabled": voice.transcriber.available,
            }
        )

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            # ---- binary: audio ----------------------------------------------
            payload = message.get("bytes")
            if payload:
                if not capturing:
                    continue  # audio outside an utterance is discarded
                if buffered + len(payload) > max_bytes:
                    if not overflowed:
                        overflowed = True
                        await websocket.send_json(
                            {"type": "error", "message": "utterance too long, truncating"}
                        )
                    continue
                chunks.append(payload)
                buffered += len(payload)
                continue

            # ---- text: control ----------------------------------------------
            text = message.get("text")
            if not text:
                continue
            try:
                command = json.loads(text).get("type", "")
            except json.JSONDecodeError:
                command = text.strip().strip('"')

            if command == "start":
                chunks, buffered, capturing, overflowed = [], 0, True, False
                await websocket.send_json({"type": "listening"})

            elif command == "cancel":
                chunks, buffered, capturing = [], 0, False
                await websocket.send_json({"type": "cancelled"})

            elif command == "ping":
                await websocket.send_json({"type": "pong"})

            elif command == "stop":
                capturing = False
                pcm = b"".join(chunks)
                chunks, buffered = [], 0

                seconds = voice.duration_seconds(len(pcm), SAMPLE_RATE)
                if seconds < MIN_UTTERANCE_SECONDS:
                    await websocket.send_json(
                        {"type": "error", "message": f"too short ({seconds:.2f}s)"}
                    )
                    continue

                await websocket.send_json({"type": "processing", "seconds": round(seconds, 2)})
                try:
                    # Gemini is a blocking network call - keep the event loop free
                    # so the e-paper sockets keep getting their pings.
                    result = await asyncio.to_thread(
                        voice.process_utterance, table_id, voice.pcm_to_wav(pcm, SAMPLE_RATE), device
                    )
                except voice.VoiceError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue

                await websocket.send_json({"type": "result", "table": table_id, **result})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - one bad mic shouldn't take the server down
        log.warning("voice socket for table %s closed: %s", table_id, exc)


# ------------------------------------------------------------------- plumbing
def _resolve_items(requests: list[OrderItemIn]) -> list[dict]:
    return [_resolve_one(r) for r in requests]


def _resolve_one(request: OrderItemIn) -> dict:
    item = _resolve(request)
    return {
        "menu_id": item.id,
        "name": item.name,
        "unit_price": item.price,
        "quantity": request.quantity,
        "modifiers": request.modifiers,
        "prep_minutes": item.prep_minutes,
    }


def _resolve(request: OrderItemIn):
    """Map a request onto a menu item, by id or by (fuzzy) name."""
    if request.menu_id:
        item = menu.get(request.menu_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"no menu item '{request.menu_id}'")
    elif request.name:
        item = menu.match_by_name(request.name)
        if item is None:
            raise HTTPException(
                status_code=404, detail=f"no menu item matching '{request.name}'"
            )
    else:
        raise HTTPException(status_code=422, detail="provide either menu_id or name")

    if not item.available:
        raise HTTPException(status_code=409, detail=f"'{item.name}' is not available")
    return item


# ---------------------------------------------------------------------- misc
@app.get("/health", tags=["ops"])
def health() -> dict:
    db.connect()
    return {
        "status": "ok",
        "menu_items": len(menu),
        "menu_source": str(menu.path),
    }


app.include_router(api)
