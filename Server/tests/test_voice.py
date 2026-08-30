"""Voice ordering tests.

Gemini is swapped for a scripted fake, so these exercise our half of the
pipeline: the WebSocket protocol, PCM buffering, menu re-validation, and how
parsed items land in (or don't touch) the table's pending order - without a
network call or an API key.
"""
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_api import client  # noqa: F401,E402  (shared fixture + env setup)

from app import voice  # noqa: E402
from app.config import SAMPLE_RATE  # noqa: E402


def pcm(seconds: float) -> bytes:
    """Silence is fine - the fake transcriber ignores the audio."""
    return struct.pack("<h", 0) * int(SAMPLE_RATE * seconds)


class FakeTranscriber(voice.Transcriber):
    """Returns a canned parse and records what audio (and pending context) it was handed."""

    available = True

    def __init__(
        self,
        items: list[dict],
        transcript: str = "test utterance",
        confirms_pending: bool = False,
        cancels_pending: bool = False,
    ):
        self.items = items
        self.transcript = transcript
        self.confirms_pending = confirms_pending
        self.cancels_pending = cancels_pending
        self.calls: list[bytes] = []
        self.pending_seen: list[list[str] | None] = []

    def parse(self, wav_bytes: bytes, pending_items: list[str] | None = None) -> voice.ParsedOrder:
        self.calls.append(wav_bytes)
        self.pending_seen.append(pending_items)
        return voice.ParsedOrder(
            transcript=self.transcript,
            items=self.items,
            confirms_pending=self.confirms_pending,
            cancels_pending=self.cancels_pending,
            raw="{}",
        )


class BrokenTranscriber(voice.Transcriber):
    available = True

    def parse(self, wav_bytes: bytes, pending_items: list[str] | None = None):
        raise voice.VoiceError("Gemini request failed: upstream 503")


@pytest.fixture()
def fake(monkeypatch):
    def install(
        items,
        transcript: str = "test utterance",
        confirms_pending: bool = False,
        cancels_pending: bool = False,
    ):
        stub = FakeTranscriber(items, transcript, confirms_pending, cancels_pending)
        monkeypatch.setattr(voice, "transcriber", stub)
        return stub

    return install


TWO_BURGERS = [
    {"menu_id": "BUR02", "spoken_name": "cheeseburgers", "quantity": 2,
     "modifiers": ["-Onions"], "confidence": 0.93},
    {"menu_id": "DRK04", "spoken_name": "draft beer", "quantity": 1,
     "modifiers": [], "confidence": 0.88},
]


# --------------------------------------------------------------------- helpers
def run_utterance(client, table: int, seconds: float = 1.0):
    """Open the socket, do one full start/stop cycle, return the `result` message."""
    with client.websocket_connect(f"/table/{table}/voice") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        ws.send_json({"type": "start"})
        assert ws.receive_json()["type"] == "listening"
        ws.send_bytes(pcm(seconds))
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["type"] == "processing"
        return ws.receive_json()


# ------------------------------------------------------------------- websocket
def test_websocket_ready_reports_enabled(client, fake):
    fake(TWO_BURGERS)
    with client.websocket_connect("/table/1/voice") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["enabled"] is True
        assert ready["sample_rate"] == SAMPLE_RATE


def test_happy_path_stages_pending(client, fake):
    stub = fake(TWO_BURGERS)
    result = run_utterance(client, 2)

    assert result["type"] == "result"
    assert result["matched_count"] == 2
    assert result["unmatched_count"] == 0
    assert [i["menu_id"] for i in result["items"]] == ["BUR02", "DRK04"]
    assert voice.is_wav(stub.calls[0])  # server wrapped raw PCM as WAV

    pending = client.get("/table/2/pending").json()
    assert len(pending) == 2
    assert {p["itemName"] for p in pending} == {"2x Bacon Cheeseburger", "Draft Beer"}


def test_voice_replaces_prior_pending(client, fake):
    client.post("/table/3/createPending", json={"items": [{"menu_id": "DRK01"}]})
    fake(TWO_BURGERS)
    run_utterance(client, 3)

    pending = client.get("/table/3/pending").json()
    assert len(pending) == 2  # the old Soda is gone, not merged
    assert "Soda" not in {p["itemName"] for p in pending}


def test_empty_utterance_leaves_pending_untouched(client, fake):
    client.post("/table/4/createPending", json={"items": [{"menu_id": "DRK01"}]})
    fake([], transcript="just chatting")
    result = run_utterance(client, 4)

    assert result["items"] == []
    assert result["matched_count"] == 0
    pending = client.get("/table/4/pending").json()
    assert len(pending) == 1
    assert pending[0]["itemName"] == "Soda"


