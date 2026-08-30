"""Speech -> order items, via Gemini (default) or a local whisper+ollama pipeline.

The mic board streams raw PCM. We buffer one utterance (button held), wrap it in
a WAV container - Gemini's inline audio accepts WAV/MP3/FLAC/etc but not headerless
PCM - and make a single `generate_content` call that both transcribes and
extracts structured order items in one round trip.

Set VOICE_PROVIDER=local to use LocalTranscriber instead: a faster-whisper
service does the transcription, then a local Ollama model does the same
item-extraction job Gemini otherwise does in one shot. Same ParsedOrder
contract either way - nothing downstream (resolve_items, process_utterance)
knows or cares which one produced it.

The live menu is injected into the prompt, so the model picks real `menu_id`s
instead of inventing dish names. Everything it returns is still re-validated
against the menu on our side; anything that doesn't resolve is surfaced as
unmatched rather than silently staged.

Parsed items land in the table's `pending` bucket (the same one
`POST /table/{id}/createPending` writes to) - nothing reaches the kitchen until
someone calls `POST /table/{id}/confirm`. Both confirming and cancelling a
pending order can also happen by voice: if something is already pending, the
prompt tells Gemini so, and a simple "yes"/"that's right" is recognized as
approval (same effect as `/confirm`) while "no"/"cancel that"/"never mind" is
recognized as a cancellation (clears pending, same effect as
`POST /table/{id}/createPending` with an empty list).
"""
from __future__ import annotations

import io
import json
import logging
import wave
from dataclasses import dataclass, field

import requests

from . import db, serializers
from .config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_TIMEOUT,
    LOCAL_TIMEOUT,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_URL,
    SAMPLE_RATE,
    VOICE_PROVIDER,
    WHISPER_URL,
)
from .events import broker, table_topic
from .menu import MenuItem, menu

log = logging.getLogger("restaurant.voice")

SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1

# The JSON contract we hold Gemini to.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {
            "type": "string",
            "description": "Verbatim transcription of everything spoken.",
        },
        "language": {
            "type": "string",
            "description": "BCP-47 code of the spoken language, e.g. en-US or es-MX.",
        },
        "items": {
            "type": "array",
            "description": "One entry per dish or drink the speaker ordered.",
            "items": {
                "type": "object",
                "properties": {
                    "menu_id": {
                        "type": "string",
                        "nullable": True,
                        "description": (
                            "The exact id from the MENU list that this order line refers to. "
                            "Null if nothing on the menu is a reasonable match."
                        ),
                    },
                    "spoken_name": {
                        "type": "string",
                        "description": "The dish as the speaker said it, in their words.",
                    },
                    "quantity": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 99,
                        "description": "How many were ordered. Default 1 if unstated.",
                    },
                    "modifiers": {
                        "type": "array",
                        "description": (
                            "Customisations, each prefixed: '-' for removals "
                            "(-Onions), '+' for additions (+Extra cheese), "
                            "'*' for preparation notes (*Medium rare)."
                        ),
                        "items": {"type": "string"},
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "How sure you are about this line, 0 to 1.",
                    },
                },
                "required": ["spoken_name", "quantity", "modifiers", "confidence"],
            },
        },
        "notes": {
            "type": "string",
            "description": "Anything said that wasn't an order (a question, a complaint, silence).",
        },
        "confirms_pending": {
            "type": "boolean",
            "description": (
                "True only if the speaker is simply approving the pending order listed "
                "in the prompt (e.g. 'yes', 'that's right', 'confirm my order', 'sounds "
                "good') with no new items mentioned. False otherwise, including when "
                "nothing is pending."
            ),
        },
        "cancels_pending": {
            "type": "boolean",
            "description": (
                "True only if the speaker is rejecting/withdrawing the pending order "
                "listed in the prompt (e.g. 'no', 'cancel that', 'never mind', 'scratch "
                "that', 'I changed my mind') with no new items mentioned. False otherwise, "
                "including when nothing is pending. Never true at the same time as "
                "confirms_pending."
            ),
        },
    },
    "required": ["transcript", "items", "confirms_pending", "cancels_pending"],
}

