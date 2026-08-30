# Restaurant Order API

Backend for the Seeed e-paper table displays. FastAPI + SQLite, one container, no timestamps — every item just carries a static `prep_minutes` from the menu, there's no countdown clock to get out of sync.

- **The e-paper ESP32** looks itself up with `GET /device/{device_number}` (which resolves device → table), polls `GET /table/{table_id}`, and/or subscribes to `ws://<host>:8000/ws/tables/{table_id}` for a push on change.
- **The mic board** holds `ws://<host>:8000/table/{table_id}/voice` open and streams audio only while its button is held; the server transcribes it and stages the parsed items in that table's pending order.
- **Anything that adds food** (tablet, POS, phone) writes the kitchen's order with `POST /table/{table_id}/createOrder`, or stages one for review with `createPending` + `confirm`.
- **The menu** is a plain CSV in the mounted volume, editable in Excel.

## Run it

```bash
cd Server
docker compose up --build
```

Then `http://localhost:8000/docs` for interactive Swagger docs, `http://localhost:8000/health` for a liveness check, and `http://localhost:8000/admin/` for a small dashboard to watch and manage devices/tables by hand.

First boot copies `data/menu.csv` into the volume if it isn't there, and seeds devices 1..`MAX_DEVICES` pointing at tables 1..`MAX_DEVICES`. `data/restaurant.db` is created alongside it, so orders survive restarts.

Without Docker:

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## The menu CSV

`data/menu.csv` — columns `id,name,category,price,prep_minutes,available`.

```csv
id,name,category,price,prep_minutes,available
BUR01,Classic Burger,main,14.00,15,1
DRK01,Soda,drink,3.00,1,1
```

`id` and `name` and `price` are required; the rest default. `available=0` hides an item and makes ordering it return `409`. Edit the file, then either `POST /menu/reload` or just make any menu/order request — the server notices the changed mtime and reloads itself.

`prep_minutes` becomes the item's static `time` field on the display — it never counts down, since the server keeps no timestamps.

## Model

Every table has at most **one current order** (what the kitchen is making) and **one pending order** (staged, awaiting review). Both are just lists of items — there's no order history, no open/closed lifecycle, no per-item status pipeline. An item has exactly one piece of state beyond its menu data: `delivered`.

```
createOrder  → replaces the current order outright, clears any pending (skips review)
createPending → replaces the pending order, current order untouched
confirm       → appends pending onto the end of current, then clears pending
```

An item's position in its list (0-based) is its id for `/table/{table_id}/{item_id}` — stable as long as you don't re-run createOrder/createPending, since that's a full replace.

Each table also carries an `order_id`: a globally-increasing integer minted fresh every time `createOrder` replaces the current order. Confirming pending items or toggling delivered keeps the same `order_id`, since those change an existing order rather than starting a new one. It's exposed via `GET /device/{device_number}` (not on `/table/{table_id}`, which stays a plain array). A table that's never had an order shows `order_id: null`; `clearTable` sets it to `0` instead, so a client can tell "explicitly cleared" apart from "never seated."

## Endpoints

### Menu

| Method | Path | Notes |
|---|---|---|
| GET | `/menu` | `?category=main`, `?include_unavailable=true` |
| GET | `/menu/categories` | |
| GET | `/menu/{menu_id}` | |
| POST | `/menu/reload` | force a re-read of the CSV |

### Device

| Method | Path | Notes |
|---|---|---|
| GET | `/devices` | every device, same shape as below, for a dashboard listing |
| GET | `/device/{device_number}` | `{device_number, table_number, order_id, order}` — what this device should show right now |
| PUT | `/device/{device_number}` | body `{"table_number": N}` — (re)assign which table a device points at |

Devices are numbered `1..MAX_DEVICES` (3 by default); anything outside that range is `422`. A fresh install seeds device *N* → table *N* so `GET /device/1` works immediately, no assignment call needed.

### Table

