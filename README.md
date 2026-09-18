# claude-orb

A floating button on your Ubuntu desktop to talk to Claude Code by voice,
walkie-talkie style: press and hold, talk, release. Claude listens, looks at
a screenshot of your screen taken at that instant, and answers by speaking.

<p align="center">🎙️ → 👀 → 🧠 → 🔊</p>

## What happens on every turn

1. You press and hold the orb → it records your voice.
2. You release → it transcribes with Whisper (local, no internet) and takes
   a screenshot at the same time.
3. A red frame flashes around the whole screen for half a second right
   after the capture, so you know exactly when it "looked". The frame is
   drawn **after** the capture, so it never shows up inside the image
   Claude actually sees.
4. It sends everything to either a Claude Code session (default: Sonnet,
   no extra memory or MCP so it stays fast) or an OpenCode session running
   a free model, with free permission to use `xdotool` (mouse/keyboard)
   and `import` (screenshots) on its own. Pick which one from the
   right-click menu — see **Two backends** below.
5. It reads the reply out loud with `edge-tts`, and the orb moves at the
   real rhythm of the audio (it's not a decorative animation: it reads the
   actual volume envelope of the mp3).
6. You can interrupt it at any time — while it's thinking or while it's
   speaking — just by pressing the button again.
7. If it stays silent for a few seconds, it fills the gap: the first time,
   it says something short out loud ("Hmm, déjame pensar"); after that, any
   further silence (typically Claude pausing between sentences to use a
   tool) gets a short looping tone instead, for as long as the silence
   lasts, instead of repeating itself.

## ⚠️ Read this before installing

This assistant can **move your mouse, type on your keyboard, and click on
its own, without asking for confirmation**. That's a deliberate design
choice (free action instead of confirming every step), not an oversight.
If you'd rather it ask before touching anything, remove the "full
freedom... without asking for confirmation" line in `SYSTEM_PROMPT` inside
`button.py` and tell it to confirm before acting instead.

It also sends a screenshot of your whole screen to Claude on every turn. If
you have something sensitive open, don't use it right then — the red frame
exists exactly so you know when it's looking.

## Requirements

- Ubuntu (or similar) with an **X11** session (not Wayland — it uses
  `xdotool`, which doesn't work on Wayland without extra layers).
- System packages: `xdotool`, `imagemagick` (for `import`), `ffmpeg`,
  `alsa-utils` (for `arecord`), `python3-gi` + `gir1.2-gtk-3.0` (GTK3 from
  Python).
- Python 3.10+.
- The `claude` CLI (Claude Code) installed and logged in.
- Optional: the `opencode` CLI installed, if you want the free-model
  backend. Not required for the Claude side to work.

```bash
sudo apt install xdotool imagemagick ffmpeg alsa-utils python3-gi gir1.2-gtk-3.0
```

## Install

```bash
git clone https://github.com/lucasmoyano-septeo/claude-orb.git
cd claude-orb
python3 -m venv venv --system-site-packages
./venv/bin/pip install faster-whisper edge-tts
chmod +x run.sh button.py
```

## Usage

```bash
bash run.sh
```

A floating circular orb appears in the bottom-right corner of the screen.

| State | Color | What it means |
|---|---|---|
| Idle | grey, breathing slowly | ready to listen |
| Recording | red, with expanding rings | hold and talk |
| Thinking | amber, bouncing dots | waiting for Claude's reply |
| Speaking | green, bars following the real audio | playing the reply |

To stop the app: `pkill -f button.py`, or close the window from your
taskbar.

**Mouse controls beyond left-click:**

- **Middle-click and drag** the orb to move it anywhere on screen. The
  position is remembered across restarts.
- **Right-click** for a menu: pick the **model**, the **voice** (7 Spanish
  neural voices) and the **speed**. Changes apply from the next turn on and
  are remembered too. The menu also has "back to the bottom-right corner"
  if you get it lost.
- Hovering shows a pointer cursor, like any clickable button.

All of this is saved in `config.json` (gitignored — it's per-machine, not
part of the project).

### Two backends

The **Modelo** submenu has two sections:

- **Claude** — Sonnet (default), Opus, Haiku, Fable. Goes through the
  `claude` CLI, uses your subscription, and is the only backend that reads
  screenshots (Claude decides per turn whether it needs to, as described
  above).
- **OpenCode (gratis)** — Big Pickle, Nemotron 3.5 Lightning, MiMo v2.5.
  Goes through the `opencode` CLI instead, using OpenCode's own free model
  tier: no subscription, no cost. Picking any of these switches the whole
  backend, not just the model name — the assistant then talks to `opencode`
  for every turn until you switch back.

Trade-offs, checked with real calls before writing this, not assumed:

- **No screenshots on OpenCode's free models.** Asked directly, `big-pickle`
  answered "no puedo ver la captura de pantalla, este modelo no acepta
  imágenes" instead of guessing — so the screenshot step is skipped
  entirely on this backend, for every model in that list, rather than
  wasting a turn on a capability that isn't there.
- **Free means smaller and rougher.** Without a matching system-prompt flag
  in the `opencode` CLI, the instructions (Spanish, brief, no stray
  "RESUMEN:"/"FIRMA:" lines) are prepended into the message itself instead
  — it follows them, but expect more rough edges than Claude on a
  demanding question.
- **No incremental streaming.** OpenCode's `--format json` hands back one
  event with the whole reply already written, not token-by-token like
  Claude's `stream-json`. The reply still gets split into sentences and
  spoken one at a time as each is synthesized, so playback still starts
  before the last sentence is ready — it's just "whole reply first, then
  streamed speech" instead of "streamed text and streamed speech".
- Each backend keeps its own separate conversation; switching mid-chat
  starts fresh on the other side rather than carrying context over.

### The "still thinking" sound

`assets/thinking_loop.mp3` ships with the Wii U loading jingle. Any mp3
works if you'd rather swap it — just overwrite that file under that exact
name; no restart needed, it's read fresh from disk on every play.

## Why it's fast (or why it isn't)

Three things, in order of impact:

1. **Streaming, sentence by sentence.** Claude's text is read as it is
   being written (`--output-format stream-json`). Each complete sentence is
   synthesized the moment it exists and played in order while the next ones
   are still being generated. You hear the first sentence about 5 seconds
   after releasing the button, instead of waiting for the whole reply to be
   written *and* fully synthesized (which used to be 9-10 seconds).
2. **A lean Claude session.** `--model sonnet` plus flags that strip out
   what a quick voice question doesn't need: memory/CLAUDE.md, MCP servers,
   skill listing, Chrome integration.
3. **In-process synthesis with a hard timeout.** The edge-tts cloud has very
   uneven latency (the same sentence measured 20 s once and 1.4 s the next
   time). Every sentence is synthesized in-process, with an 8 s timeout and
   one retry, so one slow call never stalls the whole reply.

While Claude is still thinking, one filler line ("Hmm, déjame pensar") covers
the first stretch of silence, then a short tone loops for as long as the
silence lasts. It stops the instant the first real sentence is ready, and
resumes automatically if Claude goes quiet again mid-reply (e.g. between two
sentences while it's using a tool). Only kicks in on turns that are actually
slow.

What flags can't fix: if Claude decides to use its tools (take a
screenshot, move the mouse), the turn takes as long as that takes. That's
the price of giving it freedom to act, not a bottleneck.

## Structure

```
button.py   -- the whole app: floating window, orb drawing, recording,
               transcription, Claude Code invocation, text to speech.
run.sh      -- launcher (avoids duplicate processes).
```

## License

MIT. See `LICENSE`.
