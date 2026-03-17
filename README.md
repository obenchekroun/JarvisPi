# ElevenLexa

A real-time AI voice assistant running on a **Raspberry Pi Zero 1.1**, powered by the **OpenAI Realtime API**. It listens through a Bluetooth microphone, responds via a Bluetooth speaker, and shows an animated robot face on an HDMI display.

```
┌─────────────────────────────────────┐
│  ◉ ◉  (eyes)                        │
│─────────────────────────────────────│
│   Hello! How can I help you today?  │
└─────────────────────────────────────┘
```

---

## Features

- **Fully voice-driven** — no keyboard, no touch input
- **Real-time conversation** via OpenAI `gpt-4o-realtime-preview`
- **Multilingual** — responds in English or Korean depending on the speaker
- **Retro robot face** on HDMI display with animated pixel eyes and live speech-to-text
- **Echo prevention** — mic is muted while the AI speaks, echo buffer is flushed after
- **Auto-reconnect** — transparently reconnects if the WebSocket drops
- **Graceful degradation** — runs headless (no display) without any code changes

---

## Hardware

| Component | Details |
|---|---|
| **Computer** | Raspberry Pi Zero 1.1 (single-core ARMv6 @ 1 GHz, 512 MB RAM) |
| **Speaker + Mic** | Soundcore Mini (Bluetooth) |
| **Display** | Any 800×480 HDMI screen |
| **Audio server** | PipeWire |
| **OS** | Raspberry Pi OS (Bookworm) |

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Microphone (BT HFP)                                     │
│       │                                                  │
│  pacat --record (PipeWire)                               │
│       │                                                  │
│  AudioRecorder.audio_queue ──► send_audio() ──► OpenAI  │
│                                                    │      │
│  AudioPlayer ◄── receive_events() ◄────────────────┘     │
│       │                                                  │
│  pacat --playback (PipeWire)                             │
│       │                                                  │
│  Speaker (BT A2DP)           EyeDisplay (HDMI, pygame)  │
└──────────────────────────────────────────────────────────┘
```

**`main.py`** — WebSocket session, audio I/O, event handling
**`display.py`** — Pygame rendering loop (daemon thread)

---

## Design Decisions

### Why OpenAI Realtime API?

The OpenAI Realtime API provides speech-to-text, language model inference, and text-to-speech in a single persistent WebSocket connection. This eliminates the need to chain three separate services (Whisper → GPT → TTS) and dramatically reduces latency. It also handles voice activity detection (VAD) server-side, so no local VAD library is needed.

Previous versions used ElevenLabs Conversational AI, which was replaced because the API quota ran out quickly and the OpenAI Realtime API offers a more integrated pipeline at a lower per-minute cost.

### Why PipeWire instead of ALSA or PulseAudio?

Raspberry Pi OS Bookworm ships PipeWire as the default audio server. It handles Bluetooth profile switching (A2DP ↔ HFP), resampling, and the echo-cancel module transparently — without requiring any manual ALSA configuration. `pacat` (PulseAudio-compatible client) works directly against PipeWire via its PulseAudio compatibility layer.

### Why pacat instead of a Python audio library?

Python audio libraries (PyAudio, sounddevice) require compiled native extensions and often have dependency conflicts on Raspberry Pi OS. `pacat` is a standard system tool, always available where PipeWire/PulseAudio is installed. It communicates via subprocess stdin/stdout, which is reliable, portable, and adds no Python dependencies.

### Echo prevention strategy

The Soundcore Mini's microphone (HFP) is physically close to its own speaker, making acoustic echo a serious problem. When the AI starts speaking, the microphone is muted in software (`recorder.muted = True`). After the AI finishes:

1. A 2.5-second silence allows the room echo to decay.
2. The microphone queue is flushed to discard any residual echo already captured.
3. The microphone is unmuted.

### Why 200×120 internal canvas for the display?

This project runs on a **Raspberry Pi Zero 1.1** — the original single-core ARMv6 @ 1 GHz with only 512 MB RAM and no dedicated GPU. This is one of the most constrained single-board computers available. Rendering at full 800×480 resolution every frame would saturate the CPU and starve the audio pipeline and WebSocket I/O.

By rendering the animated elements (eyes, background) on a 200×120 surface and scaling it up 4× with `pygame.transform.scale`, the number of pixels touched per frame is reduced to 6.25% of the full-resolution equivalent. Combined with dirty-flag rendering (see below), the display thread consumes a negligible fraction of the CPU.

The 4× upscale is intentional — it creates a visible pixel grid that gives the robot face a retro LED-matrix aesthetic, which fits the hardware concept.

### Why only redraw on dirty?

At idle the display does not change. Continuously calling `pygame.display.flip()` at 60 FPS would waste CPU cycles that the audio pipeline needs. The `_dirty` flag ensures the screen is only redrawn when something actually changes: new text chunk, pupil position shift, or state transition. At idle between responses, the render loop runs at 6 FPS for event polling only, and `display.flip()` is never called.

### Why render text at full 800×480 resolution?

The 4× upscale that makes eyes look retro makes small text unreadable — each font pixel becomes a 4×4 block. Text is therefore rendered directly on the output surface at full resolution, bypassing the canvas scaling. This gives sharp, readable text while preserving the pixelated eye aesthetic.

### Why NanumSquareRound for the font?

The assistant responds in English and Korean. Pygame's built-in font (`pygame.font.Font(None, ...)`) does not include Hangul glyphs, so Korean text would render as boxes. `NanumSquareRound` (from the `fonts-nanum` package) covers the full Hangul syllable block and Latin characters in one file. It also has a clean, rounded look that matches the robot display aesthetic.

### Why server-side VAD instead of local VAD?

Local VAD libraries (webrtcvad, silero-vad) require additional CPU cycles and careful tuning on embedded hardware. The OpenAI Realtime API's built-in server VAD (`turn_detection: server_vad`) runs in the cloud and handles turn detection reliably without consuming Pi resources. The tradeoff is a small additional network round-trip, which is acceptable for a conversational assistant.

### Why asyncio + threads instead of a fully async approach?

Audio I/O via subprocess (pacat) is inherently blocking. Running blocking reads in `asyncio.run_in_executor()` offloads them to a thread pool without blocking the event loop, allowing the WebSocket send/receive coroutines to run concurrently. The display runs in its own daemon thread since pygame's event loop is not asyncio-compatible.

---

## Setup

### 1. System packages

```bash
sudo apt update
sudo apt install -y pipewire pipewire-pulse fonts-nanum python3-pygame
```

### 2. PipeWire echo cancellation (optional)

The software already mutes the microphone while the AI speaks. If you still experience echo, you can load PipeWire's WebRTC AEC module as an extra layer:

```bash
pactl load-module module-echo-cancel aec_method=webrtc source_name=echo_cancel_source
```

To load it automatically, add to `~/.config/pipewire/pipewire.conf.d/echo-cancel.conf` or call it from a systemd user service.

### 3. Python dependencies

```bash
pip3 install --break-system-packages -r requirements.txt
```

### 4. Bluetooth pairing (Soundcore Mini)

```bash
bluetoothctl
  power on
  scan on
  pair <MAC>
  trust <MAC>
  connect <MAC>
