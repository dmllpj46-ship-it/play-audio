#!/usr/bin/env python3
"""Local audio player — ffplay-based, plays in the background without blocking.

Design notes:
- Playback uses `ffplay -nodisp`, launched DETACHED so this script returns immediately
  and never freezes the calling session.
- A PID state file records what's playing; stop/status verify the pid is alive.
- Playlist: a directory plus a resident worker process playing tracks one by one,
  with optional shuffle and whole-list looping.
- ffplay is discovered via PATH first, then common ffmpeg install locations.
"""
import argparse
import ctypes
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
STATE_DIR = Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp") / "wb-audio-player"
STATE_FILE = STATE_DIR / "current.json"
JOB_FILE = STATE_DIR / "job.json"
LOG_FILE = STATE_DIR / "worker.log"

# Default playlist directory; override with the WB_PLAYLIST_DIR env var.
PLAYLIST_DIR = Path(
    os.environ.get("WB_PLAYLIST_DIR") or (Path.home() / "Music" / "Playlist")
)

# Persisted defaults, stored next to SKILL.md. Edit config.json instead of the code.
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"
DEFAULT_CONFIG = {
    "playlist_dir": "",         # empty = use PLAYLIST_DIR
    "playlist_shuffle": False,  # False = sequential
    "playlist_loop": False,     # False = stop after the last track
    "volume": 100,
}

AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".ape", ".mka"}

CREATE_DETACHED = 0x00000008
CREATE_NO_WINDOW = 0x08000000
STILL_ACTIVE = 259


# ---------------------------------------------------------------- binaries
def find_bin(name: str) -> str | None:
    """Locate ffplay / ffprobe: PATH first, then common ffmpeg install dirs."""
    exe = f"{name}.exe" if IS_WIN else name
    hit = shutil.which(name)
    if hit:
        return hit

    if not IS_WIN:
        return None

    local = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        (Path(local) / "Microsoft/WinGet/Packages", "Gyan.FFmpeg*/**/bin/" + exe),
        (Path("C:/Program Files/ffmpeg/bin"), exe),
        (Path("C:/ffmpeg/bin"), exe),
        (Path(local) / "Microsoft/WinGet/Packages", "**/bin/" + exe),
    ]
    for root, pat in patterns:
        if not root.exists():
            continue
        try:
            for cand in root.glob(pat):
                if cand.is_file():
                    return str(cand)
        except OSError:
            continue
    return None


def is_alive(pid: int) -> bool:
    if not pid:
        return False
    if IS_WIN:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- config
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k in DEFAULT_CONFIG:
                if k in raw:
                    cfg[k] = raw[k]
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"warning: could not read config, using built-in defaults ({exc})", file=sys.stderr)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- state
def log(msg: str) -> None:
    """Worker diagnostic log. Failing to log must never affect playback."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(data: dict) -> None:
    """Atomically write the state file.

    On Windows, reading and writing the same file concurrently can raise
    PermissionError. This used to be unguarded, so the exception escaped the worker
    loop and killed the playback process silently (music just stopped, no log, no
    trace). Now we write to a temp file and os.replace() it, retrying on failure;
    a final failure only costs one stale status update instead of the whole session.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = STATE_FILE.with_suffix(".tmp")
    for attempt in range(6):
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, STATE_FILE)
            return
        except Exception as exc:
            if attempt == 5:
                log(f"write_state gave up after 6 attempts: {exc}")
                return
            time.sleep(0.03 * (attempt + 1))


def clear_state() -> None:
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- metadata
def probe(path: Path) -> dict:
    ffprobe = find_bin("ffprobe")
    if not ffprobe:
        return {}
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration,bit_rate:stream=codec_name,sample_rate,channels",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=25,
        )
        data = json.loads(out.stdout or "{}")
        fmt = data.get("format", {})
        st = (data.get("streams") or [{}])[0]
        return {
            "duration": float(fmt.get("duration") or 0),
            "bit_rate": int(fmt.get("bit_rate") or 0),
            "codec": st.get("codec_name", ""),
            "sample_rate": st.get("sample_rate", ""),
            "channels": st.get("channels", ""),
        }
    except Exception:
        return {}


