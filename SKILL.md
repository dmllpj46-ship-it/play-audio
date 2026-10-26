---
name: play-audio
description: Play local audio files (mp3 / wav / flac / m4a / ogg / aac / opus) on the user's machine via ffplay — background playback that never blocks the session. Supports volume, timed preview, seek, loop, a folder-based playlist (sequential / shuffled / looped), stop and status. Use when the user says "play this audio", "play the mp3", "put on some music", "preview this track", "play my playlist", "shuffle", "stop the music", "what's playing", or the Chinese equivalents 播放这个音频 / 放一下这首歌 / 听听这个 mp3 / 试听一下 / 放播放列表 / 随机放点歌 / 别放了 / 停下 / 播到哪了. Only plays audio back for the user — it does not generate, convert, download or trim audio (use ffmpeg / yt-dlp for that).
---

# play-audio

Actually play audio out loud on the user's machine. One command, the script returns
immediately, sound keeps going in the background.

## Why not just open the default player

Double-clicking an mp3 pops up Groove / Windows Media Player / Music — a window that
interrupts whatever the user is doing and has to be closed by hand. This skill runs
`ffplay -nodisp`: no window, no UI, pure audio output, exits by itself when done.

That matters most in agent contexts: the command must return instantly so the
conversation can continue while music plays.

## Quick start

```bash
S=/path/to/play-audio/scripts/play.py
PY=python3

# play a file
"$PY" "$S" "path/to/track.mp3"

# preview 30 seconds at 40% volume
"$PY" "$S" "path/to/track.mp3" --seconds 30 --volume 40

# start at 1:10
"$PY" "$S" "path/to/track.mp3" --start 70

# inspect without playing
"$PY" "$S" "path/to/track.mp3" --info

# stop / status
"$PY" "$S" --stop
"$PY" "$S" --status
```

## Playlist

**The folder is the playlist.** Every audio file dropped into the playlist directory
becomes a track, ordered by filename. Default location: `~/Music/Playlist`
(override with `--playlist-dir`, the `WB_PLAYLIST_DIR` env var, or `playlist_dir` in the config).

```bash
"$PY" "$S" --list                  # what's in it (duration, size, bitrate, total)
"$PY" "$S" --playlist              # play it (default: in order, once, no loop)
"$PY" "$S" --playlist --shuffle    # random order, this run only
"$PY" "$S" --playlist --loop       # loop the whole list
"$PY" "$S" --playlist --seconds 15 # 15 seconds of each track
```

Implementation: a detached worker process plays tracks one by one through ffplay,
advancing automatically. `--status` reports "track N/M"; `--stop` kills the worker
and its ffplay child together (`taskkill /T` on Windows, process group on POSIX).

A folder was chosen over a `.m3u` file on purpose: adding music is just a file copy,
there is no index to keep in sync, and the ordering rule is obvious.

### Default behaviour: in order, no loop

`--playlist` with no flags plays tracks in filename order, once, then stops. It does
not shuffle, does not loop, does not restart.

That default is **persisted config**, not a hardcoded constant:

```bash
"$PY" "$S" --config                 # show current defaults and where they come from
"$PY" "$S" --set-default playlist_shuffle=false --set-default playlist_loop=false
"$PY" "$S" --set-default volume=70
```

Precedence: **explicit CLI flag > `config.json` > built-in default**.
Want shuffle for one run without changing the default? Add `--shuffle`.
Want to force order despite a config that says otherwise? `--no-shuffle` (same for `--no-loop`).

Config file lives next to `SKILL.md` as `config.json` (see `config.example.json`):

```json
{
  "playlist_dir": "",
  "playlist_shuffle": false,
  "playlist_loop": false,
  "volume": 100
}
```

`playlist_loop` only affects playlist mode. Single-track `--loop` is always an explicit
request and never defaults to on.

## Flags

