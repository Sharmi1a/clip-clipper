#!/usr/bin/env python3
"""Find the best 15-40 second moments in a stream VOD.

Scores each second by: loudness spikes, hype words (from the transcript) and
YouTube's "most replayed" heatmap when the source has one. Each moment is a
window that ENDS shortly after the peak, so the payoff is at the end.

Examples
  python scripts/moments.py "https://www.twitch.tv/videos/123456789" --section 1:10:00-1:50:00
  python scripts/moments.py "https://www.youtube.com/watch?v=XXXX" --top 8
  python scripts/moments.py "D:\\vods\\stream.mp4" --no-transcribe
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ROOT, Transcriber, duration, extract_wav, fmt_ts, need,  # noqa: E402
                    parse_ts, sh)

HYPE_WORDS = {
    "no", "way", "bro", "bruh", "dude", "crazy", "insane", "wild", "dead", "dying",
    "what", "wtf", "omg", "god", "holy", "wow", "yo", "yooo", "nooo", "stop",
    "help", "scared", "lets", "go", "gone", "broke", "broken", "crash", "crashed",
    "died", "dead", "cops", "wanted", "boom", "explode", "explosion", "chase",
    "unbelievable", "seriously", "literally", "clip", "clipped",
}
HYPE_PHRASES = [
    ("oh", "my", "god"), ("no", "way"), ("let's", "go"), ("lets", "go"),
    ("what", "the"), ("are", "you", "serious"), ("i", "can't"), ("hold", "on"),
    ("wait", "wait"), ("oh", "no"), ("are", "you", "kidding"),
]


def stream_lengths(path):
    """Length in seconds of the video and audio streams, e.g. {'video': 187.0, 'audio': 143.7}."""
    out = sh(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
              "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    found = {}
    for st in json.loads(out).get("streams", []):
        try:
            found[st["codec_type"]] = max(found.get(st["codec_type"], 0.0), float(st["duration"]))
        except (KeyError, ValueError):
            pass
    return found


def audio_problem(path):
    """Return a short text if the audio is cut off, or None if it looks fine."""
    n = stream_lengths(path)
    if "video" in n and "audio" in n and n["audio"] < n["video"] - 2.0:
        return f"audio stops at {fmt_ts(n['audio'])} but the video runs to {fmt_ts(n['video'])}"
    return None


def find_video(jobdir, stem="source"):
    for cand in sorted(jobdir.glob(f"{stem}.*")):
        if cand.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov"):
            return cand
    return None


def section_bounds(section):
    """'1:10:00-1:50:00' -> (4200.0, 6000.0). The end is None if it is open ('1:10:00-inf')."""
    first, _, last = section.partition("-")
    end = None if last.strip().lower() in ("", "inf") else parse_ts(last)
    return parse_ts(first), end


YTDLP = [
    sys.executable, "-m", "yt_dlp", "--no-playlist",
    # best video + best audio, no resolution cap. -S sorts by resolution first, then frame rate,
    # so it takes 2160p60 if there is one, else 1440p60, else 1080p60, and so on down.
    "-f", "bv*+ba/b", "-S", "res,fps,ext",
    "--merge-output-format", "mp4",
    "--write-info-json",
    "--retries", "20", "--fragment-retries", "20", "--socket-timeout", "30",
]


def fetch_section(url, jobdir, section):
    """Method 1: let ffmpeg download only the wanted part (fast, but can drop the connection)."""
    cmd = YTDLP + [
        "--downloader-args", "ffmpeg_i:-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 10",
        # --force-keyframes-at-cuts re-encodes the section; without this it uses ffmpeg's low default quality
        "--downloader-args", "ffmpeg_o:-c:v libx264 -preset veryfast -crf 14 -pix_fmt yuv420p -c:a aac -b:a 192k",
        "--download-sections", f"*{section}", "--force-keyframes-at-cuts",
        "-o", str(jobdir / "source.%(ext)s"), url,
    ]
    subprocess.run(cmd, check=True)
    return find_video(jobdir)


def fetch_whole(url, jobdir, section):
    """Method 2: yt-dlp's own downloader gets the whole video (resumable), then ffmpeg trims it."""
    stem = "full" if section else "source"
    cmd = YTDLP + ["-o", str(jobdir / f"{stem}.%(ext)s")]
    if section:
        cmd += ["-o", f"infojson:{jobdir / 'source'}"]   # keep source.info.json for the replay heatmap
    cmd.append(url)
    subprocess.run(cmd, check=True)
    full = find_video(jobdir, stem)
    if full is None or not section:
        return full
    start, end = section_bounds(section)
    out = jobdir / "source.mp4"
    print(f"Trimming {fmt_ts(start)} to {fmt_ts(end) if end is not None else 'the end'} out of the full video...")
    cmd = ["ffmpeg", "-y", "-v", "error", "-stats", "-ss", f"{start:.3f}", "-i", str(full)]
    if end is not None:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", str(out)]
    subprocess.run(cmd, check=True)
    full.unlink(missing_ok=True)
    return out