def fmt_dur(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


# ---------------------------------------------------------------- playlist
def scan_playlist() -> list[Path]:
    if not PLAYLIST_DIR.exists():
        return []
    files = [p for p in PLAYLIST_DIR.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT]
    return sorted(files, key=lambda p: p.name.lower())


def do_list() -> int:
    files = scan_playlist()
    print(f"Playlist directory: {PLAYLIST_DIR}")
    if not files:
        print("(empty - drop audio files into this directory)")
        return 0
    print(f"{len(files)} track(s):\n")
    total = 0.0
    for i, f in enumerate(files, 1):
        info = probe(f)
        d = info.get("duration", 0)
        total += d
        size = f.stat().st_size / 1048576
        print(f"{i:>2}. {f.name}")
        print(f"    {fmt_dur(d)}  {size:.1f} MB  {info.get('bit_rate', 0) // 1000} kbps")
    print(f"\nTotal duration: {fmt_dur(total)}")
    return 0


def do_playlist(args) -> int:
    files = scan_playlist()
    if not files:
        print(f"Playlist is empty: {PLAYLIST_DIR}\n"
              f"Add some audio files, or point --playlist-dir elsewhere.", file=sys.stderr)
        return 2
    if args.shuffle:
        random.shuffle(files)

    ffplay = find_bin("ffplay")
    if not ffplay:
        print("ffplay not found. Install ffmpeg (it ships with ffplay).", file=sys.stderr)
        return 2

    # Stop whatever is already playing so streams never overlap
    if is_alive(read_state().get("pid", 0)):
        do_stop(quiet=True)

    job = {
        "files": [str(f) for f in files],
        "volume": args.volume,
        "loop": bool(args.loop),
        "seconds": args.seconds or 0,
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    JOB_FILE.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

    flags = CREATE_DETACHED | CREATE_NO_WINDOW if IS_WIN else 0
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--_worker", str(JOB_FILE)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=not IS_WIN,
        )
    except Exception as exc:
        print(f"Failed to start player process: {exc}", file=sys.stderr)
        return 1

    time.sleep(0.6)
    if not is_alive(proc.pid):
        print("Player process exited immediately; check ffplay and the audio files.",
              file=sys.stderr)
        return 1

    order = "shuffled" if args.shuffle else "sequential"
    print(f"Playlist: {len(files)} track(s), {order}"
          + (", looping" if args.loop else ""))
    print(f"Directory: {PLAYLIST_DIR}")
    print(f"Volume {args.volume}, pid={proc.pid}")
    print("Stop: --stop   Status: --status")
    return 0


def run_worker(job_path: str) -> int:
    """Resident process: plays tracks one at a time, advancing automatically."""
    try:
        job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"worker: cannot read job file {job_path}: {exc}")
        return 1

    ffplay = find_bin("ffplay")
    if not ffplay:
        log("worker: ffplay not found")
        return 1

    files = job.get("files") or []
    volume = job.get("volume", 100)
    limit = job.get("seconds") or 0
    loop = bool(job.get("loop"))
    total = len(files)
    if not files:
        log("worker: empty file list")
        return 1

    child_flags = CREATE_NO_WINDOW if IS_WIN else 0
    log(f"worker start: {total} track(s), volume={volume}, limit={limit}, loop={loop}, pid={os.getpid()}")

    fail_streak = 0

    while True:
        for i, f in enumerate(files):
            if not Path(f).exists():
                log(f"worker: missing file, skipped: {f}")
                continue

            cmd = [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
                   "-volume", str(volume)]
            if limit:
                cmd += ["-t", str(limit)]
            cmd.append(f)

            t_start = time.time()
            try:
                child = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=child_flags,
                )
            except Exception as exc:
                log(f"worker: failed to spawn ffplay for {Path(f).name}: {exc}")
                fail_streak += 1
                if fail_streak >= 3:
                    log("worker: 3 consecutive spawn failures, stopping")
                    break
                continue

            write_state({
                "pid": os.getpid(),
                "child": child.pid,
                "mode": "playlist",
                "index": i,
                "total": total,
                "current": Path(f).name,
                "path": f,
                "started": time.time(),
                "duration": probe(Path(f)).get("duration", 0),
                "limit": limit,
                "volume": volume,
                "loop": loop,
            })

            try:
                rc = child.wait()
            except Exception as exc:
                log(f"worker: wait() failed on {Path(f).name}: {exc}")
                rc = -1

            played = time.time() - t_start
            # An exit under 1s usually means no audio device or a corrupt file
            if played < 1.0:
                fail_streak += 1
                log(f"worker: ffplay exited after {played:.2f}s (rc={rc}) on {Path(f).name}; "
                    f"fail_streak={fail_streak}")
                if fail_streak >= 3:
                    log("worker: 3 consecutive immediate exits, giving up "
                        "(check audio device / file integrity)")
                    break
            else:
                fail_streak = 0
                log(f"worker: finished {Path(f).name} in {played:.1f}s (rc={rc})")

        if not loop or fail_streak >= 3:
            break

    log("worker end")
    clear_state()
    return 0


