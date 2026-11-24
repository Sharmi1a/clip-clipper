"""Shared helpers for the personal clipper scripts."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def need(*tools):
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        sys.exit(
            f"Missing tool(s) on PATH: {', '.join(missing)}.\n"
            "Run setup.ps1 (Windows), then close and reopen PowerShell."
        )


def sh(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], **kw)


def probe(path):
    out = sh(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height",
         "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def duration(path):
    return float(probe(path)["format"]["duration"])


def video_size(path):
    for s in probe(path)["streams"]:
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    return None


def has_audio(path):
    return any(s.get("codec_type") == "audio" for s in probe(path)["streams"])


def load_campaign(p=None):
    path = Path(p) if p else ROOT / "campaigns" / "personal.json"
    if not path.exists():
        path = ROOT / path
    if not path.exists():
        sys.exit(f"Campaign file not found: {p}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_job(job_dir):
    jobdir = Path(job_dir)
    if not jobdir.is_absolute() and not jobdir.exists():
        jobdir = ROOT / jobdir
    jobdir = jobdir.resolve()
    meta_path = jobdir / "job.json"
    if not meta_path.exists():
        sys.exit(f"{jobdir} is not a job folder (job.json missing). Run moments.py first.")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # job.json remembers the video's full path. If the folder was copied or renamed that path is
    # stale, so look for the downloaded video inside the job folder and fix job.json.
    if not Path(meta.get("source", "")).exists():
        found = next((c for c in sorted(jobdir.glob("source.*"))
                      if c.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")), None)
        if found is None:
            sys.exit(f"The video for this job is missing: {meta.get('source')}\n"
                     f"Run moments.py again for it, or copy source.mp4 into {jobdir}.")
        print(f"Job folder was moved: using {found}")
        meta["source"] = str(found)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return jobdir, meta


def fmt_ts(sec):
    sec = max(0, int(round(sec)))
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def parse_ts(text):
    """'1:02:03' / '02:03' / '123' -> seconds."""
    parts = [float(p) for p in str(text).strip().split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def extract_wav(src, out, start=None, dur=None):
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", out]
    sh(cmd, check=True)


def add_cuda_dlls():
    """Make pip-installed NVIDIA DLLs visible to faster-whisper on Windows."""
    if os.name != "nt":
        return
    import site
    bases = list(site.getsitepackages()) + [site.getusersitepackages()]
    for base in bases:
        nv = Path(base) / "nvidia"
        if nv.is_dir():
            for bindir in nv.glob("*/bin"):
                try:
                    os.add_dll_directory(str(bindir))
                except OSError:
                    pass
                os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")


class Transcriber:
    """faster-whisper wrapper: tries the GPU first, falls back to CPU on any error."""

    def __init__(self, size="small.en"):
        add_cuda_dlls()
        from faster_whisper import WhisperModel
        self._cls = WhisperModel
        self.size = size
        self.device = None
        self.model = None
        self._load("cuda", "float16")

    def _load(self, device, ctype):
        try:
            self.model = self._cls(self.size, device=device, compute_type=ctype)
            self.device = device
        except Exception as e:  # noqa: BLE001
            if device == "cuda":
                print(f"[whisper] GPU not usable ({e}). Using CPU instead.")
                self._load("cpu", "int8")
            else:
                raise

    @staticmethod
    def _load_wav(path):
        """Read our 16 kHz mono 16-bit WAV straight into a float array.

        Passing the array to faster-whisper means it never calls PyAV's
        av.open(), which crashes on some Windows installs (the
        'metadata_errors' error).
        """
        import wave
        import numpy as np
        with wave.open(str(path), "rb") as w:
            raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def words(self, wav, language="en", beam=1, prompt=None, careful=False):
        """careful=True: slower but catches more words (softer silence filter, wider search)."""
        audio = self._load_wav(wav)

        def go():
            extra = {}
            if careful:
                extra = dict(
                    vad_parameters=dict(threshold=0.3, min_silence_duration_ms=700, speech_pad_ms=400),
                    condition_on_previous_text=False,
                    no_speech_threshold=0.5,
                )
            segs, _ = self.model.transcribe(
                audio, language=language, word_timestamps=True,
                vad_filter=True, beam_size=beam, initial_prompt=prompt, **extra,
            )
            out = []
            for s in segs:
                for w in (s.words or []):
                    out.append({"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)})
            return out

        try:
            return go()
        except Exception as e:  # noqa: BLE001
            if self.device == "cuda":
                print(f"[whisper] GPU failed while transcribing ({e}). Retrying on CPU.")
                self._load("cpu", "int8")
                return go()
            raise
