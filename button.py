#!/usr/bin/env python3
"""
Floating walkie-talkie button to talk to Claude Code by voice.

Press and hold -> records. Release -> transcribes, then sends it to a fast
Claude Code session (Sonnet, no extra memory/MCP/skills) with free
permission to use xdotool/import via Bash. Claude itself decides, per turn,
whether it needs to look at the screen -- there's no automatic screenshot on
every turn, only when it actually takes one. It reads the reply out loud
with edge-tts while the orb moves at the real rhythm of the audio (not a
decorative animation).

Pressing while it's speaking or thinking interrupts it instantly and starts
recording a new question (barge-in).
"""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

import audioop
import cairo
import json
import math
import os
import random
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

HOME = os.path.expanduser("~")
ASSISTANT_DIR = os.path.join(HOME, ".claude", "floating-assistant")
LOG_PATH = os.path.join(ASSISTANT_DIR, "assistant.log")

EDGE_TTS = os.path.join(ASSISTANT_DIR, "venv", "bin", "edge-tts")
VOICE = "es-ES-ElviraNeural"
RATE = "+8%"

# Fixed path Claude is told to use when IT decides a turn needs to see the
# screen. We never take this screenshot ourselves -- we only watch this path
# for changes while Claude is working, so the red flash fires exactly when
# (and only when) a capture actually happens.
SCREENSHOT_PATH = os.path.join(ASSISTANT_DIR, "current_screen.png")

# ---- filler speech, so a slow turn isn't dead silence ----------------------
# We don't try to predict how long Claude will take (that's guessing before
# there's anything to measure). We measure the actual silence instead: if the
# reply still isn't ready after OPENER_DELAY, say something; then keep dropping
# short connectors at random gaps until the real answer is ready.
FILLER_DIR = os.path.join(ASSISTANT_DIR, "fillers")
OPENER_DELAY = 1.8          # seconds of silence before the first filler
CONNECTOR_GAP = (2.5, 5.0)  # random pause between connectors, so silence stays short

# Note on spelling: this voice reads a bare "Mmm" letter by letter ("eme eme
# eme"), and "Uhm" comes out as "un". Only "Hmm"/"Hmmm" produce an actual hum,
# so those are the only non-word fillers used here. Verified by synthesizing
# each line and transcribing it back.
OPENERS = [
    "Hmm, déjame pensar.",
    "A ver, dame un segundo.",
    "Buena pregunta, lo estoy mirando.",
    "Hmm, esto necesita un momento.",
    "Espera, que lo reviso.",
    "Dame un momentito.",
    "A ver qué encuentro.",
    "Uy, esto lleva un poco más.",
    "Estoy mirándolo, un segundo.",
    "Hmmm, a ver.",
    "Déjame revisar eso.",
    "Un momento que lo compruebo.",
    "Vale, estoy en ello.",
    "Eso me lleva un ratito, espera.",
    "Lo estoy viendo ahora.",
]

CONNECTORS = [
    "Hmm.",
    "A ver.",
    "Sigo en ello.",
    "Un poco más.",
    "Esto es más complejo de lo que parecía.",
    "Ya casi lo tengo.",
    "Dame un segundo más.",
    "Hmmm, a ver.",
    "Sigo mirando.",
    "Un momentito más.",
    "Está tardando un poco.",
    "Ya voy.",
    "Aquí sigo.",
    "Vale, ya casi.",
    "Un segundo.",
    "Esto tiene más tela.",
    "Sigo buscando.",
    "Ya mismo.",
    "Aguanta un poco.",
    "Hmm, casi está.",
]

import re as _re
import unicodedata as _unicodedata


def clean_for_speech(text):
    """Strips what can't be read out loud (markdown, links, odd symbols).
    This is a safety net: the system prompt already asks Claude for plain
    text, this catches whatever slips through."""
    t = text
    t = _re.sub(r"```.*?```", " ", t, flags=_re.S)
    t = _re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = _re.sub(r"https?://\S+", " ", t)
    t = t.replace("**", "").replace("__", "")
    t = _re.sub(r"`([^`]*)`", r"\1", t)
    t = t.replace("→", ", ").replace("←", " from ")
    t = "".join(c for c in t if _unicodedata.category(c)[0] != "S" or c in "+=<>")
    t = _re.sub(r"[ \t]+", " ", t).strip()
    return t

