#!/usr/bin/env python3
"""Cut one moment into a 1080x1920 Short.

Default look: creator/webcam in the TOP panel, game or video in the BOTTOM panel,
a hook box in the first seconds, big word-by-word captions on the seam between the
panels, and an optional small logo for the whole clip. Loudness is set to -14 LUFS.

All fonts, colours and panel sizes live in campaigns/personal.json (section
"style" and "layout"). Most can also be changed per clip with a flag.

  python scripts/cut.py jobs/<id> --start 740 --end 800 --hook "He did NOT expect that" --face 0:50:420:373 --credit "@channelname"
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ROOT, Transcriber, extract_wav, load_campaign,  # noqa: E402
                    load_job, need, sh, video_size, parse_ts)

W, H = 1080, 1920

DEFAULT_STYLE = {
    "font": "Arial",
    "bold": True,
    "caption_size": 100,
    "caption_color": "FFFFFF",
    "caption_outline_color": "000000",
    "caption_outline": 9,
    "caption_pop": 118,
    "caption_uppercase": True,
    "caption_y": None,
    "hook_size": 68,
    "hook_text_color": "000000",
    "hook_box_color": "FFD700",
    "hook_box_padding": 24,
    "hook_seconds": 3.0,
    "hook_uppercase": True,
    "hook_y": 300,
}
DEFAULT_LAYOUT = {
    "default": "auto",      # auto = split if there is a webcam box, otherwise blur
    "top_height": 960,      # pixels, or "auto" (bottom panel = exact height of the full-width video)
    "game_fit": "cover",    # "cover" fills the panel (crops sides), "contain" shows the whole video
    "game_focus": 0.5,
    "bg_blur": 6,           # 0 = no blur at all; ~4-8 = very subtle; the old look was 25
    "bg_dim": 0.25,         # 0 = original brightness; 0.25 = 25% darker so the video stands out
    "blur_zoom": 1.0,       # blur layout: 1.0 = video fits the width; 1.6 = bigger, sides cropped
    "blur_focus": 0.5,      # blur layout when zoomed: 0 = left, 0.5 = centre, 1 = right
    "blur_shift_y": 0,      # blur layout: move the video up (negative) or down (positive), in pixels
}


def bg_filter(blur, dim):
    """Filter chain text (starts with a comma, or is empty) that softens and dims a background."""
    parts = []
    if blur and blur > 0:
        parts.append(f"boxblur={int(blur)}:1")
    if dim and dim > 0:
        k = round(1 - min(0.9, float(dim)), 2)
        parts.append(f"colorchannelmixer=rr={k}:gg={k}:bb={k}")
    return ("," + ",".join(parts)) if parts else ""


def ass_ts(sec):
    sec = max(0.0, sec)
    cs = int(round(sec * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def clean(text):
    return re.sub(r"[{}\\]", "", text).replace("\n", " ").strip()


def bgr(rgb_hex):
    """'FFD700' or '#FFD700' -> ASS colour '&H0000D7FF' (ASS stores blue-green-red)."""
    h = str(rgb_hex).lstrip("#")
    if len(h) != 6:
        sys.exit(f"Colour '{rgb_hex}' must be 6 hex digits like FFD700.")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def make_ass(words, hook, st, hook_y, cap_y, dur):
    bold = -1 if st["bold"] else 0
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['caption_size']},{bgr(st['caption_color'])},&H000000FF,{bgr(st['caption_outline_color'])},&H00000000,{bold},0,0,0,100,100,0,0,1,{st['caption_outline']},2,5,40,40,0,1
Style: Hook,{st['font']},{st['hook_size']},{bgr(st['hook_text_color'])},&H000000FF,{bgr(st['hook_box_color'])},&H00000000,{bold},0,0,0,100,100,0,0,3,{st['hook_box_padding']},0,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head]
    hd = min(float(st["hook_seconds"]), dur)
    hook_txt = clean(hook).upper() if st["hook_uppercase"] else clean(hook)
    if hook_txt:
        lines.append(
            f"Dialogue: 1,{ass_ts(0)},{ass_ts(hd)},Hook,,0,0,0,,"
            f"{{\\an5\\pos({W // 2},{hook_y})\\fad(0,250)}}{hook_txt}\n"
        )
    pop = int(st["caption_pop"])
    for i, w in enumerate(words):
        s = w["s"]
        nxt = words[i + 1]["s"] if i + 1 < len(words) else None
        e = min(nxt, w["e"] + 0.25) if nxt is not None else w["e"] + 0.25
        e = max(e, s + 0.12)
        txt = clean(w["w"])
        txt = txt.upper() if st["caption_uppercase"] else txt
        if not txt:
            continue
        lines.append(
            f"Dialogue: 0,{ass_ts(s)},{ass_ts(e)},Cap,,0,0,0,,"
            f"{{\\an5\\pos({W // 2},{cap_y})\\fscx{pop}\\fscy{pop}\\t(0,90,\\fscx100\\fscy100)}}{txt}\n"
        )
    return "".join(lines)


def build_graph(layout, face, game_crop, logos, ass_filter, focus, top, game_fit, bgf, zoom=1.0, bfocus=0.5, shift_y=0):
    gc = f"crop={game_crop[2]}:{game_crop[3]}:{game_crop[0]}:{game_crop[1]}," if game_crop else ""
    bottom = H - top
    if layout == "split":
        fx, fy, fw, fh = face
        if game_fit == "contain":
            game = (
                f"[s1]{gc}split=2[b][c];"
                f"[b]scale={W}:{bottom}:force_original_aspect_ratio=increase,crop={W}:{bottom}{bgf},setsar=1[gbg];"
                f"[c]scale={W}:{bottom}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[gfg];"
                f"[gbg][gfg]overlay=(W-w)/2:(H-h)/2[game];"
            )
        else:
            game = (
                f"[s1]{gc}scale={W}:{bottom}:force_original_aspect_ratio=increase,"
                f"crop={W}:{bottom}:(iw-{W})*{focus}:(ih-{bottom})/2,setsar=1[game];"
            )
        body = (
            f"[0:v]split=2[s0][s1];"
            f"[s0]crop={fw}:{fh}:{fx}:{fy},scale={W}:{top}:force_original_aspect_ratio=increase,"
            f"crop={W}:{top},setsar=1[face];"
            + game +
            f"[face][game]vstack=inputs=2,{ass_filter}[txt];"
        )
    elif layout == "blur":
        body = (
            f"[0:v]split=2[b][c];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}{bgf},setsar=1[bg];"
            f"[c]scale={round(W * zoom)}:-2:flags=lanczos,"
            f"crop=w='min(iw,{W})':h='min(ih,{H})':x='(iw-ow)*{bfocus}':y='(ih-oh)/2',setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2+{shift_y},{ass_filter}[txt];"
        )
    else:  # fill
        body = f"[0:v]scale=-2:{H},crop={W}:{H},setsar=1,{ass_filter}[txt];"
    if not logos:
        return body + "[txt]null[v]"
    # every logo is its own ffmpeg input (1, 2, 3 ...) laid over the previous result
    parts, cur = [body], "txt"
    for i, lg in enumerate(logos, start=1):
        lx, ly, lw, lh = lg["box"]
        last = i == len(logos)
        nxt = "v" if last else f"o{i}"
        parts.append(f"[{i}:v]format=rgba,scale={lw}:{lh},colorchannelmixer=aa={lg['opacity']}[lg{i}];")
        en = f":enable='lt(t,{lg['until']:.2f})'" if lg.get("until") else ""  # hook emoji: first seconds only
        parts.append(f"[{cur}][lg{i}]overlay={lx}:{ly}:format=auto{en}[{nxt}]" + ("" if last else ";"))
        cur = nxt
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir")
    ap.add_argument("--pick", type=int, help="Moment rank from moments.json")
    ap.add_argument("--start", type=parse_ts, help="Manual start, e.g. 2:41 (min:sec) or 161 (seconds)")
    ap.add_argument("--end", type=parse_ts, help="Manual end, e.g. 3:12 (min:sec) or 192 (seconds)")
    ap.add_argument("--hook", default="", help="Hook text shown at the start (leave out for no hook box)")
    ap.add_argument("--hook-emoji", help="PNG of an emoji (full colour) shown only while the hook is on screen")
    ap.add_argument("--hook-emoji-size", type=int, default=120, help="Emoji width in pixels (default 120)")
    ap.add_argument("--hook-emoji-bg", default="FFFFFF",
                    help="Colour behind the emoji as RRGGBB (default FFFFFF white), or none for no background")
    ap.add_argument("--hook-emoji-pos", help="Emoji position as x:y in pixels (default: centred above the hook)")
    ap.add_argument("--captions", nargs="?", const="on", choices=["on", "off"],
                    help="Add word-by-word subtitles. Subtitles are OFF unless you add this flag "
                         '(or set "captions": true in the settings file)')
    ap.add_argument("--credit", help='Creator credit for the post text, e.g. "@channelname"')
    ap.add_argument("--layout", choices=["auto", "split", "blur", "fill"],
                    help="auto = split if there is a webcam box, otherwise blur. "
                         "split = creator top, video bottom. blur = one video over a subtle blur. fill = crop to fill")
    ap.add_argument("--zoom", type=float, help="blur layout: video size, 1.0 = fits the width, 1.6 = bigger (crops sides)")
    ap.add_argument("--blur-focus", type=float, help="blur layout when zoomed: 0 = left, 0.5 = centre, 1 = right")
    ap.add_argument("--shift-y", type=int, help="blur layout: move the video up (negative) or down (positive) in pixels, e.g. -150")
    ap.add_argument("--face", help="Webcam/creator box in the source video: x:y:w:h (split layout)")
    ap.add_argument("--top-height", help='Height of the TOP panel in pixels (of 1920), or "auto" so the bottom video is full width with no bars')
    ap.add_argument("--game-fit", choices=["cover", "contain"],
                    help="cover = fill the bottom panel (crops sides); contain = whole video + blurred bars")
    ap.add_argument("--bg-blur", type=float, help="Background blur strength, 0 = none, 4-8 = subtle (default from settings)")
    ap.add_argument("--bg-dim", type=float, help="Darken the background 0 to 0.9 (default from settings)")
    ap.add_argument("--game-focus", type=float, help="cover mode: 0 = left edge, 0.5 = centre, 1 = right edge")
    ap.add_argument("--game-crop", help="Crop the video area first (hide a webcam): x:y:w:h")
    ap.add_argument("--font", help="Caption and hook font name (see README, 'Fonts')")
    ap.add_argument("--caption-size", type=int, help="Caption font size (default from settings)")
    ap.add_argument("--hook-size", type=int, help="Hook font size (default from settings)")
    ap.add_argument("--logo", help="Path to your logo PNG (default from settings)")
    ap.add_argument("--no-logo", action="store_true", help="Leave the logo off this clip")
    ap.add_argument("--logo-opacity", type=float, help="0.1 to 1.0")
    ap.add_argument("--campaign", help="Settings JSON (default: campaigns/personal.json)")
    ap.add_argument("--model", default="medium.en",
                    help="Speech model for captions: small.en (fast), medium.en (default, more accurate), large-v3 (best)")
    ap.add_argument("--retranscribe", action="store_true",
                    help="Listen to this clip again with the careful (slower, more accurate) settings, "
                         "and replace its words in words.json")
    ap.add_argument("--prompt", default=None,
                    help='Names and slang to help the speech model spell them right, e.g. "Kai Cenat, Duke Dennis, rizz, W"')
    ap.add_argument("--no-captions", action="store_true", help="Skip captions (testing only)")
    ap.add_argument("--cpu-encode", action="store_true", help="Use x264 instead of the NVIDIA encoder")
    args = ap.parse_args()

    need("ffmpeg", "ffprobe")
    cfg = load_campaign(args.campaign)
    st = {**DEFAULT_STYLE, **cfg.get("style", {})}
    lay = {**DEFAULT_LAYOUT, **cfg.get("layout", {})}
    if args.font:
        st["font"] = args.font
    if args.caption_size:
        st["caption_size"] = args.caption_size
    if args.hook_size:
        st["hook_size"] = args.hook_size
    layout = args.layout or lay["default"]
    top_raw = args.top_height if args.top_height is not None else lay["top_height"]
    game_fit = args.game_fit or lay["game_fit"]
    bg_blur = args.bg_blur if args.bg_blur is not None else float(lay["bg_blur"])
    bg_dim = args.bg_dim if args.bg_dim is not None else float(lay["bg_dim"])
    bgf = bg_filter(bg_blur, bg_dim)
    zoom = round(max(1.0, args.zoom if args.zoom is not None else float(lay["blur_zoom"])), 2)
    bfocus = round(min(1.0, max(0.0, args.blur_focus if args.blur_focus is not None else float(lay["blur_focus"]))), 2)
    shift_y = int(args.shift_y if args.shift_y is not None else lay["blur_shift_y"])
    captions_on = (args.captions == "on") if args.captions else bool(cfg.get("captions", False))
    if args.no_captions:
        captions_on = False
    focus = round(min(1.0, max(0.0, args.game_focus if args.game_focus is not None else float(lay["game_focus"]))), 2)

    jobdir, meta = load_job(args.job_dir)
    src = meta["source"]
    (jobdir / "edits").mkdir(exist_ok=True)

    # ---- which part of the video
    if args.pick is not None:
        moments = json.loads((jobdir / "moments.json").read_text(encoding="utf-8"))["moments"]
        m = next((x for x in moments if x["rank"] == args.pick), None)
        if not m:
            sys.exit(f"No moment #{args.pick}. Available: 1-{len(moments)}")
        start, end = m["start"], m["end"]
        name = f"clip_{args.pick}"
    elif args.start is not None and args.end is not None:
        start, end = args.start, args.end
        name = f"clip_manual_{int(start)}"
    else:
        sys.exit("Use --pick N (from moments.json) or both --start and --end.")
    dur = end - start
    if dur < cfg["min_seconds"]:
        sys.exit(f"Clip is {dur:.1f}s, shorter than the minimum {cfg['min_seconds']}s.")
    if dur > cfg["hard_max_seconds"]:
        sys.exit(f"Clip is {dur:.1f}s, longer than the hard limit {cfg['hard_max_seconds']}s.")
    if dur > cfg["recommended_max_seconds"]:
        print(f"Note: {dur:.0f}s is longer than the recommended {cfg['recommended_max_seconds']}s.")

    # ---- panel heights
    if str(top_raw).lower() == "auto":
        gw, gh = (None, None)
        if args.game_crop:
            gw, gh = [int(v) for v in args.game_crop.split(":")[2:4]]
        else:
            size = video_size(src)
            gw, gh = size if size else (16, 9)
        bottom_px = 2 * round(W * gh / gw / 2)      # exact full-width height, kept even
        top = H - bottom_px
        game_fit = "contain"                         # whole video, nothing cropped
    else:
        top = int(top_raw)
    if not 300 <= top <= H - 300:
        sys.exit(f"Top panel height must be between 300 and {H - 300} (you gave {top}).")

    # ---- logos: the main logo ("logo_path" + "logo") plus any "extra_logos" from the settings file
    def make_logo(path, lcfg, opacity_override=None):
        lw = int(lcfg.get("width", 200))
        with Image.open(path) as im:
            lh = max(1, round(lw * im.height / im.width))
        op = opacity_override if opacity_override is not None else float(lcfg.get("opacity", 1.0))
        return {"file": path, "box": (int(lcfg.get("x", 40)), int(lcfg.get("y", 70)), lw, lh),
                "opacity": round(min(1.0, max(0.1, op)), 2)}

    logos = []
    if not args.no_logo:
        cand = Path(args.logo) if args.logo else ROOT / cfg.get("logo_path", "assets/my_logo.png")
        if cand.exists():
            logos.append(make_logo(cand, cfg.get("logo", {}), args.logo_opacity))
        elif cfg.get("logo_required", False):
            sys.exit(f"Logo not found: {cand}\nPut your logo PNG there, or pass --logo, or --no-logo.")
        else:
            print(f"No logo at {cand}, making the clip without one. (Use --no-logo to hide this note.)")
        for extra in cfg.get("extra_logos", []):
            ep = ROOT / str(extra.get("path", ""))
            if ep.is_file():
                logos.append(make_logo(ep, extra))
            else:
                print(f"Extra logo not found: {ep} (skipped).")

    # ---- creator/webcam box
    face = None
    game_crop = [int(v) for v in args.game_crop.split(":")] if args.game_crop else None
    layout_file = jobdir / "layout.json"
    saved = json.loads(layout_file.read_text(encoding="utf-8")) if layout_file.exists() else {}
    if layout == "auto":
        has_face = bool(args.face or saved.get("face") or cfg.get("facecam"))
        layout = "split" if has_face else "blur"
        print(f"Layout auto-picked: {layout} ({'webcam box found' if has_face else 'no webcam box given'}).")
    if layout == "split":
        raw = args.face or saved.get("face") or cfg.get("facecam")
        if not raw:
            sys.exit(
                "The split layout needs the creator/webcam box.\n"
                f"  1) python scripts/frame.py {args.job_dir} --at 0:05:00\n"
                "  2) open the PNG, note the box, then add  --face x:y:w:h\n"
                "No webcam in this video? Use  --layout blur  instead."
            )
        face = [int(v) for v in str(raw).split(":")]
        if len(face) != 4:
            sys.exit("--face must look like x:y:w:h")
        saved["face"] = ":".join(str(v) for v in face)
        layout_file.write_text(json.dumps(saved), encoding="utf-8")

    # ---- words for captions (relative to clip start)
    words = []
    if captions_on:
        wj = jobdir / "words.json"
        allw = json.loads(wj.read_text(encoding="utf-8")) if wj.exists() else []
        if args.retranscribe or not wj.exists():
            print(f"Listening to the clip with {args.model} (careful mode)...")
            wav = jobdir / "tmp_clip.wav"
            extract_wav(src, str(wav), start, dur)
            fresh = [{"w": w["w"], "s": round(w["s"] + start, 2), "e": round(w["e"] + start, 2)}
                     for w in Transcriber(args.model).words(wav, beam=5, prompt=args.prompt, careful=True)]
            wav.unlink(missing_ok=True)
            # keep the rest of the transcript, replace only this clip's part, and save it so you can edit it
            allw = [w for w in allw if not (start - 0.05 <= w["s"] <= end)] + fresh
            allw.sort(key=lambda w: w["s"])
            wj.write_text(json.dumps(allw), encoding="utf-8")
            print(f"Found {len(fresh)} words in this clip (saved to {wj.name}).")
        for w in allw:
            if w["s"] >= start - 0.05 and w["s"] <= end:
                words.append({"w": w["w"], "s": max(0.0, w["s"] - start),
                              "e": min(dur, w["e"] - start)})

    if layout == "split":
        hook_y = int(st["hook_y"]) if st["hook_y"] is not None else top // 3
        cap_y = int(st["caption_y"]) if st["caption_y"] is not None else top  # on the seam
    else:
        hook_y = int(st["hook_y"]) if st["hook_y"] is not None else 380
        cap_y = int(st["caption_y"]) if st["caption_y"] is not None else 1400
    # hook emoji: a coloured PNG laid over the video only for the first "hook_seconds"
    if args.hook_emoji:
        ep = Path(args.hook_emoji)
        if not ep.is_absolute() and not ep.exists():
            ep = ROOT / ep
        if not ep.is_file():
            sys.exit(f"Hook emoji not found: {args.hook_emoji}\nSave the emoji PNG in the assets folder, e.g. assets\\smile.png")
        ep = ep.resolve()  # ffmpeg runs inside the job folder, so it needs the full path
        esz = max(30, args.hook_emoji_size)
        if args.hook_emoji_pos:
            ex, ey = (int(v) for v in args.hook_emoji_pos.split(":"))
        else:
            ex, ey = (W - esz) // 2, max(10, hook_y - esz - 90)
        if args.hook_emoji_bg.lower() != "none":
            # put the emoji on a rounded badge (white by default) so it reads as a hook, not a logo
            from PIL import ImageDraw
            col = "#" + args.hook_emoji_bg.lstrip("#")
            badge = Image.new("RGBA", (esz, esz), (0, 0, 0, 0))
            ImageDraw.Draw(badge).rounded_rectangle((0, 0, esz - 1, esz - 1), radius=esz // 5, fill=col)
            with Image.open(ep) as src_em:
                inner = round(esz * 0.7)
                e2 = src_em.convert("RGBA").resize((inner, inner), Image.LANCZOS)
            badge.alpha_composite(e2, ((esz - inner) // 2, (esz - inner) // 2))
            ep = jobdir / f"hook_badge_{name}.png"
            badge.save(ep)
        em = make_logo(ep, {"width": esz, "x": ex, "y": ey, "opacity": 1.0})
        em["until"] = min(float(st["hook_seconds"]), dur)
        logos.append(em)
    ass_name = f"cap_{name}.ass"
    (jobdir / ass_name).write_text(make_ass(words, args.hook, st, hook_y, cap_y, dur), encoding="utf-8")

    # extra fonts: drop .ttf/.otf files into the "fonts" folder and use the font's own name
    ass_filter = f"ass={ass_name}"
    fonts_dir = ROOT / "fonts"
    if fonts_dir.is_dir() and any(fonts_dir.glob("*.[to]tf")):
        try:
            rel = os.path.relpath(fonts_dir, jobdir).replace("\\", "/")
            ass_filter += f":fontsdir={rel}"
        except ValueError:
            pass

    graph = build_graph(layout, face, game_crop, logos, ass_filter, focus, top, game_fit, bgf, zoom, bfocus, shift_y)
    out = jobdir / "edits" / f"{name}.mp4"
    inputs = ["-ss", f"{start:.3f}", "-i", src]
    for lg in logos:
        inputs += ["-i", str(lg["file"])]
    base = ["ffmpeg", "-y", "-v", "error", "-stats"] + inputs + [
        "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
        "-af", f"loudnorm=I={cfg['target_lufs']}:TP=-1.5:LRA=11",
        "-t", f"{dur:.3f}", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart",
        str(out),
    ]

    def run(nv):
        cmd = [str(c) for c in base]
        venc = (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"]
                if nv else
                ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"])
        return sh(cmd[:-1] + venc + [cmd[-1]], cwd=jobdir)

    if layout == "split":
        info = f"top panel {top}px, bottom {H - top}px, fit {game_fit}"
    else:
        info = f"zoom {zoom:g}" if layout == "blur" else "fill"
    print(f"Rendering {name} ({dur:.1f}s, layout {layout}, {info}, bg blur {bg_blur:g}, bg dim {bg_dim:g}, "
          f"captions {'on' if captions_on else 'off'})...")
    res = None
    if not args.cpu_encode:
        res = run(True)
    if args.cpu_encode or res.returncode != 0:
        if not args.cpu_encode:
            print("NVIDIA encoder not available here, using the CPU encoder instead.")
        res = run(False)
    if res.returncode != 0:
        sys.exit("ffmpeg failed. See the error above.")

    # ---- post text + sidecar
    parts = [clean(args.hook)] if clean(args.hook) else []
    if args.credit:
        parts.append(f"Credit: {clean(args.credit)}")
    if cfg.get("required_caption_phrase"):
        parts.append(cfg["required_caption_phrase"])
    if cfg.get("required_hashtags"):
        parts.append(" ".join(cfg["required_hashtags"]))
    out.with_suffix(".post.txt").write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    side = {
        "clip": out.name, "source": src, "start": start, "end": end, "layout": layout,
        "hook": args.hook, "hook_start": 0.0, "captions": captions_on,
        "credit": args.credit or "", "logo": any(not lg.get("until") for lg in logos),
        "logos": [{"file": str(lg["file"]), "opacity": lg["opacity"],
                   "box": {"x": lg["box"][0], "y": lg["box"][1], "w": lg["box"][2], "h": lg["box"][3]}}
                  for lg in logos if not lg.get("until")],
    }
    out.with_suffix(".json").write_text(json.dumps(side, indent=2), encoding="utf-8")

    print(f"\nDone: {out}")
    print(f"Post text: {out.with_suffix('.post.txt')}")
    print(f"Next: python scripts/check.py {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")


if __name__ == "__main__":
    main()
