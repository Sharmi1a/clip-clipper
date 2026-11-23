#!/usr/bin/env python3
"""Check a finished clip against your settings (campaigns/personal.json) before you post.

  python scripts/check.py jobs/<id>/edits/clip_2.mp4

Checks what a script CAN check (format, length, loudness, logo pixels on 3
frames, caption phrase, hashtags, banned words). Then prints the rules only
YOU can verify. Exit code 1 if anything FAILS.
"""
import argparse
import io
import json
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import duration, has_audio, load_campaign, need, sh, video_size  # noqa: E402

results = []


def rec(status, name, detail=""):
    results.append((status, name, detail))


def loudness(path):
    p = sh(["ffmpeg", "-nostats", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"],
           capture_output=True, text=True)
    found = re.findall(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", p.stderr)
    return float(found[-1]) if found else None


def frame_at(path, t):
    p = sh(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True)
    return Image.open(io.BytesIO(p.stdout)).convert("RGB")


def verify_logo(clip, lg, dur, label):
    lf = Path(lg["file"])
    if not lf.exists():
        rec("WARN", f"{label} pixels", f"Logo file {lf} not found, could not verify pixels.")
        return
    box = lg["box"]
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    op = float(lg.get("opacity", 1.0))
    if op < 0.4:
        rec("WARN", f"{label} visible", f"opacity {op:.0%} is too faint to verify by pixels. Check it by eye.")
        return
    logo = Image.open(lf).convert("RGBA").resize((w, h), Image.LANCZOS)
    la = np.asarray(logo).astype(float)
    mask = la[..., 3] > 230
    if mask.sum() < 50:
        mask = la[..., 3] > 128
    if mask.sum() < 50:
        rec("WARN", f"{label} pixels", "Logo is too transparent to verify automatically. Check it by eye.")
        return
    # With opacity op, a logo pixel L on background B is shown as op*L + (1-op)*B, so every
    # frame pixel must lie in [op*L, op*L + (1-op)*255] whatever the background is.
    tol = 25  # video compression noise
    rgb = la[..., :3]
    lo = op * rgb - tol
    hi = op * rgb + (1 - op) * 255 + tol
    g_logo = rgb.mean(axis=2)[mask]
    bad = []
    for t in (0.2, dur / 2, max(0.0, dur - 0.3)):
        arr = np.asarray(frame_at(str(clip), t).crop((x, y, x + w, y + h))).astype(float)
        inside = ((arr >= lo) & (arr <= hi)).all(axis=2)[mask].mean()
        g_fr = arr.mean(axis=2)[mask]
        if g_logo.std() > 10:  # logo has visible structure: it must show up in the frame too
            corr = float(np.corrcoef(g_logo, g_fr)[0, 1]) if g_fr.std() > 1 else 0.0
        else:
            corr = 1.0
        if inside < 0.85 or corr < 0.6:
            bad.append(f"{t:.1f}s (match {inside:.0%})")
    if bad:
        rec("FAIL", f"{label} visible whole clip", "Logo not found at: " + ", ".join(bad))
    else:
        note = f", opacity {op:.0%}" if op < 1 else ""
        rec("PASS", f"{label} visible whole clip", f"Found at start, middle and end{note}")


def check_logo(clip, side, dur, cfg):
    logos = side.get("logos")
    if logos is None and side.get("logo"):  # clip made by an older cut.py
        logos = [{"file": side["logo_file"], "opacity": side.get("logo_opacity", 1.0), "box": side["logo_box"]}]
    if not logos:
        if cfg.get("logo_required", False):
            rec("FAIL", "Logo", "No logo was added, but your settings say the logo is required.")
        else:
            rec("PASS", "Logo", "none on this clip (not required)")
        return
    for i, lg in enumerate(logos, start=1):
        verify_logo(clip, lg, dur, "Logo" if len(logos) == 1 else f"Logo {i}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip")
    ap.add_argument("--campaign")
    ap.add_argument("--post", help="Post text file (default: <clip>.post.txt)")
    args = ap.parse_args()

    need("ffmpeg", "ffprobe")
    cfg = load_campaign(args.campaign)
    clip = Path(args.clip)
    if not clip.exists():
        sys.exit(f"Clip not found: {clip}")

    # format
    size = video_size(clip)
    rec("PASS" if size == (1080, 1920) else "FAIL", "Format 1080x1920", f"{size[0]}x{size[1]}" if size else "no video")
    d = duration(clip)
    if d < cfg["min_seconds"]:
        rec("FAIL", "Length", f"{d:.1f}s is under {cfg['min_seconds']}s")
    elif d > cfg["hard_max_seconds"]:
        rec("FAIL", "Length", f"{d:.1f}s is over the {cfg['hard_max_seconds']}s Shorts limit")
    elif d > cfg["recommended_max_seconds"]:
        rec("WARN", "Length", f"{d:.1f}s works, but {cfg['recommended_max_seconds']}s or less keeps people watching")
    else:
        rec("PASS", "Length", f"{d:.1f}s")
    if not has_audio(clip):
        rec("FAIL", "Audio", "No audio track")
    else:
        lufs = loudness(str(clip))
        if lufs is None:
            rec("WARN", "Loudness", "Could not measure")
        elif abs(lufs - cfg["target_lufs"]) <= 2:
            rec("PASS", "Loudness", f"{lufs:.1f} LUFS")
        else:
            rec("WARN", "Loudness", f"{lufs:.1f} LUFS (target {cfg['target_lufs']})")

    # sidecar (written by cut.py)
    side_path = clip.with_suffix(".json")
    side = json.loads(side_path.read_text(encoding="utf-8")) if side_path.exists() else None
    if side is None:
        rec("WARN", "Sidecar", f"{side_path.name} missing. Not made with cut.py, so logo/hook can't be verified.")
    else:
        check_logo(clip, side, d, cfg)
        hook = (side.get("hook") or "").strip()
        if hook:
            rec("PASS" if side.get("hook_start", 99) <= 1.0 else "FAIL", "Hook in first second", hook[:60])
        else:
            rec("WARN", "Hook", "no hook box on this clip (a hook in the first second keeps viewers watching)")
        rec("PASS", "Captions", "word-by-word captions on" if side.get("captions") else "off (your choice)")

    # post text
    post_path = Path(args.post) if args.post else clip.with_suffix(".post.txt")
    if not post_path.exists():
        rec("FAIL", "Post text", f"{post_path.name} not found")
        post = ""
    else:
        post = post_path.read_text(encoding="utf-8")
        low = post.lower()
        phrase = cfg.get("required_caption_phrase", "")
        if phrase:
            rec("PASS" if phrase.lower() in low else "FAIL", "Required caption line", f'"{phrase}"')
        tags = cfg.get("required_hashtags", [])
        if tags:
            missing = [t for t in tags if t.lower() not in low]
            rec("FAIL" if missing else "PASS", "Hashtags",
                "missing: " + " ".join(missing) if missing else " ".join(tags))
        if cfg.get("credit_required", False):
            has_credit = re.search(r"^credit:\s*\S+", post, re.I | re.M) is not None
            rec("PASS" if has_credit else "FAIL", "Creator credit",
                "Credit line found" if has_credit else 'No "Credit: ..." line. Re-run cut.py with --credit "@channel".')
        title = post.strip().splitlines()[0] if post.strip() else ""
        rec("PASS" if 0 < len(title) <= 100 else "WARN", "Title length (Shorts max 100)", f"{len(title)} characters")

    hits = [b for b in cfg.get("banned_phrases", [])
            if re.search(r"\b" + re.escape(b) + r"\b", (post + " " + (side or {}).get("hook", "")).lower())]
    rec("FAIL" if hits else "PASS", "Banned words", ", ".join(hits) if hits else "none")

    # report
    icon = {"PASS": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}
    print(f"\nChecking {clip.name} against: {cfg['name']}\n")
    for status, name, detail in results:
        print(f"{icon[status]} {name}: {detail}")
    print("\nOnly YOU can check these before posting:")
    for m in cfg.get("manual_checks", []):
        print(f"  [ ] {m}")
    fails = sum(1 for r in results if r[0] == "FAIL")
    warns = sum(1 for r in results if r[0] == "WARN")
    print(f"\n{fails} fail, {warns} warning.")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