SIZE = 96
BASE_R = 30

COLORS = {
    "idle":      (0.35, 0.42, 0.50),
    "recording": (0.78, 0.20, 0.18),
    "thinking":  (0.80, 0.47, 0.13),
    "speaking":  (0.14, 0.55, 0.42),
}

# Flags that strip everything not needed for a quick voice question
# (global memory, MCP servers, skill listing, Chrome integration):
# this is what brings a turn down from ~12-14s (Opus, full context) to ~3-7s.
FAST_FLAGS = [
    "--model", "sonnet",
    "--setting-sources", "",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--no-chrome",
]

SYSTEM_PROMPT = (
    "You are a floating voice assistant on the user's Ubuntu desktop. "
    "On every turn you get what they said by voice. You do NOT get a screenshot automatically -- "
    "decide for yourself, per turn, whether the question needs to see their screen. Most short "
    "voice questions don't. Only when it genuinely does (they say things like 'this', 'what I have "
    "open', 'what I'm looking at', ask about something visual, or you simply can't answer well from "
    "the words alone), take one yourself by running this exact command via Bash: "
    f"import -window root -resize 1280x {SCREENSHOT_PATH} -- then read that file with your file-reading "
    "tool before answering. Skip this entirely for anything you can already answer from the spoken text. "
    "You have xdotool (move/click the mouse, type, press keys) and import (screenshots) available via Bash, "
    "with full freedom to use them without asking for confirmation whenever it helps answer or act for the user. "
    "Always reply in Spanish, since the user speaks Spanish. "
    "Always answer very briefly -- 1 to 3 sentences -- because this gets read out loud, not read on screen. "
    "And always answer EXTREMELY CLEARLY: as if explaining to a junior person who just joined the company "
    "today and knows nothing about the project. Never assume they know class names, team acronyms, or "
    "internal jargon without explaining it in the same sentence. Simple words, one idea per sentence, "
    "zero ambiguity. Short and crystal-clear matter equally: never trade one off for the other. "
    "Never use markdown, backticks, asterisks or code blocks: plain speakable text only. "
    "Do not add any summary line or the RESUMEN: label in this tool."
)


def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