def report_quality(path):
    """Print the resolution and frame rate that was actually downloaded."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True).stdout.strip().split(",")
        w, h, rate = int(out[0]), int(out[1]), out[2]
        num, _, den = rate.partition("/")
        fps = float(num) / float(den or 1)
        print(f"Downloaded quality: {w}x{h} at {fps:.0f} fps"
              + ("" if h >= 1080 else "  (the video has nothing higher than this)"))
    except Exception:  # noqa: BLE001
        pass


def download(url, jobdir, section, tries=3):
    # Try 1 uses the fast section download. If it fails or the audio is cut off (a connection
    # reset), tries 2 and 3 download the whole video with yt-dlp's own downloader instead.
    methods = [fetch_section if section else fetch_whole] + [fetch_whole] * (tries - 1)
    for attempt, method in enumerate(methods, 1):
        if attempt == 2 and section:
            print("Switching method: downloading the whole video, then cutting out your section. "
                  "(Slower for long videos, but much more reliable.)")
        print(f"Downloading with yt-dlp (attempt {attempt} of {tries})...")
        try:
            src = method(url, jobdir, section)
        except (subprocess.CalledProcessError, OSError):
            print(f"Download failed (attempt {attempt} of {tries}).")
            src = None
        if src is not None:
            problem = audio_problem(src)
            if problem is None:
                report_quality(src)
                return src
            print(f"Download is damaged: {problem} (the connection probably dropped).")
        broken = find_video(jobdir, "source")
        if broken:
            broken.unlink(missing_ok=True)
    sys.exit("Could not get a complete download after several tries. Check your internet "
             "connection (turn off any VPN or proxy) and run the same command again.")


def rms_db(path, step=0.5, sr=8000):
    """Loudness in dB for every `step` seconds, streamed so long VODs don't eat RAM."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
           "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    block = int(sr * step) * 2
    out = []
    while True:
        buf = p.stdout.read(block)
        if len(buf) < block:
            break
        x = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
        out.append(20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-6))
    p.wait()
    return np.array(out, dtype=np.float32)


