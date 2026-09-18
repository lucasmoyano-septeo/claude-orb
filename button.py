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

import asyncio
import audioop
import cairo
import json
import math
import os
import queue
import random
import re
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

try:
    import edge_tts  # in-process synthesis: no interpreter start-up per sentence
except ImportError:  # falls back to the CLI, slower but works
    edge_tts = None

HOME = os.path.expanduser("~")
ASSISTANT_DIR = os.path.join(HOME, ".claude", "floating-assistant")
LOG_PATH = os.path.join(ASSISTANT_DIR, "assistant.log")

EDGE_TTS = os.path.join(ASSISTANT_DIR, "venv", "bin", "edge-tts")

# ---- persisted settings (backend, model, voice, rate, window position) ----
CONFIG_PATH = os.path.join(ASSISTANT_DIR, "config.json")
DEFAULT_CONFIG = {
    "backend": "claude",
    "claude_model": "sonnet",
    "opencode_model": "opencode/big-pickle",
    "voice": "es-ES-ElviraNeural",
    "rate": "+8%",
    "pos_x": None,
    "pos_y": None,
}

# A few known-good Spanish neural voices (same list the /play skill already
# uses) and a handful of speed presets. Shown in the right-click menu.
VOICE_CHOICES = [
    ("es-ES-ElviraNeural", "Elvira (España)"),
    ("es-ES-XimenaNeural", "Ximena (España)"),
    ("es-AR-ElenaNeural", "Elena (Argentina)"),
    ("es-MX-DaliaNeural", "Dalia (México)"),
    ("es-CO-SalomeNeural", "Salomé (Colombia)"),
    ("es-CL-CatalinaNeural", "Catalina (Chile)"),
    ("es-PE-CamilaNeural", "Camila (Perú)"),
]
RATE_CHOICES = [
    ("-15%", "Más lenta"),
    ("+0%", "Normal"),
    ("+8%", "Un poco rápida (por defecto)"),
    ("+20%", "Rápida"),
    ("+35%", "Muy rápida"),
]
# Model menu, in two sections. Claude models go through the `claude` CLI (paid,
# your subscription); OpenCode models go through the `opencode` CLI, and the
# ones listed here are free tiers of opencode's own provider. Every alias in
# both lists was checked with a real call before being added -- none are
# guessed. Free models are a different kind of thing: smaller, and in this
# session's testing, "big-pickle" has no vision at all (it says so itself
# rather than making something up), so the screenshot step is skipped
# entirely on the OpenCode backend, for every model in that list.
CLAUDE_MODEL_CHOICES = [
    ("sonnet", "Sonnet (rápido, recomendado)"),
    ("opus", "Opus (más profundo, más lento)"),
    ("haiku", "Haiku (el más rápido, menos matizado)"),
    ("fable", "Fable (el más nuevo)"),
]
OPENCODE_MODEL_CHOICES = [
    ("opencode/big-pickle", "Big Pickle (gratis)"),
    ("opencode/nemotron-3.5-lightning-free", "Nemotron 3.5 Lightning (gratis)"),
    ("opencode/mimo-v2.5-free", "MiMo v2.5 (gratis)"),
]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"could not save config: {e!r}")


CONFIG = load_config()
BACKEND = CONFIG["backend"]
CLAUDE_MODEL = CONFIG["claude_model"]
OPENCODE_MODEL = CONFIG["opencode_model"]
VOICE = CONFIG["voice"]
RATE = CONFIG["rate"]

# Fixed path Claude is told to use when IT decides a turn needs to see the
# screen. We never take this screenshot ourselves -- we only watch this path
# for changes while Claude is working, so the red flash fires exactly when
# (and only when) a capture actually happens.
SCREENSHOT_PATH = os.path.join(ASSISTANT_DIR, "current_screen.png")

