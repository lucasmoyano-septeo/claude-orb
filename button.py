#!/usr/bin/env python3
"""
Botón flotante tipo walkie-talkie para hablar con Claude Code.

Mantener pulsado -> graba. Soltar -> transcribe (a la vez que hace la captura
de pantalla), se lo manda a una sesión de Claude Code rápida (Sonnet, sin
memoria/MCP/skills de más) con permiso libre para usar xdotool/import por
Bash, y lee la respuesta en voz con edge-tts mientras el orbe se mueve al
ritmo real del audio (no una animación decorativa).

Pulsar mientras habla o piensa lo interrumpe al instante y empieza a grabar
una nueva pregunta (barge-in).
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
    """Quita lo que no se puede leer en voz alta (markdown, enlaces, símbolos raros).
    Es una red de seguridad: el system prompt ya le pide a Claude texto plano,
    esto cubre lo que se le escape."""
    t = text
    t = _re.sub(r"```.*?```", " ", t, flags=_re.S)
    t = _re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = _re.sub(r"https?://\S+", " ", t)
    t = t.replace("**", "").replace("__", "")
    t = _re.sub(r"`([^`]*)`", r"\1", t)
    t = t.replace("→", ", ").replace("←", " desde ")
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

# Flags que quitan todo lo que no hace falta para una pregunta rápida de voz
# (memoria global, MCP servers, listado de skills, integración de Chrome):
# esto es lo que baja el turno de ~12-14s (Opus, contexto completo) a ~3-7s.
FAST_FLAGS = [
    "--model", "sonnet",
    "--setting-sources", "",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--no-chrome",
]

SYSTEM_PROMPT = (
    "Eres un asistente de voz flotante en el escritorio Ubuntu del usuario. "
    "En cada turno recibes lo que dijo por voz y una captura de pantalla tomada en ese instante; "
    "léela con tu herramienta de lectura antes de responder si puede ser relevante. "
    "Tienes xdotool (mover/clicar el ratón, escribir, teclas) e import (capturas) disponibles vía Bash, "
    "con libertad total para usarlos sin pedir confirmación cuando ayude a responder o a actuar por el usuario. "
    "Responde siempre muy breve -- 1 a 3 frases -- porque esto se lee en voz alta, no se lee en pantalla. "
    "Y responde siempre SUMAMENTE CLARO: como si le explicaras a una persona junior que acaba de entrar a la "
    "empresa hoy y no conoce nada del proyecto. Nunca asumas que conoce nombres de clases, siglas del equipo, "
    "ni jerga interna sin explicarla en la misma frase. Palabras simples, una idea por frase, cero ambigüedad. "
    "Corto y clarísimo son igual de importantes: nunca sacrifiques uno por el otro. "
    "Nunca uses markdown, backticks, asteriscos ni bloques de código: solo texto plano hablable. "
    "No añadas ninguna línea de resumen ni la etiqueta RESUMEN: en esta herramienta."
)


def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


class CaptureFlash(Gtk.Window):
    """Marco rojo que cubre toda la pantalla medio segundo justo después de
    tomar una captura, para que quede claro en qué momento Claude "miró".
    Se dibuja DESPUÉS de capturar (nunca antes), así el marco nunca contamina
    la propia captura. No bloquea clics: el área es transparente a eventos."""

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
    """Envolvente de volumen real del audio (RMS por ventana), normalizada 0..1.
    Sirve para que el orbe se mueva con la voz de verdad, no con un patrón inventado."""
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
        else:  # speaking -- el radio respira con el volumen REAL del audio
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
        # Cada barra lee la envolvente real en un instante ligeramente distinto:
        # sigue la voz de verdad, no un patrón decorativo desacoplado del audio.
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
            log("aviso: sin composición, la transparencia no se verá bien")

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

        log("=== arrancado (modelo sonnet, flags rápidos) ===")

    def set_state(self, state):
        self.orb.set_state(state)

    def load_whisper(self):
        from faster_whisper import WhisperModel
        t0 = time.time()
        self.whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        log(f"whisper cargado en {time.time()-t0:.1f}s")
        GLib.idle_add(self.set_state, "idle")

    # ---------- interrupciones ----------
    def _stop_tts(self):
        if self.tts_proc and self.tts_proc.poll() is None:
            self.tts_proc.terminate()

    def _kill_claude(self):
        if self.claude_proc and self.claude_proc.poll() is None:
            self.claude_proc.terminate()

    # ---------- grabación ----------
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
        log(f"grabando... (turno {self.current_turn})")
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

    # ---------- turno completo (interrumpible) ----------
    def process_turn(self, wav_path, my_turn):
        def superseded():
            return my_turn != self.current_turn

        screenshot_path = None
        try:
            size = os.path.getsize(wav_path) if os.path.exists(wav_path) else 0
            if size < 8000:
                log("clip demasiado corto, se ignora")
                return

            # captura de pantalla en paralelo con la transcripción, no en serie
            screenshot_path = tempfile.mktemp(suffix=".png", dir=ASSISTANT_DIR)
            shot_proc = subprocess.Popen(
                ["import", "-window", "root", "-silent", "-resize", "1280x", screenshot_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            t0 = time.time()
            segments, info = self.whisper_model.transcribe(wav_path, language="es")
            text = " ".join(s.text for s in segments).strip()
            shot_proc.wait(timeout=10)
            log(f"transcrito+captura ({time.time()-t0:.1f}s, turno {my_turn}): {text!r}")
            if not text or superseded():
                return

            # el marco rojo se dibuja AHORA, después de que la captura ya existe en disco,
            # así el propio marco nunca aparece dentro de la imagen que Claude va a leer
            GLib.idle_add(flash_capture_indicator)

            prompt = (
                f"El usuario dice por voz: \"{text}\"\n\n"
                f"Captura de su pantalla en este instante: {screenshot_path}"
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
                log(f"turno {my_turn} interrumpido a mitad de Claude")
                return
            if superseded():
                log(f"turno {my_turn} descartado (llegó una respuesta obsoleta)")
                return

            reply = stdout.strip()
            if rc != 0:
                log(f"claude ERROR: {stderr[:500]}")
                reply = "Hubo un error al procesar eso."
            else:
                self.session_started = True
            log(f"claude respondió ({time.time()-t0:.1f}s, turno {my_turn}): {reply!r}")

            for junk in ("```", "**"):
                reply = reply.replace(junk, "")

            if superseded() or not reply:
                return
            self._speak(reply, my_turn, superseded)

        except Exception as e:
            log(f"EXCEPCION turno {my_turn}: {e!r}")
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

    # ---------- voz de salida, con el orbe sincronizado al audio real ----------
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
            log(f"tts generado ({time.time()-t0:.1f}s, turno {my_turn})")
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
