# AlwaysAvailableWaitingWaiter

Built as an entry for a Seeed Studio contest, this is a little table-side gadget that lets restaurant guests order **by voice**, tracks what's coming and when, and shows it all on an e-paper display. Tap it onto a table with an NFC tag, press the button, say what you want, and it takes it from there.

<img src="pics%20&%20vids/currentmodel.jpg" width="600" alt="Current build: orange 3D-printed waiter with LED eyes and e-paper order screen"><br>
<sub>The current model — eyes, e-paper screen, and a very serious "Fake Restaurant" test menu.</sub>

## What it actually does

- **Listens.** Press the button, say your order out loud, let go. No app, no menu-fumbling.
- **Takes orders.** You can add thing to your order just by voice.
- **Knows where it lives.** Tap an NFC tag at a table and the puck reassigns itself — same hardware, any table.
- **Has feelings, sort of.** A pair of LED matrix "eyes" blink, look around, and react while it's listening or thinking.
- **Puts it on the board.** A table-side e-paper display shows the live order — items, prices, prep time — updating as dishes go out.

<p align="center">
  <video src="pics%20&%20vids/screen_video.mp4" controls width="480">
    Your viewer doesn't do inline video — <a href="pics%20&%20vids/screen_video.mp4">watch the e-paper display in action here</a>.
  </video>
</p>

## The hardware

Two custom devices, built around these core parts:

- **5.83" Monochrome eInk / ePaper Display** — 648×480 pixels, the table-side order board
- **XIAO ePaper Display Board (ESP32-S3) — EE04** — drives the display
- **reSpeaker Lite 2-Mic Array Voice Kit** — the Waiter's ears and voice

| Device | What it's for | Lives in |
|---|---|---|
| **The Waiter** | mic + speaker, NFC reader, LED "eyes", call button | [`Audio/seeedSpeaker`](Audio/seeedSpeaker) |
| **The Board** | e-paper table display showing the live order | [`Screen`](Screen) |

Under the hood there's also an NFC reader board (PN532) for the table-tap trick:

<img src="pics%20&%20vids/NFC.jpg" width="500" alt="Close-up of the PN532 NFC reader board"><br>
<sub>The part that lets the Waiter figure out which table it's sitting on.</sub>

Everything physical was designed from scratch and printed at home:

- **[`ME/`](ME)** — the enclosure: body, bell-shaped dome, arm, bottom plate (with a battery variant), all as SolidWorks parts + STL + gcode pre-sliced.
- **[`EE/button`](EE/button)** — a small custom KiCad PCB for the call button, gerbers included.

## How it works

The devices don't do the thinking themselves — they talk over Wi-Fi to a small backend that keeps track of tables, menus, and orders, and turns a voice clip into a structured order using speech recognition plus a language model on the other end (OLLAMA+whisper).
