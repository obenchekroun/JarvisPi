"""
JarvisPi LED
==================
LED as eyes for the JarvisPi voice assistant, used to display current status

Rendering :
  - LEDs off when sleeping
  - LEDs on when waking up/awaiting user speech
  - LEDs fading when streaming response from OpenAI websocket


States:
  LED_STATE_SLEEPING  — LEDs off
  LED_STATE_IDLE      — LEDs on
  LED_STATE_LISTENING — LEDs on
  LED_STATE_SPEAKING  — LEDs fading
  LED_STATE_ON        — LEDs on
  LED_STATE_OFF       — LEDs off
  LED_STATE_FADING    — LEDs fading

"""

import os
import RPi.GPIO as GPIO
import threading
import time

# # Try display drivers in priority order.
# # If $DISPLAY is set (X11 session running), prefer x11/wayland first.
# _DRIVERS = ["kmsdrm", "fbcon", "x11", "wayland", "directfb"]
# if os.environ.get("DISPLAY"):
#     _DRIVERS = ["x11", "wayland"] + _DRIVERS

# import pygame

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

# RENDER_W, RENDER_H = 200, 120   # Internal canvas (eyes, 4x upscaled)
# OUT_W,    OUT_H    = 800, 480   # Output resolution
# FPS = 6                          # Event polling rate; draws only happen on dirty

# # Layout
# EYE_AREA_H  = 65    # Height of eye area in the 200x120 canvas
# TEXT_AREA_Y = 280   # Y start of text area on the 800x480 output

# # Colour palette
# DARK_BG    = (10,  28,  10)    # Dark green background (full screen)
# EYE_RING   = (30,  80,  30)    # Outer eye ring
# EYE_MID    = (60, 150,  50)    # Iris mid tone
# EYE_BRIGHT = (140, 220, 100)   # Bright iris inner area
# PUPIL_COL  = (5,   15,   5)    # Pupil (near black)
# HIGHLIGHT  = (220, 255, 200)   # Pupil highlight dot
# TEXT_COL   = (140, 220, 100)   # Active text (matches iris bright)
# TEXT_DIM   = (50,  100,  40)   # Dimmed text (idle state)
# SEP_COL    = (30,   70,  30)   # Separator line between eye/text areas

# # Eye geometry (canvas coordinates)
# L_EYE           = (58,  33)
# R_EYE           = (142, 33)
# EYE_RX, EYE_RY  = 22, 18       # Eye ellipse semi-axes
# PUPIL_R         = 7             # Pupil radius
# MAX_PUPIL_OFFSET = 6            # Max random pupil displacement in px

# # Korean font path (installed via `sudo apt install fonts-nanum`)
# _KOREAN_FONT = "/usr/share/fonts/truetype/nanum/NanumSquareRoundR.ttf"

# States (mirrored in main.py)
LED_STATE_SLEEPING  = "leds_off"   # Wake-word mode: eyes closed
LED_STATE_IDLE      = "leds_on"
LED_STATE_LISTENING = "leds_on"
LED_STATE_SPEAKING  = "leds_fading"
LED_STATE_ON        = "leds_on"
LED_STATE_OFF       = "leds_off"
LED_STATE_FADING    = "leds_fading"

# # ---------------------------------------------------------------------------
# # Drawing helpers
# # ---------------------------------------------------------------------------

# def _draw_eye(surf, cx: int, cy: int, px_off: int, py_off: int):
#     """
#     Draw one pixel-art eye on surf.

#     Args:
#         cx, cy:       Eye centre (canvas coordinates)
#         px_off/py_off: Pupil displacement from centre
#     """
#     # Outer dark ring
#     pygame.draw.ellipse(surf, EYE_RING,
#                         (cx-EYE_RX-3, cy-EYE_RY-3,
#                          (EYE_RX+3)*2, (EYE_RY+3)*2))
#     # Iris base
#     pygame.draw.ellipse(surf, EYE_MID,
#                         (cx-EYE_RX, cy-EYE_RY,
#                          EYE_RX*2, EYE_RY*2))
#     # Bright iris inner fill
#     pygame.draw.ellipse(surf, EYE_BRIGHT,
#                         (cx-EYE_RX+3, cy-EYE_RY+3,
#                          (EYE_RX-3)*2, (EYE_RY-3)*2))
#     # Pupil (displaced)
#     pygame.draw.circle(surf, PUPIL_COL,
#                        (cx + px_off, cy + py_off), PUPIL_R)
#     # Specular highlight (fixed relative to pupil)
#     pygame.draw.circle(surf, HIGHLIGHT,
#                        (cx + px_off - 3, cy + py_off - 3), 2)


# ---------------------------------------------------------------------------
# EyeDisplay class
# ---------------------------------------------------------------------------

class LEDDisplay:
    """
    Manages the LEDs eyes in a background daemon thread.

    Public API (thread-safe):
      start()              — launch the LEDs thread
      stop()               — signal the thread to exit
      set_state(state)     — switch between on / off / fading
      clear()         — switch off LEDs
    """

    def __init__(self):
        self._thread     = None
        self._running    = False
        self._lock       = threading.Lock()
        self.state       = LED_STATE_IDLE
        #self._text       = ""
        self._dirty      = True
        self._LED1_pin   = 18
        self._LED2_pin   = 23
        self._event = threading.Event()
        #self._pupil_off  = (0, 0)
        #self._next_look  = time.time() + 2.0  # when to next shift pupils


    # ------------------------------------------------------------------ API

    def start(self):
        self._running = True
        self._event.clear()
        
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