class CaptureFlash(Gtk.Window):
    """Red frame covering the whole screen for half a second right after
    taking a screenshot, so it's clear exactly when Claude "looked". It is
    drawn AFTER capturing (never before), so the frame never contaminates
    the capture itself. It doesn't block clicks: the area is click-through."""

    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_app_paintable(True)
        self.set_skip_taskbar_hint(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        geo = Gdk.Display.get_default().get_monitor(0).get_geometry()
        self.set_default_size(geo.width, geo.height)
        self.move(0, 0)
        self.connect("draw", self._on_draw)
        self.connect("realize", self._on_realize)

    def _on_realize(self, widget):
        win = self.get_window()
        if win:
            win.input_shape_combine_region(cairo.Region(), 0, 0)

    def _on_draw(self, widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        border = 14
        cr.set_source_rgba(0.88, 0.12, 0.12, 0.85)
        cr.set_line_width(border)
        cr.rectangle(border / 2, border / 2, w - border, h - border)
        cr.stroke()
        return False


def flash_capture_indicator(duration_ms=420):
    overlay = CaptureFlash()
    overlay.show_all()
    GLib.timeout_add(duration_ms, lambda: (overlay.destroy(), False)[1])


def _filler_path(kind, index):
    return os.path.join(FILLER_DIR, f"{kind}_{index:02d}.mp3")


def ensure_fillers():
    """Renders the filler phrases to mp3 once, in parallel. Re-renders only if
    the phrase list changed (tracked by a manifest), so startup is free after
    the first run."""
    os.makedirs(FILLER_DIR, exist_ok=True)
    manifest_path = os.path.join(FILLER_DIR, "phrases.json")
    wanted = {"openers": OPENERS, "connectors": CONNECTORS}
    try:
        with open(manifest_path, encoding="utf-8") as f:
            if json.load(f) == wanted and all(
                os.path.exists(_filler_path(k[:-1], i))
                for k, items in wanted.items()
                for i in range(len(items))
            ):
                return
    except Exception:
        pass

    jobs = []
    for kind, phrases in (("opener", OPENERS), ("connector", CONNECTORS)):
        for i, phrase in enumerate(phrases):
            jobs.append((kind, i, phrase))

    def render(job):
        kind, i, phrase = job
        txt = tempfile.mktemp(suffix=".txt", dir=FILLER_DIR)
        try:
            with open(txt, "w", encoding="utf-8") as f:
                f.write(phrase)
            subprocess.run(
                [EDGE_TTS, "--voice", VOICE, f"--rate={RATE}", "--file", txt,
                 "--write-media", _filler_path(kind, i)],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
        finally:
            try:
                os.remove(txt)
            except OSError:
                pass

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(render, jobs))
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(wanted, f, ensure_ascii=False)
    log(f"fillers rendered ({len(jobs)} phrases, {time.time()-t0:.1f}s)")


class FillerPlayer:
    """Speaks short filler lines while Claude is still thinking, so a slow turn
    isn't dead air. Stops the instant the real reply is ready (or the user
    interrupts): it never overlaps with the actual answer."""

    def __init__(self):
        self.active = False
        self.proc = None
        self._last = {}

    def _pick(self, kind, phrases):
        """Random, but never the same line twice in a row."""
        choices = [i for i in range(len(phrases)) if i != self._last.get(kind)]
        idx = random.choice(choices or list(range(len(phrases))))
        self._last[kind] = idx
        return _filler_path(kind, idx)

    def _play(self, path):
        if not self.active or not os.path.exists(path):
            return
        self.proc = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
            stdin=subprocess.DEVNULL,
        )
        self.proc.wait()
        self.proc = None

    def _run(self):
        time.sleep(OPENER_DELAY)
        if not self.active:
            return
        self._play(self._pick("opener", OPENERS))
        while self.active:
            gap = random.uniform(*CONNECTOR_GAP)
            waited = 0.0
            while self.active and waited < gap:
                time.sleep(0.15)
                waited += 0.15
            if not self.active:
                return
            self._play(self._pick("connector", CONNECTORS))

    def start(self):
        if self.active:
            return
        self.active = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self, wait=False):
        """wait=True lets the line currently playing finish (so the real answer
        doesn't cut it off mid-word); wait=False kills it instantly, which is
        what a user interruption needs."""
        self.active = False
        proc = self.proc
        if proc and proc.poll() is None:
            if wait:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.terminate()
            else:
                proc.terminate()
        self.proc = None


