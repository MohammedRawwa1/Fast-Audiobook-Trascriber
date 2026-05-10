# PDF to MP3 — Audio Transcribing & TTS Converter

This repository provides several Python scripts to convert PDF documents into audio files (MP3/M4B). The project supports both Google TTS (gTTS) for quick/simple conversions and Edge TTS (`edge-tts`) pipelines for higher-quality neural voices. Two Edge TTS pipelines are included: an English-focused pipeline with voice presets and an Arabic-focused pipeline with Arabic voice support and translation fallback.

This README documents how to install, run, and troubleshoot the project, and explains the main features of each script.

**Key features**
- Extracts text from PDFs (PyPDF2 / PyMuPDF)
- Translate text when needed (googletrans)
- Two TTS backends:
   - **Edge TTS (English)**: neural voices, SSML support, voice presets, chunked synthesis for long texts, retries/concurrency, MP3/M4B merging.
   - **Edge TTS (Arabic)**: Arabic voice preset (e.g. `ar-AE-HamdanNeural`), translation-to-Arabic fallback, RTL / punctuation handling, chunking and merging.
   - **Google TTS (gTTS)**: lightweight option for small/simple conversions.
- Chunking, trimming, validation and robust merge (ffmpeg + ffprobe required)

## Contents
- `pdf-to-mp3.py` — simple interactive script using PyPDF2 + `googletrans` + `gTTS` for small conversions.
- `pdf-to-mp3-english.py` — advanced English Edge TTS pipeline with SSML, voice presets, chunking, concurrency and merging.
- `pdf-to-mp3-arabic.py` — Arabic-focused Edge TTS pipeline (translate-to-Arabic fallback, Arabic SSML, Arabic voice preset).
- `pdf/` — expected input folder for PDFs.
- `mp3/` — output folder for generated MP3 files.

## Requirements & compatibility

- Python 3.8+ recommended (3.10+ preferred).
- System `ffmpeg`/`ffprobe` must be installed and on `PATH` (used for trimming/merging and duration checks).
- Python packages (see `requirements.txt`).

Note about `httpx` / `httpcore`: some versions of `edge-tts` or other HTTP clients can require specific `httpx`/`httpcore` ranges. If you run into problems related to HTTP client compatibility, a known working environment (from a tested setup) includes:

```
httpx==0.13.3
httpcore==0.9.1
```

If you install the latest packages and encounter conflicts, try pinning those versions (or let pip resolve, using the working config above if needed).

## Install (Windows example)

1. Install Python 3.10+ and create a virtual environment:

```powershell
python -m venv venv
venv\Scripts\activate
```

2. Install system ffmpeg (Windows):

Using Chocolatey (if available):

```powershell
choco install ffmpeg -y
```

Or download from https://ffmpeg.org and add `ffmpeg`/`ffprobe` to your `PATH`.

3. Install Python dependencies:

```powershell
pip install -r requirements.txt
# If you encounter googletrans issues, run:
pip uninstall googletrans -y
pip install googletrans==4.0.0rc1
```

If you encounter compatibility problems with `edge-tts` and HTTP client libraries, you can force the working versions:

```powershell
pip install "httpx==0.13.3" "httpcore==0.9.1"
```

## Quick usage

- Basic (Google TTS):

```powershell
python pdf-to-mp3.py
```

Place PDFs into the `pdf/` folder and follow the interactive prompts. The script extracts text, optionally translates it, and produces MP3 files in `mp3/`.

- Advanced (Edge TTS English pipeline):

```powershell
python pdf-to-mp3-english.py
```

This script performs robust text extraction (PyMuPDF), chapter detection, chunking, SSML generation and synthesizes audio chunks concurrently using `edge-tts`. It then validates and merges chunks into a final audio file (MP3 and optionally M4B).

- Arabic-focused pipeline:

```powershell
python pdf-to-mp3-arabic.py
```

This script targets Arabic output: it can auto-detect source language, translate to Arabic using `googletrans` when appropriate, and synthesizes using an Arabic neural voice.

## Edge TTS: English vs Arabic (what this repo provides)

- Edge TTS (English pipeline):
   - Multiple voice presets (e.g. `ChristopherNeural`, `BrianNeural`, `GuyNeural`, `RyanNeural`, `AriaNeural`).
   - SSML output to control rate/pitch/style per-preset.
   - Chunking to handle long texts and avoid service limits.
   - Concurrency + retries with backoff and corruption checks.
   - Output normalization, trimming and final merge to MP3/M4B.

- Edge TTS (Arabic pipeline):
   - Arabic voice preset (e.g. `ar-AE-HamdanNeural`).
   - Arabic punctuation/RTL handling and text cleanup.
   - Translation fallback: translate to Arabic first when source isn't Arabic.
   - Same chunking/retry/merge pipeline as English for robustness.

## Google TTS

- `gTTS` is used for quick conversions when highest voice quality is not necessary. It is simpler but may be slower or limited for very long texts. Use `pdf-to-mp3.py` for this path.

## Troubleshooting

- "ffmpeg not found" — install ffmpeg and ensure `ffmpeg` and `ffprobe` are on `PATH`.
- HTTP errors or `edge-tts` failures — try installing compatible `httpx`/`httpcore` versions shown above.
- Very long PDFs or memory spikes — run pipelines on a machine with adequate RAM and limit `MAX_WORKERS` in the offline scripts.

## Notes

- The repo includes multiple scripts to demonstrate different tradeoffs: simplicity (`pdf-to-mp3.py`) vs high-quality neural voices and robustness (`pdf-to-mp3-english.py`, `pdf-to-mp3-arabic.py`).
- The offline scripts rely on the third-party `edge-tts` package (which in turn uses HTTP clients). If you maintain a deployment environment, pin versions in `requirements.txt` and test upgrades carefully.

---

If you want, I can also update `requirements.txt` to pin exact versions used in a working environment, or prepare a `pip freeze`-style `requirements-full.txt` from your working setup.