PROMPT = """You are taking a food order at a restaurant table from a short audio clip.
{pending_block}
Do the following:
1. Transcribe exactly what was said.
2. If a pending order is listed above and the speaker is just approving it - "yes",
   "that's right", "confirm my order", "sounds good" - with no new items mentioned,
   set confirms_pending to true and leave items empty.
3. If a pending order is listed above and the speaker is rejecting or withdrawing it -
   "no", "cancel that", "never mind", "scratch that", "I changed my mind" - with no new
   items mentioned, set cancels_pending to true and leave items empty.
4. Otherwise, set both confirms_pending and cancels_pending to false and extract every
   dish or drink being ordered as usual (this covers a new order, a change, or nothing
   relevant at all).

Rules:
- Match each order line to an id from the MENU below. Use the id verbatim.
- If nothing on the menu is a reasonable match, set menu_id to null and still fill in spoken_name.
- Quantities: "a couple of" is 2, "a few" is 3, unstated is 1.
- Put customisations in modifiers with a prefix: "-" removes ("no onions" -> "-Onions"),
  "+" adds ("extra cheese" -> "+Extra cheese"), "*" is a preparation note
  ("medium rare" -> "*Medium rare").
- Ignore chit-chat, background conversation and anything that isn't an order. Put it in notes.
- If nobody ordered anything, return an empty items array. Do not invent an order.
- Confidence should reflect audio clarity and how certain the menu match is.

MENU:
{menu}
"""

# Same contract minus transcript/language - a local whisper service already
# gave us those, so the local LLM only needs to do the extraction half.
LOCAL_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": RESPONSE_SCHEMA["properties"]["items"],
        "notes": RESPONSE_SCHEMA["properties"]["notes"],
        "confirms_pending": RESPONSE_SCHEMA["properties"]["confirms_pending"],
        "cancels_pending": RESPONSE_SCHEMA["properties"]["cancels_pending"],
    },
    "required": ["items", "confirms_pending", "cancels_pending"],
}

LOCAL_EXTRACTION_PROMPT = """You are taking a food order at a restaurant table. A speech-to-text
system already transcribed what the customer said - it's given below.
{pending_block}
TRANSCRIPT: "{transcript}"

Do the following:
1. If a pending order is listed above and the speaker is just approving it - "yes",
   "that's right", "confirm my order", "sounds good" - with no new items mentioned,
   set confirms_pending to true and leave items empty.
2. If a pending order is listed above and the speaker is rejecting or withdrawing it -
   "no", "cancel that", "never mind", "scratch that", "I changed my mind" - with no new
   items mentioned, set cancels_pending to true and leave items empty.
3. Otherwise, set both confirms_pending and cancels_pending to false and extract every
   dish or drink being ordered as usual (this covers a new order, a change, or nothing
   relevant at all).

Rules:
- Match each order line to an id from the MENU below. Use the id verbatim.
- If nothing on the menu is a reasonable match, set menu_id to null and still fill in spoken_name.
- Quantities: "a couple of" is 2, "a few" is 3, unstated is 1.
- Put customisations in modifiers with a prefix: "-" removes ("no onions" -> "-Onions"),
  "+" adds ("extra cheese" -> "+Extra cheese"), "*" is a preparation note
  ("medium rare" -> "*Medium rare").
- Ignore chit-chat and anything that isn't an order - if nobody ordered anything, return
  an empty items array. Do not invent an order.
- Confidence should reflect how certain the menu match is (the audio is already
  transcribed, so this isn't about audio clarity).
- Respond with ONLY the JSON object - no other text.

MENU:
{menu}
"""


class VoiceError(RuntimeError):
    """Anything that stops us turning audio into items."""


@dataclass
class ParsedOrder:
    transcript: str = ""
    language: str | None = None
    notes: str = ""
    items: list[dict] = field(default_factory=list)
    confirms_pending: bool = False
    cancels_pending: bool = False
    raw: str = ""


@dataclass
class ResolvedLine:
    menu_item: MenuItem | None
    spoken_name: str
    quantity: int
    modifiers: list[str]
    confidence: float


# ------------------------------------------------------------------- audio I/O
def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap headerless 16-bit mono PCM in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def duration_seconds(pcm_bytes: int, sample_rate: int = SAMPLE_RATE) -> float:
    return pcm_bytes / (sample_rate * SAMPLE_WIDTH * CHANNELS)


# ---------------------------------------------------------------- transcribers
class Transcriber:
    """Interface so tests (and a keyless deployment) can swap the model out."""

    available: bool = True

    def parse(
        self, wav_bytes: bytes, pending_items: list[str] | None = None
    ) -> ParsedOrder:  # pragma: no cover - interface
        raise NotImplementedError


class GeminiTranscriber(Transcriber):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or GEMINI_API_KEY
        self.model = model or GEMINI_MODEL
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise VoiceError("GEMINI_API_KEY is not set")
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise VoiceError("google-genai is not installed") from exc
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def parse(self, wav_bytes: bytes, pending_items: list[str] | None = None) -> ParsedOrder:
        client = self._get_client()
        from google.genai import types

        prompt = PROMPT.format(menu=_menu_for_prompt(), pending_block=_pending_block(pending_items))

        try:
            response = client.models.generate_content(
                model=self.model,
                contents=[
                    prompt,
                    types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                    http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT * 1000)),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surface upstream failures as an error
            raise VoiceError(f"Gemini request failed: {exc}") from exc

        text = getattr(response, "text", None)
        if not text:
            raise VoiceError("Gemini returned an empty response")
        return _parse_model_json(text)


