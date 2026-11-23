# Personal clipper - Windows 11 setup
# Run from this folder in PowerShell:   powershell -ExecutionPolicy Bypass -File .\setup.ps1

$ErrorActionPreference = "Stop"
Write-Host "== Personal clipper setup ==" -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python not found. Install Python 3.10+ from python.org (tick 'Add python.exe to PATH'), then run this again." -ForegroundColor Yellow
    exit 1
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg not found. Installing with winget..." -ForegroundColor Yellow
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    Write-Host "ffmpeg installed. CLOSE this window, open a new PowerShell, and run setup.ps1 again." -ForegroundColor Yellow
    exit 0
}

python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt

# Optional: CUDA libraries so faster-whisper can use your RTX 4050.
# If this fails, nothing breaks - transcription just falls back to the CPU.
try {
    .\.venv\Scripts\python -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
} catch {
    Write-Host "GPU libraries not installed (that's OK, CPU will be used)." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force assets, jobs, fonts | Out-Null

Write-Host ""
Write-Host "Setup done." -ForegroundColor Green
Write-Host "Next: put your logo at assets\my_logo.png (optional, a PNG with a transparent background looks best)."
Write-Host "Then read README.md for the 4 commands."