#### ICI à continuer
        
    def stop(self):
        self._running = False
        self._event.set()

    def set_state(self, state: str):
        with self._lock:
            if self.state != state:
                self.state  = state
                self._event.set()
                self._dirty = True

    # def set_text(self, text: str):
    #     """Replace the full text (called at end of AI response)."""
    #     with self._lock:
    #         self._text  = text
    #         self._dirty = True

    # def append_text(self, chunk: str):
    #     """Append a streaming chunk (called on response.audio_transcript.delta)."""
    #     with self._lock:
    #         self._text += chunk
    #         self._dirty = True

    # def clear_text(self):
    #     with self._lock:
    #         self._text  = ""
    #         self._dirty = True

    def clear(self):
        with self._lock:
            self.state  = LED_STATE_OFF
            self._event.set()
            self._dirty = True

    # ------------------------------------------------------------------ Main loop

    def _run(self):
        """Display thread: initialise pygame, then render loop."""
        leds = None
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)

            GPIO.setup(self._LED1_pin, GPIO.OUT)
            GPIO.output(self._LED1_pin, GPIO.LOW)

            GPIO.setup(self._LED2_pin, GPIO.OUT)
            GPIO.output(self._LED2_pin, GPIO.LOW)
            leds = True
        except Exception:
            leds = None

        if leds is None:
            print("DEBUG LEDs: LEDs initialisation failed.", flush=True)
            return

        while self._running:
            # Handle window/keyboard events
            # for ev in pygame.event.get():
            #     if ev.type == pygame.QUIT:
            #         self._running = False
            #     elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            #         self._running = False

            # Pupil wander — shift every 3–6 s, costs one redraw
            # now = time.time()
            # with self._lock:
            #     if now >= self._next_look:
            #         self._pupil_off = (
            #             random.randint(-MAX_PUPIL_OFFSET, MAX_PUPIL_OFFSET),
            #             random.randint(-MAX_PUPIL_OFFSET, MAX_PUPIL_OFFSET),
            #         )
            #         self._next_look = now + random.uniform(3.0, 6.0)
            #         self._dirty     = True

            # Read shared state
            with self._lock:
                dirty  = self._dirty
                state  = self.state
                #text   = self._text
                #px, py = self._pupil_off
                self._dirty = False

            # Skip draw if nothing changed
            if not dirty:
                time.sleep(0.1)
                #clock.tick(FPS)
                continue

            # --- Draw eye canvas (200x120, pixelated) ---
            # canvas.fill(DARK_BG)
            # pygame.draw.line(canvas, SEP_COL,
            #                  (0, EYE_AREA_H), (RENDER_W, EYE_AREA_H), 1)
            if state == LED_STATE_ON:
                # Closed eyes: horizontal lines (eyelids shut)
                GPIO.output(self._LED1_pin, GPIO.HIGH) # LED on
                GPIO.output(self._LED2_pin, GPIO.HIGH) # LED on
                # for cx, cy in (L_EYE, R_EYE):
                #     pygame.draw.line(canvas, EYE_MID,
                #                      (cx - EYE_RX, cy), (cx + EYE_RX, cy), 3)
            elif state == LED_STATE_FADING:
                t_fade = threading.Thread(target=self._fade_leds, args=(self._event,))
                t_fade.start()
                #draw_eye(canvas, L_EYE[0], L_EYE[1], px, py)
                #draw_eye(canvas, R_EYE[0], R_EYE[1], px, py)
                
            else:
                GPIO.output(self._LED1_pin, GPIO.HIGH) # LED on
                GPIO.output(self._LED2_pin, GPIO.HIGH) # LED on

            # # --- Upscale to 800x480 ---
            # scaled = pygame.transform.scale(canvas, (OUT_W, OUT_H))
            # screen.blit(scaled, (0, 0))

            # # --- Render text at full resolution (sharp) ---
            # if text:
            #     col = TEXT_COL if state != STATE_IDLE else TEXT_DIM
            #     self._draw_text(screen, font, text, col)

            #pygame.display.flip()
            #clock.tick(FPS)
            time.sleep(0.1)

        #pygame.quit()

    # ------------------------------------------------------------------ Text rendering

    # def _draw_text(self, surf, font, text: str, color):
    #     """
    #     Word-wrap text and centre it in the lower text area.
    #     Renders directly on the 800x480 surface for sharp output.
    #     Shows the last 3 lines if text overflows.
    #     """
    #     max_w = OUT_W - 60
    #     words = text.split()
    #     lines = []
    #     line  = ""
    #     for w in words:
    #         test = f"{line} {w}".strip()
    #         if font.size(test)[0] <= max_w:
    #             line = test
    #         else:
    #             if line:
    #                 lines.append(line)
    #             line = w
    #     if line:
    #         lines.append(line)

    #     lines   = lines[-3:]    # keep last 3 lines
    #     line_h  = 48
    #     total_h = len(lines) * line_h
    #     y = TEXT_AREA_Y + (OUT_H - TEXT_AREA_Y - total_h) // 2

    #     for ln in lines:
    #         rendered = font.render(ln, True, color)
    #         x = (OUT_W - rendered.get_width()) // 2
    #         surf.blit(rendered, (x, y))
    #         y += line_h
    
    # ------------------------------------------------------------------ Text rendering
    def _fade_leds(self):
        pwm1 = GPIO.PWM(self._LED1_pin, 200)
        pwm2 = GPIO.PWM(self._LED2_pin, 200)

        self._event.clear()

        while not self._event.is_set():
            pwm1.start(0)
            pwm2.start(0)
            for dc in range(0, 101, 5):
                pwm1.ChangeDutyCycle(dc)  
                pwm2.ChangeDutyCycle(dc)
                time.sleep(0.05)
            time.sleep(0.75)
            for dc in range(100, -1, -5):
                pwm1.ChangeDutyCycle(dc)                
                pwm2.ChangeDutyCycle(dc)
                time.sleep(0.05)
            time.sleep(0.75)