class NullTranscriber(Transcriber):
    """Stand-in when no API key is configured. Fails loudly, never silently."""

    available = False

    def parse(self, wav_bytes: bytes, pending_items: list[str] | None = None) -> ParsedOrder:
        raise VoiceError("voice ordering is disabled: set GEMINI_API_KEY")


class LocalTranscriber(Transcriber):
    """VOICE_PROVIDER=local: a faster-whisper service for STT, Ollama for extraction.

    Two network calls where Gemini does one, but no API key, no quota, no
    per-request cost, and everything stays on this machine.
    """

    available = True

    def parse(self, wav_bytes: bytes, pending_items: list[str] | None = None) -> ParsedOrder:
        transcript, language = self._transcribe(wav_bytes)
        if not transcript:
            return ParsedOrder(transcript="", language=language, notes="No speech detected.", raw="{}")
        return self._extract(transcript, language, pending_items)

    def _transcribe(self, wav_bytes: bytes) -> tuple[str, str | None]:
        try:
            response = requests.post(
                f"{WHISPER_URL}/asr",
                params={"output": "json"},
                files={"audio_file": ("clip.wav", wav_bytes, "audio/wav")},
                timeout=LOCAL_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise VoiceError(f"Whisper request failed: {exc}") from exc

        data = response.json()
        return str(data.get("text") or "").strip(), data.get("language") or None

    def _extract(
        self, transcript: str, language: str | None, pending_items: list[str] | None
    ) -> ParsedOrder:
        prompt = LOCAL_EXTRACTION_PROMPT.format(
            menu=_menu_for_prompt(),
            pending_block=_pending_block(pending_items),
            transcript=transcript,
        )
        try:
            response = requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "format": LOCAL_EXTRACTION_SCHEMA,
                    "stream": False,
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                },
                timeout=LOCAL_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise VoiceError(f"Ollama request failed: {exc}") from exc

        text = response.json().get("response")
        if not text:
            raise VoiceError("Ollama returned an empty response")

        parsed = _parse_model_json(text)
        parsed.transcript = transcript
        parsed.language = language
        return parsed


def _parse_model_json(text: str) -> ParsedOrder:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VoiceError(f"Model returned invalid JSON: {text[:200]}") from exc
    if not isinstance(data, dict):
        raise VoiceError("Gemini returned JSON that wasn't an object")
    return ParsedOrder(
        transcript=str(data.get("transcript") or "").strip(),
        language=(data.get("language") or None),
        notes=str(data.get("notes") or "").strip(),
        items=[i for i in (data.get("items") or []) if isinstance(i, dict)],
        confirms_pending=bool(data.get("confirms_pending") or False),
        cancels_pending=bool(data.get("cancels_pending") or False),
        raw=text,
    )


def _menu_for_prompt() -> str:
    """Compact one-line-per-dish rendering; ids must be quotable verbatim."""
    lines = [f"{item.id} | {item.name} | {item.category} | {item.price:.2f}" for item in menu.all()]
    return "\n".join(lines)


def _pending_block(pending_items: list[str] | None) -> str:
    """Tells the model what's already staged, so it can recognize approval/cancellation."""
    if not pending_items:
        return ""
    lines = "\n".join(f"- {name}" for name in pending_items)
    return (
        "\nA pending order is already staged for this table, awaiting the customer's "
        f"confirmation or cancellation:\n{lines}\n"
    )


def pending_summary(table_id: int) -> list[str]:
    """Human-readable names for whatever's currently staged, for the prompt above."""
    rows = db.list_items(table_id, "pending")
    return [f"{r['quantity']}x {r['name']}" if r["quantity"] > 1 else r["name"] for r in rows]


def build_transcriber() -> Transcriber:
    if VOICE_PROVIDER == "local":
        log.info("voice ordering: using local whisper (%s) + ollama (%s)", WHISPER_URL, OLLAMA_URL)
        return LocalTranscriber()
    transcriber = GeminiTranscriber()
    if not transcriber.available:
        log.warning("GEMINI_API_KEY not set - voice ordering disabled")
        return NullTranscriber()
    return transcriber


transcriber: Transcriber = build_transcriber()


