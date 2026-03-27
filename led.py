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

# ---------------------------------------------------------------------------
# LED constants
# ---------------------------------------------------------------------------

FPS = 10                          # Event polling rate; draws only happen on dirty

# States (mirrored in main.py)
LED_STATE_SLEEPING  = "leds_off"   # Wake-word mode: eyes closed
LED_STATE_IDLE      = "leds_on"
LED_STATE_LISTENING = "leds_fading"
LED_STATE_SPEAKING  = "leds_fading"
LED_STATE_ON        = "leds_on"
LED_STATE_OFF       = "leds_off"
LED_STATE_FADING    = "leds_fading"

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
      clear()              — switch off LEDs
    """

    def __init__(self):
        self._thread     = None
        self._running    = False
        self._lock       = threading.Lock()
        self.state       = LED_STATE_SLEEPING
        self._dirty      = True
        self._LED1_pin   = 18
        self._LED2_pin   = 23
        self._event = threading.Event()


    # ------------------------------------------------------------------ API

    def start(self):
        self._running = True
        self._event.clear()
        
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        
    def stop(self):
        self._running = False
        self._event.set()

    def set_state(self, state: str):
        with self._lock:
            if self.state != state:
                self.state  = state
                self._event.set()
                self._dirty = True

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
            # Read shared state
            with self._lock:
                dirty  = self._dirty
                state  = self.state
                #text   = self._text
                #px, py = self._pupil_off
                self._dirty = False

            # Skip draw if nothing changed
            if not dirty:
                time.sleep(1/FPS)
                #clock.tick(FPS)
                continue

            # --- Illuminate LEDs depending on state ---
            if state == LED_STATE_ON:
                # Illuminated LEDs
                #print("DEBUG: LED_STATE = ON")
                GPIO.output(self._LED1_pin, GPIO.HIGH)
                GPIO.output(self._LED2_pin, GPIO.HIGH)

            elif state == LED_STATE_FADING:
                # fading LEDs
                #print("DEBUG: LED_STATE = Fading")
                threading.Thread(target=self._fade_leds, daemon=True).start()
                
            else:
                # Tuned off LEDs
                #print("DEBUG: LED_STATE = OFF")
                GPIO.output(self._LED1_pin, GPIO.LOW) # LED off
                GPIO.output(self._LED2_pin, GPIO.LOW)

                
            time.sleep(1/FPS)


    # ------------------------------------------------------------------ LEDs fading
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
                #time.sleep(0.05)
                time.sleep(0.03)
            #time.sleep(0.75)
            time.sleep(0.4)
            for dc in range(100, -1, -5):
                pwm1.ChangeDutyCycle(dc)                
                pwm2.ChangeDutyCycle(dc)
                #time.sleep(0.05)
                time.sleep(0.03)
            #time.sleep(0.75)
            time.sleep(0.4)

        self.set_state(LED_STATE_IDLE)