def test_hallucinated_menu_id_falls_back_to_name(client, fake):
    fake([{"menu_id": "NOPE99", "spoken_name": "classic burger", "quantity": 1,
           "modifiers": [], "confidence": 0.9}])
    result = run_utterance(client, 5)

    assert result["items"][0]["menu_id"] == "BUR01"  # rescued by name
    assert result["items"][0]["matched"] is True
    assert client.get("/table/5/pending").json()[0]["itemName"] == "Classic Burger"


def test_unmatched_item_reported_not_staged(client, fake):
    fake([{"menu_id": None, "spoken_name": "wizard hat", "quantity": 1,
           "modifiers": [], "confidence": 0.4}])
    result = run_utterance(client, 6)

    assert result["matched_count"] == 0
    assert result["unmatched_count"] == 1
    assert result["items"][0]["matched"] is False
    assert result["items"][0]["menu_id"] is None
    assert client.get("/table/6/pending").json() == []


def test_too_short_utterance_is_rejected(client, fake):
    stub = fake(TWO_BURGERS)
    with client.websocket_connect("/table/7/voice") as ws:
        ws.receive_json()
        ws.send_json({"type": "start"})
        ws.receive_json()
        ws.send_bytes(pcm(0.05))
        ws.send_json({"type": "stop"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert "too short" in err["message"]
    assert stub.calls == []
    assert client.get("/table/7/pending").json() == []


def test_too_long_utterance_truncates(client, fake):
    """Send audio in small chunks, like real hardware does, until it overflows."""
    fake(TWO_BURGERS)
    with client.websocket_connect("/table/8/voice") as ws:
        ready = ws.receive_json()
        max_seconds = ready["max_seconds"]
        ws.send_json({"type": "start"})
        ws.receive_json()

        chunk = pcm(1.0)
        for _ in range(int(max_seconds) + 5):
            ws.send_bytes(chunk)

        # the overflow warning fires exactly once, once buffering would exceed max_bytes
        warn = ws.receive_json()
        assert warn["type"] == "error"
        assert "too long" in warn["message"]

        ws.send_json({"type": "stop"})
        assert ws.receive_json()["type"] == "processing"
        result = ws.receive_json()
        assert result["type"] == "result"  # still completes on the truncated audio


def test_cancel_discards_the_utterance(client, fake):
    stub = fake(TWO_BURGERS)
    with client.websocket_connect("/table/9/voice") as ws:
        ws.receive_json()
        ws.send_json({"type": "start"})
        ws.receive_json()
        ws.send_bytes(pcm(1.0))
        ws.send_json({"type": "cancel"})
        assert ws.receive_json()["type"] == "cancelled"
    assert stub.calls == []


def test_disabled_without_api_key(client, monkeypatch):
    monkeypatch.setattr(voice, "transcriber", voice.NullTranscriber())
    with client.websocket_connect("/table/10/voice") as ws:
        ready = ws.receive_json()
        assert ready["enabled"] is False
        ws.send_json({"type": "start"})
        ws.receive_json()
        ws.send_bytes(pcm(1.0))
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["type"] == "processing"
        err = ws.receive_json()
        assert err["type"] == "error"
        assert "GEMINI_API_KEY" in err["message"]
    assert client.get("/table/10/pending").json() == []


def test_gemini_failure_surfaces_as_error(client, monkeypatch):
    monkeypatch.setattr(voice, "transcriber", BrokenTranscriber())
    with client.websocket_connect("/table/11/voice") as ws:
        ws.receive_json()
        ws.send_json({"type": "start"})
        ws.receive_json()
        ws.send_bytes(pcm(1.0))
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["type"] == "processing"
        err = ws.receive_json()
        assert "upstream 503" in err["message"]


def test_confirm_still_works_after_voice_stage(client, fake):
    client.post("/table/12/createOrder", json={"items": [{"menu_id": "DRK01"}]})
    fake(TWO_BURGERS)
    run_utterance(client, 12)

    r = client.post("/table/12/confirm")
    assert r.status_code == 200
    names = [i["itemName"] for i in r.json()]
    assert names == ["Soda", "2x Bacon Cheeseburger", "Draft Beer"]
    assert client.get("/table/12/pending").json() == []


# ------------------------------------------------------------- voice confirm
def test_confirm_pending_by_voice(client, fake):
    client.post("/table/20/createOrder", json={"items": [{"menu_id": "DRK01"}]})
    client.post("/table/20/createPending", json={"items": [{"menu_id": "BUR01"}]})

    stub = fake([], transcript="yes that's right", confirms_pending=True)
    result = run_utterance(client, 20)

    assert result["confirmed"] is True
    assert result["items"] == []
    assert stub.pending_seen[0] == ["Classic Burger"]  # told what it would be confirming

    current = client.get("/table/20").json()
    assert [i["itemName"] for i in current] == ["Soda", "Classic Burger"]
    assert client.get("/table/20/pending").json() == []


def test_confirms_pending_with_nothing_pending_is_a_noop(client, fake):
    client.post("/table/21/createOrder", json={"items": [{"menu_id": "DRK01"}]})
    # nothing staged in pending - a stray "yes" shouldn't do anything
    fake([], transcript="yes", confirms_pending=True)
    result = run_utterance(client, 21)

    assert result["confirmed"] is False
    current = client.get("/table/21").json()
    assert [i["itemName"] for i in current] == ["Soda"]  # unchanged


def test_confirm_by_voice_pushes_order_updated(client, fake):
    client.post("/table/22/createOrder", json={"items": [{"menu_id": "DRK01"}]})
    client.post("/table/22/createPending", json={"items": [{"menu_id": "BUR01"}]})
    fake([], confirms_pending=True)

    with client.websocket_connect("/ws/tables/22") as ws:
        ws.receive_json()  # snapshot
        run_utterance(client, 22)
        msg = ws.receive_json()
        assert msg["type"] == "order.updated"
        assert [i["itemName"] for i in msg["display"]] == ["Soda", "Classic Burger"]


# -------------------------------------------------------------- voice cancel
def test_cancel_pending_by_voice(client, fake):
    client.post("/table/25/createOrder", json={"items": [{"menu_id": "DRK01"}]})
    client.post("/table/25/createPending", json={"items": [{"menu_id": "BUR01"}]})

    stub = fake([], transcript="no, cancel that", cancels_pending=True)
    result = run_utterance(client, 25)

    assert result["cancelled"] is True
    assert result["confirmed"] is False
    assert result["items"] == []
    assert stub.pending_seen[0] == ["Classic Burger"]  # told what it would be cancelling

    # current order is untouched - only pending was cleared
    current = client.get("/table/25").json()
    assert [i["itemName"] for i in current] == ["Soda"]
    assert client.get("/table/25/pending").json() == []


def test_cancels_pending_with_nothing_pending_is_a_noop(client, fake):
    client.post("/table/26/createOrder", json={"items": [{"menu_id": "DRK01"}]})
    # nothing staged in pending - a stray "no" shouldn't do anything
    fake([], transcript="no", cancels_pending=True)
    result = run_utterance(client, 26)

    assert result["cancelled"] is False
    current = client.get("/table/26").json()
    assert [i["itemName"] for i in current] == ["Soda"]  # unchanged


def test_cancel_by_voice_pushes_voice_cancelled_event(client, fake):
    client.post("/table/27/createPending", json={"items": [{"menu_id": "BUR01"}]})
    fake([], cancels_pending=True)

    with client.websocket_connect("/ws/tables/27") as ws:
        ws.receive_json()  # snapshot
        run_utterance(client, 27)
        msg = ws.receive_json()
        assert msg["type"] == "voice.cancelled"
        assert msg["pending"] == []


def test_bad_table_number_closes_socket(client, fake):
    fake(TWO_BURGERS)
    with pytest.raises(Exception):
        with client.websocket_connect("/table/0/voice") as ws:
            ws.receive_json()


# -------------------------------------------------------------- http upload path
def test_http_upload_matches_websocket_shape(client, fake):
    stub = fake(TWO_BURGERS)
    r = client.post(
        "/table/13/voice",
        files={"file": ("order.wav", voice.pcm_to_wav(pcm(1.5)), "audio/wav")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["matched_count"] == 2
    assert [i["menu_id"] for i in body["items"]] == ["BUR02", "DRK04"]
    assert voice.is_wav(stub.calls[0])  # passed through unchanged, already a WAV

    pending = client.get("/table/13/pending").json()
    assert len(pending) == 2


def test_http_upload_wraps_raw_pcm(client, fake):
    stub = fake(TWO_BURGERS)
    r = client.post(
        "/table/14/voice",
        files={"file": ("order.pcm", pcm(1.0), "application/octet-stream")},
    )
    assert r.status_code == 200
    assert voice.is_wav(stub.calls[0])  # raw PCM got wrapped before reaching the fake


def test_http_upload_surfaces_voice_error(client, monkeypatch):
    monkeypatch.setattr(voice, "transcriber", BrokenTranscriber())
    r = client.post(
        "/table/15/voice",
        files={"file": ("order.wav", voice.pcm_to_wav(pcm(1.0)), "audio/wav")},
    )
    assert r.status_code == 502
    assert "upstream 503" in r.json()["detail"]