# ---------------------------------------------------------------- actions
def do_stop(quiet=False) -> int:
    st = read_state()
    pid = st.get("pid")
    if not pid or not is_alive(pid):
        clear_state()
        if not quiet:
            print("Nothing is playing.")
        return 0
    try:
        if IS_WIN:
            # /T also kills child processes (in playlist mode ffplay is a child of the worker)
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(pid), 15)
    except Exception as exc:
        print(f"Failed to stop playback: {exc}", file=sys.stderr)
        return 1
    clear_state()
    if not quiet:
        print(f"Stopped: {st.get('current') or st.get('name', '')}")
    return 0


def do_status() -> int:
    st = read_state()
    pid = st.get("pid")
    if not pid or not is_alive(pid):
        clear_state()
        print("Status: idle (nothing playing)")
        return 0
    elapsed = time.time() - float(st.get("started", time.time()))
    total = float(st.get("duration") or 0)
    limit = float(st.get("limit") or 0)
    span = min(total, limit) if limit else total

    if st.get("mode") == "playlist":
        idx = int(st.get("index", 0)) + 1
        cnt = int(st.get("total", 0))
        print(f"Status: playing playlist  pid={pid}  track {idx}/{cnt}")
    else:
        print(f"Status: playing  pid={pid}")
    print(f"File: {st.get('current') or st.get('name')}")
    print(f"Progress: {fmt_dur(elapsed)}" + (f" / {fmt_dur(span)}" if span else ""))
    if total and elapsed > total + 3 and st.get("mode") != "playlist":
        print("Note: past the track's duration; the process is probably about to exit.")
    return 0


def do_play(args) -> int:
    ffplay = find_bin("ffplay")
    if not ffplay:
        print("ffplay not found. Install ffmpeg (it ships with ffplay).", file=sys.stderr)
        return 2

    path = Path(args.file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        print(f"No such file: {path}", file=sys.stderr)
        return 2
    if path.is_dir():
        print(f"That's a directory, not an audio file: {path}", file=sys.stderr)
        return 2
    if path.suffix.lower() not in AUDIO_EXT:
        print(f"warning: {path.suffix} is not a common audio extension, trying anyway.",
              file=sys.stderr)

    info = probe(path)
    if args.info:
        print(f"File: {path.name}")
        print(f"Path: {path}")
        if info:
            print(f"Duration: {fmt_dur(info['duration'])}  codec: {info['codec']}  "
                  f"bitrate: {info['bit_rate'] // 1000} kbps  "
                  f"sample rate: {info['sample_rate']} Hz / {info['channels']} ch")
        else:
            print("(ffprobe could not read metadata)")
        return 0

    # Stop whatever is already playing so streams never overlap
    if is_alive(read_state().get("pid", 0)):
        do_stop(quiet=True)

    cmd = [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
           "-volume", str(args.volume)]
    if args.seconds and args.seconds > 0:
        cmd += ["-t", str(args.seconds)]
    if args.loop:
        cmd += ["-loop", "0"]
    if args.start and args.start > 0:
        cmd += ["-ss", str(args.start)]
    cmd.append(str(path))

    flags = CREATE_DETACHED | CREATE_NO_WINDOW if IS_WIN else 0
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=not IS_WIN,
        )
    except Exception as exc:
        print(f"Failed to start ffplay: {exc}", file=sys.stderr)
        return 1

    time.sleep(0.45)  # brief grace period to catch an immediate crash
    if not is_alive(proc.pid):
        print("ffplay exited immediately - usually no usable audio device or a corrupt file.",
              file=sys.stderr)
        return 1

    write_state({
        "pid": proc.pid,
        "name": path.name,
        "current": path.name,
        "path": str(path),
        "started": time.time(),
        "duration": info.get("duration", 0),
        "limit": args.seconds or 0,
        "volume": args.volume,
        "loop": bool(args.loop),
        "mode": "single",
    })

    head = f"Playing: {path.name}"
    if info.get("duration"):
        head += f" ({fmt_dur(info['duration'])})"
    print(head)
    print(f"Volume {args.volume}" + (f", {args.seconds}s only" if args.seconds else "")
          + (", looping" if args.loop else "") + f", pid={proc.pid}")
    print("Stop: python play.py --stop")
    return 0


