#!/usr/bin/env python3
"""
mb_tag_apply.py

Interactive MusicBrainz tagger + cover art embedder for existing MP3 album folders.

Features:
- Recursively finds album folders (any folder containing .mp3 files)
- Searches MusicBrainz releases (prefers catalog number via catno:)
- Prompts user to pick the correct release for METADATA
- Prompts user to choose cover art source:
    A) cover from selected release (default)
    B) cover from a different search hit (cleaner cover variant)
    C) skip cover
  If the chosen cover source has multiple "front" images, you can pick one.
- Applies tags IN PLACE (no moving/renaming/copying)
- Writes a per-album report (TXT + JSON) inside the album folder

Requirements:
  python3 -m pip install mutagen tqdm requests

Notes:
- Obeys MusicBrainz rate limit (~1 req/sec average) and sets User-Agent.
  https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from urllib3.exceptions import MaxRetryError

from tqdm import tqdm
from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3,
    APIC,
    ID3NoHeaderError,
    TIT2,
    TALB,
    TPE1,
    TPE2,
    TRCK,
    TDRC,
    TPUB,
    TXXX,
    TDOR,
    TSRC,
    TMED,
    TPOS,
    TSOA,
    TSOP,
)

# ------------------ Config ------------------
MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"

USER_AGENT_DEFAULT = "gapless-mp3-pipeline/1.2 (local-script; no-email)"
MIN_SECONDS_BETWEEN_MB_CALLS = 1.1


# ------------------ Helpers ------------------
def die(msg: str, code: int = 2) -> None:
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(code)


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def http_get_json(url: str, user_agent: str, timeout: int = 60) -> Dict:
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    session = requests.Session()
    retries = Retry(total=10, backoff_factor=1, status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    try:
        response = session.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except MaxRetryError as e:
        if "EOF occurred in violation of protocol" in str(e):
            raise Exception("SSL EOF error after retries—try running again or upgrading Python to 3.13")
        raise


def http_get_bytes(url: str, user_agent: str, timeout: int = 60) -> Tuple[bytes, str]:
    headers = {"User-Agent": user_agent}
    session = requests.Session()
    retries = Retry(total=10, backoff_factor=1, status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    try:
        response = session.get(url, headers=headers, timeout=timeout, stream=True)
        print(f"Response status: {response.status_code}")
        response.raise_for_status()
        ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = response.content
        return data, ctype
    except MaxRetryError as e:
        if "EOF occurred in violation of protocol" in str(e):
            raise Exception("SSL EOF error after retries—try running again or checking your network")
        raise


class RateLimiter:
    def __init__(self, min_interval_sec: float):
        self.min_interval = float(min_interval_sec)
        self._last = 0.0

    def wait(self):
        now = time.time()
        delta = now - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.time()


def sanitize_component(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_mp3_album_folders(root: Path) -> List[Path]:
    out: List[Path] = []
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            if any(x.suffix.lower() == ".mp3" for x in p.iterdir() if x.is_file()):
                out.append(p)
    uniq = []
    seen = set()
    for f in out:
        rp = str(f.resolve())
        if rp not in seen:
            seen.add(rp)
            uniq.append(f)
    return uniq


def parse_catalog_from_folder_name(folder: Path) -> Optional[str]:
    name = folder.name
    parens = re.findall(r"\(([^)]+)\)", name)
    candidates = list(reversed(parens)) + [name]

    token_patterns = [
        r"\b([A-Z]{2,10}-\d{3,8})\b",
        r"\b([A-Z]{2,10}\s\d{3,8})\b",
        r"\b([A-Z]{2,10}\d{3,8})\b",
        r"\b([A-Z]{2,10}-\d{2,4}-\d{2,6})\b",
        r"\b(\d{4} \d \d{5} \d \d)\b",  # Added for formats like "7243 4 96917 0 4"
        r"\b(\d{4}-\d-\d{5}-\d-\d)\b",
    ]

    for c in candidates:
        for pat in token_patterns:
            m = re.search(pat, c, re.I)
            if m:
                return m.group(1)
    return None


def parse_artist_year_album_from_folder_name(folder: Path) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    name = folder.name
    m = re.match(r"^\s*(.+?)\s*-\s*\[(\d{4})\]\s*(.+?)\s*(?:\(.+\))?\s*$", name)
    if not m:
        return None, None, None
    artist = sanitize_component(m.group(1))
    year = m.group(2)
    album = sanitize_component(m.group(3))
    return artist or None, year or None, album or None


def parse_disc_from_folder_name(folder: Path) -> Optional[int]:
    name = folder.name.lower()
    m = re.search(r"(disc|cd|disk|part)\s*(\d+)", name)
    if m:
        return int(m.group(2))
    return None


def sorted_mp3s_by_tracknum(folder: Path) -> List[Path]:
    mp3s = sorted([p for p in folder.glob("*.mp3") if p.is_file()])

    def key(p: Path) -> Tuple[int, str]:
        m = re.match(r"^\s*(\d+)\s*-\s*", p.name)
        if m:
            return int(m.group(1)), p.name.lower()
        return 10**9, p.name.lower()

    return sorted(mp3s, key=key)


def write_album_report(report_dir: Path, txt: str, js: Dict) -> None:
    (report_dir / "mb_tag_report.txt").write_text(txt, encoding="utf-8", errors="replace")
    (report_dir / "mb_tag_report.json").write_text(json.dumps(js, indent=2), encoding="utf-8", errors="replace")


# ------------------ MusicBrainz ------------------
@dataclass
class ReleaseHit:
    mbid: str
    title: str
    artist_credit: str
    date: str
    country: str
    status: str
    label: str
    catno: str
    barcode: str
    score: int


def mb_release_search(query: str, user_agent: str, rl: RateLimiter, limit: int = 10) -> List[ReleaseHit]:
    rl.wait()
    url = f"{MB_BASE}/release/?query={quote(query)}&fmt=json&limit={int(limit)}"
    data = http_get_json(url, user_agent=user_agent)

    hits: List[ReleaseHit] = []
    for r in data.get("releases", []) or []:
        mbid = r.get("id", "")
        title = r.get("title", "")
        score = int(r.get("score", 0) or 0)
        date = r.get("date", "") or ""
        country = r.get("country", "") or ""
        status = r.get("status", "") or ""

        ac = r.get("artist-credit") or []
        artist_credit = "".join([a.get("name", "") for a in ac]) if ac else ""

        label = ""
        catno = ""
        li = r.get("label-info") or []
        if li:
            for item in li:
                if not label and item.get("label") and item["label"].get("name"):
                    label = item["label"]["name"]
                if not catno and item.get("catalog-number"):
                    catno = item.get("catalog-number")
        barcode = r.get("barcode") or ""

        if mbid:
            hits.append(
                ReleaseHit(
                    mbid=mbid,
                    title=title,
                    artist_credit=artist_credit,
                    date=date,
                    country=country,
                    status=status,
                    label=label,
                    catno=catno,
                    barcode=barcode,
                    score=score,
                )
            )
    return hits


def mb_release_details(mbid: str, user_agent: str, rl: RateLimiter) -> Dict:
    rl.wait()
    url = f"{MB_BASE}/release/{mbid}?inc=recordings+artist-credits+labels+release-groups+url-rels+isrcs&fmt=json"
    return http_get_json(url, user_agent=user_agent)


# ------------------ Cover Art Archive ------------------
def caa_release_images_json(release_mbid: str, user_agent: str, rl: RateLimiter) -> Optional[Dict]:
    url = f"{CAA_BASE}/release/{release_mbid}"
    rl.wait()
    try:
        return http_get_json(url, user_agent=user_agent)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return None
        return None
    except Exception:
        return None


@dataclass
class CoverChoice:
    release_mbid: str
    image_id: Optional[int]
    mime: str
    bytes_data: bytes
    source_note: str


def fetch_cover_front(release_mbid: str, user_agent: str, rl: RateLimiter) -> Optional[CoverChoice]:
    url = f"{CAA_BASE}/release/{release_mbid}/front"
    rl.wait()
    try:
        data, ctype = http_get_bytes(url, user_agent=user_agent)
        if not data:
            return None
        mime = ctype if ctype.startswith("image/") else "image/jpeg"
        return CoverChoice(
            release_mbid=release_mbid,
            image_id=None,
            mime=mime,
            bytes_data=data,
            source_note=f"CAA front (release {release_mbid})",
        )
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in (404, 405):
            return None
        return None
    except Exception:
        return None


def fetch_cover_by_image_id(release_mbid: str, image_id: int, user_agent: str, rl: RateLimiter) -> Optional[CoverChoice]:
    url = f"{CAA_BASE}/release/{release_mbid}/{int(image_id)}"
    print(f"Fetching image: {url}")
    rl.wait()
    try:
        data, ctype = http_get_bytes(url, user_agent=user_agent)
        if not data:
            return None
        mime = ctype if ctype.startswith("image/") else "image/jpeg"
        return CoverChoice(
            release_mbid=release_mbid,
            image_id=int(image_id),
            mime=mime,
            bytes_data=data,
            source_note=f"CAA image id {image_id} (release {release_mbid})",
        )
    except requests.exceptions.HTTPError:
        return None
    except Exception:
        return None


def choose_cover_interactively(hits: List[ReleaseHit], selected_release_mbid: str, user_agent: str, rl: RateLimiter) -> Optional[CoverChoice]:
    print("\nCover art options:")
    print("  1) Use cover from the SELECTED release (default)")
    print("  2) Use cover from ANOTHER search hit (cleaner cover variant)")
    print("  3) Skip embedding cover art")

    while True:
        raw = input("Pick cover option [1]: ").strip()
        if raw == "":
            opt = 1
            break
        if raw.isdigit() and int(raw) in (1, 2, 3):
            opt = int(raw)
            break
        print("Invalid choice.")

    if opt == 3:
        return None

    cover_release_mbid = selected_release_mbid
    if opt == 2:
        print("\nWhich alternate release for cover?")
        print_hits(hits)
        pick = prompt_pick(len(hits))
        if pick is None:
            return None
        cover_release_mbid = hits[pick - 1].mbid

    img_json = caa_release_images_json(cover_release_mbid, user_agent=user_agent, rl=rl)
    if not img_json:
        print("  No cover art list found; trying fallback /front image...")
        cover = fetch_cover_front(cover_release_mbid, user_agent=user_agent, rl=rl)
        if cover is None:
            return choose_cover_interactively(hits, selected_release_mbid, user_agent, rl)
        return cover

    images = img_json.get("images", []) or []
    fronts = [img for img in images if img.get("front", False)]
    if not fronts:
        print("  No front images found; trying fallback /front image...")
        cover = fetch_cover_front(cover_release_mbid, user_agent=user_agent, rl=rl)
        if cover is None:
            return choose_cover_interactively(hits, selected_release_mbid, user_agent, rl)
        return cover

    if len(fronts) == 1:
        img = fronts[0]
        img = fronts[0]
        cover = fetch_cover_by_image_id(cover_release_mbid, img["id"], user_agent=user_agent, rl=rl)
        if cover is None:
            return choose_cover_interactively(hits, selected_release_mbid, user_agent, rl)
        return cover

    print("\nMultiple front images found. Pick one:")
    for i, img in enumerate(fronts, 1):
        comment = img.get("comment", "") or "(no comment)"
        print(f"  {i}) {comment}")

    pick = prompt_pick(len(fronts))
    if pick is None:
        return None

    img = fronts[pick - 1]
    img = fronts[pick - 1]
    cover = fetch_cover_by_image_id(cover_release_mbid, img["id"], user_agent=user_agent, rl=rl)
    if cover is None:
        return choose_cover_interactively(hits, selected_release_mbid, user_agent, rl)
    return cover

# ------------------ Tag writing ------------------
def ensure_id3(mp3_path: Path) -> ID3:
    try:
        audio = MP3(str(mp3_path), ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        return audio.tags
    except ID3NoHeaderError:
        audio = MP3(str(mp3_path))
        audio.add_tags()
        return audio.tags
    except Exception as e:
        raise RuntimeError(f"failed to load id3: {e}")


def set_text_frame(tags: ID3, frame_cls, text: str) -> None:
    tags.setall(frame_cls.__name__, [])
    tags.add(frame_cls(encoding=3, text=text))


def set_txxx(tags: ID3, desc: str, value: str) -> None:
    existing = [f for f in tags.getall("TXXX") if (f.desc or "") != desc]
    tags.delall("TXXX")
    for f in existing:
        tags.add(f)
    tags.add(TXXX(encoding=3, desc=desc, text=value))


def set_apic_cover(tags: ID3, img_bytes: bytes, mime: str) -> None:
    tags.delall("APIC")
    tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=img_bytes))


def apply_release_to_folder(mp3_folder: Path, rel: Dict, cover: Optional[CoverChoice]) -> Dict[str, object]:
    mp3s = sorted_mp3s_by_tracknum(mp3_folder)
    if not mp3s:
        return {"ok": False, "error": "no mp3 files found"}

    folder_cat = parse_catalog_from_folder_name(mp3_folder)

    rel_title = rel.get("title", "")
    rel_date = rel.get("date", "") or ""
    rel_id = rel.get("id", "")

    rel_group = rel.get("release-group", {})
    release_group_id = rel_group.get("id", "")
    first_release_date = rel_group.get("first-release-date", "")
    primary_type = rel_group.get("primary-type", "") or ""
    secondary_types = rel_group.get("secondary-types", []) or []
    album_type = "/".join([primary_type] + secondary_types) if primary_type else ""

    status = rel.get("status", "")
    country = rel.get("country", "")
    text_rep = rel.get("text-representation", {}) or {}
    script = text_rep.get("script", "")

    li = rel.get("label-info") or []
    labels = "; ".join(sorted(set(item.get("label", {}).get("name", "") for item in li if "label" in item and item["label"] is not None)))
    catnos = "; ".join(item.get("catalog-number", "") for item in li if item.get("catalog-number"))

    barcode = rel.get("barcode") or ""

    asin = ""
    for rrel in rel.get("relations", []):
        if rrel.get("type") == "amazon asin":
            url = rrel["url"].get("resource", "")
            if url:
                asin = url.rstrip('/').split('/')[-1]
            break

    album_artist_credits = rel.get("artist-credit", []) or []
    rel_artist = "".join([a.get("name", "") + (a.get("joinphrase", "") or "") for a in album_artist_credits])
    album_artist_sort = "".join([a["artist"].get("sort-name", "") + (a.get("joinphrase", "") or "") for a in album_artist_credits if "artist" in a])
    album_artist_ids = " ".join([a["artist"].get("id", "") for a in album_artist_credits if "artist" in a])

    media = rel.get("media", []) or []
    total_discs = len(media)

    # Detect discnum for per-disc folders
    discnum = None
    if folder_cat:
        for i, item in enumerate(li):
            if item.get("catalog-number") == folder_cat:
                discnum = i + 1
                break

    if discnum is None:
        folder_disc = parse_disc_from_folder_name(mp3_folder)
        if folder_disc is not None:
            discnum = folder_disc

    if discnum is None:
        possible_discs = [m["position"] for m in media if m.get("track-count") == len(mp3s)]
        if len(possible_discs) == 1:
            discnum = possible_discs[0]

    if discnum is None:
        discnum = 1  # Fallback

    # Ensure discnum is within bounds
    if discnum < 1 or discnum > total_discs:
        discnum = 1

    # Check if this is a per-disc folder
    is_per_disc = False
    if discnum <= total_discs:
        med = media[discnum - 1]
        tracks_on_disc = med.get("track-count", 0)
        if tracks_on_disc == len(mp3s):
            is_per_disc = True

    if is_per_disc:
        # Per-disc mode: local tracks only
        track_list = med.get("tracks", []) or []
        track_by_pos = {tr.get("position"): tr for tr in track_list}
        med_format_base = med.get("format", "")
    else:
        # Global mode: all tracks sequenced
        global_track_count = 0
        medium_formats = {}
        medium_track_counts = {}
        track_list = []
        for med in media:
            disc_pos = med.get("position")
            if not isinstance(disc_pos, int):
                disc_pos = 1
            medium_formats[disc_pos] = med.get("format", "") or ""
            med_tracks = med.get("tracks", []) or []
            medium_track_counts[disc_pos] = len(med_tracks)
            for tr in med_tracks:
                local_pos = tr.get("position")
                if not isinstance(local_pos, int):
                    local_pos = 1
                global_track_count += 1
                tr_extra = tr.copy()
                tr_extra["disc_number"] = disc_pos
                tr_extra["local_position"] = local_pos
                tr_extra["tracks_on_disc"] = len(med_tracks)
                tr_extra["medium_format"] = medium_formats[disc_pos]
                track_list.append(tr_extra)
        track_by_global_pos = {i + 1: track_list[i] for i in range(len(track_list))}

    ok_count = 0
    fail_count = 0
    failures: List[str] = []

    for mp3_path in tqdm(mp3s, desc="tagging", unit="trk"):
        try:
            m = re.match(r"^\s*(\d+)\s*-\s*", mp3_path.name)
            if m:
                trk_num = int(m.group(1))
                file_title_guess = mp3_path.stem[len(m.group(0)):]
            else:
                trk_num = ok_count + 1
                file_title_guess = mp3_path.stem

            if is_per_disc:
                tr = track_by_pos.get(trk_num)
            else:
                tr = track_by_global_pos.get(trk_num)

            if tr:
                title = tr.get("title") or file_title_guess
                rec = tr.get("recording", {})
                rec_id = rec.get("id", "")
                tr_id = tr.get("id", "")
                isrcs = rec.get("isrcs", []) or []
                isrc = "; ".join(isrcs) if isrcs else ""
                local_pos = trk_num if is_per_disc else tr["local_position"]
                tracks_on_disc = len(mp3s) if is_per_disc else tr["tracks_on_disc"]
                med_format = med_format_base if is_per_disc else tr["medium_format"]
                track_artist_credits = tr.get("artist-credit") or album_artist_credits
                track_artist = "".join([a.get("name", "") + (a.get("joinphrase", "") or "") for a in track_artist_credits])
                track_artist_sort = "".join([a["artist"].get("sort-name", "") + (a.get("joinphrase", "") or "") for a in track_artist_credits if "artist" in a])
                track_artist_ids = " ".join([a["artist"].get("id", "") for a in track_artist_credits if "artist" in a])
            else:
                title = file_title_guess
                rec_id = ""
                tr_id = ""
                isrc = ""
                local_pos = trk_num
                tracks_on_disc = len(mp3s)
                med_format = ""
                track_artist = rel_artist
                track_artist_sort = album_artist_sort
                track_artist_ids = album_artist_ids

            tags = ensure_id3(mp3_path)

            set_text_frame(tags, TALB, rel_title)
            set_text_frame(tags, TPE1, track_artist)
            set_text_frame(tags, TPE2, rel_artist)
            set_text_frame(tags, TIT2, title)
            set_text_frame(tags, TRCK, f"{local_pos}/{tracks_on_disc}" if tracks_on_disc > 0 else str(local_pos))
            if rel_date:
                set_text_frame(tags, TDRC, rel_date)
            if labels:
                set_text_frame(tags, TPUB, labels)

            if catnos:
                set_txxx(tags, "CATALOGNUMBER", catnos)
            if barcode:
                set_txxx(tags, "BARCODE", barcode)
            if asin:
                set_txxx(tags, "ASIN", asin)
            if country:
                set_txxx(tags, "MusicBrainz Album Release Country", country)
            if status:
                set_txxx(tags, "MusicBrainz Album Status", status)
            if album_type:
                set_txxx(tags, "MusicBrainz Album Type", album_type)
            if script:
                set_txxx(tags, "script", script)
            if release_group_id:
                set_txxx(tags, "MusicBrainz Release Group Id", release_group_id)
            if rel_id:
                set_txxx(tags, "MusicBrainz Album Id", rel_id)
            if tr_id:
                set_txxx(tags, "MusicBrainz Track Id", tr_id)
            if rec_id:
                set_txxx(tags, "MusicBrainz Recording Id", rec_id)
            if album_artist_ids:
                set_txxx(tags, "MusicBrainz Album Artist Id", album_artist_ids)
            if track_artist_ids:
                set_txxx(tags, "MusicBrainz Artist Id", track_artist_ids)
            if first_release_date:
                set_text_frame(tags, TDOR, first_release_date)
            if isrc:
                set_text_frame(tags, TSRC, isrc)
            if med_format:
                set_text_frame(tags, TMED, med_format)
            if track_artist_sort:
                set_text_frame(tags, TSOP, track_artist_sort)
            if album_artist_sort:
                set_text_frame(tags, TSOA, album_artist_sort)
            set_text_frame(tags, TPOS, f"{discnum}/{total_discs}")

            if cover:
                set_apic_cover(tags, cover.bytes_data, cover.mime)

            tags.save(str(mp3_path), v2_version=3)

            ok_count += 1

        except Exception as e:
            fail_count += 1
            failures.append(f"{mp3_path.name}: {e}")

    return {
        "ok": True if fail_count == 0 else False,
        "tracks_total": len(mp3s),
        "tracks_tagged_ok": ok_count,
        "tracks_failed": fail_count,
        "failures": failures,
        "release_title": rel_title,
        "release_artist": rel_artist,
        "release_date": rel_date,
        "labels": labels,
        "catalog_numbers": catnos,
        "barcode": barcode,
        "cover_used": bool(cover),
        "cover_source": cover.source_note if cover else "",
    }


# ------------------ UI ------------------
def print_hits(hits: List[ReleaseHit]) -> None:
    for i, h in enumerate(hits, start=1):
        parts = []
        if h.date:
            parts.append(h.date)
        if h.country:
            parts.append(h.country)
        if h.status:
            parts.append(h.status)
        extra = " | ".join(parts)

        label_bit = ""
        if h.label or h.catno:
            label_bit = f" | {h.label} {h.catno}".strip()

        barcode_bit = f" | barcode {h.barcode}" if h.barcode else ""
        print(f"{i:2d}) score={h.score:3d} | {h.artist_credit} — {h.title} | {extra}{label_bit}{barcode_bit}")
        print(f"     mbid: {h.mbid}")


def prompt_pick(n: int) -> Optional[int]:
    while True:
        raw = input(f"Pick 1-{n} to apply, 's' to skip, 'q' to quit: ").strip().lower()
        if raw in ("q", "quit"):
            return -1
        if raw in ("s", "skip"):
            return None
        if raw.isdigit():
            k = int(raw)
            if 1 <= k <= n:
                return k
        print("Invalid choice.")


# ------------------ Main ------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Interactive MusicBrainz tagger for MP3 album folders (no move/rename/copy).")
    ap.add_argument("root", nargs="?", default=".", help="Root folder containing MP3 album folders (recursive)")
    ap.add_argument("--user-agent", default=USER_AGENT_DEFAULT, help="User-Agent for MusicBrainz/CAA requests")
    ap.add_argument("--limit", type=int, default=10, help="How many MusicBrainz hits to show per album")
    ap.add_argument("--no-cover", action="store_true", help="Do not embed cover art")
    ap.add_argument("--resume", action="store_true", help="Skip folders that already have mb_tag_report.json")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    reports_root = Path("ConversionTemp/MusicBrainzTagApplyReports")
    reports_root.mkdir(exist_ok=True)
    if not root.exists():
        die(f"root does not exist: {root}")

    rl = RateLimiter(MIN_SECONDS_BETWEEN_MB_CALLS)

    album_folders = find_mp3_album_folders(root)
    if not album_folders:
        print("No MP3 folders found.")
        return

    print(f"Found {len(album_folders)} MP3 folder(s). No renaming/moving/copying will be performed.\n")

    for folder in album_folders:  # or with tqdm
        report_dir = reports_root / folder.name
        report_dir.mkdir(exist_ok=True)
        if args.resume and (folder / "mb_tag_report.json").exists():
            print("=" * 80)
            print(f"Album folder: {folder}")
            print(" (resume enabled; report already exists - skipped)")
            continue
        print("=" * 80)
        print(f"Album folder: {folder}")

        cat = parse_catalog_from_folder_name(folder)
        artist, year, album = parse_artist_year_album_from_folder_name(folder)

        queries: List[str] = []
        if cat and artist:
            queries.append(f'catno:"{cat}" AND artist:"{artist}"')
        if cat and album:
            queries.append(f'catno:"{cat}" AND release:"{album}"')
        if artist and album:
            if year:
                queries.append(f'artist:"{artist}" AND release:"{album}" AND date:{year}')
            queries.append(f'artist:"{artist}" AND release:"{album}"')

        if not queries:
            queries.append(f'release:"{folder.name}"')

        hits: List[ReleaseHit] = []
        used_query = ""
        search_errors: List[str] = []
        for q in queries:
            try:
                hits = mb_release_search(q, user_agent=args.user_agent, rl=rl, limit=args.limit)
            except Exception as e:
                search_errors.append(f"{q}: {e}")
                hits = []
            if hits:
                used_query = q
                break

        if not hits:
            txt = "\n".join(
                [
                    "== MusicBrainz Tag Apply Report ==",
                    f"Time:     {now_stamp()}",
                    f"Folder:   {folder}",
                    "",
                    "Result:   NO MATCHES",
                    f"Catalog:  {cat or '(none)'}",
                    f"Artist:   {artist or '(unknown)'}",
                    f"Year:     {year or '(unknown)'}",
                    f"Album:    {album or '(unknown)'}",
                    "",
                    "Tried queries:",
                    *[f"  - {q}" for q in queries],
                    "",
                    "Errors:",
                    *([f"  - {e}" for e in search_errors] if search_errors else ["  (none)"]),
                    "",
                ]
            )
            js = {
                "time": now_stamp(),
                "folder": str(folder),
                "result": "NO_MATCHES",
                "catalog": cat or "",
                "artist": artist or "",
                "year": year or "",
                "album": album or "",
                "queries": queries,
                "errors": search_errors,
            }
            write_album_report(report_dir, txt, js)
            print(f"  No MusicBrainz matches found. Wrote report to {report_dir} and skipped.\n")
            continue

        print(f"\nQuery used: {used_query}\n")
        print_hits(hits)

        pick = prompt_pick(len(hits))
        if pick == -1:
            print("Quit.")
            return
        if pick is None:
            txt = "\n".join(
                [
                    "== MusicBrainz Tag Apply Report ==",
                    f"Time:     {now_stamp()}",
                    f"Folder:   {folder}",
                    "",
                    "Result:   SKIPPED_BY_USER",
                    f"Catalog:  {cat or '(none)'}",
                    f"Query:    {used_query}",
                    "",
                ]
            )
            js = {
                "time": now_stamp(),
                "folder": str(folder),
                "result": "SKIPPED_BY_USER",
                "catalog": cat or "",
                "query_used": used_query,
                "hits": [asdict(h) for h in hits],
            }
            write_album_report(report_dir, txt, js)
            print(f"Skipped. Report written to {report_dir}.\n")
            continue

        chosen = hits[pick - 1]
        print(f"\nFetching release details for METADATA: {chosen.mbid}")
        try:
            rel = mb_release_details(chosen.mbid, user_agent=args.user_agent, rl=rl)
        except Exception as e:
            txt = "\n".join(
                [
                    "== MusicBrainz Tag Apply Report ==",
                    f"Time:     {now_stamp()}",
                    f"Folder:   {folder}",
                    "",
                    "Result:   FAILED_FETCH_DETAILS",
                    f"Error:    {e}",
                    f"Query:    {used_query}",
                    f"Chosen:   {chosen.mbid}",
                    "",
                ]
            )
            js = {
                "time": now_stamp(),
                "folder": str(folder),
                "result": "FAILED_FETCH_DETAILS",
                "error": str(e),
                "query_used": used_query,
                "chosen": asdict(chosen),
                "hits": [asdict(h) for h in hits],
            }
            write_album_report(report_dir, txt, js)
            print(f"  Failed to fetch release details. Report written to {report_dir}.\n")
            continue

        cover_choice: Optional[CoverChoice] = None
        if not args.no_cover:
            cover_choice = choose_cover_interactively(hits=hits, selected_release_mbid=chosen.mbid, user_agent=args.user_agent, rl=rl)
            if cover_choice is None:
                print("Cover: (skipped)")
            else:
                print(f"Cover: {cover_choice.source_note}")

        print("\nApplying tags (in place)...")
        summary = apply_release_to_folder(folder, rel, cover_choice)

        result_line = "OK" if summary.get("ok") else "FAIL"
        txt_lines = [
            "== MusicBrainz Tag Apply Report ==",
            f"Time:     {now_stamp()}",
            f"Folder:   {folder}",
            "",
            f"Result:   {result_line}",
            f"Catalog:  {cat or '(none)'}",
            f"Query:    {used_query}",
            "",
            "Metadata release chosen:",
            f"  MBID:   {chosen.mbid}",
            f"  Title:  {chosen.title}",
            f"  Artist: {chosen.artist_credit}",
            f"  Date:   {chosen.date}  Country: {chosen.country}  Status: {chosen.status}",
            f"  Label:  {chosen.label}  CatNo: {chosen.catno}  Barcode: {chosen.barcode}",
            "",
        ]

        if cover_choice:
            txt_lines += ["Cover choice:", f"  {cover_choice.source_note}", ""]
        else:
            txt_lines += ["Cover choice:", "  (skipped)", ""]

        if summary.get("ok"):
            txt_lines += [
                "Tagging summary:",
                f"  Tracks total:     {summary.get('tracks_total')}",
                f"  Tagged OK:        {summary.get('tracks_tagged_ok')}",
                f"  Failed:           {summary.get('tracks_failed')}",
                f"  Release title:    {summary.get('release_title')}",
                f"  Release artist:   {summary.get('release_artist')}",
                f"  Release date:     {summary.get('release_date')}",
                f"  Labels:           {summary.get('labels')}",
                f"  Catalog numbers:  {summary.get('catalog_numbers')}",
                f"  Barcode:          {summary.get('barcode')}",
                f"  Cover embedded:   {summary.get('cover_used')} ({summary.get('cover_source')})",
                "",
            ]
            failures = summary.get("failures") or []
            if failures:
                txt_lines += ["Failures (first 20):", *[f"  - {x}" for x in failures], ""]
        else:
            txt_lines += [f"Error: {summary.get('error')}", ""]

        cover_json: Dict[str, object] = {}
        if cover_choice:
            cover_json = {
                "release_mbid": cover_choice.release_mbid,
                "image_id": cover_choice.image_id,
                "mime": cover_choice.mime,
                "byte_len": len(cover_choice.bytes_data) if cover_choice.bytes_data else 0,
                "source_note": cover_choice.source_note,
            }

        report_json = {
            "time": now_stamp(),
            "folder": str(folder),
            "result": result_line,
            "catalog_from_folder": cat or "",
            "query_used": used_query,
            "hits": [asdict(h) for h in hits],
            "chosen_metadata_release": asdict(chosen),
            "cover_choice": cover_json,
            "apply_summary": summary,
        }

        write_album_report(report_dir, "\n".join(txt_lines), report_json)
        print(f"Done. Report written to {report_dir}/mb_tag_report.txt / {report_dir}/mb_tag_report.json\n")


if __name__ == "__main__":
    main()