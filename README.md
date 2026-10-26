# play-audio

**Play local audio on your machine without a media player window — and without blocking
your terminal or your AI agent.**

A small, dependency-free Python script that wraps `ffplay` into a proper little audio
player: background playback, a folder-as-playlist, volume control, timed previews, and
a state file so anything (you, a shell script, an AI agent) can ask *what's playing?*

```bash
python scripts/play.py ~/Music/track.mp3
# → plays immediately, returns at once, sound continues in the background
```

---

## Why this exists

Double-clicking an audio file opens a media player: a window appears, it steals focus,
and you have to close it afterwards. That's fine when you want to *watch* a player. It's
wrong when you just want sound to happen while you keep working.

Worse for automation: a naive `ffplay track.mp3` blocks until the track ends. If an AI
agent runs it, the conversation freezes for three minutes.

`play-audio` solves both:

- **No window.** `ffplay -nodisp` — pure audio output.
- **Non-blocking.** The process is detached; the command returns in milliseconds.
- **Introspectable.** A state file tracks pid, current track and elapsed time.
- **Composable.** `--stop` and `--status` work from anywhere, any time.

## Features

- Single file playback with volume, seek and timed preview
- **Folder-as-playlist** — drop files in, they're the playlist, ordered by filename
- Sequential / shuffled / looped playlist modes
- **Persisted defaults** — order and loop behaviour are config, not hardcoded
- Metadata readout (`--info`) via `ffprobe`
- Idempotent `--stop`; dead processes are detected and cleaned up automatically
- No third-party packages — Python standard library only
- Windows, Linux and macOS

## Requirements

- **Python 3.10+** (uses `str | None` syntax)
- **ffmpeg** — provides `ffplay` and `ffprobe`
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu: `sudo apt install ffmpeg`

## Install

There is nothing to install — it's one script.

```bash
git clone https://github.com/dmllpj46-ship-it/play-audio.git
cd play-audio
python scripts/play.py --help
```

Optionally alias it:

```bash
alias pa="python /path/to/play-audio/scripts/play.py"
```

## Usage

```bash
# Play a file (runs in the background)
python scripts/play.py song.mp3

# Volume, preview, seek
python scripts/play.py song.mp3 --volume 60
python scripts/play.py song.mp3 --seconds 30
python scripts/play.py song.mp3 --start 70

# Inspect without playing
python scripts/play.py song.mp3 --info

# Control
python scripts/play.py --status
python scripts/play.py --stop
```

### Playlist

The playlist is just a directory. Default: `~/Music/Playlist`.

```bash
python scripts/play.py --list                  # contents + total duration
python scripts/play.py --playlist              # in order, once, then stop
python scripts/play.py --playlist --shuffle    # random, this run only
python scripts/play.py --playlist --loop       # loop the whole list
python scripts/play.py --playlist --seconds 15 # 15s of each track
```

Point it somewhere else:

```bash
python scripts/play.py --playlist --playlist-dir /mnt/music
# or export WB_PLAYLIST_DIR=/mnt/music
```

### Defaults

Order and loop behaviour are persisted, not hardcoded:

```bash
python scripts/play.py --config      # show defaults and their source
python scripts/play.py --set-default playlist_shuffle=false --set-default playlist_loop=false
python scripts/play.py --set-default volume=70
```

Precedence: **CLI flag > `config.json` > built-in default.**
`--shuffle` / `--no-shuffle` and `--loop` / `--no-loop` override per run without touching
the saved defaults.

## Use as an AI agent skill

`SKILL.md` follows the agent-skill format (frontmatter with `name` + `description`,
trigger phrases in the description, instructions in the body). Drop this directory into
your agent's skills folder — e.g. `~/.claude/skills/`, or wherever your agent looks for
skills — and the agent can play audio on request.

Two contracts are written into `SKILL.md` because agents get them wrong otherwise:

1. **A returned command is not a finished track.** Check `--status`.
2. **The agent cannot hear the audio.** Report what's playing; never invent an opinion
   about how it sounds.

## How it works

| Piece | Approach |
|---|---|
| Playback | `ffplay -nodisp -autoexit -loglevel quiet` |
| Detachment | `DETACHED_PROCESS \| CREATE_NO_WINDOW` on Windows, `start_new_session` on POSIX |
| Playlist | Detached worker loops through files, spawning ffplay per track |
| Stopping | `taskkill /T /F` (kills worker + ffplay) on Windows, `killpg` on POSIX |
| State | JSON in the temp dir; liveness checked via `OpenProcess` + `GetExitCodeProcess` |

Playlist tracks are played one at a time rather than concatenated with ffmpeg's concat
demuxer, because real-world playlists mix sample rates (44.1 kHz and 48 kHz side by side
will break concat).

## License

MIT — see [LICENSE](LICENSE).