| Method | Path | Notes |
|---|---|---|
| GET | `/tables` | every table that's ever had an order, as counts only: `{table_number, version, order_id, item_count, pending_count}` |
| GET | `/table/{table_id}` | the current order — `?rows=N` clips it, default `DISPLAY_ROWS` |
| GET | `/table/{table_id}/pending` | the staged order, same shape |
| GET | `/table/{table_id}/checkPending` | just `true`/`false` — is anything staged? |
| POST | `/table/{table_id}/confirm` | merge pending onto the end of current |
| POST | `/table/{table_id}/createOrder` | body `{"items":[...]}` — **replaces** current wholesale, clears pending |
| POST | `/table/{table_id}/createPending` | body `{"items":[...]}` — **replaces** pending wholesale |
| POST | `/table/{table_id}/clearTable` | erases the current order, resets `order_id` to `0`; pending is untouched |
| POST | `/table/{table_id}/voice` | upload a WAV/PCM clip — test the voice pipeline without a mic |
| GET | `/table/{table_id}/{item_id}` | one item's full detail (position-based id) |
| POST | `/table/{table_id}/{item_id}/delivered` | **toggles** delivered (not just set-true) |

`items` in `createOrder`/`createPending` are menu-referenced, same shape either way:

```json
{"items": [
  {"menu_id": "BUR01", "quantity": 2, "modifiers": ["-Onions", "+Extra cheese"]},
  {"name": "classic burger"}
]}
```

`menu_id` is preferred; `name` falls back to exact → substring → fuzzy match against the menu (handy for a hand-typed or transcribed dish name). An id/name that doesn't resolve is `404`; a menu item with `available=0` is `409`.

### Display shape

Every table/pending/item-list response is the same flat row shape — the whole body *is* the array, no wrapper object:

```json
[
  {"itemName": "2x Ribeye Steak", "details": "-Onions, +Medium rare", "price": 56.00, "time": 25.0, "delivered": false}
]
```

`price` is the line total (`unit_price * quantity`); `time` is the item's static `prep_minutes` — a float, not a countdown. An empty table just returns `[]`. Each row maps one-to-one onto `drawItem(i, itemName, price, details, time, delivered)` in `layout.h`.

`GET /table/{table_id}/{item_id}` returns the same five fields plus `menu_id`, `quantity`, and the untruncated `modifiers` list.

The list endpoints (`/table/{table_id}`, `/pending`) carry an `ETag`; send it back as `If-None-Match` on `/table/{table_id}` and you get a `304` when nothing changed.

### Example

```bash
# kitchen's current order for table 7
curl -X POST localhost:8000/table/7/createOrder \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"menu_id":"BUR01","quantity":2,"modifiers":["-Onions"]}]}'

# stage a beer for review instead of committing it straight away
curl -X POST localhost:8000/table/7/createPending \
  -H 'Content-Type: application/json' -d '{"items":[{"menu_id":"DRK04"}]}'

# staff approves it
curl -X POST localhost:8000/table/7/confirm

# kitchen marks the burgers out
curl -X POST localhost:8000/table/7/0/delivered

# device 1 finds out what table it's showing and what's on it
curl localhost:8000/device/1
```

## WebSocket

```
ws://<host>:8000/ws/tables/{table_id}
```