# ---- filler for silence: one spoken line the first time, a looping tone -----
# ---- for every gap after that -----------------------------------------------
# We don't try to predict how long Claude will take (that's guessing before
# there's anything to measure). We measure the actual silence instead: the
# first time it runs past FIRST_GAP_DELAY, say one opener line out loud; from
# then on, any further silence past RESUME_GAP_DELAY (between sentences, e.g.
# while Claude is using a tool) gets the looping tone instead of more talking.
FILLER_DIR = os.path.join(ASSISTANT_DIR, "fillers")
LOOP_SOUND_PATH = os.path.join(ASSISTANT_DIR, "assets", "thinking_loop.mp3")

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

# OpenCode's `run` command has no --append-system-prompt equivalent, so this
# gets prepended into the message text itself every turn instead. No
# screenshot instruction here on purpose: tested live, opencode/big-pickle
# has no vision at all and says so plainly rather than making something up,
# so offering a capability that doesn't exist would just waste a turn.
OPENCODE_SYSTEM_PROMPT = (
    "INSTRUCCIONES FIJAS, válidas para todo lo que sigue: responde SIEMPRE en español, en 1 a 3 "
    "frases muy breves y clarísimas, como si le explicaras a alguien nuevo en la empresa que no sabe "
    "nada del proyecto. Nunca uses markdown ni backticks. Tienes bash disponible, con libertad total "
    "para usarlo sin pedir confirmación cuando ayude a responder o actuar. Termina justo después de "
    "responder la pregunta -- nunca añadas una línea de RESUMEN, FIRMA, ni nada parecido al final."
)


def build_command(prompt, session):
    """Builds the CLI invocation for the active backend, and says whether its
    stdout is Claude's stream-json schema or OpenCode's. `session` is the
    per-backend dict tracked on WalkieButton: {"id": ..., "started": bool}."""
    if BACKEND == "opencode":
        cmd = ["opencode", "run", OPENCODE_SYSTEM_PROMPT + "\n\n" + prompt,
               "--model", OPENCODE_MODEL, "--format", "json", "--auto"]
        if session["id"]:
            cmd += ["--session", session["id"], "--continue"]
        return cmd, "opencode"
    cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions",
           "--append-system-prompt", SYSTEM_PROMPT,
           "--model", CLAUDE_MODEL, "--setting-sources", "", "--disable-slash-commands",
           "--strict-mcp-config", "--no-chrome",
           "--output-format", "stream-json", "--include-partial-messages", "--verbose"]
    if session["started"]:
        cmd += ["--resume", session["id"]]
    else:
        cmd += ["--session-id", session["id"]]
    return cmd, "claude"


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
    """Renders the opener phrases to mp3 once, in parallel. Re-renders only if
    the phrase list or voice changed (tracked by a manifest), so startup is
    free after the first run."""
    os.makedirs(FILLER_DIR, exist_ok=True)
    manifest_path = os.path.join(FILLER_DIR, "phrases.json")
    wanted = {"openers": OPENERS, "voice": VOICE, "rate": RATE}
    try:
        with open(manifest_path, encoding="utf-8") as f:
            if json.load(f) == wanted and all(
                os.path.exists(_filler_path("opener", i)) for i in range(len(OPENERS))
            ):
                return
    except Exception:
        pass

    jobs = [("opener", i, phrase) for i, phrase in enumerate(OPENERS)]

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


