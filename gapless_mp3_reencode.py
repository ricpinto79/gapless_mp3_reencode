#!/usr/bin/env python3
"""
gapless_mp3_reencode.py

Recursively re-encodes lossless albums (FLAC, APE, ALAC/M4A) to gapless-capable MP3 using:
- Option A: single lossless + CUE → decode → album-wide LAME → split by CUE
- Option B: multiple lossless → concat WAV (ffmpeg) → album-wide LAME → generate CUE (python) → split

Polishing included:
- Checks required external tools
- Interactive quality menu
- Reads album identity from CUE header / tags / folder fallback
- Progress bars that MOVE during long steps:
    * decode progress by output WAV size vs expected WAV size
    * LAME progress by parsing LAME percent output
    * Split progress by counting produced MP3 files vs CUE track count
- Explicit START/END logs that do NOT break tqdm rendering
- Writes a final report (JSON + TXT) at end
- Prompts to delete temp folders one-by-one at the end

Consolidated fixes:
- True gapless verification: parse LAME delay/padding fields per track
- Boundary continuity checks (optional, included in report)
- Avoids validation dict mixing crash
- Avoids variable name collision crash
- Fixes LAME progress bar 101% issue

New feature:
- Catalogue number discovery (CUE → tags → folder name)
- Output folder becomes: "Artist - [year] Album (CATNO)" when found
- TXT report prints "Catalog#: (not found)" otherwise
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


# ---- Python dependency checks -------------------------------------------------
def _require_python_pkg(import_name: str, pip_name: Optional[str] = None) -> None:
    try:
        __import__(import_name)
    except ImportError:
        pip = pip_name or import_name
        print(
            f"\nERROR: Missing Python package '{import_name}'. Install with:\n"
            f"  python3 -m pip install {pip}\n"
        )
        sys.exit(2)


_require_python_pkg("tqdm")
_require_python_pkg("mutagen")

from tqdm import tqdm  # noqa: E402
from mutagen.flac import FLAC  # noqa: E402
from mutagen.monkeysaudio import MonkeysAudio  # noqa: E402
from mutagen.mp4 import MP4  # noqa: E402


# ---- External tool checks -----------------------------------------------------
REQUIRED_TOOLS = ["flac", "lame", "mp3splt", "ffmpeg"]


def which(tool: str) -> Optional[str]:
    return shutil.which(tool)


def check_external_tools() -> None:
    missing = [t for t in REQUIRED_TOOLS if which(t) is None]
    if missing:
        print("\nERROR: Missing required system tools:")
        for t in missing:
            print(f"  - {t}")
        print("\nInstall on Debian/Ubuntu/Raspberry Pi OS with:")
        print("  sudo apt update && sudo apt install -y " + " ".join(missing))
        sys.exit(2)


# ---- Logging that doesn't break tqdm ------------------------------------------
def log(msg: str) -> None:
    try:
        tqdm.write(msg)
    except Exception:
        print(msg)


# ---- Helpers -----------------------------------------------------------------
def sanitize_component(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[\/\\:\*\?\"<>\|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip('. ')  # NEW: Remove trailing dots/spaces to avoid Windows FS issues
    return s[:180] if len(s) > 180 else s


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def find_cue_file(folder: Path) -> Optional[Path]:
    cues = sorted(folder.glob("*.cue"))
    return cues[0] if cues else None


def list_lossless(folder: Path) -> List[Path]:
    flacs = folder.glob("*.flac")
    apes = folder.glob("*.ape")
    m4as = folder.glob("*.m4a")
    return sorted(list(flacs) + list(apes) + list(m4as))


def extract_year_from_text(s: str) -> Optional[str]:
    m = re.search(r"(\d{4})", s or "")
    return m.group(1) if m else None


def load_audio(path: Path):
    ext = path.suffix.lower()
    if ext == '.flac':
        return FLAC(str(path))
    elif ext == '.ape':
        return MonkeysAudio(str(path))
    elif ext == '.m4a':
        return MP4(str(path))
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def get_album_tags_from_file(path: Path) -> Dict[str, List[str]]:
    audio = load_audio(path)
    if isinstance(audio, MP4):
        tag_map = {
            '\xa9nam': 'title',
            '\xa9ART': 'artist',
            'aART': 'albumartist',
            '\xa9alb': 'album',
            '\xa9day': 'date',
            '\xa9cmt': 'comment',
        }
        tags = {}
        for k, v in (audio.tags or {}).items():
            # Normalize key
            if k.startswith('----'):
                try:
                    parts = k.split(':', 2)
                    norm_k = parts[2].lower() if len(parts) == 3 else k.lower()
                except:
                    norm_k = k.lower()
            else:
                norm_k = tag_map.get(k, k.lower().replace(' ', ''))

            # Safe handling of value types
            if isinstance(v, (list, tuple)):
                tags[norm_k] = [str(item) for item in v if item is not None]
            elif isinstance(v, bool):
                tags[norm_k] = ["1" if v else "0"]
            elif isinstance(v, (int, float)):
                tags[norm_k] = [str(v)]
            elif v is None:
                tags[norm_k] = []
            else:
                tags[norm_k] = [str(v)]

            # Special handling for trkn / disk (tuples)
            if 'trkn' in audio.tags:
                trkn = audio.tags['trkn']
                if isinstance(trkn, tuple) and len(trkn) >= 1:
                    tags['tracknumber'] = [str(trkn[0])]
                    if len(trkn) >= 2:
                        tags['tracktotal'] = [str(trkn[1])]
            if 'disk' in audio.tags:
                disk = audio.tags['disk']
                if isinstance(disk, tuple) and len(disk) >= 1:
                    tags['discnumber'] = [str(disk[0])]
                    if len(disk) >= 2:
                        tags['disctotal'] = [str(disk[1])]

    else:
        # FLAC / APE
        tags = {str(k).lower(): list(v) for k, v in (audio.tags or {}).items()}
   
    # Normalize common keys
    normalizations = {
        'album artist': 'albumartist',
        'year': 'date',
        'catalog number': 'catalognumber',
        'cat #': 'catno',
        'catalog': 'catalognumber',
    }
    for old, new in normalizations.items():
        if old in tags and new not in tags:
            tags[new] = tags.pop(old)

    return tags


def extract_year_from_tags(tags: Dict[str, List[str]]) -> str:
    for k in ("date", "year", "originaldate", "originalyear"):
        if k in tags and tags[k]:
            y = extract_year_from_text(tags[k][0])
            if y:
                return y
    return "0000"


def parse_cue_metadata(cue_path: Path) -> Dict[str, str]:
    meta = {"performer": "", "title": "", "year": ""}
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return meta

    header = text.split("TRACK", 1)[0]

    def get_quoted(line_re: str) -> Optional[str]:
        m = re.search(line_re, header, flags=re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else None

    performer = get_quoted(r'^\s*PERFORMER\s+"([^"]+)"\s*$')
    title = get_quoted(r'^\s*TITLE\s+"([^"]+)"\s*$')

    year = None
    for pat in [
        r'^\s*REM\s+DATE\s+"?(\d{4})"?\s*$',
        r'^\s*REM\s+ORIGINALDATE\s+"?(\d{4})"?\s*$',
        r'^\s*REM\s+ORIGINALYEAR\s+"?(\d{4})"?\s*$',
        r'^\s*REM\s+YEAR\s+"?(\d{4})"?\s*$',
        r'^\s*DATE\s+"?(\d{4})"?\s*$',
    ]:
        m = re.search(pat, header, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            year = m.group(1)
            break

    meta["performer"] = performer or ""
    meta["title"] = title or ""
    meta["year"] = year or ""
    return meta


# ---- NEW: catalogue number discovery ------------------------------------------
def _is_probable_catalog_number(s: str) -> bool:
    """
    Heuristic: catalog numbers usually contain BOTH letters and digits.
    """
    if not s:
        return False
    s = s.strip()
    has_letter = bool(re.search(r"[A-Za-z]", s))
    has_digit = bool(re.search(r"\d", s))
    return has_letter and has_digit


def _normalize_catalog_number(s: str) -> str:
    """
    Normalize and, if possible, extract a clean catalog token from a noisy string.

    Examples:
      "1993 Japan TOCP-7598 EMI" -> "TOCP-7598"
      "Japan TOCP-7598"          -> "TOCP-7598"
      "TOCP7598"                 -> "TOCP7598"
      "EMI TOCP-7598 (Japan)"    -> "TOCP-7598"
    """
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .,:;-_()[]{}")
    s = s.replace("_", "-")

    # Try to extract a "catalog-like" token from within the string.
    # Common shapes:
    #   ABCD-1234
    #   ABC 1234
    #   ABCD1234
    #   ABCD-12-345 (rare but exists)
    token_patterns = [
        r"\b([A-Z]{2,10}-\d{3,8})\b",            # TOCP-7598, CDP-12345
        r"\b([A-Z]{2,10}\s\d{3,8})\b",           # TOCP 7598
        r"\b([A-Z]{2,10}\d{3,8})\b",             # TOCP7598
        r"\b([A-Z]{2,10}-\d{2,4}-\d{2,6})\b",    # ABC-12-3456
    ]

    upper = s.upper()
    for pat in token_patterns:
        m = re.search(pat, upper)
        if m:
            return m.group(1).replace(" ", "-")

    return s


def parse_cue_catalog_number(cue_path: Path) -> Optional[str]:
    """
    Try to extract a catalog number from a CUE file.
    Looks for common fields like:
      REM CATALOG, REM CATALOGUE, REM CATALOGNUMBER, CATALOG, etc.
    """
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    patterns = [
        r'^\s*REM\s+CATALOG(?:UE)?\s+"?([^"\n]+)"?\s*$',
        r'^\s*REM\s+CATALOGNUMBER\s+"?([^"\n]+)"?\s*$',
        r'^\s*REM\s+CATALOG\s+#?\s+"?([^"\n]+)"?\s*$',
        r'^\s*CATALOG(?:UE)?\s+"?([^"\n]+)"?\s*$',
        r'^\s*CATALOGNUMBER\s+"?([^"\n]+)"?\s*$',
        r'^\s*REM\s+LABELNO\s+"?([^"\n]+)"?\s*$',
    ]

    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            cand = _normalize_catalog_number(m.group(1))
            if _is_probable_catalog_number(cand):
                return cand

    return None


def catalog_number_from_tags(tags: Dict[str, List[str]]) -> Optional[str]:
    """
    Extract catalog number from tags.
    Common keys: catalognumber, catno, labelno
    """
    for k in ("catalognumber", "catno", "labelno", "labelno.", "catalog", "cat #", "catalog number", "catalogue"):
        vals = tags.get(k)
        if vals:
            cand = _normalize_catalog_number(vals[0])
            if _is_probable_catalog_number(cand):
                return cand
    return None


def catalog_number_from_folder_name(folder: Path) -> Optional[str]:
    """
    Try to extract catalog number from folder naming conventions.
    Prefer tokens in (...) or [...].
    """
    name = folder.name

    # 1) Bracketed token: (ABC-123) or [ABC-123]
    m = re.search(r"[\(\[]\s*([A-Za-z0-9][A-Za-z0-9 _\-/\.]{2,60})\s*[\)\]]", name)
    if m:
        cand = _normalize_catalog_number(m.group(1))
        if _is_probable_catalog_number(cand):
            return cand

    # 2) Standalone token must contain letters+digits
    for m in re.finditer(r"\b([A-Za-z]{1,10}[\s_\-/\.]?\d{2,8}[A-Za-z0-9_\-/\.]{0,10})\b", name):
        cand = _normalize_catalog_number(m.group(1))
        if _is_probable_catalog_number(cand):
            return cand

    return None


def discover_catalog_number(folder: Path, files: List[Path], cue: Optional[Path]) -> Optional[str]:
    """
    Priority:
      1) CUE file
      2) tags
      3) Parent folder naming
    """
    # 1) CUE
    if cue and cue.exists():
        c = parse_cue_catalog_number(cue)
        if c:
            return c

    # 2) tags (first file that has it wins)
    for f in files:
        try:
            tags = get_album_tags_from_file(f)
        except Exception:
            continue
        c = catalog_number_from_tags(tags)
        if c:
            return c

    # 3) Folder naming convention
    c = catalog_number_from_folder_name(folder)
    if c:
        return c

    return None


def choose_artist_year_album_catalog(folder: Path, files: List[Path], cue: Optional[Path]) -> Tuple[str, str, str, Optional[str]]:
    artist, year, album = choose_album_identity(folder, files, cue)
    catalog_number = discover_catalog_number(folder, files, cue)
    return artist, year, album, catalog_number


def choose_album_identity(folder: Path, files: List[Path], cue: Optional[Path]) -> Tuple[str, str, str]:
    if cue and cue.exists():
        cm = parse_cue_metadata(cue)
        a = sanitize_component(cm.get("performer", ""))
        t = sanitize_component(cm.get("title", ""))
        y = extract_year_from_text(cm.get("year", "")) or "0000"
        if a and t:
            return a, y, t

    # === ROBUST TAG READING (NEW) ===
    for f in files:
        try:
            tags = get_album_tags_from_file(f)
        except Exception as e:          # catches FLACNoHeaderError, Monkeysaudio errors, etc.
            continue                    # skip corrupted file, try next one

        artist = (tags.get("albumartist") or tags.get("artist") or [""])[0]
        album = (tags.get("album") or [""])[0]
        year = extract_year_from_tags(tags)
        artist = sanitize_component(artist)
        album = sanitize_component(album)

        # Disc number handling (also protected now)
        disc = ""
        if files:
            try:
                disc_tags = get_album_tags_from_file(files[0])  # still use first file for disc info
                disc_num = disc_tags.get("discnumber", [""])[0].strip()
                disc_total = disc_tags.get("disctotal", [""])[0].strip()
                if disc_num and disc_num != "1":
                    disc = " (Disc " + disc_num
                    if disc_total and disc_total != "1":
                        disc += "/" + disc_total
                    disc += ")"
            except Exception:
                disc = ""

        if not disc and cue:
            cue_stem = cue.stem.lower()
            m = re.search(r"(cd|disc)\s*(\d+)", cue_stem)
            if m:
                disc = " (Disc " + m.group(2) + ")"

        album += disc
        if artist and album:
            return artist, year, album

    # Fallback when no valid file could be read
    y = extract_year_from_text(folder.name) or "0000"
    return "Unknown Artist", y, sanitize_component(folder.name) or "Unknown Album"


def cue_track_count(cue_path: Path) -> int:
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0
    return len(re.findall(r"^\s*TRACK\s+\d+\s+AUDIO\s*$", text, flags=re.IGNORECASE | re.MULTILINE))


def cue_index01_times(cue_path: Path) -> Dict[int, str]:
    times: Dict[int, str] = {}
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return times

    track_re = re.compile(r"^\s*TRACK\s+(\d+)\s+AUDIO\s*$", re.IGNORECASE)
    idx_re = re.compile(r"^\s*INDEX\s+01\s+(\d{2}:\d{2}:\d{2})\s*$", re.IGNORECASE)

    cur_track: Optional[int] = None
    for line in text.splitlines():
        m = track_re.match(line)
        if m:
            cur_track = int(m.group(1))
            continue
        m2 = idx_re.match(line)
        if m2 and cur_track is not None:
            times[cur_track] = m2.group(1)
    return times


def cue_referenced_files(cue_path: Path) -> List[str]:
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    files: List[str] = []
    for m in re.finditer(r'^\s*FILE\s+"([^"]+)"', text, flags=re.IGNORECASE | re.MULTILINE):
        files.append(m.group(1).strip())
    return files


def dryrun_check_cue_blockers(folder: Path, cue_path: Path) -> Tuple[bool, List[str]]:
    msgs: List[str] = []

    ntracks = cue_track_count(cue_path)
    if ntracks <= 0:
        msgs.append("BLOCKER: CUE has 0 AUDIO tracks (no TRACK nn AUDIO lines found).")
        return False, msgs

    idx = cue_index01_times(cue_path)
    missing = [t for t in range(1, ntracks + 1) if t not in idx]
    if missing:
        msgs.append(f"BLOCKER: CUE missing INDEX 01 for track(s): {', '.join(f'{t:02d}' for t in missing)}")
        return False, msgs

    # FILE references (optional in some cues, but when present they should exist)
    ref_files = cue_referenced_files(cue_path)
    if ref_files:
        base = cue_path.parent
        missing_files: List[str] = []
        for rf in ref_files:
            p = (base / rf)
            if not p.exists():
                missing_files.append(rf)
        if missing_files:
            msgs.append("BLOCKER: CUE references FILE(s) that do not exist next to the CUE:")
            for mf in missing_files[:10]:
                msgs.append(f"  - {mf}")
            if len(missing_files) > 10:
                msgs.append(f"  ... and {len(missing_files) - 10} more")
            return False, msgs

    msgs.append(f"CUE OK: tracks={ntracks}, all INDEX 01 present" + ("; FILE refs OK" if ref_files else "; no FILE refs"))
    return True, msgs


# ---- True gapless verification ------------------------------------------------
def mp3_lame_delay_padding(mp3_path: Path) -> Tuple[bool, str, Optional[int], Optional[int]]:
    try:
        data = mp3_path.read_bytes()[:65536]
        i = data.find(b"LAME")
        if i < 0:
            if b"Xing" in data or b"Info" in data:
                return False, "found Xing/Info but no LAME tag", None, None
            return False, "no Xing/Info and no LAME tag", None, None

        if i + 9 > len(data):
            return False, "truncated LAME tag (no version)", None, None
        enc_ver = data[i:i + 9].decode("latin1", errors="replace").strip()

        # Standard LAME tag layout offsets:
        # 9 bytes version
        # 1 byte tag rev + vbr method
        # 1 byte lowpass
        # 8 bytes replaygain fields
        # 1 byte encoding flags + ATH
        # 1 byte bitrate
        # 3 bytes delay/padding
        off = i + 9 + 1 + 1 + 8 + 1 + 1
        if off + 3 > len(data):
            return False, f"truncated LAME tag (no delay/padding) ver={enc_ver}", None, None

        b0, b1, b2 = data[off], data[off + 1], data[off + 2]
        delay = (b0 << 4) | (b1 >> 4)
        padding = ((b1 & 0x0F) << 8) | b2

        return True, f"LAME tag ok ver={enc_ver} delay={delay} padding={padding}", delay, padding

    except Exception as e:
        return False, f"error parsing LAME tag: {e}", None, None


# ---- Boundary continuity checks (gapless in practice) -------------------------
def decode_pcm_segment(mp3_path: Path, start_sec: float, dur_sec: float, sr: int = 44100) -> Optional[bytes]:
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-ss", f"{start_sec:.6f}",
        "-t", f"{dur_sec:.6f}",
        "-i", str(mp3_path),
        "-vn",
        "-ac", "2",
        "-ar", str(sr),
        "-f", "s16le",
        "pipe:1",
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _err = p.communicate()
    if p.returncode != 0:
        return None
    return out


def boundary_continuity_check(
    mp3_a: Path,
    mp3_b: Path,
    delay_a: int,
    pad_a: int,
    delay_b: int,
    pad_b: int,
    sr: int = 44100,
    window_ms: int = 200,
) -> Tuple[bool, str]:
    win = window_ms / 1000.0
    extra = 0.100

    cmd = ["ffmpeg", "-i", str(mp3_a)]
    rc, out, err = run_cmd(cmd, quiet=True)
    _text = out + "\n" + err
    m = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.\d+)", _text)
    if not m:
        return False, "could not parse duration for A"

    h, mi, se = int(m.group(1)), int(m.group(2)), float(m.group(3))
    dur_a = h * 3600 + mi * 60 + se

    dur_a = h * 3600 + mi * 60 + se

    a_start = max(0.0, dur_a - (win + extra))
    a_pcm = decode_pcm_segment(mp3_a, a_start, win + extra, sr=sr)
    b_pcm = decode_pcm_segment(mp3_b, 0.0, win + extra, sr=sr)
    if a_pcm is None or b_pcm is None:
        return False, "ffmpeg segment decode failed"

    # s16le stereo: 2ch * 2 bytes = 4 bytes per frame
    bytes_per_frame = 4

    # Trim end padding from A
    trim_end = pad_a * bytes_per_frame
    if 0 < trim_end < len(a_pcm):
        a_pcm = a_pcm[:len(a_pcm) - trim_end]

    # Trim start delay from B
    trim_start = delay_b * bytes_per_frame
    if 0 < trim_start < len(b_pcm):
        b_pcm = b_pcm[trim_start:]

    # Compare overlap (50ms)
    overlap_ms = 50
    overlap_bytes = int(sr * (overlap_ms / 1000.0) * bytes_per_frame)
    if len(a_pcm) < overlap_bytes or len(b_pcm) < overlap_bytes:
        return False, "decoded segments too small after trimming"

    tail = a_pcm[-overlap_bytes:]
    head = b_pcm[:overlap_bytes]

    if tail == head:
        return True, f"perfect match over {overlap_ms}ms"

    import array
    ta = array.array("h", tail)
    hb = array.array("h", head)
    if len(ta) != len(hb):
        return False, "PCM compare length mismatch"

    diff_sq = 0
    for x, y in zip(ta, hb):
        d = x - y
        diff_sq += d * d
    rms = (diff_sq / len(ta)) ** 0.5

    if rms < 2.0:
        return True, f"near-match overlap (RMS diff {rms:.2f})"

    tail_sq = 0
    head_sq = 0
    for x in ta:
        tail_sq += x * x
    for y in hb:
        head_sq += y * y
    rms_tail = (tail_sq / len(ta)) ** 0.5
    rms_head = (head_sq / len(hb)) ** 0.5

    SILENCE_RMS = 80.0  # conservative; can tune later
    # Both sides silent → intentional pause → OK
    if rms_tail < SILENCE_RMS and rms_head < SILENCE_RMS:
        return True, f"silence boundary (tail RMS {rms_tail:.1f}, head RMS {rms_head:.1f})"

    # One side silent and the other not → suspicious (possible gap/truncation) → FAIL
    if (rms_tail < SILENCE_RMS) != (rms_head < SILENCE_RMS):
        return False, (
            f"possible gap (one side silent) "
            f"(tail RMS {rms_tail:.1f}, head RMS {rms_head:.1f}, diff RMS {rms:.2f})"
        )

    # Neither side is silent → continuous program material (live ambience etc.) → OK
    return True, (
        f"continuous boundary (non-silent) "
        f"(tail RMS {rms_tail:.1f}, head RMS {rms_head:.1f}, diff RMS {rms:.2f})"
    )


# ---- Command runner with START/END logs --------------------------------------
def shlex_quote(s: str) -> str:
    if re.search(r"\s", s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def run_cmd(cmd: List[str], cwd: Optional[Path] = None, quiet: bool = False) -> Tuple[int, str, str]:
    cmd_str = " ".join([shlex_quote(c) for c in cmd])
    if not quiet:
        log(f"\n[START] {cmd_str}")
        if cwd:
            log(f"       (cwd: {cwd})")
        sys.stdout.flush()

    p = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = p.communicate()

    if not quiet:
        log(f"[END]   rc={p.returncode}")
        if p.returncode != 0:
            log("----- stderr (last 40 lines) -----")
            log("\n".join(err.splitlines()[-40:]))
    sys.stdout.flush()

    return p.returncode, out, err


# ---- Progress helpers that make tqdm MOVE ------------------------------------
def expected_wav_bytes_from_lossless(path: Path) -> Optional[int]:
    """
    Compute expected PCM WAV size from metadata.
    WAV header is ~44 bytes; we approximate.
    """
    try:
        audio = load_audio(path)
        info = audio.info
        if not info:
            return None
        sample_rate = getattr(info, "sample_rate", None)
        channels = getattr(info, "channels", None)
        bits_per_sample = getattr(info, "bits_per_sample", None)
        if None in (sample_rate, channels, bits_per_sample):
            return None
        if hasattr(info, "samples"):
            total_samples = info.samples
        else:
            total_samples = round(info.length * sample_rate)
        bytes_per_sample = int(bits_per_sample) // 8
        pcm_bytes = int(total_samples) * int(channels) * bytes_per_sample
        return pcm_bytes + 44
    except Exception:
        return None


def run_decode_with_progress(input_path: Path, wav_out: Path, use_progress: bool) -> Tuple[bool, str]:
    ext = input_path.suffix.lower()
    if ext == '.flac':
        cmd = ["flac", "-d", "-f", "-o", str(wav_out), str(input_path)]
    else:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(input_path), "-f", "wav", "-y", str(wav_out)]
    exp = expected_wav_bytes_from_lossless(input_path)

    cmd_str = " ".join([shlex_quote(c) for c in cmd])
    log(f"\n[START] {cmd_str}")
    log(f"       (cwd: {input_path.parent})")

    p = subprocess.Popen(cmd, cwd=str(input_path.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if use_progress and exp and exp > 0:
        bar = tqdm(total=exp, desc="2/6 to_wav (decode)", unit="B", unit_scale=True, ascii=True, leave=True, file=sys.stderr)
        last = 0
        try:
            while p.poll() is None:
                if wav_out.exists():
                    cur = wav_out.stat().st_size
                    if cur > last:
                        bar.update(cur - last)
                        last = cur
                time.sleep(0.2)
            if wav_out.exists():
                cur = wav_out.stat().st_size
                if cur > last:
                    bar.update(cur - last)
            bar.close()
        except Exception:
            bar.close()
    else:
        if use_progress:
            bar = tqdm(total=0, desc="2/6 to_wav (decode)", unit="tick", ascii=True, leave=True, file=sys.stderr)
            try:
                while p.poll() is None:
                    bar.update(1)
                    time.sleep(0.2)
                bar.close()
            except Exception:
                bar.close()

    out, err = p.communicate()
    rc = p.returncode

    log(f"[END]   rc={rc}")
    if rc != 0:
        log("----- stderr (last 40 lines) -----")
        log("\n".join(err.splitlines()[-40:]))

    if rc != 0:
        return False, f"decode failed ({rc})"
    return True, f"decoded single to album wav: {wav_out.name}"


def run_lame_with_progress(wav_in: Path, mp3_out: Path, cmd: List[str], cwd: Path, use_progress: bool) -> Tuple[bool, str]:
    """
    LAME prints progress like:  53/100 (53%)| ...
    We'll parse percent and update tqdm(total=100).
    """
    cmd_str = " ".join([shlex_quote(c) for c in cmd])
    log(f"\n[START] {cmd_str}")
    log(f"       (cwd: {cwd})")

    p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    percent_re = re.compile(r"(\d{1,3})%")
    last_pct = 0  # FIX: avoid 101%

    bar = None
    if use_progress:
        bar = tqdm(total=100, desc="3/6 encode_mp3 (lame)", unit="%", ascii=True, leave=True, file=sys.stderr)

    stderr_lines: List[str] = []
    try:
        while True:
            line = p.stderr.readline()
            if line == "" and p.poll() is not None:
                break
            if line:
                stderr_lines.append(line.rstrip("\n"))
                m = percent_re.search(line)
                if m and bar is not None:
                    pct = int(m.group(1))
                    if pct > 100:
                        pct = 100
                    if pct > last_pct:
                        bar.update(pct - last_pct)
                        last_pct = pct
    finally:
        out, err = p.communicate()
        rc = p.returncode
        if bar is not None:
            if last_pct < 100:
                bar.update(100 - last_pct)
            bar.close()

    log(f"[END]   rc={rc}")
    if rc != 0:
        log("----- stderr (last 40 lines) -----")
        log("\n".join((("\n".join(stderr_lines) + "\n" + err).splitlines())[-40:]))

    if rc != 0:
        return False, f"lame encode failed ({rc})"
    return True, "encoded album wav to album mp3 with LAME"


def run_mp3splt_with_progress(album_mp3: Path, cue: Path, out_dir: Path, use_progress: bool) -> Tuple[bool, str]:
    ensure_dir(out_dir)
    total_tracks = cue_track_count(cue)

    cmd = ["mp3splt", "-Q", "-b", "-c", str(cue), "-d", str(out_dir), "-o", "@n - @t", str(album_mp3)]
    cmd_str = " ".join([shlex_quote(c) for c in cmd])

    log(f"\n[START] {cmd_str}")
    log(f"       (cwd: {album_mp3.parent})")

    p = subprocess.Popen(cmd, cwd=str(album_mp3.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    bar = None
    if use_progress and total_tracks > 0:
        bar = tqdm(total=total_tracks, desc="4/6 split_tracks (mp3splt)", unit="trk", ascii=True, leave=True, file=sys.stderr)

    last_count = 0
    try:
        while p.poll() is None:
            cur_count = len(list(out_dir.glob("*.mp3")))
            if bar is not None and cur_count > last_count:
                bar.update(cur_count - last_count)
            last_count = cur_count
            time.sleep(0.2)
    finally:
        out, err = p.communicate()
        rc = p.returncode
        if bar is not None:
            cur_count = len(list(out_dir.glob("*.mp3")))
            if cur_count > last_count:
                bar.update(cur_count - last_count)
            bar.close()

    log(f"[END]   rc={rc}")
    if rc != 0:
        log("----- stderr (last 40 lines) -----")
        log("\n".join(err.splitlines()[-40:]))

    if rc != 0:
        return False, f"mp3splt failed ({rc})"
    return True, "split album mp3 into tracks using cue via mp3splt"


# ---- Interactive quality menu -------------------------------------------------
BITRATES = [96, 128, 160, 192, 224, 256, 320]


@dataclass
class EncodeSettings:
    mode: str  # "VBR" or "CBR"
    vbr_quality: int = 0     # NEW: LAME -V0..-V9 (0=highest quality)
    vbr_min: int = 192
    vbr_max: int = 320
    cbr: int = 320
    true_stereo: bool = True


def prompt_menu_int(title: str, options: List[str], default_index: int = 0) -> int:
    print("\n" + title)
    for i, opt in enumerate(options):
        print(f"{i}) {opt}")
    while True:
        raw = input(f"Pick a number [{default_index}]: ").strip()
        if raw == "":
            return default_index
        if raw.isdigit():
            n = int(raw)
            if 0 <= n < len(options):
                return n
        print("Invalid selection. Try again.")


def prompt_bitrate(title: str, allowed: List[int], default: int) -> int:
    opts = [str(b) for b in allowed]
    default_index = opts.index(str(default)) if str(default) in opts else 0
    idx = prompt_menu_int(title, opts, default_index=default_index)
    return allowed[idx]


def prompt_vbr_quality(default: int = 0) -> int:
    opts = [
        "-V0 (highest quality / typically larger files)",
        "-V1 (very high quality)",
        "-V2 (common sweet spot: high quality vs size)",
        "-V3 (good quality / smaller files)",
        "-V4 (good-ish / smaller)",
        "-V5 (medium quality)",
        "-V6 (lower quality)",
        "-V7 (low quality)",
        "-V8 (very low quality)",
        "-V9 (lowest quality / smallest files)",
    ]
    default_index = default if 0 <= default <= 9 else 0
    idx = prompt_menu_int("Select LAME VBR quality (lower is better):", opts, default_index=default_index)
    return idx


def prompt_encode_settings() -> EncodeSettings:
    top = prompt_menu_int(
        "Select desired quality mode:",
        [
            "Default (VBR, min 192 kbps, max 320 kbps, choose -V, stereo choice)",
            "VBR (choose min and max bitrate, choose -V, stereo choice)",
            "CBR (choose fixed bitrate, stereo choice)",
        ],
        default_index=0,
    )

    # Stereo mode prompt (default Yes = true stereo)
    print("\nStereo mode:")
    print("  - Yes: True stereo (-m s) → keeps L/R fully independent (often larger files).")
    print("  - No: Joint stereo (-m j) → standard efficient stereo (often same quality with fewer bits).")
    true_stereo = prompt_yes_no("Force TRUE stereo?", default=True)

    if top == 0:
        # NEW: always ask -V for VBR paths, default to 0
        vq = prompt_vbr_quality(default=0)
        return EncodeSettings(mode="VBR", vbr_quality=vq, vbr_min=192, vbr_max=320, true_stereo=true_stereo)

    if top == 1:
        vmin = prompt_bitrate("Lowest bitrate (VBR min):", BITRATES, default=192)
        vmax = prompt_bitrate("Highest bitrate (VBR max):", BITRATES, default=320)
        if vmax < vmin:
            print(f"Note: max {vmax} < min {vmin}. Swapping them.")
            vmin, vmax = vmax, vmin
        # NEW: always ask -V for VBR paths, default to 0
        vq = prompt_vbr_quality(default=0)
        return EncodeSettings(mode="VBR", vbr_quality=vq, vbr_min=vmin, vbr_max=vmax, true_stereo=true_stereo)

    cbr = prompt_bitrate("CBR bitrate:", BITRATES, default=320)
    return EncodeSettings(mode="CBR", cbr=cbr, true_stereo=true_stereo)


def prompt_yes_no(question: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        ans = input(f"{question} [{d}]: ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("Please answer y or n.")


# ---- Reporting ----------------------------------------------------------------
@dataclass
class StepResult:
    name: str
    ok: bool
    details: str
    outputs: List[str] = field(default_factory=list)


@dataclass
class AlbumReport:
    source_folder: str
    detected_files: List[str]
    detected_cue: Optional[str]
    mode_used: str = ""
    artist: str = ""
    year: str = ""
    album: str = ""
    output_folder: str = ""
    catalog_number: str = ""
    steps: List[StepResult] = field(default_factory=list)
    validations: List[Dict[str, object]] = field(default_factory=list)
    verdict: str = "UNKNOWN"
    temps: List[str] = field(default_factory=list)


@dataclass
class RunReport:
    root: str
    started_at: str
    finished_at: str = ""
    encode_settings: Dict[str, object] = field(default_factory=dict)
    albums: List[AlbumReport] = field(default_factory=list)
    summary: Dict[str, object] = field(default_factory=dict)


def _normalize_folder_key(p: Path) -> str:
    try:
        return str(p.expanduser().resolve())
    except Exception:
        return str(p)


def load_dryrun_lists(dryrun_json_path: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    try:
        data = json.loads(dryrun_json_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}, {}

    ok_map: Dict[str, str] = {}
    blocked_map: Dict[str, str] = {}

    albums = data.get("albums") or []
    for a in albums:
        src = a.get("source_folder") or ""
        if not src:
            continue
        key = _normalize_folder_key(Path(src))

        verdict = str(a.get("verdict") or "")
        if verdict.startswith("DRYRUN PASS"):
            ok_map[key] = verdict
        elif verdict.startswith("DRYRUN FAIL"):
            # Try to capture a short reason
            m = re.search(r"^DRYRUN FAIL\s*\(blocking issue\):\s*(.+)$", verdict)
            if m:
                reason = m.group(1).strip()
            else:
                reason = verdict
                try:
                    vals = a.get("validations") or []
                    blk = next((v for v in vals if "dryrun_blockers" in v), None)
                    if blk and blk.get("dryrun_blockers"):
                        reason = str(blk["dryrun_blockers"][0]).replace("BLOCKER: ", "").strip()
                except Exception:
                    pass
            blocked_map[key] = reason

    return ok_map, blocked_map


def discover_album_folders(root: Path) -> List[Path]:
    album_folders: List[Path] = []
    skip_names = {"MP3", "ConversionTemp"}  # NEW: skip ConversionTemp too
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_names and not d.startswith(".")]
        if any(fn.lower().endswith((".flac", ".ape", ".m4a")) for fn in filenames):
            album_folders.append(Path(dirpath))
    return sorted(set(album_folders))


# ---- Simple decode for Option A single lossless -------------------------------
def decode_single_to_wav(input_path: Path, wav_out: Path, use_progress: bool) -> Tuple[bool, str]:
    return run_decode_with_progress(input_path, wav_out, use_progress)


def mp3splt_split_with_cue(album_mp3: Path, cue: Path, out_dir: Path, use_progress: bool) -> Tuple[bool, str]:
    return run_mp3splt_with_progress(album_mp3, cue, out_dir, use_progress)


# ---- Track rename helpers ------------------------------------------------------
def parse_cue_track_titles(cue_path: Path) -> List[Tuple[int, str]]:
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines = text.splitlines()

    tracks: List[Tuple[int, str]] = []
    current_track: Optional[int] = None
    current_title: Optional[str] = None

    track_re = re.compile(r"^\s*TRACK\s+(\d+)\s+AUDIO\s*$", re.IGNORECASE)
    title_re = re.compile(r'^\s*TITLE\s+"([^"]+)"\s*$', re.IGNORECASE)

    for ln in lines:
        m = track_re.match(ln)
        if m:
            if current_track is not None and current_title:
                tracks.append((current_track, current_title.strip()))
            current_track = int(m.group(1))
            current_title = None
            continue

        if current_track is not None:
            mt = title_re.match(ln)
            if mt:
                current_title = mt.group(1)
                continue

    if current_track is not None and current_title:
        tracks.append((current_track, current_title.strip()))

    return tracks


def _extract_num_and_title_from_filename(name: str) -> Optional[Tuple[int, str]]:
    m = re.match(r"^\s*(\d+)\s*-\s*(.+?)\s*\.mp3\s*$", name, flags=re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1)), m.group(2).strip()


def rename_tracks_from_cue_with_fallback(out_album_folder: Path, cue_path: Optional[Path]) -> Tuple[bool, str]:
    mp3s = sorted(out_album_folder.glob("*.mp3"))
    if not mp3s:
        return False, "no mp3 files to rename"

    cue_tracks: List[Tuple[int, str]] = []
    if cue_path and cue_path.exists():
        cue_tracks = parse_cue_track_titles(cue_path)

    total_tracks = len(cue_tracks) if cue_tracks else len(mp3s)
    width = max(2, len(str(total_tracks)))

    cue_map: Dict[int, str] = {}
    if cue_tracks:
        for n, t in cue_tracks:
            cue_map[int(n)] = sanitize_component(t)

    plan: List[Tuple[Path, Path]] = []
    used_targets: set = set()

    for idx, src in enumerate(mp3s, start=1):
        parsed = _extract_num_and_title_from_filename(src.name)
        if parsed:
            n, existing_title = parsed
        else:
            n, existing_title = idx, src.stem

        title = cue_map.get(n) or sanitize_component(existing_title) or f"Track {n}"
        new_name = f"{n:0{width}d} - {title}.mp3"
        dst = out_album_folder / new_name

        if str(dst).lower() in used_targets:
            dst = out_album_folder / f"{n:0{width}d} - {title} ({idx}).mp3"
        used_targets.add(str(dst).lower())

        if dst.name != src.name:
            plan.append((src, dst))

    if not plan:
        if cue_tracks:
            return True, "track names already match CUE titles/padding"
        return True, "track names already normalized (fallback)"

    tmp_pairs: List[Tuple[Path, Path]] = []
    for src, dst in plan:
        tmp = src.with_name(src.name + ".__renametmp__")
        if tmp.exists():
            tmp.unlink()
        src.rename(tmp)
        tmp_pairs.append((tmp, dst))

    for tmp, dst in tmp_pairs:
        if dst.exists():
            dst.unlink()
        tmp.rename(dst)

    if cue_tracks:
        return True, f"renamed {len(plan)} track(s) using CUE titles with {width}-digit padding"
    return True, f"renamed {len(plan)} track(s) using fallback naming with {width}-digit padding"


# ---- Processing primitives ----------------------------------------------------
def secs_to_cue_time(secs: float) -> str:
    total_secs = int(secs)
    frac = secs - total_secs
    mins = total_secs // 60
    ss = total_secs % 60
    ff = int(round(frac * 75))
    if ff >= 75:
        ff -= 75
        ss += 1
    if ss >= 60:
        ss -= 60
        mins += 1
    return f"{mins:02d}:{ss:02d}:{ff:02d}"


def concat_lossless_to_wav(files: List[Path], wav_out: Path, cwd: Path, use_progress: bool) -> Tuple[bool, str]:
    if not files:
        return False, "no files for concat"

    inputs = sum([["-i", str(f)] for f in files], [])
    filter_parts = "".join(f"[{i}:a]" for i in range(len(files)))
    filter_complex = f"{filter_parts}concat=n={len(files)}:v=0:a=1[a]"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
    ] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[a]",
        "-f", "wav", "-y", str(wav_out)
    ]

    # Expected total size for progress (sum of individual expected WAV sizes)
    exp_total = 0
    for f in files:
        exp = expected_wav_bytes_from_lossless(f)
        if exp:
            exp_total += exp
        else:
            exp_total = 0  # If any missing, fallback to no size-based progress
            break

    cmd_str = " ".join([shlex_quote(c) for c in cmd])
    log(f"\n[START] {cmd_str}")
    log(f"       (cwd: {cwd})")

    p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    bar = None
    if use_progress:
        if exp_total > 0:
            bar = tqdm(total=exp_total, desc="2/6 to_wav (ffmpeg concat)", unit="B", unit_scale=True, ascii=True, leave=True, file=sys.stderr)
            last = 0
            try:
                while p.poll() is None:
                    if wav_out.exists():
                        cur = wav_out.stat().st_size
                        if cur > last:
                            bar.update(cur - last)
                            last = cur
                    time.sleep(0.2)
                if wav_out.exists():
                    cur = wav_out.stat().st_size
                    if cur > last:
                        bar.update(cur - last)
            finally:
                if bar:
                    bar.close()
        else:
            # Fallback to tick-based if expected size unknown
            bar = tqdm(total=0, desc="2/6 to_wav (ffmpeg concat)", unit="tick", ascii=True, leave=True, file=sys.stderr)
            try:
                while p.poll() is None:
                    bar.update(1)
                    time.sleep(0.2)
            finally:
                if bar:
                    bar.close()

    out, err = p.communicate()
    rc = p.returncode

    log(f"[END]   rc={rc}")
    if rc != 0:
        log("----- stderr (last 40 lines) -----")
        log("\n".join(err.splitlines()[-40:]))

    if rc != 0:
        return False, f"ffmpeg concat failed ({rc})"
    return True, f"concatenated {len(files)} files to album wav: {wav_out.name}"


def generate_cue(files: List[Path], cue_out: Path, cwd: Path) -> Tuple[bool, str]:
    if not files:
        return False, "no files for cue generation"

    first_tags = get_album_tags_from_file(files[0])
    performer = (first_tags.get("albumartist") or first_tags.get("artist") or ["Unknown Artist"])[0]
    album = (first_tags.get("album") or [files[0].parent.name or "Unknown Album"])[0]

    lines = []
    if performer:
        lines.append(f'PERFORMER "{performer}"')
    if album:
        lines.append(f'TITLE "{album}"')
    lines.append('FILE "album" WAVE')

    cum_secs = 0.0
    for idx, f in enumerate(files, 1):
        audio = load_audio(f)
        tags = get_album_tags_from_file(f)
        title = (tags.get("title") or [f.stem])[0]
        lines.append(f'  TRACK {idx:02d} AUDIO')
        lines.append(f'    TITLE "{title}"')
        if performer:
            lines.append(f'    PERFORMER "{performer}"')
        lines.append(f'    INDEX 01 {secs_to_cue_time(cum_secs)}')
        cum_secs += audio.info.length

    cue_text = "\n".join(lines)
    cue_out.write_text(cue_text, encoding="utf-8", errors="replace")
    return True, f"generated cue: {cue_out.name}"


def lame_encode_album_wav_to_mp3(wav_in: Path, mp3_out: Path, enc: EncodeSettings, use_progress: bool) -> Tuple[bool, str]:
    # Stereo mode is user-controlled.
    # True stereo  -> -m s
    # Joint stereo -> -m j
    stereo_flag = "s" if enc.true_stereo else "j"
    stereo_desc = "true stereo (-m s)" if enc.true_stereo else "joint stereo (-m j)"

    base = ["lame", "-m", stereo_flag]

    if enc.mode == "VBR":
        vq = int(enc.vbr_quality)
        if vq < 0:
            vq = 0
        if vq > 9:
            vq = 9
        cmd = base + [f"-V{vq}", "-b", str(enc.vbr_min), "-B", str(enc.vbr_max), str(wav_in), str(mp3_out)]
        desc = f"LAME VBR -V{vq} bounds {enc.vbr_min}..{enc.vbr_max} kbps; {stereo_desc}"
    else:
        cmd = base + ["-b", str(enc.cbr), "--cbr", str(wav_in), str(mp3_out)]
        desc = f"LAME CBR {enc.cbr} kbps; {stereo_desc}"

    # Include exact command line used (recorded in report)
    cmd_str = " ".join([shlex_quote(c) for c in cmd])
    desc = f"{desc}\n      lame_cmd: {cmd_str}"

    ok, msg = run_lame_with_progress(wav_in, mp3_out, cmd, cwd=wav_in.parent, use_progress=use_progress)
    if not ok:
        return False, msg
    return True, desc


# ---- Main album processing -----------------------------------------------------
def process_album_folder(
    folder: Path,
    out_root: Path,
    temp_root: Path,
    enc: EncodeSettings,
    use_progress: bool = True,
    dry_run: bool = False,
) -> Union[AlbumReport, List[AlbumReport]]:
    files = list_lossless(folder)
    cues = sorted(folder.glob("*.cue"))

    cd_groups = {}
    for f in files:
        m = re.search(r"-CD(\d+)-", f.stem)
        if m:
            cd_num = m.group(1)
            cd_groups.setdefault(cd_num, []).append(f)

    if len(cd_groups) > 1:
        reports = []
        for cd_num, group_files in cd_groups.items():
            cd_cue = next((c for c in cues if "-CD" + cd_num + "-" in c.stem), None)
            # Choose identity for this disc
            artist, year, album, catalog_number = choose_artist_year_album_catalog(folder, group_files, cd_cue)
            artist = sanitize_component(artist)
            year = sanitize_component(year or "")
            album = sanitize_component(album)
            catalog_number = sanitize_component(catalog_number)
            # Append "(Disc {cd_num})" to album title
            album = album + " (Disc " + cd_num + ")"
            catalog_suffix = ""
            if catalog_number:
                cat_clean = catalog_number
                if cat_clean and (cat_clean.lower() not in album.lower()):
                    catalog_suffix = " (" + cat_clean + ")"
            out_album_folder = out_root / (artist + " - [" + year + "] " + album + catalog_suffix)
            if not dry_run:
                ensure_dir(out_album_folder)
            report_disc = AlbumReport(
                source_folder=str(folder),
                detected_files=[str(f) for f in group_files],
                detected_cue=str(cd_cue) if cd_cue else None,
                mode_used="",
                artist=artist,
                year=year,
                album=album,
                output_folder=str(out_album_folder),
                catalog_number=catalog_number or "",
            )
            album_name = artist + " - [" + year + "] " + album + catalog_suffix
            album_temp_dir = temp_root / album_name
            if not dry_run:
                ensure_dir(album_temp_dir)
            album_wav = album_temp_dir / "_step1_album.wav"
            album_mp3 = album_temp_dir / "_step2_album.mp3"
            split_dir = album_temp_dir / "_step3_split_tracks"
            generated_cue = album_temp_dir / "_generated_album.cue"
            option_a = bool(cd_cue) and len(group_files) == 1
            report_disc.mode_used = "A" if option_a else "B"
            # ---- DRY RUN: analyze only, no filesystem changes, no external commands ----
            if dry_run:
                report_disc.steps.append(StepResult("prepare", True, f"DRY RUN: no folders created, no encoding. Would use temp dir: {album_temp_dir}"))

                blockers: List[str] = []

                if not group_files:
                    blockers.append("BLOCKER: no lossless files found (unexpected).")

                # Option A requires a CUE file and exactly one file
                if option_a:
                    if not cd_cue or not cd_cue.exists():
                        blockers.append("BLOCKER: Option A selected but no CUE file found.")
                    else:
                        okc, msgs = dryrun_check_cue_blockers(folder, cd_cue)
                        if not okc:
                            blockers.extend(msgs)
                        else:
                            report_disc.steps.append(StepResult("cue_check", True, "DRY RUN: " + " | ".join(msgs)))
                else:
                    # Option B can generate CUE via python; existing CUE is optional.
                    if cd_cue and cd_cue.exists():
                        okc, msgs = dryrun_check_cue_blockers(folder, cd_cue)
                        if not okc:
                            blockers.extend(msgs)
                        else:
                            report_disc.steps.append(StepResult("cue_check", True, "DRY RUN (optional cue): " + " | ".join(msgs)))
                    else:
                        report_disc.steps.append(StepResult("cue_check", True, "DRY RUN: no CUE found; Option B would generate one via python."))

                # Build "would run" commands (strings only)
                album_wav = album_temp_dir / "_step1_album.wav"
                album_mp3 = album_temp_dir / "_step2_album.mp3"
                split_dir = album_temp_dir / "_step3_split_tracks"
                generated_cue = album_temp_dir / "_generated_album.cue"

                if option_a and group_files:
                    ext = group_files[0].suffix.lower()
                    if ext == '.flac':
                        dec_cmd = ["flac", "-d", "-f", "-o", str(album_wav), str(group_files[0])]
                    else:
                        dec_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(group_files[0]), "-f", "wav", "-y", str(album_wav)]
                    report_disc.steps.append(StepResult("A1_decode_to_album_wav", True, "SKIP (dry run)\n      would_run: " + " ".join(shlex_quote(c) for c in dec_cmd)))
                    cue_to_use = cd_cue
                else:
                    inputs = " ".join(f' -i "{f.name}"' for f in group_files)
                    filter_parts = "".join(f"[{i}:a]" for i in range(len(group_files)))
                    filter_complex = f' -filter_complex "{filter_parts}concat=n={len(group_files)}:v=0:a=1[a]" -map "[a]"'
                    ff_cmd = f'ffmpeg -hide_banner -loglevel error{inputs}{filter_complex} -f wav -y "{album_wav}"'
                    report_disc.steps.append(StepResult("B1_concat_to_album_wav", True, "SKIP (dry run)\n      would_run: " + ff_cmd))
                    report_disc.steps.append(StepResult("B3_generate_cue", True, "SKIP (dry run)\n      would_generate: python cue to " + str(generated_cue)))
                    cue_to_use = generated_cue

                # LAME command (matches real behavior)
                stereo_flag = "s" if enc.true_stereo else "j"
                if enc.mode == "VBR":
                    vq = int(enc.vbr_quality)
                    if vq < 0:
                        vq = 0
                    if vq > 9:
                        vq = 9
                    lame_cmd = ["lame", "-m", stereo_flag, f"-V{vq}", "-b", str(enc.vbr_min), "-B", str(enc.vbr_max), str(album_wav), str(album_mp3)]
                else:
                    lame_cmd = ["lame", "-m", stereo_flag, "-b", str(enc.cbr), "--cbr", str(album_wav), str(album_mp3)]
                report_disc.steps.append(StepResult(f"{report_disc.mode_used}2_lame_encode_album_mp3", True, "SKIP (dry run)\n      would_run: " + " ".join(shlex_quote(c) for c in lame_cmd)))

                # mp3splt command
                splt_cmd = ["mp3splt", "-Q", "-b", "-c", str(cue_to_use), "-d", str(split_dir), "-o", "@n - @t", str(album_mp3)]
                report_disc.steps.append(StepResult(f"{report_disc.mode_used}3_split_with_mp3splt", True, "SKIP (dry run)\n      would_run: " + " ".join(shlex_quote(c) for c in splt_cmd)))

                report_disc.steps.append(StepResult("organize_output", True, "SKIP (dry run)"))
                report_disc.steps.append(StepResult("rename_tracks_from_cue", True, "SKIP (dry run)"))
                report_disc.steps.append(StepResult("validate", True, "SKIP (dry run)"))

                if blockers:
                    report_disc.verdict = "DRYRUN FAIL (blocking issue): " + blockers[0].replace("BLOCKER: ", "")
                    # keep the full list in validations so it’s visible in reports
                    report_disc.validations = [{"dryrun_blockers": blockers}]
                else:
                    report_disc.verdict = "DRYRUN PASS (no blocking issues found)"
                    report_disc.validations = [{"dryrun_blockers": []}]

                report_disc.steps.append(StepResult("dry_run_verdict", True, report_disc.verdict))
                # No temps to delete in dry run (we did not create them)
                report_disc.temps = []
            else:
                # Step 1/6: prepare
                if use_progress:
                    bar1 = tqdm(total=1, desc="1/6 prepare", unit="step", ascii=True, leave=True, file=sys.stderr)
                    report_disc.steps.append(StepResult("prepare", True, f"temp dir: {album_temp_dir}"))
                    bar1.update(1)
                    bar1.close()
                else:
                    report_disc.steps.append(StepResult("prepare", True, f"temp dir: {album_temp_dir}"))

                # Step 2/6: to_wav
                if option_a:
                    ok, details = decode_single_to_wav(group_files[0], album_wav, use_progress=use_progress)
                    report_disc.steps.append(StepResult("A1_decode_to_album_wav", ok, details, outputs=[str(album_wav)] if ok else []))
                else:
                    ok, details = concat_lossless_to_wav(group_files, album_wav, cwd=folder, use_progress=use_progress)
                    report_disc.steps.append(StepResult("B1_concat_to_album_wav", ok, details, outputs=[str(album_wav)] if ok else []))

                if not ok:
                    report_disc.verdict = "FAIL (could not create album WAV)"
                    report_disc.temps.append(str(album_temp_dir))
                    reports.append(report_disc)
                    continue

                # Step 3/6: encode_mp3
                ok, details = lame_encode_album_wav_to_mp3(album_wav, album_mp3, enc, use_progress=use_progress)
                report_disc.steps.append(StepResult(f"{report_disc.mode_used}2_lame_encode_album_mp3", ok, details, outputs=[str(album_mp3)] if ok else []))
                if not ok:
                    report_disc.verdict = "FAIL (LAME encode failed)"
                    report_disc.temps.append(str(album_temp_dir))
                    reports.append(report_disc)
                    continue

                # Step 4/6: split_tracks
                cue_to_use = cd_cue
                if not option_a:
                    okc, detc = generate_cue(group_files, generated_cue, cwd=folder)
                    report_disc.steps.append(StepResult("B3_generate_cue", okc, detc, outputs=[str(generated_cue)] if okc else []))
                    if not okc:
                        report_disc.verdict = "FAIL (could not generate cue)"
                        report_disc.temps.append(str(album_temp_dir))
                        reports.append(report_disc)
                        continue
                    cue_to_use = generated_cue

                oks, dets = mp3splt_split_with_cue(album_mp3, cue_to_use, split_dir, use_progress=use_progress)
                report_disc.steps.append(StepResult(f"{report_disc.mode_used}3_split_with_mp3splt", oks, dets, outputs=[str(split_dir)] if oks else []))
                if not oks:
                    report_disc.verdict = "FAIL (mp3 splitting failed)"
                    report_disc.temps.append(str(album_temp_dir))
                    reports.append(report_disc)
                    continue

                # Step 5/6: organize output
                if use_progress:
                    bar5 = tqdm(total=1, desc="5/6 organize", unit="step", ascii=True, leave=True, file=sys.stderr)

                split_mp3s = sorted(split_dir.glob("*.mp3"))
                moved: List[str] = []
                for mp3 in split_mp3s:
                    dest = out_album_folder / mp3.name
                    if dest.exists():
                        dest.unlink()
                    shutil.move(str(mp3), str(dest))
                    moved.append(str(dest))

                report_disc.steps.append(StepResult("organize_output", True, f"moved {len(moved)} tracks to {out_album_folder}", outputs=moved))
                if use_progress:
                    bar5.update(1)
                    bar5.close()

                # Track filename polish
                rok, rmsg = rename_tracks_from_cue_with_fallback(out_album_folder, cue_to_use)
                report_disc.steps.append(StepResult("rename_tracks_from_cue", rok, rmsg))

                # Step 6/6: validate
                if use_progress:
                    bar6 = tqdm(total=1, desc="6/6 validate", unit="step", ascii=True, leave=True, file=sys.stderr)

                mp3s = sorted(out_album_folder.glob("*.mp3"))
                track_validations: List[Dict[str, object]] = []

                for mp3 in mp3s:
                    ok_gapless, reason, delay, padding = mp3_lame_delay_padding(mp3)
                    track_validations.append({
                        "file": str(mp3),
                        "gapless_tag_ok": ok_gapless,
                        "lame_check": reason,
                        "encoder_delay_samples": delay,
                        "encoder_padding_samples": padding,
                    })

                # Boundary checks (stored separately to avoid mixing dict types)
                boundary_checks: List[Dict[str, object]] = []
                if len(mp3s) >= 2:
                    for i in range(len(mp3s) - 1):
                        mp3_a = mp3s[i]
                        mp3_next = mp3s[i + 1]
                        va = track_validations[i]
                        vb = track_validations[i + 1]

                        okb, detb = boundary_continuity_check(
                            mp3_a,
                            mp3_next,
                            int(va.get("encoder_delay_samples") or 0),
                            int(va.get("encoder_padding_samples") or 0),
                            int(vb.get("encoder_delay_samples") or 0),
                            int(vb.get("encoder_padding_samples") or 0),
                        )
                        boundary_checks.append({
                            "from": mp3_a.name,
                            "to": mp3_next.name,
                            "ok": okb,
                            "details": detb,
                        })

                report_disc.validations = [
                    {"tracks": track_validations},
                    {"boundary_checks": boundary_checks},
                ]

                # Verdict
                if not mp3s:
                    report_disc.verdict = "FAIL (no MP3s produced)"
                else:
                    bad_tags = [Path(v["file"]).name for v in track_validations if not v.get("gapless_tag_ok")]
                    if bad_tags:
                        report_disc.verdict = "WARN (gapless fields missing/unreadable): " + ", ".join(bad_tags[:5]) + (" ..." if len(bad_tags) > 5 else "")
                    else:
                        bad_bounds = [f'{b["from"]}→{b["to"]}' for b in boundary_checks if not b.get("ok")]
                        if bad_bounds:
                            report_disc.verdict = "WARN (boundary mismatch): " + ", ".join(bad_bounds[:3]) + (" ..." if len(bad_bounds) > 3 else "")
                        else:
                            report_disc.verdict = "PASS (all tracks have LAME delay/padding; boundaries OK)"

                report_disc.steps.append(StepResult("validate", True, report_disc.verdict))

                if use_progress:
                    bar6.update(1)
                    bar6.close()

                report_disc.temps.append(str(album_temp_dir))
            reports.append(report_disc)
        return reports
    else:
        # Existing single-album logic
        cue: Optional[Path] = cues[0] if cues else None
        if not files:
            report = AlbumReport(
                source_folder=str(folder),
                detected_files=[],
                detected_cue=str(cue) if cue else None,
                mode_used="",
                artist="",
                year="",
                album="",
                output_folder="",
            )
            report.verdict = "FAIL (no lossless files found)"
            return report

        extensions = {f.suffix.lower() for f in files}
        if len(extensions) > 1:
            report = AlbumReport(
                source_folder=str(folder),
                detected_files=[str(f) for f in files],
                detected_cue=str(cue) if cue else None,
                mode_used="",
                artist="",
                year="",
                album="",
                output_folder="",
            )
            report.verdict = "FAIL (mixed file formats in folder)"
            return report

        ext = list(extensions)[0]

        if ext == '.m4a':
            for f in files:
                cmd = [
                    "ffprobe", "-v", "error", "-select_streams", "a:0",
                    "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
                    str(f)
                ]
                rc, out, err = run_cmd(cmd, quiet=True)
                codec = out.strip()
                if rc != 0 or codec != 'alac':
                    report = AlbumReport(
                        source_folder=str(folder),
                        detected_files=[str(f) for f in files],
                        detected_cue=str(cue) if cue else None,
                        mode_used="",
                        artist="",
                        year="",
                        album="",
                        output_folder="",
                    )
                    report.steps.append(StepResult("check_lossless", False, f"{f.name} is not ALAC (codec: {codec})"))
                    report.verdict = "FAIL (non-lossless M4A)"
                    return report

        # NEW: early validation for FLAC/APE (prevents later decode failures)
        if ext in ('.flac', '.ape'):
            for f in files:
                try:
                    audio = load_audio(f)
                    _ = audio.info  # force full header check
                except Exception as e:
                    report = AlbumReport(
                        source_folder=str(folder),
                        detected_files=[str(f) for f in files],
                        detected_cue=str(cue) if cue else None,
                        mode_used="",
                        artist="",
                        year="",
                        album="",
                        output_folder="",
                    )
                    report.verdict = f"FAIL (invalid {ext.upper()} file: {f.name} - {type(e).__name__}: {e})"
                    return report

        artist, year, album, catalog_number = choose_artist_year_album_catalog(folder, files, cue)

        catalog_suffix = ""
        if catalog_number:
            cat_clean = sanitize_component(catalog_number)
            if cat_clean and (cat_clean.lower() not in album.lower()):
                catalog_suffix = f" ({cat_clean})"

        out_album_folder = out_root / f"{artist} - [{year}] {album}{catalog_suffix}"
        if not dry_run:
            ensure_dir(out_album_folder)

        report = AlbumReport(
            source_folder=str(folder),
            detected_files=[str(f) for f in files],
            detected_cue=str(cue) if cue else None,
            mode_used="",
            artist=artist,
            year=year,
            album=album,
            output_folder=str(out_album_folder),
            catalog_number=catalog_number or "",
        )

        # NEW: use central temp + per-album subfolder
        album_name = f"{artist} - [{year}] {album}{catalog_suffix}"
        album_temp_dir = temp_root / album_name
        if not dry_run:
            ensure_dir(album_temp_dir)

        album_wav = album_temp_dir / "_step1_album.wav"
        album_mp3 = album_temp_dir / "_step2_album.mp3"
        split_dir = album_temp_dir / "_step3_split_tracks"
        generated_cue = album_temp_dir / "_generated_album.cue"

        option_a = bool(cue) and len(files) == 1
        report.mode_used = "A" if option_a else "B"

        # ---- DRY RUN: analyze only, no filesystem changes, no external commands ----
        if dry_run:
            report.steps.append(StepResult("prepare", True, f"DRY RUN: no folders created, no encoding. Would use temp dir: {album_temp_dir}"))

            blockers: List[str] = []

            if not files:
                blockers.append("BLOCKER: no lossless files found (unexpected).")

            # Option A requires a CUE file and exactly one file
            if option_a:
                if not cue or not cue.exists():
                    blockers.append("BLOCKER: Option A selected but no CUE file found.")
                else:
                    okc, msgs = dryrun_check_cue_blockers(folder, cue)
                    if not okc:
                        blockers.extend(msgs)
                    else:
                        report.steps.append(StepResult("cue_check", True, "DRY RUN: " + " | ".join(msgs)))
            else:
                # Option B can generate CUE via python; existing CUE is optional.
                if cue and cue.exists():
                    okc, msgs = dryrun_check_cue_blockers(folder, cue)
                    if not okc:
                        blockers.extend(msgs)
                    else:
                        report.steps.append(StepResult("cue_check", True, "DRY RUN (optional cue): " + " | ".join(msgs)))
                else:
                    report.steps.append(StepResult("cue_check", True, "DRY RUN: no CUE found; Option B would generate one via python."))

            # Build "would run" commands (strings only)
            album_wav = album_temp_dir / "_step1_album.wav"
            album_mp3 = album_temp_dir / "_step2_album.mp3"
            split_dir = album_temp_dir / "_step3_split_tracks"
            generated_cue = album_temp_dir / "_generated_album.cue"

            if option_a and files:
                if ext == '.flac':
                    dec_cmd = ["flac", "-d", "-f", "-o", str(album_wav), str(files[0])]
                else:
                    dec_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(files[0]), "-f", "wav", "-y", str(album_wav)]
                report.steps.append(StepResult("A1_decode_to_album_wav", True, "SKIP (dry run)\n      would_run: " + " ".join(shlex_quote(c) for c in dec_cmd)))
                cue_to_use = cue
            else:
                inputs = " ".join(f' -i "{f.name}"' for f in files)
                filter_parts = "".join(f"[{i}:a]" for i in range(len(files)))
                filter_complex = f' -filter_complex "{filter_parts}concat=n={len(files)}:v=0:a=1[a]" -map "[a]"'
                ff_cmd = f'ffmpeg -hide_banner -loglevel error{inputs}{filter_complex} -f wav -y "{album_wav}"'
                report.steps.append(StepResult("B1_concat_to_album_wav", True, "SKIP (dry run)\n      would_run: " + ff_cmd))
                report.steps.append(StepResult("B3_generate_cue", True, "SKIP (dry run)\n      would_generate: python cue to " + str(generated_cue)))
                cue_to_use = generated_cue

            # LAME command (matches real behavior)
            stereo_flag = "s" if enc.true_stereo else "j"
            if enc.mode == "VBR":
                vq = int(enc.vbr_quality)
                if vq < 0:
                    vq = 0
                if vq > 9:
                    vq = 9
                lame_cmd = ["lame", "-m", stereo_flag, f"-V{vq}", "-b", str(enc.vbr_min), "-B", str(enc.vbr_max), str(album_wav), str(album_mp3)]
            else:
                lame_cmd = ["lame", "-m", stereo_flag, "-b", str(enc.cbr), "--cbr", str(album_wav), str(album_mp3)]
            report.steps.append(StepResult(f"{report.mode_used}2_lame_encode_album_mp3", True, "SKIP (dry run)\n      would_run: " + " ".join(shlex_quote(c) for c in lame_cmd)))

            # mp3splt command
            splt_cmd = ["mp3splt", "-Q", "-b", "-c", str(cue_to_use), "-d", str(split_dir), "-o", "@n - @t", str(album_mp3)]
            report.steps.append(StepResult(f"{report.mode_used}3_split_with_mp3splt", True, "SKIP (dry run)\n      would_run: " + " ".join(shlex_quote(c) for c in splt_cmd)))

            report.steps.append(StepResult("organize_output", True, "SKIP (dry run)"))
            report.steps.append(StepResult("rename_tracks_from_cue", True, "SKIP (dry run)"))
            report.steps.append(StepResult("validate", True, "SKIP (dry run)"))

            if blockers:
                report.verdict = "DRYRUN FAIL (blocking issue): " + blockers[0].replace("BLOCKER: ", "")
                # keep the full list in validations so it’s visible in reports
                report.validations = [{"dryrun_blockers": blockers}]
            else:
                report.verdict = "DRYRUN PASS (no blocking issues found)"
                report.validations = [{"dryrun_blockers": []}]

            report.steps.append(StepResult("dry_run_verdict", True, report.verdict))
            # No temps to delete in dry run (we did not create them)
            report.temps = []
            return report
        # ---- end DRY RUN ----------------------------------------------------------

        # Step 1/6: prepare
        if use_progress:
            bar1 = tqdm(total=1, desc="1/6 prepare", unit="step", ascii=True, leave=True, file=sys.stderr)
            report.steps.append(StepResult("prepare", True, f"temp dir: {album_temp_dir}"))
            bar1.update(1)
            bar1.close()
        else:
            report.steps.append(StepResult("prepare", True, f"temp dir: {album_temp_dir}"))

        # Step 2/6: to_wav
        if option_a:
            ok, details = decode_single_to_wav(files[0], album_wav, use_progress=use_progress)
            report.steps.append(StepResult("A1_decode_to_album_wav", ok, details, outputs=[str(album_wav)] if ok else []))
        else:
            ok, details = concat_lossless_to_wav(files, album_wav, cwd=folder, use_progress=use_progress)
            report.steps.append(StepResult("B1_concat_to_album_wav", ok, details, outputs=[str(album_wav)] if ok else []))

        if not ok:
            report.verdict = "FAIL (could not create album WAV)"
            report.temps.append(str(album_temp_dir))
            return report

        # Step 3/6: encode_mp3
        ok, details = lame_encode_album_wav_to_mp3(album_wav, album_mp3, enc, use_progress=use_progress)
        report.steps.append(StepResult(f"{report.mode_used}2_lame_encode_album_mp3", ok, details, outputs=[str(album_mp3)] if ok else []))
        if not ok:
            report.verdict = "FAIL (LAME encode failed)"
            report.temps.append(str(album_temp_dir))
            return report

        # Step 4/6: split_tracks
        cue_to_use = cue
        if not option_a:
            okc, detc = generate_cue(files, generated_cue, cwd=folder)
            report.steps.append(StepResult("B3_generate_cue", okc, detc, outputs=[str(generated_cue)] if okc else []))
            if not okc:
                report.verdict = "FAIL (could not generate cue)"
                report.temps.append(str(album_temp_dir))
                return report
            cue_to_use = generated_cue

        oks, dets = mp3splt_split_with_cue(album_mp3, cue_to_use, split_dir, use_progress=use_progress)
        report.steps.append(StepResult(f"{report.mode_used}3_split_with_mp3splt", oks, dets, outputs=[str(split_dir)] if oks else []))
        if not oks:
            report.verdict = "FAIL (mp3 splitting failed)"
            report.temps.append(str(album_temp_dir))
            return report

        # Step 5/6: organize output
        if use_progress:
            bar5 = tqdm(total=1, desc="5/6 organize", unit="step", ascii=True, leave=True, file=sys.stderr)

        split_mp3s = sorted(split_dir.glob("*.mp3"))
        moved: List[str] = []
        for mp3 in split_mp3s:
            dest = out_album_folder / mp3.name
            if dest.exists():
                dest.unlink()
            shutil.move(str(mp3), str(dest))
            moved.append(str(dest))

        report.steps.append(StepResult("organize_output", True, f"moved {len(moved)} tracks to {out_album_folder}", outputs=moved))
        if use_progress:
            bar5.update(1)
            bar5.close()

        # Track filename polish
        rok, rmsg = rename_tracks_from_cue_with_fallback(out_album_folder, cue_to_use)
        report.steps.append(StepResult("rename_tracks_from_cue", rok, rmsg))

        # Step 6/6: validate
        if use_progress:
            bar6 = tqdm(total=1, desc="6/6 validate", unit="step", ascii=True, leave=True, file=sys.stderr)

        mp3s = sorted(out_album_folder.glob("*.mp3"))
        track_validations: List[Dict[str, object]] = []

        for mp3 in mp3s:
            ok_gapless, reason, delay, padding = mp3_lame_delay_padding(mp3)
            track_validations.append({
                "file": str(mp3),
                "gapless_tag_ok": ok_gapless,
                "lame_check": reason,
                "encoder_delay_samples": delay,
                "encoder_padding_samples": padding,
            })

        # Boundary checks (stored separately to avoid mixing dict types)
        boundary_checks: List[Dict[str, object]] = []
        if len(mp3s) >= 2:
            for i in range(len(mp3s) - 1):
                mp3_a = mp3s[i]
                mp3_next = mp3s[i + 1]
                va = track_validations[i]
                vb = track_validations[i + 1]

                okb, detb = boundary_continuity_check(
                    mp3_a,
                    mp3_next,
                    int(va.get("encoder_delay_samples") or 0),
                    int(va.get("encoder_padding_samples") or 0),
                    int(vb.get("encoder_delay_samples") or 0),
                    int(vb.get("encoder_padding_samples") or 0),
                )
                boundary_checks.append({
                    "from": mp3_a.name,
                    "to": mp3_next.name,
                    "ok": okb,
                    "details": detb,
                })

        report.validations = [
            {"tracks": track_validations},
            {"boundary_checks": boundary_checks},
        ]

        # Verdict
        if not mp3s:
            report.verdict = "FAIL (no MP3s produced)"
        else:
            bad_tags = [Path(v["file"]).name for v in track_validations if not v.get("gapless_tag_ok")]
            if bad_tags:
                report.verdict = "WARN (gapless fields missing/unreadable): " + ", ".join(bad_tags[:5]) + (" ..." if len(bad_tags) > 5 else "")
            else:
                bad_bounds = [f'{b["from"]}→{b["to"]}' for b in boundary_checks if not b.get("ok")]
                if bad_bounds:
                    report.verdict = "WARN (boundary mismatch): " + ", ".join(bad_bounds[:3]) + (" ..." if len(bad_bounds) > 3 else "")
                else:
                    report.verdict = "PASS (all tracks have LAME delay/padding; boundaries OK)"

        report.steps.append(StepResult("validate", True, report.verdict))

        if use_progress:
            bar6.update(1)
            bar6.close()

        report.temps.append(str(album_temp_dir))
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-encode lossless albums to gapless MP3 (album-wide LAME + cue split).")
    parser.add_argument("root", nargs="?", default=".", help="Root folder to scan (default: current folder)")
    parser.add_argument("--out", default="MP3", help="Output folder name under root (default: MP3)")
    parser.add_argument("--report-name", default="mp3_reencode_report", help="Base name for report files (default: mp3_reencode_report)")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bars")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only: no encoding/splitting, no folder creation; write a dry-run report")
    args = parser.parse_args()
    # NEW: interactive prompt for dry-run if not explicitly requested
    if not args.dry_run:
        print("\nDry-run mode:")
        print("  - Yes: analysis only (no encoding, no temp/output folders created)")
        print("  - No:  full run (encode + split + validate)")
        args.dry_run = prompt_yes_no("Run in DRY-RUN (analysis-only) mode?", default=True)


    root = Path(args.root).expanduser().resolve()
    out_root = root / args.out
    if not args.dry_run:
        ensure_dir(out_root)
    temp_root = root / "ConversionTemp"
    if not args.dry_run:
        ensure_dir(temp_root)


    print("\n== Gapless MP3 Re-encode ==")
    print(f"Root:     {root}")
    print(f"Output:   {out_root}")

    print("\nChecking dependencies...")
    check_external_tools()
    print("OK: All required tools found:")
    for t in REQUIRED_TOOLS:
        print(f"  - {t}: {which(t)}")

    enc = prompt_encode_settings()


    print("\nEncoding settings selected:")
    stereo_desc = "True stereo (-m s)" if enc.true_stereo else "Joint stereo (-m j)"
    if enc.mode == "VBR":
        print(f"  Mode: VBR (-V{enc.vbr_quality}), min {enc.vbr_min} kbps, max {enc.vbr_max} kbps")
    else:
        print(f"  Mode: CBR {enc.cbr} kbps")
    print(f"  Stereo: {stereo_desc}")


    use_progress = (not args.no_progress) and sys.stderr.isatty()

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    run_report = RunReport(root=str(root), started_at=started, encode_settings=asdict(enc))

    album_folders = discover_album_folders(root)
    # NEW: in real runs, optionally filter albums using prior dry-run results
    dryrun_ok_map: Dict[str, str] = {}
    dryrun_blocked_map: Dict[str, str] = {}
    use_dryrun_filter = False

    if (not args.dry_run) and album_folders:
        dryrun_json_path = root / (args.report_name + "_dryrun.json")
        if dryrun_json_path.exists():
            print(f"\nFound dry-run report: {dryrun_json_path}")
            if prompt_yes_no("Only process albums that were DRYRUN OK in that report?", default=True):
                ok_map, blocked_map = load_dryrun_lists(dryrun_json_path)
                dryrun_ok_map = ok_map
                dryrun_blocked_map = blocked_map
                use_dryrun_filter = True

                ok_set = set(dryrun_ok_map.keys())
                before = len(album_folders)
                album_folders = [f for f in album_folders if _normalize_folder_key(f) in ok_set]
                after = len(album_folders)

                print(f"\nUsing dry-run OK list: will process {after}/{before} album folder(s).")
                if before != after:
                    print(f"Skipping {before - after} album folder(s) not marked DRYRUN OK (see dry-run report for details).")

    if not album_folders:
        print("\nNo lossless albums found under this root. Nothing to do.")
        return

    print(f"\nFound {len(album_folders)} folder(s) containing lossless files.")
    print("Processing…")

    temps_to_offer_delete: List[str] = []
    # NEW: dry-run per-album verdict tracking
    dryrun_ok_list: List[str] = []
    dryrun_blocked_list: List[Tuple[str, str]] = []


    for folder in album_folders:
        try:
            folder.relative_to(out_root)
            continue
        except ValueError:
            pass

        if folder.name == "._gapless_tmp":
            continue

        print(f"\n=== Album folder: {folder} ===")
        rep = process_album_folder(folder, out_root, temp_root, enc, use_progress=use_progress, dry_run=args.dry_run)
        if isinstance(rep, list):
            run_report.albums.extend(rep)
            for r in rep:
                temps_to_offer_delete.extend(r.temps)
                if args.dry_run:
                    if r.verdict.startswith("DRYRUN PASS"):
                        print(f"DRYRUN OK:      {r.source_folder}")
                        dryrun_ok_list.append(r.source_folder)
                    elif r.verdict.startswith("DRYRUN FAIL"):
                        reason = ""
                        m = re.search(r"^DRYRUN FAIL\s*\(blocking issue\):\s*(.+)$", r.verdict)
                        if m:
                            reason = m.group(1).strip()
                        else:
                            try:
                                blk = next((v for v in r.validations if "dryrun_blockers" in v), None)
                                if blk and blk.get("dryrun_blockers"):
                                    reason = str(blk["dryrun_blockers"][0]).replace("BLOCKER: ", "").strip()
                            except Exception:
                                pass
                        print(f"DRYRUN BLOCKED: {r.source_folder} — {reason}")
                        dryrun_blocked_list.append((r.source_folder, reason))
        else:
            run_report.albums.append(rep)
            temps_to_offer_delete.extend(rep.temps)
            if args.dry_run:
                if rep.verdict.startswith("DRYRUN PASS"):
                    print(f"DRYRUN OK:      {rep.source_folder}")
                    dryrun_ok_list.append(rep.source_folder)
                elif rep.verdict.startswith("DRYRUN FAIL"):
                    reason = ""
                    m = re.search(r"^DRYRUN FAIL\s*\(blocking issue\):\s*(.+)$", rep.verdict)
                    if m:
                        reason = m.group(1).strip()
                    else:
                        try:
                            blk = next((v for v in rep.validations if "dryrun_blockers" in v), None)
                            if blk and blk.get("dryrun_blockers"):
                                reason = str(blk["dryrun_blockers"][0]).replace("BLOCKER: ", "").strip()
                        except Exception:
                            pass
                    print(f"DRYRUN BLOCKED: {rep.source_folder} — {reason}")
                    dryrun_blocked_list.append((rep.source_folder, reason))

    run_report.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")

    total = len(run_report.albums)
    passes = sum(1 for a in run_report.albums if a.verdict.startswith("PASS"))
    warns = sum(1 for a in run_report.albums if a.verdict.startswith("WARN"))
    fails = sum(1 for a in run_report.albums if a.verdict.startswith("FAIL"))

    # NEW: dry-run verdict counts
    dryrun_pass = sum(1 for a in run_report.albums if a.verdict.startswith("DRYRUN PASS"))
    dryrun_fail = sum(1 for a in run_report.albums if a.verdict.startswith("DRYRUN FAIL"))
    # NEW: screen-level dry-run summary (fast preflight feedback)
    if args.dry_run:
        print("\n== Dry-run summary ==")
        print(f"Albums analyzed: {total} | OK: {dryrun_pass} | BLOCKED: {dryrun_fail}")

        if dryrun_fail:
            print("\nBlocked albums (first 10):")
            shown = 0
            for a in run_report.albums:
                if not a.verdict.startswith("DRYRUN FAIL"):
                    continue

                reason = ""
                # Prefer the explicit verdict reason
                m = re.search(r"^DRYRUN FAIL\s*\(blocking issue\):\s*(.+)$", a.verdict)
                if m:
                    reason = m.group(1).strip()
                else:
                    # Fallback: try to read the first blocker from validations
                    try:
                        blk = next((v for v in a.validations if "dryrun_blockers" in v), None)
                        if blk and blk.get("dryrun_blockers"):
                            first = str(blk["dryrun_blockers"][0])
                            reason = first.replace("BLOCKER: ", "").strip()
                    except Exception:
                        pass

                if not reason:
                    reason = "blocking issue (see report for details)"

                print(f"  - {a.source_folder}")
                print(f"      blocked: {reason}")

                shown += 1
                if shown >= 10:
                    remaining = dryrun_fail - shown
                    if remaining > 0:
                        print(f"  ... and {remaining} more blocked album(s). See the _dryrun report for full details.")
                    break
        # NEW: print explicit OK/BLOCKED lists
        if dryrun_ok_list:
            print("\nOK albums:")
            for p in dryrun_ok_list[:50]:
                print(f"  - {p}")
            if len(dryrun_ok_list) > 50:
                print(f"  ... and {len(dryrun_ok_list) - 50} more")

        if dryrun_blocked_list:
            print("\nBLOCKED albums:")
            for p, r in dryrun_blocked_list[:50]:
                print(f"  - {p}")
                print(f"      blocked: {r}")
            if len(dryrun_blocked_list) > 50:
                print(f"  ... and {len(dryrun_blocked_list) - 50} more")

        if not dryrun_blocked_list:
            print("\nAll albums look OK in dry-run (no blockers detected).")



    run_report.summary = {
        "total_albums": total,
        "pass": passes,
        "warn": warns,
        "fail": fails,
        "dryrun_pass": dryrun_pass,   # NEW
        "dryrun_fail": dryrun_fail,   # NEW
        "output_root": str(out_root),
    }


    report_suffix = "_dryrun" if args.dry_run else ""
    report_base = root / (args.report_name + report_suffix)
    json_path = Path(str(report_base) + ".json")
    txt_path = Path(str(report_base) + ".txt")

    json_path.write_text(json.dumps(asdict(run_report), indent=2), encoding="utf-8")

    lines: List[str] = []
    lines.append("== Gapless MP3 Re-encode Report ==")
    lines.append(f"Started:  {run_report.started_at}")
    lines.append(f"Finished: {run_report.finished_at}")
    lines.append("")
    lines.append(f"Root:   {run_report.root}")
    lines.append(f"Output: {run_report.summary['output_root']}")
    lines.append("")
    lines.append(f"Albums: {total} | PASS: {passes} | WARN: {warns} | FAIL: {fails} | DRYRUN_PASS: {dryrun_pass} | DRYRUN_FAIL: {dryrun_fail}")
    lines.append("")
    lines.append(f"Encode settings: {run_report.encode_settings}")
    # NEW: explicit stereo info in the TXT report header
    stereo_desc = "True stereo (-m s)" if enc.true_stereo else "Joint stereo (-m j)"
    lines.append(f"Stereo: {stereo_desc}")
    if enc.mode == "VBR":
        lines.append(f"VBR quality: -V{enc.vbr_quality} (0=highest, 9=lowest)")
    lines.append("Note: exact LAME command line is recorded in each album's encode step details.")
    lines.append("")

    for a in run_report.albums:
        lines.append("-" * 80)
        lines.append(f"Source:   {a.source_folder}")
        lines.append(f"Mode:     Option {a.mode_used}")
        lines.append(f"Album:    {a.artist} - [{a.year}] {a.album}")
        if a.catalog_number:
            lines.append(f"Catalog#: {a.catalog_number}")
        else:
            lines.append("Catalog#: (not found)")
        lines.append(f"Output:   {a.output_folder}")
        lines.append(f"Verdict:  {a.verdict}")
        lines.append(f"Files:    {len(a.detected_files)}")
        if a.detected_cue:
            lines.append(f"CUE:      {a.detected_cue}")
        lines.append("")
        lines.append("Steps:")
        for s in a.steps:
            lines.append(f"  - {s.name}: {'OK' if s.ok else 'FAIL'}")
            if s.details:
                lines.append(f"      {s.details}")
            if s.outputs:
                lines.append(f"      outputs: {len(s.outputs)}")
        lines.append("")

        # Print track validations
        lines.append("Validation (tracks):")
        tracks_block = next((v for v in a.validations if "tracks" in v), {"tracks": []})
        for v in tracks_block.get("tracks", []):
            fn = Path(v["file"]).name
            lines.append(
                f"  - {fn}: gapless_tag_ok={v.get('gapless_tag_ok')} "
                f"delay={v.get('encoder_delay_samples')} padding={v.get('encoder_padding_samples')}"
            )
            if v.get("lame_check"):
                lines.append(f"      lame_check: {v['lame_check']}")
        lines.append("")

        # Print boundary checks
        bc_block = next((v for v in a.validations if "boundary_checks" in v), {"boundary_checks": []})
        bcs = bc_block.get("boundary_checks", [])
        if bcs:
            lines.append("Boundary checks:")
            for b in bcs:
                lines.append(f"  - {b['from']} -> {b['to']}: ok={b['ok']} ({b['details']})")
        else:
            lines.append("Boundary checks: (not run; need at least 2 tracks)")
        lines.append("")

    txt_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- NEW: folder status report (OK vs BLOCKED) ----------------------------
    status_path = Path(str(report_base) + "_folders.txt")

    ok_entries: List[Tuple[str, str]] = []
    blocked_entries: List[Tuple[str, str]] = []

    if args.dry_run:
        # Dry-run: OK=DRYRUN PASS, BLOCKED=DRYRUN FAIL (include reason if available)
        for a in run_report.albums:
            if a.verdict.startswith("DRYRUN PASS"):
                ok_entries.append((a.source_folder, a.verdict))
            elif a.verdict.startswith("DRYRUN FAIL"):
                reason = ""
                m = re.search(r"^DRYRUN FAIL\s*\(blocking issue\):\s*(.+)$", a.verdict)
                if m:
                    reason = m.group(1).strip()
                else:
                    # Fallback: try to read the first blocker from validations
                    try:
                        blk = next((v for v in a.validations if "dryrun_blockers" in v), None)
                        if blk and blk.get("dryrun_blockers"):
                            reason = str(blk["dryrun_blockers"][0]).replace("BLOCKER: ", "").strip()
                    except Exception:
                        pass
                blocked_entries.append((a.source_folder, reason or a.verdict))
    else:
        # Real run: OK=PASS/WARN, BLOCKED=FAIL
        for a in run_report.albums:
            if a.verdict.startswith("PASS") or a.verdict.startswith("WARN"):
                ok_entries.append((a.source_folder, a.verdict))
            elif a.verdict.startswith("FAIL"):
                blocked_entries.append((a.source_folder, a.verdict))

    status_lines: List[str] = []
    status_lines.append("== Folder Status Report (OK vs BLOCKED) ==")
    status_lines.append(f"Started:  {run_report.started_at}")
    status_lines.append(f"Finished: {run_report.finished_at}")
    status_lines.append("")
    status_lines.append(f"Root: {run_report.root}")
    status_lines.append(f"Mode: {'DRY RUN' if args.dry_run else 'REAL RUN'}")
    status_lines.append("")

    status_lines.append(f"OK folders: {len(ok_entries)}")
    for p, info in ok_entries:
        status_lines.append(f"  - {p}")
        status_lines.append(f"      {info}")

    status_lines.append("")
    status_lines.append(f"BLOCKED folders: {len(blocked_entries)}")
    for p, reason in blocked_entries:
        status_lines.append(f"  - {p}")
        status_lines.append(f"      {reason}")

    status_path.write_text("\n".join(status_lines), encoding="utf-8")
    # --------------------------------------------------------------------------

    print("\nDone.")
    print(f"Report (JSON): {json_path}")
    print(f"Report (TXT):  {txt_path}")
    print(f"Report (Folders): {status_path}")
    print(f"Summary: PASS={passes}, WARN={warns}, FAIL={fails}, DRYRUN_PASS={dryrun_pass}, DRYRUN_FAIL={dryrun_fail}")

    unique_temp_roots: List[Path] = []
    seen = set()
    for t in temps_to_offer_delete:
        t = Path(t).resolve()
        if t.exists() and t not in seen:
            seen.add(t)
            unique_temp_roots.append(t)

    if unique_temp_roots:
        print("\nTemporary folders were created inside ConversionTemp.")
        if prompt_yes_no("Would you like to delete all temp files/folders?", default=True):
            deleted_count = 0
            for t in unique_temp_roots:
                try:
                    shutil.rmtree(t)
                    deleted_count += 1
                    print(f"  Deleted: {t}")
                except Exception as e:
                    print(f"  ERROR deleting {t}: {e}")
            print(f"\nDeleted {deleted_count} temporary folder(s).")
        else:
            print("\nKeeping temporary folders. Asking per folder...")
            for t in unique_temp_roots:
                if not t.exists():
                    continue
                if prompt_yes_no(f"Delete temp folder '{t}' ?", default=True):
                    try:
                        shutil.rmtree(t)
                        print("  deleted.")
                    except Exception as e:
                        print(f"  ERROR deleting: {e}")
                else:
                    print("  kept.")
    else:
        print("\nNo temporary folders found to delete.")


if __name__ == "__main__":
    main()