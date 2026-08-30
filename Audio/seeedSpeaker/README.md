# Voice ordering mic — XIAO ESP32S3 + ReSpeaker Lite

Push-to-talk mic for the restaurant server in `../../Server`.

Hold the button on **D3**, speak, release. The clip is streamed to the server, which sends it to Gemini and stages the parsed order for confirmation. Nothing reaches the kitchen until someone confirms.

## Setup

1. Copy `src/secrets.example.h` to `src/secrets.h` and fill in Wi-Fi, the server address, and this mic's `DEVICE_NUMBER` (the table it's on is looked up at runtime via `GET /device/{DEVICE_NUMBER}`, not configured here — share the same device number as the e-paper display at that table).
2. Flash the ReSpeaker Lite with `../respeaker_lite_i2s_dfu_firmware_v1.0.9.bin` if you haven't (it must be in I2S mode — it drives BCLK/WS as master).
3. `pio run -t upload && pio device monitor`

## Audio path

The ReSpeaker gives us 32-bit stereo I2S frames at 16 kHz. The firmware takes the left channel, shifts down to 16-bit, and sends raw little-endian mono PCM — a quarter of the bytes over Wi-Fi, and exactly the format the server hands to Gemini.

```
ReSpeaker Lite ──I2S 16k/2ch/32bit──▶ ESP32S3 ──WS binary 16k/1ch/16bit──▶ Server ──WAV──▶ Gemini
```

Two details worth knowing if you touch this:

- The DMA buffer is drained continuously while idle, and flushed again on button press, so a clip starts at the press rather than a second earlier.
- A stuck button can't stream forever — capture stops at `MAX_UTTERANCE_MS` (30s), matching the server's own cap.

## Status LED

| Colour | Meaning |
|---|---|
| Red | no Wi-Fi, or socket down |
| Green | connected, idle |
| Blue | recording |
| Amber | waiting on Gemini (or: server has no API key) |
| Red flash | error — check the serial log |

The NeoPixel is optional; if `Adafruit_NeoPixel` isn't installed the firmware compiles without it and reports status over serial only.

## Serial output

```
[wifi] connected, ip=192.168.1.71
[device] mic 1 assigned to table 14
[ws] connecting to ws://192.168.1.50:8000/table/14/voice?device=mic-1
[ws] ready, table=14 voice_enabled=1
[mic] start
[mic] stop
[ws] processing 2.31s of audio
[ws] heard: two cheeseburgers no onions and a draft beer
      2x Bacon Cheeseburger
      1x Draft Beer
[ws] 2 matched, 0 unmatched, staged to pending
```

## Protocol

Documented in full in `../../Server/README.md` under "Voice ordering". Short version — client sends `{"type":"start"}`, binary PCM frames, then `{"type":"stop"}`; server replies `listening` → `processing` → `result`. Parsed items land in the table's pending order; nothing reaches the kitchen until someone calls `POST /table/{id}/confirm`.
