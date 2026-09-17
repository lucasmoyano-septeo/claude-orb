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
4. It sends everything to a Claude Code session (Sonnet model, no extra
   memory or MCP so it stays fast) with free permission to use `xdotool`
   (mouse/keyboard) and `import` (screenshots) on its own.
5. It reads the reply out loud with `edge-tts`, and the orb moves at the
   real rhythm of the audio (it's not a decorative animation: it reads the
   actual volume envelope of the mp3).
6. You can interrupt it at any time — while it's thinking or while it's
   speaking — just by pressing the button again.

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

## Why it's fast (or why it isn't)

The turn uses `--model sonnet` plus several flags that strip out what a
quick voice question doesn't need (memory/CLAUDE.md, MCP servers, skill
listing, Chrome integration). With that, a simple question with no
screenshot gets an answer in 3 to 6 seconds. With a screenshot, a bit more,
because the model has to actually read the image. And if Claude decides to
use its tools on its own (look at something more carefully, move the
mouse), it takes as long as it actually takes — that's not a bottleneck
flags can fix, it's the price of giving it freedom to act.

## Structure

```
button.py   -- the whole app: floating window, orb drawing, recording,
               transcription, Claude Code invocation, text to speech.
run.sh      -- launcher (avoids duplicate processes).
```

## License

MIT. See `LICENSE`.
