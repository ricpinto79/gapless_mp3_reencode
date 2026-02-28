# Gapless MP3 Re-encode

**Convert your lossless albums (FLAC, APE, ALAC/M4A) into perfect gapless MP3s** using album-wide LAME encoding + CUE splitting.

This tool solves the #1 problem with normal MP3 conversions: **gaps between tracks** (especially on live albums, classical, or continuous mixes). It encodes the **entire album as one big MP3** first (so LAME can apply proper delay/padding), then splits it perfectly using the CUE sheet.

### ✨ What makes this tool special

- **True gapless output**:
  - Album-wide LAME encoding (no per-track re-encoding)
  - Full LAME delay/padding tag verification on every track
  - Optional boundary continuity check (compares actual PCM at track boundaries)

- **Two smart modes**:
  - **Option A** (single lossless + CUE): decode → album LAME → split
  - **Option B** (multiple files): concat to WAV → album LAME → auto-generate CUE → split

- **New in this version**:
  - Automatic **catalog number** detection (CUE → tags → folder name)
  - Beautiful output folder naming: `Artist - [year] Album (CATNO)`
  - Robust error handling (corrupted files no longer crash the script)
  - Progress bars that actually move during long operations
  - Detailed JSON + TXT + folder-status reports
  - Dry-run mode (analyze everything without touching files)
  - Interactive quality menu (V0–V9 VBR, CBR, true/joint stereo)

- **Polishing features**:
  - Checks all required tools before starting
  - Safe filename sanitization (Windows-friendly)
  - Auto-renames tracks using CUE titles with proper padding
  - Prompts to delete temp folders at the end
  - Works with multi-disc albums automatically

### Requirements



**System tools** (install with `sudo apt install` on Debian/Ubuntu):
```bash
sudo apt update && sudo apt install -y flac lame mp3splt ffmpeg
```

**Python packages** (install with pip):
```bash
pip install tqdm mutagen
```

**How to use:** 
```bash
./run_gapless.sh
```

**Or directly:**
```bash
python3 gapless_mp3_reencode.py /path/to/lossless/folder
```

The script will:

Scan for lossless albums
Ask for encoding quality (recommended: Default + V0)
Run in dry-run first (highly recommended)
Create perfect gapless MP3s in ./MP3/
Write detailed reports

Example output folder
```bash
textMP3/
└── Slayer - [1984] Show No Mercy (RR 34 9868)/
    ├── 01 - Evil Has No Boundaries.mp3
    ├── 02 - Antichrist.mp3
    ...

Reports created:

mp3_reencode_report.json (machine readable)
mp3_reencode_report.txt (human readable)
mp3_reencode_report_folders.txt (OK vs BLOCKED list)
````

Why this beats every other tool
Most tools either:

Encode track-by-track (introduces gaps), or
Don't preserve LAME delay/padding correctly.

