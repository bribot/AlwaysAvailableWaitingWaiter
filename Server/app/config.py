"""Runtime configuration, all overridable through environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))

DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "restaurant.db"))
MENU_CSV = Path(os.environ.get("MENU_CSV", DATA_DIR / "menu.csv"))

# Optional shared secret. When set, every request must carry `X-API-Key`.
# Leave unset on a trusted LAN so the ESP32 firmware stays simple.
API_KEY = os.environ.get("API_KEY") or None

# How many item rows the e-paper layout can physically render.
DISPLAY_ROWS = int(os.environ.get("DISPLAY_ROWS", "6"))

# Character budgets for the e-paper strings (font size 2 on a 648px wide panel).
DISPLAY_NAME_CHARS = int(os.environ.get("DISPLAY_NAME_CHARS", "22"))
DISPLAY_DETAIL_CHARS = int(os.environ.get("DISPLAY_DETAIL_CHARS", "44"))

# Number of physical e-paper devices in service. Each is assigned a table via
# PUT /device/{device_number}.
MAX_DEVICES = int(os.environ.get("MAX_DEVICES", "3"))

# --- voice ordering ---
# Without a key, voice ordering is disabled and /table/{id}/voice returns an
# error - the rest of the API is unaffected.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or None
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_TIMEOUT = float(os.environ.get("GEMINI_TIMEOUT", "20"))

# The mic board streams 16-bit mono PCM at this rate - must match the
# firmware's fixed sample rate, nothing here enforces that at runtime.
SAMPLE_RATE = int(os.environ.get("SAMPLE_RATE", "16000"))

# "gemini" (default) or "local" - swaps the whole Transcriber implementation,
# see voice.build_transcriber(). The local path needs the whisper+ollama
# services from docker-compose.yml's "local-ai" profile.
VOICE_PROVIDER = os.environ.get("VOICE_PROVIDER", "gemini")
WHISPER_URL = os.environ.get("WHISPER_URL", "http://localhost:9000")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
# Ollama (and to a lesser extent whisper) unloads an idle model after a few
# minutes, so the first request after a lull has to reload it from disk into
# VRAM - that alone can take longer than GEMINI_TIMEOUT. Give the local
# pipeline its own, more generous budget.
LOCAL_TIMEOUT = float(os.environ.get("LOCAL_TIMEOUT", "90"))
# How long Ollama keeps the model loaded in VRAM after a request.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