# ---------------------------------------------------------------- entry point
def main() -> int:
    global PLAYLIST_DIR

    ap = argparse.ArgumentParser(description="Local audio player (ffplay, background playback)")
    ap.add_argument("file", nargs="?", help="path to an audio file")
    ap.add_argument("--stop", action="store_true", help="stop playback")
    ap.add_argument("--status", action="store_true", help="show playback status")
    ap.add_argument("--info", action="store_true", help="print audio metadata without playing")
    ap.add_argument("--volume", type=int, default=None, help="volume 0-100 (default from config)")
    ap.add_argument("--seconds", type=int, default=0, help="play only N seconds")
    ap.add_argument("--start", type=int, default=0, help="start at second N")
    ap.add_argument("--loop", dest="loop", action="store_const", const=True, default=None,
                    help="loop (whole list in playlist mode)")
    ap.add_argument("--no-loop", dest="loop", action="store_const", const=False,
                    help="force no looping, overriding config")
    ap.add_argument("--playlist", action="store_true", help="play the whole default playlist")
    ap.add_argument("--shuffle", dest="shuffle", action="store_const", const=True, default=None,
                    help="with --playlist, play in random order")
    ap.add_argument("--no-shuffle", dest="shuffle", action="store_const", const=False,
                    help="force sequential order, overriding config")
    ap.add_argument("--list", action="store_true", help="list playlist contents")
    ap.add_argument("--playlist-dir", default="", help="playlist directory, overriding the default")
    ap.add_argument("--config", action="store_true", help="show current defaults")
    ap.add_argument("--set-default", action="append", default=[], metavar="KEY=VALUE",
                    help="persist a default, e.g. --set-default playlist_shuffle=false")
    ap.add_argument("--_worker", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    cfg = load_config()

    # ---- update persisted defaults
    if args.set_default:
        for item in args.set_default:
            if "=" not in item:
                print(f"expected KEY=VALUE, got: {item}", file=sys.stderr)
                return 2
            key, val = item.split("=", 1)
            key, val = key.strip(), val.strip()
            if key not in DEFAULT_CONFIG:
                print(f"unknown config key: {key}\nvalid keys: {', '.join(DEFAULT_CONFIG)}",
                      file=sys.stderr)
                return 2
            if isinstance(DEFAULT_CONFIG[key], bool):
                cfg[key] = val.lower() in {"1", "true", "yes", "on", "y"}
            elif isinstance(DEFAULT_CONFIG[key], int):
                cfg[key] = int(val)
            else:
                cfg[key] = val
        save_config(cfg)
        print(f"Saved defaults to {CONFIG_FILE}")
        for k in DEFAULT_CONFIG:
            print(f"  {k} = {cfg[k]}")
        return 0

    if args.config:
        exists = CONFIG_FILE.exists()
        print(f"Config file: {CONFIG_FILE}")
        print(f"Exists: {'yes' if exists else 'no (using built-in defaults)'}")
        for k in DEFAULT_CONFIG:
            src = "config" if exists else "built-in"
            print(f"  {k} = {cfg[k]}  [{src}]")
        return 0

    if args.playlist_dir:
        PLAYLIST_DIR = Path(args.playlist_dir).expanduser()
    elif cfg.get("playlist_dir"):
        PLAYLIST_DIR = Path(str(cfg["playlist_dir"])).expanduser()

    # ---- tri-state resolution: explicit CLI flag > config file > built-in default
    if args.playlist:
        if args.shuffle is None:
            args.shuffle = bool(cfg["playlist_shuffle"])
        if args.loop is None:
            args.loop = bool(cfg["playlist_loop"])
    else:
        args.shuffle = bool(args.shuffle) if args.shuffle is not None else False
        # single-track mode never inherits the playlist loop setting
        args.loop = bool(args.loop) if args.loop is not None else False

    if args.volume is None:
        args.volume = int(cfg.get("volume", 100))
    args.volume = max(0, min(100, args.volume))

    if args._worker:
        # Safety net: any unhandled exception lands in the log instead of
        # letting the process vanish without a trace.
        try:
            return run_worker(args._worker)
        except Exception:
            import traceback
            log("worker crashed with unhandled exception:\n" + traceback.format_exc())
            clear_state()
            return 1
    if args.stop:
        return do_stop()
    if args.status:
        return do_status()
    if args.list:
        return do_list()
    if args.playlist:
        return do_playlist(args)
    if not args.file:
        ap.print_help()
        return 2
    return do_play(args)


if __name__ == "__main__":
    sys.exit(main())
