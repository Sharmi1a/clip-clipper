#!/usr/bin/env python3
"""Save one frame from a job's source video so you can find the webcam box.

  python scripts/frame.py jobs/<id> --at 0:05:30

Open the PNG in Paint, hover the corners of the webcam, and read the pixel
position. Then pass it to cut.py as --face x:y:w:h (top-left x, y, then width, height).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_job, need, parse_ts, sh, video_size  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir")
    ap.add_argument("--at", default="0:00:10", help="Time inside the downloaded source (H:MM:SS)")
    args = ap.parse_args()

    need("ffmpeg", "ffprobe")
    jobdir, meta = load_job(args.job_dir)
    t = parse_ts(args.at)
    out = jobdir / f"frame_{int(t)}.png"
    sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", meta["source"],
        "-frames:v", "1", str(out)], check=True)
    w, h = video_size(meta["source"])
    print(f"Saved {out}  (video is {w}x{h})")
    print("Find the webcam rectangle, then use:  --face x:y:w:h")


if __name__ == "__main__":
    main()