On connect the server sends a `snapshot` immediately, so a rebooting ESP32 gets a full picture without a second HTTP call. After that, one message per change to the *current* order (pending changes don't push):

```json
{"type":"snapshot",      "table":14, "version":3, "display": [ ... ]}
{"type":"order.updated", "table":14, "version":4, "display": [ ... ]}
{"type":"ping"}
```

`ping` arrives every 25s to keep NAT/router state alive — ignore it. Each subscriber has a bounded queue; if a device stops draining mid-refresh the oldest update is dropped rather than stalling the server, and the next message it does receive is a complete display array, so it can never end up half-updated. Compare `version` with the last one drawn — if it changed, redraw.

### ESP32 sketch

Add to `Screen/platformio.ini`:

```ini
lib_deps =
    links2004/WebSockets @ ^2.4.1
    bblanchon/ArduinoJson @ ^7.0.4
```

```cpp
#include <WebSocketsClient.h>
#include <ArduinoJson.h>

WebSocketsClient ws;
int lastVersion = -1;

void onWsEvent(WStype_t type, uint8_t* payload, size_t len) {
    if (type != WStype_TEXT) return;

    JsonDocument doc;
    if (deserializeJson(doc, payload, len)) return;

    const char* msgType = doc["type"] | "";
    if (strcmp(msgType, "ping") == 0) return;

    int version = doc["version"] | 0;
    if (version == lastVersion) return;   // nothing changed, spare the panel
    lastVersion = version;

    redraw(doc["display"].as<JsonArray>());
}

void redraw(JsonArray items) {
    epaper.fillScreen(TFT_WHITE);
    epaper.update();

    int row = 0;
    for (JsonObject r : items) {
        drawItem(row++,
                 (char*)r["itemName"].as<const char*>(),
                 String(r["price"].as<float>(), 2).c_str(),
                 (char*)r["details"].as<const char*>(),
                 String(r["time"].as<float>(), 0).c_str(),
                 r["delivered"].as<bool>());
    }
    epaper.update();
}

void setup() {
    // ... WiFi.begin(ssid, password) ...
    ws.begin("192.168.1.50", 8000, "/ws/tables/14");
    ws.onEvent(onWsEvent);
    ws.setReconnectInterval(5000);
}

void loop() { ws.loop(); }
```

Put the device number in `secrets.h` next to the Wi-Fi credentials, look it up once via `GET /device/{n}` on boot to learn the table, then open the socket for that table. If you'd rather not hold a socket open, just poll `GET /table/{table_id}` and send its `ETag` back as `If-None-Match` each time.

## Voice ordering

The mic board holds a WebSocket open and streams audio only while its button is held. On release the server wraps the buffered PCM in a WAV container and makes **one** Gemini call that transcribes *and* extracts order items — the live menu is injected into the prompt so the model returns real `menu_id`s rather than invented dish names.

Nothing goes to the kitchen automatically. Parsed items are staged into that table's **pending** order — the exact same bucket `POST /table/{id}/createPending` writes to. A new utterance replaces whatever was already pending, except when Gemini hears no order attempt at all (silence, chit-chat): an empty parse leaves pending untouched rather than wiping out something already staged.

If something is already pending, the prompt tells the model so — a plain approval ("yes", "that's right", "confirm my order") is recognized as **confirming it by voice**, which does exactly what `POST /table/{id}/confirm` does (merges pending onto current and pushes an `order.updated` push to `/ws/tables/{table_id}`). A plain rejection ("no", "cancel that", "never mind", "scratch that") is recognized as **cancelling it by voice** instead — clears pending (same as `createPending` with an empty list) and pushes a `voice.cancelled` event, current order untouched. Anything else (a new order, a change, or nothing relevant) is treated as a normal utterance. None of this requires staff to touch anything, though they still can — a tablet/POS tapping "confirm" works exactly the same.

Don't confuse this with the `{"type":"cancel"}` wire-protocol frame below — that one discards the audio buffer *before* it's even sent to Gemini (e.g. the button was pressed by accident). "Cancelling by voice" is a `result` after a full transcription, meaning Gemini understood the *speech itself* as a rejection of the pending order.

```
button held    → PCM streams to the server
button released → Gemini call:
                   - a new order → matched items replace the table's pending order
                   - "yes, that's right" (something already pending) → confirmed onto current
                   - "no, cancel that" (something already pending) → pending cleared
staff can also just tap "confirm" on a tablet/POS instead of the customer saying so
```

### Local voice pipeline (no Gemini, no API key, no quota)

Set `VOICE_PROVIDER=local` and bring up the two extra services (off by default, opt-in via a Compose profile so nobody pays for them unless they want them):

```bash
docker compose --profile local-ai up -d --build
docker exec ollama ollama pull qwen2.5:7b-instruct   # one-time, several GB
```

This runs a GPU-accelerated [faster-whisper](https://github.com/SYSTRAN/faster-whisper) service for transcription and a local [Ollama](https://ollama.com) model for the same item-extraction job Gemini otherwise does in one call — two network hops instead of one, but no external dependency, no per-request cost, and no daily quota. Needs an NVIDIA GPU + the NVIDIA Container Toolkit on the host; `whisper`/`ollama` both request `capabilities: [gpu]` in `docker-compose.yml`.

`voice.py`'s `Transcriber` interface doesn't change — `LocalTranscriber` produces the exact same `ParsedOrder` shape `GeminiTranscriber` does, so `process_utterance` and everything downstream is unaffected by which one is active.

| Env var | Default | Purpose |
|---|---|---|
| `VOICE_PROVIDER` | `gemini` | `local` switches to the whisper+ollama pipeline |
| `WHISPER_URL` | `http://localhost:9000` | set to `http://whisper:9000` in Docker (already wired in `docker-compose.yml`) |
| `OLLAMA_URL` | `http://localhost:11434` | same, `http://ollama:11434` in Docker |
| `OLLAMA_MODEL` | `qwen2.5:7b-instruct` | any model you've pulled into the `ollama` container |

Swap models any time with `docker exec ollama ollama pull <model>` + updating `OLLAMA_MODEL` — no code change. A smaller/faster model (e.g. `llama3.2:3b`) trades extraction quality for latency if a live order feels sluggish; `ASR_MODEL` in `docker-compose.yml`'s `whisper` service does the same tradeoff for transcription speed vs. accuracy.

### Wire protocol

```
ws://<host>:8000/table/{table_id}/voice?device=mic-1
```

Client → server:

| Frame | Meaning |
|---|---|
| `{"type":"start"}` | button pressed, begin an utterance |
| binary frames | raw 16-bit **mono** PCM @ `SAMPLE_RATE` (16 kHz), little-endian |
| `{"type":"stop"}` | button released — transcribe and stage |
| `{"type":"cancel"}` | throw the buffer away, no Gemini call |
| `{"type":"ping"}` | keepalive |

Server → client: `ready`, `listening`, `processing`, then `result` or `error`.

```json
{"type":"result","table":14,"transcript":"Two cheeseburgers no onions and a draft beer",
 "confirmed":false,"cancelled":false,"matched_count":2,"unmatched_count":0,
 "items":[{"menu_id":"BUR02","name":"Bacon Cheeseburger","spoken_name":"cheeseburgers",
           "quantity":2,"modifiers":["-Onions"],"confidence":0.93,"matched":true}],
 "pending":[{"itemName":"2x Bacon Cheeseburger","details":"-Onions","price":33.0,"time":17.0,"delivered":false}]}
```

When the utterance is recognized as a voice confirmation or cancellation instead, `confirmed`/`cancelled` flips accordingly, `items`/`pending` come back empty, and no staging happens:

```json
{"type":"result","table":14,"transcript":"yes that's right","confirmed":true,"cancelled":false,
 "items":[],"matched_count":0,"unmatched_count":0,"pending":[]}
{"type":"result","table":14,"transcript":"no, cancel that","confirmed":false,"cancelled":true,
 "items":[],"matched_count":0,"unmatched_count":0,"pending":[]}
```

Audio sent outside a `start`/`stop` window is discarded. Utterances shorter than `MIN_UTTERANCE_SECONDS` are rejected; anything past `MAX_UTTERANCE_SECONDS` is truncated with a warning rather than buffered forever. The Gemini call runs off the event loop, so the e-paper sockets keep ticking while a table is ordering.

An item Gemini heard but couldn't match to the menu (a bad id, or the spoken name didn't resolve) shows up in `items` with `matched: false, menu_id: null` — surfaced so staff can add it by hand — but it's never written into `pending`.

The table's display socket (`/ws/tables/{table_id}`) also gets a `voice.pending` event when an utterance stages something, and a `voice.cancelled` event when one clears pending by voice — neither bumps `version`, so a firmware that only redraws on version change correctly ignores them, but a future staff dashboard could show a live transcript. A voice *confirmation* is different: it changes the current order, so it pushes the normal `order.updated` event (same as `POST /confirm`) instead.

Test the whole path without a mic:

```bash
curl -F file=@order.wav localhost:8000/table/7/voice
curl -X POST localhost:8000/table/7/confirm
```

Without `GEMINI_API_KEY` the rest of the API runs normally and voice endpoints return an `error`/`502` instead of transcribing.

## Admin dashboard

`http://localhost:8000/admin/` — a single static page (`app/static/index.html`, no build step, no external dependencies) for watching and managing things by hand instead of curling the API directly:

- **Devices**: every device, the table it's pointed at, its `order_id`, item count, and a one-field reassign (`PUT /device/{n}`).
- **Tables**: every table that's ever had an order, with current/pending item counts (`GET /tables`) — click one, or type any table number, to open it.
- **Table detail**: the current order with a per-item "mark delivered" toggle, a "clear table" button, the pending order with confirm/cancel buttons, and a small order builder (pick a menu id, quantity, modifiers, add rows, then "save as current" or "save as pending") that maps straight onto `createOrder`/`createPending`.
- Live updates: it opens `/ws/tables/{table_id}` for whichever table is selected, so `order.updated`/`voice.pending`/`voice.cancelled` events refresh the view without polling (everything else — the device and table lists — just polls every 8s).

If `API_KEY` is set, paste it into the "API key" field in the header; it's kept in `localStorage` and sent as `X-API-Key` on every call the page makes (and `?api_key=` on its WebSocket).

## Auth

Off by default so the firmware stays simple on a trusted LAN. Set `API_KEY` in `.env` to require `X-API-Key: <key>` on every REST call, and `?api_key=<key>` on both WebSocket URLs (`/ws/tables/{table_id}` and `/table/{table_id}/voice`).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `./data` | where the DB and CSV live |
| `DB_PATH` | `$DATA_DIR/restaurant.db` | |
| `MENU_CSV` | `$DATA_DIR/menu.csv` | |
| `API_KEY` | unset | require `X-API-Key` when set |
| `DISPLAY_ROWS` | `6` | item rows the panel can render |
| `DISPLAY_NAME_CHARS` | `22` | truncation budget for item names |
| `DISPLAY_DETAIL_CHARS` | `44` | truncation budget for modifiers |
| `MAX_DEVICES` | `3` | how many physical devices exist right now |
| `GEMINI_API_KEY` | unset | enables voice ordering |
| `GEMINI_MODEL` | `gemini-3.6-flash` | |
| `GEMINI_TIMEOUT` | `20` | seconds |
| `SAMPLE_RATE` | `16000` | PCM rate the mic board sends — must match the firmware |
| `VOICE_PROVIDER` | `gemini` | `local` uses the whisper+ollama pipeline instead — see "Local voice pipeline" above |
| `WHISPER_URL` / `OLLAMA_URL` / `OLLAMA_MODEL` | see above | only used when `VOICE_PROVIDER=local` |

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

51 tests: device/table listing, device lookup/reassignment/bounds, order id lifecycle, createOrder/createPending/confirm/checkPending/clearTable and how they interact, item detail and delivered-toggle, fuzzy name matching, unavailable-item rejection, ETag 304s, row clipping, CSV hot reload, WebSocket snapshot/push/isolation — plus the voice path with Gemini swapped for a scripted fake: PCM buffering, overflow truncation, cancel (audio-buffer discard), menu re-validation against a hallucinated id, unmatched-item reporting, the empty-utterance pending guard, confirming/cancelling a pending order by voice, and the HTTP upload convenience. No API key or network needed to run them.

## Layout

```
Server/
├── app/
│   ├── config.py       env-driven settings
│   ├── db.py           SQLite schema + queries (no timestamps)
│   ├── menu.py         CSV catalog with hot reload
│   ├── voice.py        PCM -> WAV -> Gemini -> validated items
│   ├── serializers.py  row -> the flat e-paper JSON shape
│   ├── schemas.py      request/response models
│   ├── events.py       WebSocket fan-out broker
│   ├── main.py         routes
│   └── static/
│       └── index.html  admin dashboard, served at /admin/
├── data/menu.csv
├── tests/
│   ├── test_api.py
│   └── test_voice.py
├── Dockerfile
└── docker-compose.yml
```