def compute_envelope(mp3_path, window_ms=60, sample_rate=16000):
    """Real volume envelope of the audio (RMS per window), normalized 0..1.
    Lets the orb move with the actual voice, not with a made-up pattern."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", mp3_path, "-ar", str(sample_rate), "-ac", "1", "-f", "s16le", "pipe:1"],
            capture_output=True, timeout=15,
        )
        raw = proc.stdout
    except Exception:
        return [0.0], window_ms
    bytes_per_sample = 2
    window_bytes = int(sample_rate * window_ms / 1000) * bytes_per_sample
    values = []
    for i in range(0, len(raw) - window_bytes, window_bytes):
        chunk = raw[i:i + window_bytes]
        values.append(audioop.rms(chunk, 2))
    if not values:
        return [0.0], window_ms
    peak = max(values) or 1
    return [v / peak for v in values], window_ms


class Ripple:
    __slots__ = ("born",)

    def __init__(self):
        self.born = time.time()

    def age(self):
        return time.time() - self.born

    def alive(self, lifetime):
        return self.age() < lifetime


class Orb(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(SIZE, SIZE)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.state = "idle"
        self.hover = False
        self.pressed = False
        self.t0 = time.time()
        self.ripples = []
        self.last_ripple = 0
        self.envelope = [0.0]
        self.envelope_step_ms = 60
        self.speak_start = None
        self.connect("draw", self.on_draw)
        self.connect("enter-notify-event", lambda w, e: self._set_hover(True))
        self.connect("leave-notify-event", lambda w, e: self._set_hover(False))
        GLib.timeout_add(33, self._tick)

    def _set_hover(self, v):
        self.hover = v
        self.queue_draw()

    def set_state(self, state):
        self.state = state
        self.ripples = []
        if state != "speaking":
            self.speak_start = None
        self.queue_draw()

    def start_speaking(self, envelope, step_ms):
        self.envelope = envelope
        self.envelope_step_ms = step_ms
        self.speak_start = time.time()

    def _tick(self):
        now = time.time()
        if self.state == "recording" and now - self.last_ripple > 0.75:
            self.ripples.append(Ripple())
            self.last_ripple = now
        self.ripples = [r for r in self.ripples if r.alive(1.6)]
        self.queue_draw()
        return True

    def _current_amplitude(self):
        if not self.speak_start:
            return 0.0
        elapsed_ms = (time.time() - self.speak_start) * 1000
        idx = int(elapsed_ms / self.envelope_step_ms)
        if idx < 0 or idx >= len(self.envelope):
            return 0.0
        return self.envelope[idx]

    def on_draw(self, widget, cr):
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        cx, cy = w / 2, h / 2
        t = time.time() - self.t0

        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.restore()

        r, g, b = COLORS.get(self.state, COLORS["idle"])

        if self.state == "idle":
            pulse = (math.sin(t * 1.3) + 1) / 2
            radius = BASE_R + pulse * 2.5
            fill_a = 0.72 + pulse * 0.08
        elif self.state == "recording":
            pulse = (math.sin(t * 7.0) + 1) / 2
            radius = BASE_R + 1.5 + pulse * 3.5
            fill_a = 0.88
        elif self.state == "thinking":
            pulse = (math.sin(t * 2.2) + 1) / 2
            radius = BASE_R + pulse * 2.0
            fill_a = 0.80
        else:  # speaking -- the radius breathes with the REAL volume of the audio
            amp = self._current_amplitude()
            radius = BASE_R + 1.5 + amp * 9.0
            fill_a = 0.82 + amp * 0.15

        if self.hover and self.state == "idle":
            radius += 2
        if self.pressed:
            radius -= 2

        if self.state == "recording":
            for rip in self.ripples:
                age = rip.age()
                life = 1.6
                k = age / life
                rr = radius + k * 34
                alpha = max(0.0, (1 - k) * 0.45)
                cr.set_source_rgba(r, g, b, alpha)
                cr.set_line_width(2.4)
                cr.arc(cx, cy, rr, 0, 2 * math.pi)
                cr.stroke()

        glow_layers = 5
        for i in range(glow_layers, 0, -1):
            k = i / glow_layers
            cr.set_source_rgba(r, g, b, 0.05 * (1 - k * 0.4))
            cr.arc(cx, cy, radius + i * 3.5, 0, 2 * math.pi)
            cr.fill()

        for i in range(4, 0, -1):
            cr.set_source_rgba(0, 0, 0, 0.035)
            cr.arc(cx, cy + 1.5, radius + i * 1.8, 0, 2 * math.pi)
            cr.fill()

        grad = cairo.RadialGradient(cx - radius * 0.3, cy - radius * 0.3, radius * 0.1, cx, cy, radius)
        grad.add_color_stop_rgba(0, min(r + 0.18, 1), min(g + 0.18, 1), min(b + 0.18, 1), fill_a)
        grad.add_color_stop_rgba(1, r, g, b, fill_a)
        cr.set_source(grad)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.set_line_width(1.4)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.stroke()

        if self.state == "thinking":
            self._draw_dots(cr, cx, cy, t)
        elif self.state == "recording":
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.arc(cx, cy, 7, 0, 2 * math.pi)
            cr.fill()
        elif self.state == "speaking":
            self._draw_bars(cr, cx, cy)
        else:
            self._draw_mic(cr, cx, cy)

        return False

    def _draw_dots(self, cr, cx, cy, t):
        spacing = 11
        for i in range(3):
            phase = t * 5.0 - i * 0.9
            bounce = max(0, math.sin(phase))
            a = 0.35 + bounce * 0.65
            x = cx - spacing + i * spacing
            cr.set_source_rgba(1, 1, 1, a)
            cr.arc(x, cy, 3.4, 0, 2 * math.pi)
            cr.fill()

    def _draw_bars(self, cr, cx, cy):
        # Each bar reads the real envelope at a slightly different instant:
        # it follows the actual voice, not a decorative pattern detached from the audio.
        bars = 4
        elapsed_ms = (time.time() - self.speak_start) * 1000 if self.speak_start else 0
        offsets = [-2, -1, 0, 1]
        for i, off in enumerate(offsets):
            idx = int(elapsed_ms / self.envelope_step_ms) + off
            idx = max(0, min(len(self.envelope) - 1, idx))
            amp = self.envelope[idx]
            hgt = 4 + amp * 22
            x = cx - (bars * 4) + i * 8
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.rectangle(x, cy - hgt / 2, 3.2, hgt)
            cr.fill()

    def _draw_mic(self, cr, cx, cy):
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.save()
        cr.translate(cx, cy - 2)
        cr.arc(0, -6, 6, 0, 2 * math.pi)
        cr.fill()
        cr.rectangle(-6, -6, 12, 12)
        cr.fill()
        cr.arc(0, 6, 6, math.pi, 2 * math.pi)
        cr.fill()
        cr.set_line_width(2)
        cr.arc(0, 4, 10, 0.35 * math.pi, 0.65 * math.pi)
        cr.stroke()
        cr.move_to(0, 14)
        cr.line_to(0, 18)
        cr.move_to(-6, 18)
        cr.line_to(6, 18)
        cr.stroke()
        cr.restore()


class WalkieButton(Gtk.Window):
    def __init__(self):
        super().__init__(title="Claude walkie-talkie")
        self.set_default_size(SIZE, SIZE)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(False)
        self.set_app_paintable(True)
        self.stick()

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        else:
            log("warning: no compositing, transparency won't look right")

        geo = Gdk.Display.get_default().get_monitor(0).get_geometry()
        self.move(geo.width - SIZE - 46, geo.height - SIZE - 96)

        self.orb = Orb()
        self.add(self.orb)
        self.orb.connect("button-press-event", self.on_press)
        self.orb.connect("button-release-event", self.on_release)

        self.session_id = str(uuid.uuid4())
        self.session_started = False
        self.recording_proc = None
        self.wav_path = None
        self.current_turn = 0
        self.claude_proc = None
        self.tts_proc = None

        self.filler = FillerPlayer()

        self.whisper_model = None
        threading.Thread(target=self.load_whisper, daemon=True).start()
        threading.Thread(target=ensure_fillers, daemon=True).start()

        log("=== started (sonnet model, fast flags) ===")

    def set_state(self, state):
        self.orb.set_state(state)

    def load_whisper(self):
        from faster_whisper import WhisperModel
        t0 = time.time()
        self.whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        log(f"whisper loaded in {time.time()-t0:.1f}s")
        GLib.idle_add(self.set_state, "idle")

    # ---------- watch whether Claude decides to take a screenshot ----------
    def _watch_for_capture(self, proc):
        """Polls SCREENSHOT_PATH's mtime while `proc` (the claude subprocess) is alive.
        Flashes the red frame the moment it detects Claude actually wrote a new capture --
        never on a fixed schedule, only on real writes."""
        seen_mtime = os.path.getmtime(SCREENSHOT_PATH) if os.path.exists(SCREENSHOT_PATH) else None
        while proc.poll() is None:
            time.sleep(0.15)
            if os.path.exists(SCREENSHOT_PATH):
                mtime = os.path.getmtime(SCREENSHOT_PATH)
                if mtime != seen_mtime:
                    seen_mtime = mtime
                    GLib.idle_add(flash_capture_indicator)

    # ---------- interruptions ----------
    def _stop_tts(self):
        self.filler.stop()
        if self.tts_proc and self.tts_proc.poll() is None:
            self.tts_proc.terminate()

    def _kill_claude(self):
        self.filler.stop()
        if self.claude_proc and self.claude_proc.poll() is None:
            self.claude_proc.terminate()

    # ---------- recording ----------
    def on_press(self, widget, event):
        if self.whisper_model is None or self.orb.state == "recording":
            return True
        self.orb.pressed = True
        if self.orb.state == "speaking":
            self._stop_tts()
        elif self.orb.state == "thinking":
            self._kill_claude()

        self.current_turn += 1
        self.wav_path = tempfile.mktemp(suffix=".wav", dir=ASSISTANT_DIR)
        self.recording_proc = subprocess.Popen(
            ["arecord", "-q", "-f", "cd", "-t", "wav", self.wav_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.set_state("recording")
        log(f"recording... (turn {self.current_turn})")
        return True

    def on_release(self, widget, event):
        self.orb.pressed = False
        if self.recording_proc is None:
            return True
        self.recording_proc.terminate()
        try:
            self.recording_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.recording_proc.kill()
        self.recording_proc = None
        self.set_state("thinking")
        threading.Thread(target=self.process_turn, args=(self.wav_path, self.current_turn), daemon=True).start()
        return True

    # ---------- full turn (interruptible) ----------
    def process_turn(self, wav_path, my_turn):
        def superseded():
            return my_turn != self.current_turn

        try:
            size = os.path.getsize(wav_path) if os.path.exists(wav_path) else 0
            if size < 8000:
                log("clip too short, ignoring")
                return

            t0 = time.time()
            segments, info = self.whisper_model.transcribe(wav_path, language="es")
            text = " ".join(s.text for s in segments).strip()
            log(f"transcribed ({time.time()-t0:.1f}s, turn {my_turn}): {text!r}")
            if not text or superseded():
                return

            prompt = (
                f"The user says by voice: \"{text}\"\n\n"
                f"(Only if you need to see their screen: run `import -window root -resize 1280x "
                f"{SCREENSHOT_PATH}` via Bash, then read that file. Skip it otherwise.)"
            )
            cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions",
                   "--append-system-prompt", SYSTEM_PROMPT] + FAST_FLAGS
            if self.session_started:
                cmd += ["--resume", self.session_id]
            else:
                cmd += ["--session-id", self.session_id]

            if superseded():
                return
            t0 = time.time()
            self.claude_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # Claude decides per turn whether it needs to look at the screen. We never take a
            # screenshot ourselves; we just watch the fixed path it's told to write to, and flash
            # the red frame the moment (and only the moments) it actually captures something.
            threading.Thread(target=self._watch_for_capture, args=(self.claude_proc,), daemon=True).start()
            # fills the silence if this turn turns out to be slow; stops the
            # moment there's a real answer to speak
            self.filler.start()
            stdout, stderr = self.claude_proc.communicate(timeout=90)
            rc = self.claude_proc.returncode
            self.claude_proc = None
            # note: the filler keeps talking on purpose until the reply audio is
            # actually ready to play (generating it takes a couple of seconds too)

            if rc is not None and rc < 0:
                log(f"turn {my_turn} interrupted mid-Claude")
                return
            if superseded():
                log(f"turn {my_turn} discarded (got a stale reply)")
                return

            reply = stdout.strip()
            if rc != 0:
                log(f"claude ERROR: {stderr[:500]}")
                reply = "Hubo un error al procesar eso."
            else:
                self.session_started = True
            log(f"claude replied ({time.time()-t0:.1f}s, turn {my_turn}): {reply!r}")

            for junk in ("```", "**"):
                reply = reply.replace(junk, "")

            if superseded() or not reply:
                return
            self._speak(reply, my_turn, superseded)

        except Exception as e:
            log(f"EXCEPTION turn {my_turn}: {e!r}")
        finally:
            self.filler.stop()
            if os.path.exists(SCREENSHOT_PATH):
                try:
                    os.remove(SCREENSHOT_PATH)
                except OSError:
                    pass
            try:
                os.remove(wav_path)
            except OSError:
                pass
            if not superseded():
                GLib.idle_add(self.set_state, "idle")

    # ---------- spoken output, with the orb synced to the real audio ----------
    def _speak(self, text, my_turn, superseded):
        clean_txt = tempfile.mktemp(suffix=".txt", dir=ASSISTANT_DIR)
        mp3_path = tempfile.mktemp(suffix=".mp3", dir=ASSISTANT_DIR)
        try:
            with open(clean_txt, "w", encoding="utf-8") as f:
                f.write(clean_for_speech(text))

            if superseded():
                return
            t0 = time.time()
            subprocess.run(
                [EDGE_TTS, "--voice", VOICE, f"--rate={RATE}", "--file", clean_txt, "--write-media", mp3_path],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
            log(f"tts generated ({time.time()-t0:.1f}s, turn {my_turn})")
            if superseded() or not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
                return

            envelope, step_ms = compute_envelope(mp3_path)
            if superseded():
                return

            # the reply audio is ready now: let any filler line finish its word,
            # then take over. They never overlap.
            self.filler.stop(wait=True)
            GLib.idle_add(lambda: (self.set_state("speaking"), self.orb.start_speaking(envelope, step_ms)))
            self.tts_proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", mp3_path],
                stdin=subprocess.DEVNULL,
            )
            self.tts_proc.wait()
            self.tts_proc = None
        finally:
            for p in (clean_txt, mp3_path):
                try:
                    os.remove(p)
                except OSError:
                    pass


if __name__ == "__main__":
    win = WalkieButton()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