```

### 5. Configuration

Edit `main.py` and set:

```python
OPENAI_API_KEY = "sk-..."   # Your OpenAI API key
VOICE = "verse"             # AI voice (alloy, echo, nova, shimmer, verse, ...)
INSTRUCTIONS = "..."        # System prompt / personality
```

### 6. Run

```bash
python3 main.py
```

---

## File Structure

```
ElevenLexa/
├── main.py           # Main application — audio pipeline + OpenAI session
├── display.py        # Pygame HDMI display (optional, auto-detected)
├── requirements.txt  # Python dependencies
├── README.md         # This file
└── archive/          # Old debug scripts and patches (not needed for running)
```

---

## Customisation

| What | Where | How |
|---|---|---|
| AI personality | `main.py → INSTRUCTIONS` | Edit the system prompt string |
| Voice | `main.py → VOICE` | Any OpenAI Realtime voice name |
| Languages | `main.py → INSTRUCTIONS` | Change language instructions |
| VAD sensitivity | `main.py → turn_detection.threshold` | 0.0–1.0, lower = more sensitive |
| Eye colours | `display.py → colour constants` | RGB tuples at the top of the file |
| Display layout | `display.py → EYE_AREA_H, TEXT_AREA_Y` | Adjust the split point |

---

## License

MIT — free to use, modify, and distribute.