# ------------------------------------------------------------------ resolution
def resolve_items(parsed: ParsedOrder) -> list[ResolvedLine]:
    """Re-validate the model's picks against the real menu.

    A model-supplied `menu_id` is trusted only if it actually exists;
    otherwise we fall back to matching the spoken name.
    """
    resolved: list[ResolvedLine] = []
    for raw in parsed.items:
        spoken = str(raw.get("spoken_name") or "").strip()
        item: MenuItem | None = None

        menu_id = raw.get("menu_id")
        if menu_id:
            item = menu.get(str(menu_id))
        if item is None and spoken:
            item = menu.match_by_name(spoken)

        try:
            quantity = max(1, min(int(raw.get("quantity") or 1), 99))
        except (TypeError, ValueError):
            quantity = 1

        modifiers = [str(m).strip() for m in (raw.get("modifiers") or []) if str(m).strip()][:8]

        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        resolved.append(
            ResolvedLine(
                menu_item=item,
                spoken_name=spoken,
                quantity=quantity,
                modifiers=modifiers,
                confidence=round(max(0.0, min(confidence, 1.0)), 3),
            )
        )
    return resolved


def to_staged_dict(line: ResolvedLine) -> dict | None:
    """The shape `db.replace_items` needs, or None if there's nothing sellable to stage."""
    item = line.menu_item
    if item is None or not item.available:
        return None
    return {
        "menu_id": item.id,
        "name": item.name,
        "unit_price": item.price,
        "quantity": line.quantity,
        "modifiers": line.modifiers,
        "prep_minutes": item.prep_minutes,
    }


def to_reported_dict(line: ResolvedLine) -> dict:
    """What goes back over the wire to the mic - includes unmatched lines."""
    item = line.menu_item
    return {
        "menu_id": item.id if item else None,
        "name": item.name if item else (line.spoken_name or "unknown item"),
        "spoken_name": line.spoken_name,
        "quantity": line.quantity,
        "modifiers": line.modifiers,
        "confidence": line.confidence,
        "matched": bool(item and item.available),
    }


def _broadcast_current_order(table_id: int) -> None:
    """Same event shape `POST /table/{id}/confirm` pushes - so listeners don't care who triggered it."""
    display = [serializers.display_row(r) for r in db.list_items(table_id, "current")]
    broker.publish(
        table_topic(table_id),
        {
            "type": "order.updated",
            "table": table_id,
            "version": db.get_version(table_id),
            "display": display,
        },
    )


# -------------------------------------------------------------- orchestration
def process_utterance(table_id: int, wav_bytes: bytes, device: str) -> dict:
    """Gemini -> validate against the menu -> stage matched items in pending.

    If something is already pending, the model is told so and can recognize a
    plain "yes"/"that's right" as approval - in which case this does the same
    thing as `POST /table/{id}/confirm` (merges pending into current) - or a
    plain "no"/"cancel that" as a cancellation, which clears pending the same
    way `POST /table/{id}/createPending` with an empty list would.

    An utterance where Gemini heard no order attempt, confirmation, or
    cancellation (empty `items`) leaves the table's pending order untouched,
    rather than wiping out something already staged because of a false trigger.
    """
    pending_names = pending_summary(table_id)
    parsed = transcriber.parse(wav_bytes, pending_names)

    if parsed.confirms_pending and pending_names:
        db.merge_pending_into_current(table_id)
        _broadcast_current_order(table_id)
        return {
            "transcript": parsed.transcript,
            "language": parsed.language,
            "notes": parsed.notes,
            "confirmed": True,
            "cancelled": False,
            "items": [],
            "matched_count": 0,
            "unmatched_count": 0,
            "pending": [],
        }

    if parsed.cancels_pending and pending_names:
        db.replace_items(table_id, "pending", [])
        broker.publish(
            table_topic(table_id),
            {
                "type": "voice.cancelled",
                "table": table_id,
                "device": device,
                "transcript": parsed.transcript,
                "pending": [],
            },
        )
        return {
            "transcript": parsed.transcript,
            "language": parsed.language,
            "notes": parsed.notes,
            "confirmed": False,
            "cancelled": True,
            "items": [],
            "matched_count": 0,
            "unmatched_count": 0,
            "pending": [],
        }

    resolved = resolve_items(parsed)
    reported = [to_reported_dict(r) for r in resolved]

    if parsed.items:
        staged = [d for d in (to_staged_dict(r) for r in resolved) if d is not None]
        db.replace_items(table_id, "pending", staged)
        broker.publish(
            table_topic(table_id),
            {
                "type": "voice.pending",
                "table": table_id,
                "device": device,
                "transcript": parsed.transcript,
                "pending": [serializers.display_row(r) for r in db.list_items(table_id, "pending")],
            },
        )

    return {
        "transcript": parsed.transcript,
        "language": parsed.language,
        "notes": parsed.notes,
        "confirmed": False,
        "cancelled": False,
        "items": reported,
        "matched_count": sum(1 for d in reported if d["matched"]),
        "unmatched_count": sum(1 for d in reported if not d["matched"]),
        "pending": [serializers.display_row(r) for r in db.list_items(table_id, "pending")],
    }
