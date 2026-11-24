# Personal clipper (Windows 11, NVIDIA RTX)

Turns any YouTube video into 1080x1920 Shorts: creator on the top panel, the
video on the bottom panel, hook box at the start, word-by-word captions on the
seam, optional small logo. No campaign, no required hashtags. Nothing is
uploaded for you.

## Please read: permission and credit

Without a campaign you have no permission to use someone's footage. YouTube can
still claim, demonetize or strike reused content. Safer: clip creators who allow
clips, credit them in every post (`--credit`), and add your own hook and
commentary. This is not legal advice.

## 1. Setup (once)

1. Install Python 3.10+ from python.org (tick "Add python.exe to PATH").
2. In PowerShell, in this folder:
   `powershell -ExecutionPolicy Bypass -File .\setup.ps1`
3. Optional: save your logo as `assets\my_logo.png` (PNG with transparent background).

Run everything with `.\.venv\Scripts\python scripts\...`

## 2. Every clip

**a) Find moments** (use `--section` on long videos):

    .\.venv\Scripts\python scripts\moments.py "https://www.youtube.com/watch?v=XXXX" --section 0:10:00-0:40:00

    #if the file is small so download directly:
    .\\.venv\\Scripts\\python scripts\\moments.py "https://www.youtube.com/watch?v=XXXX"

**b) Find the creator/webcam box** (skip if there is no webcam, use `--layout blur`):

    .\.venv\Scripts\python scripts\frame.py jobs\<job-id> --at 0:05:00

Open the PNG, read the webcam box as x, y, width, height.

**c) Cut:**

    .\.venv\Scripts\python scripts\cut.py jobs\<job-id> --start 740 --end 800 --hook "YOUR HOOK" --face 0:50:420:373 --credit "@channelname"

 ex (default):   
 .\.venv\Scripts\python scripts\cut.py jobs\20261122-000806 --start 2:41 --end 3:14   --layout blur --zoom 1.4 --credit "@Fake channel"  

 (if you want to zoom, --zoom 1.0):
.\.venv\Scripts\python scripts\cut.py jobs\<job-id> --start 0:06 --end 3:14 --layout blur --zoom 1.4 --credit "@channelname"


Use `--pick 1` instead of `--start/--end` to take a moment from the list.

**d) Check, then post the text from the `.post.txt` file:**

    .\.venv\Scripts\python scripts\check.py jobs\<job-id>\edits\clip_manual_740.mp4

## 3. Where to change fonts

Everything about text is in **`campaigns\personal.json`**, section `"style"`:

| Setting | What it does |
|---|---|
| `font` | Font name for captions and hook, e.g. `"Arial"`, `"Impact"`, `"Segoe UI Black"` |
| `caption_size` / `hook_size` | Text size (default 100 / 68) |
| `caption_color`, `caption_outline_color` | Colours as RRGGBB hex, e.g. `"FFFFFF"` |
| `caption_outline` | Outline thickness |
| `caption_pop` | Start size of the pop effect in percent (100 = no pop) |
| `caption_uppercase`, `hook_uppercase` | `true` / `false` |
| `hook_box_color`, `hook_text_color` | Colour of the hook box and its text |
| `hook_seconds` | How long the hook stays on screen |
| `hook_y`, `caption_y` | Vertical position in pixels (of 1920). `caption_y: null` = on the seam |

One-off changes without editing the file: `--font "Impact" --caption-size 120 --hook-size 80`.

**Fonts that aren't on your PC:** copy the `.ttf` or `.otf` file into the `fonts`
folder here and use the font's own name (as shown when you open the file) in `font`.
Windows fonts that work with no setup include Arial, Impact, Verdana, Tahoma,
Trebuchet MS, Segoe UI, Bahnschrift.

## 4. Where to adjust the video size in both panels

In **`campaigns\personal.json`**, section `"layout"`:

| Setting | What it does |
|---|---|
| `top_height` | Height of the TOP panel in pixels (video is 1920 tall). 960 = equal halves. 720 = top 37%, bottom 63%. The bottom panel gets the rest. |
| `game_fit` | `"cover"` fills the bottom panel and crops the sides. `"contain"` shows the whole video with blurred bars. |
| `game_focus` | In `cover` mode, which part of the video: 0 = left edge, 0.5 = centre, 1 = right edge |

Per clip: `--top-height 800 --game-fit contain --game-focus 0.3`.

**Top panel zoom** is the `--face x:y:w:h` box: a smaller box zooms in on the face,
a bigger box shows more of the room. Keep the box shape close to the panel shape
(for equal halves, width a little more than height, about 1.1 : 1) so nothing gets cut.

**Logo** size, position and opacity: `"logo"` in the same file (`width`, `x`, `y`,
`opacity`). Set `"logo_required": true` if you want clips without a logo to fail the check.

## Other settings

- `required_hashtags` and `required_caption_phrase`: added to every post text.
- `credit_required`: the checker fails clips that have no `Credit:` line.
- `banned_phrases` and `manual_checks`: edit freely.

## Troubleshooting

- "GPU not usable ... using CPU": transcription still works, only slower.
- Font looks wrong: name must match the installed font exactly; or drop the file into `fonts`.
- yt-dlp fails: `.\.venv\Scripts\python -m pip install -U yt-dlp`.
- Always watch the clip once before posting; captions can mishear.