| Flag | Meaning |
|---|---|
| `file` | Audio path; relative paths resolve against the **current working directory**. Quote paths containing spaces. |
| `--volume N` | 0–100, falls back to config (default 100). Out-of-range values are clamped. |
| `--seconds N` | Play only N seconds; in playlist mode, N seconds **per track** |
| `--start N` | Start at second N (single track only) |
| `--loop` / `--no-loop` | Loop / force no loop. Playlist mode does not loop by default |
| `--info` | Print duration, codec, bitrate, sample rate. Does **not** play |
| `--playlist` | Play the whole default playlist (in order, once) |
| `--shuffle` / `--no-shuffle` | Shuffle / force sequential, overriding config |
| `--list` | List playlist contents |
| `--playlist-dir PATH` | Use a different playlist folder |
| `--config` | Show current defaults and their source |
| `--set-default K=V` | Persist a default; repeatable |
| `--stop` | Stop playback (idempotent — no error when nothing is playing) |
| `--status` | Show the current track and elapsed time |

## Three behavioural contracts

1. **The script returns immediately; sound continues in the background.** Command
   completion does not mean playback finished. Use `--status` to check. Never treat
   "the command returned" as "the track is over".
2. **The agent cannot hear anything.** Playback is for the user. After starting, state
   what is playing and how long it runs. Do not pretend to hear the melody or review
   the audio quality.
3. **One stream at a time.** Starting new audio stops the previous track automatically,
   so two songs never overlap.

## Why background playback survives session switches

`DETACHED_PROCESS` detaches the console but **not the process tree**. Agent hosts
(WorkBuddy, Claude Code, and similar) commonly tear down the entire tree spawned by a
tool call when a session ends or switches — background playback dies with it. The
symptom is distinctive: music stops and the log has no closing line at all.

So on Windows the worker is created through **WMI `Win32_Process.Create`**. Its parent
is `WmiPrvSE.exe`, entirely outside the caller's tree. Observed chain:

```
python.exe (worker) ← python.exe (venv launcher) ← WmiPrvSE.exe ← svchost.exe ← services.exe
```

Fallback order: WMI → `CREATE_BREAKAWAY_FROM_JOB` → plain `DETACHED_PROCESS`.

Two consequences, both handled:

- A WMI-spawned process **does not reliably inherit the caller's environment**, so the
  state directory cannot live under `%TEMP%`. It is fixed at
  `%LOCALAPPDATA%\wb-audio-player` (`~/.cache/wb-audio-player` on POSIX).
- The state dir and the absolute ffplay path are written into the job file and passed to
  the worker explicitly, so nothing depends on inherited environment.
- The pid WMI returns is not necessarily the worker's real pid (a venv `python.exe` is a
  launcher that spawns another interpreter). Liveness checks therefore trust the pid the
  **worker writes into the state file itself**.

## State file

`%LOCALAPPDATA%\wb-audio-player\current.json` holds pid, current track, index, duration,
volume. In playlist mode the pid is the **worker's**; ffplay is its child.
Stale files are harmless — `--status` and `--stop` verify the pid is alive and treat a
dead pid as idle, then clean up.

`worker.log` in the same directory is the playlist diagnostic log: per-track start/end,
ffplay exit codes, exception tracebacks. **When playback stops unexpectedly, read that
file first** instead of guessing.

State writes are atomic (write `.tmp`, then `os.replace`, with retries). This is not
decoration: on Windows, reading and writing the same file concurrently raises
`PermissionError`. An earlier version left that unguarded, so the worker crashed
silently — music simply stopped, with no log and no trace.

## Gotchas

- **`ffplay` not found** → install ffmpeg (`winget install Gyan.FFmpeg`, `brew install ffmpeg`,
  `apt install ffmpeg`). ffplay ships with it. The script searches `PATH` first, then the
  winget `Gyan.FFmpeg*` package directory, then `C:\ffmpeg\bin`.
- **"exited immediately after launch"** → usually no usable audio device (detached RDP
  session, headless box) or a corrupt file. The script says so explicitly — do not treat
  it as a successful playback.
- **Non-ASCII / spaces in paths** → always quote. In bash, `[` and `]` can also be eaten
  by globbing, so quote those too.
- **Only some tracks play** → only known audio extensions are picked up; images, lyrics
  and `desktop.ini` are ignored.
- **Need conversion or trimming** → out of scope, use ffmpeg directly.
- **Volume flag doesn't move the system slider** → `--volume` only affects this ffplay
  stream, not the OS master volume.

## Requirements

- Python 3.10+ (uses `str | None` type hints). Standard library only — no pip installs.
- ffmpeg (for `ffplay` and `ffprobe`).
- Windows, Linux and macOS. Windows adds detached-process flags so no console window
  flashes; POSIX uses `start_new_session` instead.