class GapFiller:
    """Covers silence during a turn, driven purely by measured silence, never
    a fixed schedule: the first time the gap runs past FIRST_GAP_DELAY, it
    speaks one opener line ("Hmm, déjame pensar") and sets the orb back to
    "thinking". After that, any further gap -- typically Claude pausing
    between sentences to use a tool -- gets the short looping tone instead,
    starting after the shorter RESUME_GAP_DELAY, for as long as the silence
    lasts. It never overlaps real speech: SpeechPipeline calls
    notify_audio_starting()/notify_audio_ended() around every sentence it
    plays, which is what arms and disarms the watch."""

    FIRST_GAP_DELAY = 3.5
    RESUME_GAP_DELAY = 1.6

    def __init__(self, set_state):
        self.set_state = set_state
        self._active = False
        self._epoch = 0
        self._proc = None
        self._last_opener = None

    def arm(self):
        """Call once per turn, right when Claude starts working."""
        self._active = True
        self._epoch += 1
        self._watch(self._epoch, first=True)

    def notify_audio_starting(self):
        """Call right before a REAL sentence is about to play."""
        self._epoch += 1  # invalidates any in-flight watch/loop
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=1.0)  # let the current line finish its word if it's quick
            except subprocess.TimeoutExpired:
                proc.terminate()
        self._proc = None

    def notify_audio_ended(self):
        """Call right after a real sentence finishes, in case more is still coming."""
        if self._active:
            self._epoch += 1
            self._watch(self._epoch, first=False)

    def stop(self):
        self._active = False
        self._epoch += 1
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        self._proc = None

    def _watch(self, my_epoch, first):
        threading.Thread(target=self._watch_body, args=(my_epoch, first), daemon=True).start()

    def _watch_body(self, my_epoch, first):
        delay = self.FIRST_GAP_DELAY if first else self.RESUME_GAP_DELAY
        waited = 0.0
        while waited < delay:
            if my_epoch != self._epoch or not self._active:
                return
            time.sleep(0.1)
            waited += 0.1
        if my_epoch != self._epoch or not self._active:
            return
        GLib.idle_add(self.set_state, "thinking")
        if first:
            self._play_once(self._pick_opener())
        while my_epoch == self._epoch and self._active:
            self._play_once(LOOP_SOUND_PATH)

    def _pick_opener(self):
        choices = [i for i in range(len(OPENERS)) if i != self._last_opener]
        idx = random.choice(choices or list(range(len(OPENERS))))
        self._last_opener = idx
        return _filler_path("opener", idx)

    def _play_once(self, path):
        if not os.path.exists(path):
            return
        self._proc = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
            stdin=subprocess.DEVNULL,
        )
        self._proc.wait()
        self._proc = None


def synth_sentence(text, out_path, timeout=8.0):
    """Renders one sentence to mp3. The edge-tts cloud has very uneven latency
    (measured: the same line took 20s once and 1.4s the next time), so every
    call gets a hard timeout and one retry -- a hung call must never stall
    the whole reply. Returns True on success."""
    for attempt in (1, 2):
        try:
            if edge_tts is not None:
                async def run():
                    comm = edge_tts.Communicate(text, VOICE, rate=RATE)
                    with open(out_path, "wb") as f:
                        async for chunk in comm.stream():
                            if chunk["type"] == "audio":
                                f.write(chunk["data"])
                asyncio.run(asyncio.wait_for(run(), timeout))
            else:
                txt = out_path + ".txt"
                with open(txt, "w", encoding="utf-8") as f:
                    f.write(text)
                subprocess.run([EDGE_TTS, "--voice", VOICE, f"--rate={RATE}", "--file", txt,
                                "--write-media", out_path],
                               check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=timeout)
                os.remove(txt)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return True
        except Exception as e:
            log(f"tts attempt {attempt} failed for {text[:40]!r}: {e!r}")
    return False


# A sentence ends at . ! ? … followed by whitespace/end (so "3.5" is not split),
# or at a line break. Short fragments are merged with the next sentence so we
# don't fire the synthesizer for a two-word stub.
_SENTENCE_END = re.compile(r"[.!?…]+(?:\s+|$)|\n+")


def split_ready_sentences(buf, min_len=25):
    """Returns (complete_sentences, remaining_text) from a streaming buffer."""
    ready = []
    start = 0
    for m in _SENTENCE_END.finditer(buf):
        seg = buf[start:m.end()].strip()
        if len(seg) >= min_len:
            ready.append(seg)
            start = m.end()
    return ready, buf[start:]