def moving_avg(x, n):
    n = max(1, int(n))
    pad = np.pad(x, (n // 2, n - 1 - n // 2), mode="edge")
    c = np.cumsum(np.insert(pad, 0, 0))
    return (c[n:] - c[:-n]) / n


def hype_counts(words, n_sec):
    arr = np.zeros(n_sec, dtype=np.float32)
    toks = [w["w"].lower().strip(".,!?\"'()[]") for w in words]
    toks_raw = [w["w"].lower().strip(".,!?\"()[]") for w in words]
    for i, w in enumerate(words):
        sec = int(w["s"])
        if sec >= n_sec:
            continue
        if toks[i] in HYPE_WORDS:
            arr[sec] += 0.6
        for ph in HYPE_PHRASES:
            if tuple(toks_raw[i:i + len(ph)]) == ph:
                arr[sec] += 1.2
    return arr


def heat_series(info_path, offset, n_sec):
    if not info_path.exists():
        return None
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    hm = info.get("heatmap")
    if not hm:
        return None
    arr = np.zeros(n_sec, dtype=np.float32)
    for seg in hm:
        a = int(seg["start_time"] - offset)
        b = int(seg["end_time"] - offset)
        a, b = max(0, a), min(n_sec, b)
        if b > a:
            arr[a:b] = seg["value"]
    if arr.max() > arr.min():
        arr = (arr - arr.min()) / (arr.max() - arr.min())
    return arr


def build_window(peak, words, total, lo, hi, tail=4.0, target=25.0):
    end = min(total, peak + tail)
    if words:
        for w in words:
            if w["e"] >= end and w["s"] <= end + 3:
                end = min(total, w["e"] + 0.4)
                break
    start = max(0.0, end - target)
    if words:
        cands = [w for w in words if start <= w["s"] <= start + 8]
        pick = None
        prev_end = None
        for w in cands:
            if prev_end is not None and w["s"] - prev_end >= 0.6:
                pick = w
                break
            prev_end = w["e"]
        if pick is None and cands:
            pick = cands[0]
        if pick is not None:
            start = max(0.0, pick["s"] - 0.3)
    if end - start < lo:
        start = max(0.0, end - lo)
    if end - start > hi:
        start = end - hi
    return round(start, 2), round(end, 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="Video URL (Twitch/YouTube/Kick VOD) or a local video file")
    ap.add_argument("--section", help="Only this part of a URL, e.g. 1:10:00-1:50:00 (saves a LOT of time)")
    ap.add_argument("--top", type=int, default=8, help="How many moments to list")
    ap.add_argument("--min", type=int, default=15, dest="lo", help="Shortest moment in seconds")
    ap.add_argument("--max", type=int, default=40, dest="hi", help="Longest moment in seconds")
    ap.add_argument("--model", default="small.en", help="faster-whisper model (tiny.en, base.en, small.en, medium.en)")
    ap.add_argument("--no-transcribe", action="store_true", help="Loudness (+heatmap) only, no speech scoring")
    ap.add_argument("--job", help="Job folder name (default: timestamp)")
    args = ap.parse_args()

    need("ffmpeg", "ffprobe")
    job_id = args.job or time.strftime("%Y%m%d-%H%M%S")
    jobdir = ROOT / "jobs" / job_id
    (jobdir / "edits").mkdir(parents=True, exist_ok=True)

    offset = 0.0
    url = None
    if args.source.lower().startswith(("http://", "https://")):
        url = args.source
        if args.section:
            offset = parse_ts(args.section.split("-")[0])
        src = download(url, jobdir, args.section)
    else:
        src = Path(args.source).expanduser().resolve()
        if not src.exists():
            sys.exit(f"File not found: {src}")

    total = duration(src)
    print(f"Source: {src.name}  ({fmt_ts(total)} long)")

    db = rms_db(src)
    n_sec = len(db) // 2
    if n_sec < 10:
        sys.exit("Video is too short to analyse.")
    sec_db = np.maximum(db[0:n_sec * 2:2], db[1:n_sec * 2:2])
    spike = np.maximum(0, sec_db - moving_avg(sec_db, 60))
    loud = np.clip(spike / (np.percentile(spike, 99) + 1e-6), 0, 1)

    words = []
    if not args.no_transcribe:
        print(f"Transcribing with faster-whisper ({args.model})...")
        wav = jobdir / "audio16k.wav"
        extract_wav(str(src), str(wav))
        tr = Transcriber(args.model)
        words = tr.words(wav)
        print(f"  {len(words)} words transcribed on {tr.device}.")
        (jobdir / "words.json").write_text(json.dumps(words), encoding="utf-8")
        wav.unlink(missing_ok=True)

    hype = np.clip(moving_avg(hype_counts(words, n_sec), 5) * 5 / 3.0, 0, 1) if words else np.zeros(n_sec, np.float32)
    heat = heat_series(jobdir / "source.info.json", offset, n_sec) if url else None

    if words and heat is not None:
        w_l, w_h, w_r = 0.45, 0.30, 0.25
    elif words:
        w_l, w_h, w_r = 0.60, 0.40, 0.0
    elif heat is not None:
        w_l, w_h, w_r = 0.70, 0.0, 0.30
    else:
        w_l, w_h, w_r = 1.0, 0.0, 0.0
    score = w_l * loud + w_h * hype + w_r * (heat if heat is not None else 0)
    smooth = moving_avg(score, 3)

    chosen = []
    work = smooth.copy()
    radius = max(20, args.hi // 2)
    guard = 0
    while len(chosen) < args.top and guard < args.top * 6:
        guard += 1
        p = int(np.argmax(work))
        if work[p] <= 0:
            break
        s, e = build_window(p, words, total, args.lo, args.hi)
        work[max(0, p - radius):p + radius + 1] = 0
        if any(not (e <= c["start"] or s >= c["end"]) for c in chosen):
            continue
        text = " ".join(w["w"] for w in words if s <= w["s"] <= e)
        chosen.append({
            "start": s, "end": e, "length": round(e - s, 1),
            "score": round(float(smooth[p]), 3),
            "loud": round(float(loud[p]), 2),
            "hype": round(float(hype[p]), 2),
            "heat": round(float(heat[p]), 2) if heat is not None else None,
            "source_time": fmt_ts(offset + s),
            "text": text[:160],
        })
    chosen.sort(key=lambda m: -m["score"])
    for i, m in enumerate(chosen, 1):
        m["rank"] = i

    (jobdir / "job.json").write_text(json.dumps({
        "source": str(src), "offset": offset, "url": url, "job": job_id,
    }, indent=2), encoding="utf-8")
    (jobdir / "moments.json").write_text(json.dumps({"moments": chosen}, indent=2), encoding="utf-8")

    print(f"\nTop moments (job: {job_id})  -  source time is where it sits in the full stream\n")
    print(f"{'#':>2}  {'stream time':>11}  {'len':>5}  {'score':>5}  text")
    for m in chosen:
        print(f"{m['rank']:>2}  {m['source_time']:>11}  {m['length']:>4}s  {m['score']:>5}  {m['text'][:70]}")
    print(f"\nNext: python scripts/cut.py jobs/{job_id} --pick 1 --hook \"your hook\" --face x:y:w:h")


if __name__ == "__main__":
    main()
