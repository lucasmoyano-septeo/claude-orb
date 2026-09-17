#!/usr/bin/env python3
"""
Floating walkie-talkie button to talk to Claude Code by voice.

Press and hold -> records. Release -> transcribes (while taking a screenshot
at the same time), sends it all to a fast Claude Code session (Sonnet, no
extra memory/MCP/skills) with free permission to use xdotool/import via
Bash, and reads the reply out loud with edge-tts while the orb moves at the
real rhythm of the audio (not a decorative animation).

Pressing while it's speaking or thinking interrupts it instantly and starts
recording a new question (barge-in).
"""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

import audioop
import cairo
import math
import os
import subprocess
import tempfile
import threading
import time
import uuid

HOME = os.path.expanduser("~")
ASSISTANT_DIR = os.path.join(HOME, ".claude", "floating-assistant")
LOG_PATH = os.path.join(ASSISTANT_DIR, "assistant.log")

EDGE_TTS = os.path.join(ASSISTANT_DIR, "venv", "bin", "edge-tts")
VOICE = "es-ES-ElviraNeural"
RATE = "+8%"

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
    "On every turn you get what they said by voice and a screenshot taken at that instant; "
    "read it with your file-reading tool before answering if it could be relevant. "
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

        self.whisper_model = None
        threading.Thread(target=self.load_whisper, daemon=True).start()

        log("=== started (sonnet model, fast flags) ===")

    def set_state(self, state):
        self.orb.set_state(state)

    def load_whisper(self):
        from faster_whisper import WhisperModel
        t0 = time.time()
        self.whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        log(f"whisper loaded in {time.time()-t0:.1f}s")
        GLib.idle_add(self.set_state, "idle")

    # ---------- interruptions ----------
    def _stop_tts(self):
        if self.tts_proc and self.tts_proc.poll() is None:
            self.tts_proc.terminate()

    def _kill_claude(self):
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

        screenshot_path = None
        try:
            size = os.path.getsize(wav_path) if os.path.exists(wav_path) else 0
            if size < 8000:
                log("clip too short, ignoring")
                return

            # screenshot in parallel with transcription, not in series
            screenshot_path = tempfile.mktemp(suffix=".png", dir=ASSISTANT_DIR)
            shot_proc = subprocess.Popen(
                ["import", "-window", "root", "-silent", "-resize", "1280x", screenshot_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            t0 = time.time()
            segments, info = self.whisper_model.transcribe(wav_path, language="es")
            text = " ".join(s.text for s in segments).strip()
            shot_proc.wait(timeout=10)
            log(f"transcribed+captured ({time.time()-t0:.1f}s, turn {my_turn}): {text!r}")
            if not text or superseded():
                return

            # the red frame is drawn NOW, after the screenshot already exists on disk,
            # so the frame itself never shows up inside the image Claude is going to read
            GLib.idle_add(flash_capture_indicator)

            prompt = (
                f"The user says by voice: \"{text}\"\n\n"
                f"Screenshot of their screen at this instant: {screenshot_path}"
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
            stdout, stderr = self.claude_proc.communicate(timeout=90)
            rc = self.claude_proc.returncode
            self.claude_proc = None

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
            if screenshot_path:
                try:
                    os.remove(screenshot_path)
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