class SpeechPipeline:
    """Sentence-level streaming speech. Claude's text comes in as it is
    generated; each complete sentence is synthesized as soon as it exists and
    played in order, while the next ones are synthesized in the background.
    The listener hears the first sentence a couple of seconds after Claude
    starts writing, instead of waiting for the whole reply to be written AND
    fully synthesized."""

    def __init__(self, orb, gap, set_state):
        self.orb = orb
        self.gap = gap
        self.set_state = set_state
        self.sentences = queue.Queue()
        self.ready = queue.Queue()
        self.cancelled = False
        self.proc = None
        self.first_audio_at = None
        self.done = threading.Event()
        self._t0 = time.time()

    def start(self):
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    def feed(self, sentence):
        if not self.cancelled:
            self.sentences.put(sentence)

    def finish(self):
        self.sentences.put(None)

    def cancel(self):
        self.cancelled = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
        self.sentences.put(None)
        self.ready.put(None)

    def wait(self, timeout=None):
        return self.done.wait(timeout)

    def _synth_loop(self):
        while True:
            sentence = self.sentences.get()
            if sentence is None or self.cancelled:
                self.ready.put(None)
                return
            clean = clean_for_speech(sentence)
            if not clean:
                continue
            mp3 = tempfile.mktemp(suffix=".mp3", dir=ASSISTANT_DIR)
            if synth_sentence(clean, mp3) and not self.cancelled:
                env, step = compute_envelope(mp3)
                self.ready.put((mp3, env, step))
            else:
                try:
                    os.remove(mp3)
                except OSError:
                    pass

    def _play_loop(self):
        first = True
        try:
            while True:
                item = self.ready.get()
                if item is None or self.cancelled:
                    return
                mp3, env, step = item
                if first:
                    first = False
                    self.first_audio_at = time.time() - self._t0
                # a real sentence is about to play: let any filler line or loop tone
                # finish its current beat, then take over. Never overlaps.
                self.gap.notify_audio_starting()
                if self.cancelled:
                    return
                GLib.idle_add(lambda e=env, s=step: (self.set_state("speaking"),
                                                     self.orb.start_speaking(e, s)))
                self.proc = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", mp3],
                    stdin=subprocess.DEVNULL,
                )
                self.proc.wait()
                self.proc = None
                # if Claude pauses again (e.g. to use a tool) before the next sentence
                # is ready, this re-arms the gap watch so the loop tone can resume
                self.gap.notify_audio_ended()
                try:
                    os.remove(mp3)
                except OSError:
                    pass
        finally:
            # drop anything still queued (cancelled mid-way)
            while not self.ready.empty():
                try:
                    item = self.ready.get_nowait()
                    if item:
                        os.remove(item[0])
                except Exception:
                    pass
            self.done.set()


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
        win = self.get_window()
        if win:
            name = "pointer" if v else "default"
            win.set_cursor(Gdk.Cursor.new_from_name(Gdk.Display.get_default(), name))

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
        if CONFIG.get("pos_x") is not None and CONFIG.get("pos_y") is not None:
            self.move(CONFIG["pos_x"], CONFIG["pos_y"])
        else:
            self.move(geo.width - SIZE - 46, geo.height - SIZE - 96)
        self._pos_save_timer = None
        self.connect("configure-event", self._on_configure)

        self.orb = Orb()
        self.add(self.orb)
        self.orb.connect("button-press-event", self.on_press)
        self.orb.connect("button-release-event", self.on_release)

        self.sessions = {
            "claude": {"id": str(uuid.uuid4()), "started": False},
            "opencode": {"id": None, "started": False},
        }
        self.recording_proc = None
        self.wav_path = None
        self.current_turn = 0
        self.backend_proc = None
        self.pipeline = None

        self.gap = GapFiller(self.set_state)

        self.whisper_model = None
        threading.Thread(target=self.load_whisper, daemon=True).start()
        threading.Thread(target=ensure_fillers, daemon=True).start()

        current = CLAUDE_MODEL if BACKEND == "claude" else OPENCODE_MODEL
        log(f"=== started (backend={BACKEND}, model={current}, voice={VOICE}, rate={RATE}) ===")

    # ---------- position: middle-click drag, persisted ----------
    def _on_configure(self, widget, event):
        if self._pos_save_timer:
            GLib.source_remove(self._pos_save_timer)
        self._pos_save_timer = GLib.timeout_add(500, self._save_position)

    def _save_position(self):
        x, y = self.get_position()
        CONFIG["pos_x"], CONFIG["pos_y"] = x, y
        save_config()
        self._pos_save_timer = None
        return False

    # ---------- right-click menu: model / voice / speed ----------
    def _build_menu(self):
        menu = Gtk.Menu()

        def submenu(title, choices, current, on_pick):
            item = Gtk.MenuItem(label=title)
            sub = Gtk.Menu()
            group = None
            for value, label in choices:
                mi = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
                group = mi
                if value == current:
                    mi.set_active(True)
                mi.connect("toggled", lambda w, v=value: w.get_active() and on_pick(v))
                sub.append(mi)
            item.set_submenu(sub)
            menu.append(item)

        def pick_claude_model(v):
            global BACKEND, CLAUDE_MODEL
            BACKEND = "claude"
            CLAUDE_MODEL = v
            CONFIG["backend"] = "claude"
            CONFIG["claude_model"] = v
            save_config()
            log(f"backend=claude, model switched to {v}")

        def pick_opencode_model(v):
            global BACKEND, OPENCODE_MODEL
            BACKEND = "opencode"
            OPENCODE_MODEL = v
            CONFIG["backend"] = "opencode"
            CONFIG["opencode_model"] = v
            save_config()
            log(f"backend=opencode, model switched to {v}")

        def build_model_submenu():
            item = Gtk.MenuItem(label="Modelo")
            sub = Gtk.Menu()
            group = None

            def header(text):
                h = Gtk.MenuItem(label=text)
                h.set_sensitive(False)
                sub.append(h)

            header("— Claude —")
            for value, label in CLAUDE_MODEL_CHOICES:
                mi = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
                group = mi
                if BACKEND == "claude" and value == CLAUDE_MODEL:
                    mi.set_active(True)
                mi.connect("toggled", lambda w, v=value: w.get_active() and pick_claude_model(v))
                sub.append(mi)

            sub.append(Gtk.SeparatorMenuItem())
            header("— OpenCode (gratis) —")
            for value, label in OPENCODE_MODEL_CHOICES:
                mi = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
                group = mi
                if BACKEND == "opencode" and value == OPENCODE_MODEL:
                    mi.set_active(True)
                mi.connect("toggled", lambda w, v=value: w.get_active() and pick_opencode_model(v))
                sub.append(mi)

            item.set_submenu(sub)
            menu.append(item)

        def pick_voice(v):
            global VOICE
            VOICE = v
            CONFIG["voice"] = v
            save_config()
            log(f"voice switched to {v}")
            threading.Thread(target=ensure_fillers, daemon=True).start()

        def pick_rate(v):
            global RATE
            RATE = v
            CONFIG["rate"] = v
            save_config()
            log(f"rate switched to {v}")
            threading.Thread(target=ensure_fillers, daemon=True).start()

        build_model_submenu()
        submenu("Voz", VOICE_CHOICES, VOICE, pick_voice)
        submenu("Velocidad", RATE_CHOICES, RATE, pick_rate)

        menu.append(Gtk.SeparatorMenuItem())
        reset_item = Gtk.MenuItem(label="Volver a la esquina inferior derecha")
        reset_item.connect("activate", lambda w: self._reset_position())
        menu.append(reset_item)

        menu.show_all()
        return menu

    def _reset_position(self):
        geo = Gdk.Display.get_default().get_monitor(0).get_geometry()
        self.move(geo.width - SIZE - 46, geo.height - SIZE - 96)

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
    def _abort_turn(self):
        """Barge-in: kill everything the current turn is doing, instantly.
        With streaming, Claude may still be writing while we're already
        speaking, so all three are stopped regardless of the visible state."""
        self.gap.stop()
        pipeline = self.pipeline
        if pipeline:
            pipeline.cancel()
        if self.backend_proc and self.backend_proc.poll() is None:
            self.backend_proc.terminate()

    # ---------- recording (left button) / drag (middle) / menu (right) ----------
    def on_press(self, widget, event):
        if event.button == 2:  # middle click: grab and move the orb anywhere
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        if event.button == 3:  # right click: model / voice / speed
            self._build_menu().popup_at_pointer(event)
            return True

        if self.whisper_model is None or self.orb.state == "recording":
            return True
        self.orb.pressed = True
        if self.orb.state in ("speaking", "thinking"):
            self._abort_turn()

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

            backend = BACKEND
            session = self.sessions[backend]

            if backend == "claude":
                prompt = (
                    f"The user says by voice: \"{text}\"\n\n"
                    f"(Only if you need to see their screen: run `import -window root -resize 1280x "
                    f"{SCREENSHOT_PATH}` via Bash, then read that file. Skip it otherwise.)"
                )
            else:
                prompt = f'El usuario dice por voz: "{text}"'
            cmd, kind = build_command(prompt, session)

            if superseded():
                return
            # Streaming (Claude only): its text arrives as it is written, so each complete
            # sentence goes straight to the speech pipeline while the next ones are still
            # being generated. OpenCode's --format json has no incremental deltas -- its
            # one "text" event carries the whole reply -- so there it's "whole reply
            # written, then sentence-by-sentence synthesis+playback" instead.
            t0 = time.time()
            stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            self.backend_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file, text=True)
            if kind == "claude":
                # Claude decides per turn whether it needs to look at the screen. We never take
                # a screenshot ourselves; we just watch the fixed path it's told to write to,
                # and flash the red frame the moment (and only the moments) it actually
                # captures something. Not offered at all on OpenCode's free models (no vision).
                threading.Thread(target=self._watch_for_capture, args=(self.backend_proc,), daemon=True).start()
            # fills the silence until the first real sentence is ready to play
            self.gap.arm()
            pipeline = SpeechPipeline(self.orb, self.gap, self.set_state)
            self.pipeline = pipeline
            pipeline.start()

            buf, full_text, result_text = "", "", None
            first_sentence_at = None
            for line in self.backend_proc.stdout:
                if superseded():
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if kind == "claude":
                    if ev.get("type") == "stream_event":
                        e = ev.get("event", {})
                        if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                            piece = e["delta"]["text"]
                            buf += piece
                            full_text += piece
                            ready, buf = split_ready_sentences(buf)
                            for s in ready:
                                if first_sentence_at is None:
                                    first_sentence_at = time.time() - t0
                                pipeline.feed(s)
                    elif ev.get("type") == "result":
                        result_text = ev.get("result") or ""
                else:  # opencode
                    sid = ev.get("sessionID") or (ev.get("part") or {}).get("sessionID")
                    if sid and not session["id"]:
                        session["id"] = sid
                    if ev.get("type") == "text":
                        piece = (ev.get("part") or {}).get("text") or ""
                        if piece:
                            buf += piece
                            full_text += piece
                            if first_sentence_at is None:
                                first_sentence_at = time.time() - t0
                            ready, buf = split_ready_sentences(buf)
                            for s in ready:
                                pipeline.feed(s)

            self.backend_proc.wait(timeout=15)
            rc = self.backend_proc.returncode
            self.backend_proc = None

            if (rc is not None and rc < 0) or superseded():
                log(f"turn {my_turn} interrupted/discarded")
                pipeline.cancel()
                return

            if rc != 0:
                stderr_file.seek(0)
                log(f"{backend} ERROR: {stderr_file.read()[:500]}")
                pipeline.feed("Hubo un error al procesar eso.")
            else:
                session["started"] = True
                if not full_text and result_text:
                    # no deltas came through (shouldn't happen) -- speak the final result instead
                    full_text = result_text
                    ready, buf = split_ready_sentences(result_text)
                    for s in ready:
                        pipeline.feed(s)
                if buf.strip():
                    pipeline.feed(buf.strip())
            stderr_file.close()
            pipeline.finish()

            log(f"{backend} done ({time.time()-t0:.1f}s, first sentence at "
                f"{first_sentence_at if first_sentence_at is None else round(first_sentence_at, 1)}s, "
                f"turn {my_turn}): {full_text[:300]!r}")

            pipeline.wait()
            if pipeline.first_audio_at is not None:
                log(f"first audio played at {pipeline.first_audio_at:.1f}s after Claude started (turn {my_turn})")
            self.pipeline = None

        except Exception as e:
            log(f"EXCEPTION turn {my_turn}: {e!r}")
        finally:
            self.gap.stop()
            if superseded() and self.pipeline:
                self.pipeline.cancel()
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


if __name__ == "__main__":
    win = WalkieButton()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
