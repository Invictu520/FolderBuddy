#!/usr/bin/env python3
"""
FolderBuddy — sort photos and videos by capture date.

Reads EXIF / QuickTime / XMP / IPTC metadata via exiftool, with filesystem
mtime as a last-resort fallback. Moves (or copies) files into:

    <dest>/<YEAR>_<suffix>/<Month>/<filename>

Key features
------------
* Persistent SHA-1 cache of the destination — only new or changed files get
  re-hashed on subsequent runs.
* Size-based prefilter — source files whose size doesn't appear anywhere in
  the destination are never hashed at all (they can't be duplicates).
* Batched exiftool calls — one process for hundreds of files instead of one
  per file (huge Windows speedup).
* Atomic transfer — copy to <dst>.partial, hash-verify, rename, then delete
  the source. An interrupted run never leaves a half-written file at the
  destination.
* Locale-stable month names — folders are always English (`March`, never
  `März`), regardless of system locale.
* Dry-run + CSV log of every action.
* Settings file (folderbuddy.ini next to this script) so a plain
  `python main.py` remembers destination, suffix and mode.
* Detects camera cards / cameras (drives with a DCIM folder) as source.
* German end-of-run summary per month, plus a list of ignored file types.

A simple window for all of this lives in gui.py.

Requires: Python 3.9+ and the `exiftool` binary (on PATH or next to this
script). `tqdm` is optional and only used for progress bars on the console.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Callable

try:
    from tqdm import tqdm
except ImportError:  # progress bars are a nice-to-have, not a requirement
    def tqdm(iterable, **_kwargs):
        return iterable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    # images
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".heic", ".heif",
    # video
    ".mp4", ".mov", ".avi", ".mkv", ".hevc", ".webm", ".3gp", ".wmv", ".m4v",
    ".mts", ".m2ts",
    # raw
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".rw2", ".orf", ".raf",
}

# Locale-independent month names. Matches existing folders like 2025_Daniel/March.
ENGLISH_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Capture-time first; ModifyDate / FileModifyDate only as last resort.
PRIORITY_KEYS = [
    "DateTimeOriginal", "DateTimeDigitized", "CreateDate",
    "QuickTime:CreateDate", "MediaCreateDate", "TrackCreateDate",
    "XMP:DateCreated", "XMP:CreateDate",
    "IPTC:DateCreated", "IPTC:DigitalCreationDate",
    "ModifyDate", "FileModifyDate",
]

EXIFTOOL_TAGS = [
    "-EXIF:DateTimeOriginal", "-EXIF:DateTimeDigitized", "-EXIF:CreateDate",
    "-QuickTime:CreateDate", "-QuickTime:MediaCreateDate", "-QuickTime:TrackCreateDate",
    "-XMP:DateCreated", "-XMP:CreateDate",
    "-IPTC:DateCreated", "-IPTC:DigitalCreationDate",
    "-EXIF:ModifyDate", "-FileModifyDate",
]

DATE_FORMATS = ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S")

# Files that show up on cards / in folders but are never worth reporting as
# "ignored" (system junk, our own bookkeeping).
NOISE_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}

HASH_BLOCK_SIZE = 1 << 20      # 1 MiB
EXIFTOOL_BATCH_SIZE = 500      # files per exiftool invocation
DEFAULT_CACHE_NAME = ".folderbuddy_cache.json"

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "folderbuddy.ini"
CONFIG_SECTION = "FolderBuddy"

# Month folder styles: "name" -> March (default, matches the existing
# archive), "number" -> 03_March (sorts correctly in Explorer).
MONTH_STYLES = ("name", "number")

# progress(phase, done, total) — used by the GUI instead of tqdm.
ProgressFn = Callable[[str, int, int], None]

log = logging.getLogger("folderbuddy")


def _norm(p: Path | str) -> str:
    """Normalize a path for case-insensitive equality on Windows."""
    return os.path.normcase(os.path.normpath(str(p)))


# ---------------------------------------------------------------------------
# Settings file
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "source": "",
    "dest": "",
    "year_suffix": "Daniel",
    "copy": False,
    "month_style": "name",
    "log_file": "",
}


def load_settings(path: Path | None = None) -> dict:
    """Read folderbuddy.ini; missing keys fall back to DEFAULT_SETTINGS."""
    path = path or CONFIG_PATH
    settings = dict(DEFAULT_SETTINGS)
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError) as e:
        log.warning("Einstellungsdatei %s nicht lesbar (%s) — nutze Standardwerte", path, e)
        return settings
    if not cp.has_section(CONFIG_SECTION):
        return settings
    sec = cp[CONFIG_SECTION]
    for key, default in DEFAULT_SETTINGS.items():
        if key not in sec:
            continue
        if isinstance(default, bool):
            try:
                settings[key] = sec.getboolean(key)
            except ValueError:
                pass
        else:
            settings[key] = sec.get(key)
    if settings["month_style"] not in MONTH_STYLES:
        settings["month_style"] = "name"
    return settings


def save_settings(settings: dict, path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    cp = configparser.ConfigParser()
    cp[CONFIG_SECTION] = {
        k: ("yes" if v is True else "no" if v is False else str(v or ""))
        for k, v in settings.items() if k in DEFAULT_SETTINGS
    }
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)


# ---------------------------------------------------------------------------
# Finding exiftool and camera cards
# ---------------------------------------------------------------------------

def find_exiftool() -> str | None:
    """exiftool next to this script wins (no PATH fiddling needed), then PATH."""
    # Not "exiftool(-k).exe": that build waits for a key press and would hang.
    for name in ("exiftool.exe", "exiftool"):
        candidate = APP_DIR / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("exiftool")


def find_camera_sources(roots: list[Path] | None = None) -> list[Path]:
    """Return DCIM folders on attached drives (SD cards, cameras, USB sticks).

    Phones that connect via MTP (most Android phones, iPhones) don't show up
    as a drive with a path and therefore can't be found here.
    """
    if roots is None:
        if os.name == "nt":
            # Empty card-reader slots must not pop up "no disk in drive" dialogs.
            try:
                import ctypes
                ctypes.windll.kernel32.SetErrorMode(1)  # SEM_FAILCRITICALERRORS
            except (ImportError, AttributeError, OSError):
                pass
            roots = [Path(f"{letter}:\\") for letter in "DEFGHIJKLMNOPQRSTUVWXYZ"]
        else:
            roots = []
            for base in ("/media", "/run/media", "/Volumes", "/mnt"):
                b = Path(base)
                try:
                    for child in b.iterdir():
                        roots.append(child)
                        if child.is_dir() and base != "/Volumes":
                            roots.extend(c for c in child.iterdir() if c.is_dir())
                except OSError:
                    continue
    found = []
    for root in roots:
        dcim = root / "DCIM"
        try:
            if dcim.is_dir():
                found.append(dcim)
        except OSError:  # e.g. empty card reader slot on Windows
            continue
    return found


def open_folder(path: Path) -> None:
    """Open a folder in Explorer / Finder / the file manager."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as e:
        log.warning("Ordner konnte nicht geöffnet werden: %s", e)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def compute_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_BLOCK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Persistent hash cache
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    size: int
    mtime: float
    sha1: str


class HashCache:
    """Persistent (path -> size, mtime, sha1) map for the destination tree.

    Keys are paths relative to dest_root, so the cache survives if the drive
    is remounted under a different letter. On reconcile, entries are validated
    against the filesystem: stale entries (size or mtime changed) get rehashed,
    missing files get dropped, and new files get hashed and added.

    Also maintains an in-memory `size -> {sha1, ...}` index for the size
    prefilter — a source file whose size doesn't appear here can't be a
    duplicate of anything we already have, so we don't bother hashing it.
    """

    def __init__(self, dest_root: Path, cache_path: Path):
        self.dest_root = dest_root
        self.cache_path = cache_path
        self.entries: dict[str, CacheEntry] = {}
        self._size_index: dict[int, set[str]] = {}
        self._dirty = False

    @classmethod
    def load(cls, dest_root: Path, cache_path: Path) -> "HashCache":
        cache = cls(dest_root, cache_path)
        if cache_path.exists():
            try:
                with cache_path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                for rel, entry in raw.items():
                    cache.entries[rel] = CacheEntry(**entry)
                log.info("%d Cache-Einträge geladen aus %s",
                         len(cache.entries), cache_path)
            except (json.JSONDecodeError, OSError, TypeError, KeyError) as e:
                log.warning("Cache-Datei unlesbar (%s) — wird neu aufgebaut", e)
        return cache

    def save(self) -> None:
        if not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({rel: asdict(e) for rel, e in self.entries.items()}, f)
        os.replace(tmp, self.cache_path)
        self._dirty = False
        log.debug("Saved %d cache entries to %s",
                  len(self.entries), self.cache_path)

    def rebuild_size_index(self) -> None:
        self._size_index.clear()
        for e in self.entries.values():
            self._size_index.setdefault(e.size, set()).add(e.sha1)

    def hashes_for_size(self, size: int) -> set[str]:
        return self._size_index.get(size, set())

    def add(self, rel_path: str, size: int, mtime: float, sha1: str) -> None:
        self.entries[rel_path] = CacheEntry(size=size, mtime=mtime, sha1=sha1)
        self._size_index.setdefault(size, set()).add(sha1)
        self._dirty = True

    def discard_path(self, rel_path: str) -> None:
        if rel_path in self.entries:
            del self.entries[rel_path]
            self._dirty = True

    def reconcile(self, supported_exts: set[str], no_progress: bool = False,
                  progress: ProgressFn | None = None) -> None:
        log.info("Durchsuche Zielordner: %s", self.dest_root)
        if progress:
            progress("Zielordner wird durchsucht…", 0, 0)

        on_disk: dict[str, tuple[int, float]] = {}
        for path in self.dest_root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in supported_exts:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            try:
                rel = str(path.relative_to(self.dest_root))
            except ValueError:
                continue
            on_disk[rel] = (st.st_size, st.st_mtime)

        # Drop cache entries for files that no longer exist on disk.
        gone = [rel for rel in self.entries if rel not in on_disk]
        for rel in gone:
            self.discard_path(rel)
        if gone:
            log.info("%d Cache-Einträge entfernt (Dateien gibt es nicht mehr).",
                     len(gone))

        # Identify files that need (re)hashing.
        to_hash: list[str] = []
        for rel, (size, mtime) in on_disk.items():
            entry = self.entries.get(rel)
            if (entry is None
                    or entry.size != size
                    or abs(entry.mtime - mtime) > 1e-3):
                to_hash.append(rel)

        if not to_hash:
            log.info("Cache aktuell — %d Dateien im Zielordner bekannt.", len(self.entries))
            self.rebuild_size_index()
            return

        log.info("Lese %d neue/geänderte Dateien im Zielordner ein…", len(to_hash))
        for i, rel in enumerate(tqdm(to_hash, desc="Indexing destination",
                                     unit="file",
                                     disable=no_progress or progress is not None)):
            if progress:
                progress("Zielordner wird eingelesen (nur beim ersten Mal langsam)…",
                         i, len(to_hash))
            full = self.dest_root / rel
            try:
                sha1 = compute_hash(full)
                size, mtime = on_disk[rel]
                self.add(rel, size, mtime, sha1)
            except OSError as e:
                log.warning("Konnte %s nicht lesen: %s", full, e)

        self.rebuild_size_index()
        log.info("Zielordner eingelesen: %d Dateien.", len(self.entries))


# ---------------------------------------------------------------------------
# Date extraction (batched exiftool)
# ---------------------------------------------------------------------------

class ExiftoolMissing(RuntimeError):
    def __init__(self):
        super().__init__(
            "exiftool wurde nicht gefunden. Lade es von https://exiftool.org/ "
            "herunter, benenne 'exiftool(-k).exe' in 'exiftool.exe' um und lege "
            "es (samt Ordner 'exiftool_files') in den FolderBuddy-Ordner.")


def _parse_exif_date(data: dict) -> tuple[datetime | None, str | None]:
    """Pick the best date from an exiftool JSON record. Returns (dt, source_key)."""
    # Mirror XMP/QuickTime fallbacks into the generic keys so the priority list works.
    if not data.get("DateTimeOriginal") and data.get("XMP:DateCreated"):
        data["DateTimeOriginal"] = data["XMP:DateCreated"]
    if not data.get("CreateDate"):
        if data.get("XMP:CreateDate"):
            data["CreateDate"] = data["XMP:CreateDate"]
        elif data.get("QuickTime:CreateDate"):
            data["CreateDate"] = data["QuickTime:CreateDate"]

    for key in PRIORITY_KEYS:
        val = data.get(key)
        if not val:
            continue
        for fmt in DATE_FORMATS:
            try:
                dt = datetime.strptime(val, fmt)
                if dt.tzinfo is not None:
                    # Convert to local naive — folder structure is local-time-based.
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt, key
            except ValueError:
                continue
    return None, None


def read_dates_batch(paths: list[Path], progress: ProgressFn | None = None
                     ) -> dict[Path, tuple[datetime, str]]:
    """Read capture dates for many files in one or a few exiftool calls.

    Falls back to filesystem mtime for any file exiftool can't extract
    a date from.
    """
    result: dict[Path, tuple[datetime, str]] = {}
    if not paths:
        return result

    for chunk_start in range(0, len(paths), EXIFTOOL_BATCH_SIZE):
        chunk = paths[chunk_start:chunk_start + EXIFTOOL_BATCH_SIZE]
        if progress:
            progress("Aufnahmedaten werden gelesen…", chunk_start, len(paths))
        # Build a normalized lookup table for matching exiftool's SourceFile
        # output back to our original Path objects (Windows: forward vs back slashes).
        norm_to_path = {_norm(p): p for p in chunk}

        # Args-file avoids command-line length limits and handles Unicode safely.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tf:
            argfile = Path(tf.name)
            for p in chunk:
                tf.write(str(p) + "\n")

        try:
            cmd = [
                find_exiftool() or "exiftool",
                "-charset", "filename=utf8",
                "-api", "QuickTimeUTC",
                "-api", "LargeFileSupport=1",
                "-s", "-json",
                "-d", "%Y-%m-%d %H:%M:%S%z",
                *EXIFTOOL_TAGS,
                "-@", str(argfile),
            ]
            try:
                r = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                )
            except FileNotFoundError:
                raise ExiftoolMissing()

            if r.returncode != 0:
                log.warning("exiftool meldet Code %d: %s",
                            r.returncode, r.stderr.strip()[:500])

            if r.stdout.strip():
                try:
                    items = json.loads(r.stdout)
                except json.JSONDecodeError as e:
                    log.warning("exiftool-Ausgabe nicht lesbar: %s", e)
                    items = []

                for item in items:
                    src = item.get("SourceFile")
                    if not src:
                        continue
                    orig = norm_to_path.get(_norm(src))
                    if orig is None:
                        continue
                    dt, key = _parse_exif_date(item)
                    if dt is not None:
                        result[orig] = (dt, key)
        finally:
            try:
                argfile.unlink()
            except OSError:
                pass

    # Filesystem fallback for anything we couldn't read.
    for p in paths:
        if p in result:
            continue
        try:
            ts = p.stat().st_mtime
            result[p] = (datetime.fromtimestamp(ts), "FS:mtime")
        except OSError:
            result[p] = (datetime.now(), "FS:now")

    return result


# ---------------------------------------------------------------------------
# Atomic transfer
# ---------------------------------------------------------------------------

def safe_transfer(src: Path, dst: Path, copy: bool,
                  expected_hash: str | None = None) -> str:
    """Copy src -> dst with hash computation; return the SHA-1 of the data.

    The file is streamed once into <dst>.partial, with SHA-1 computed during
    the copy. If `expected_hash` is provided, the copy must match it. The
    partial is then atomically renamed to its final name. On any failure the
    partial is removed and the source is left untouched.

    If `copy=False`, the source is deleted after a successful rename.

    Note: hashing during read gives us the source's hash for free, which is
    plenty for dedup. It does not protect against silent disk write
    corruption — but neither did the original tool, and modern filesystems
    handle this. If you ever want stronger guarantees, re-read the
    destination after rename and compare.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = dst.with_suffix(dst.suffix + ".partial")
    h = hashlib.sha1()
    try:
        with open(src, "rb") as fsrc, open(partial, "wb") as fdst:
            while True:
                buf = fsrc.read(HASH_BLOCK_SIZE)
                if not buf:
                    break
                fdst.write(buf)
                h.update(buf)
        shutil.copystat(src, partial)
        sha1 = h.hexdigest()
        if expected_hash is not None and sha1 != expected_hash:
            raise IOError(f"Hash mismatch after copy: {sha1} != {expected_hash}")
        os.replace(partial, dst)
    except Exception:
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass
        raise

    if not copy:
        try:
            src.unlink()
        except OSError as e:
            log.warning("Kopiert, aber Original konnte nicht gelöscht werden: %s: %s", src, e)

    return sha1


def unique_destination(dst_folder: Path, filename: str,
                       reserved: set[str] | None = None) -> Path:
    """Return a path inside dst_folder that doesn't exist yet, suffixing _1, _2 …

    `reserved` holds normalized paths already planned in this run, so a dry
    run shows the same names a real run would produce.
    """
    reserved = reserved if reserved is not None else set()
    base, ext = os.path.splitext(filename)
    candidate = dst_folder / filename
    counter = 1
    while candidate.exists() or _norm(candidate) in reserved:
        candidate = dst_folder / f"{base}_{counter}{ext}"
        counter += 1
    return candidate


def month_folder_name(month: int, style: str = "name") -> str:
    name = ENGLISH_MONTHS[month - 1]
    return f"{month:02d}_{name}" if style == "number" else name


# ---------------------------------------------------------------------------
# Main transfer
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    transferred: int = 0
    skipped_duplicate: int = 0
    errors: int = 0
    bytes_transferred: int = 0
    dry_run: bool = False
    copy: bool = False
    # "2026_Daniel\March" -> number of files transferred there
    per_folder: Counter = field(default_factory=Counter)
    # ".xmp" -> number of files left behind because the type isn't supported
    ignored: Counter = field(default_factory=Counter)
    error_messages: list[str] = field(default_factory=list)


def collect_source_files(src_folder: Path, exts: set[str]) -> list[Path]:
    files, _ = scan_source(src_folder, exts)
    return files


def scan_source(src_folder: Path, exts: set[str]) -> tuple[list[Path], Counter]:
    """Return supported files plus a per-extension count of everything else."""
    files: list[Path] = []
    ignored: Counter = Counter()
    for path in src_folder.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in exts:
            files.append(path)
        elif (path.name.lower() not in NOISE_FILES
              and not path.name.startswith(".")
              and suffix != ".partial"):
            ignored[suffix or "(ohne Endung)"] += 1
    return files, ignored


def open_log_writer(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not log_path.exists() or log_path.stat().st_size == 0
    f = log_path.open("a", newline="", encoding="utf-8")
    writer = csv.writer(f)
    if new_file:
        writer.writerow([
            "timestamp", "action", "src", "dst", "sha1",
            "size_bytes", "date_used", "date_source", "note",
        ])
    return f, writer


def _fmt_size(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB".replace(".", ",")
    return f"{n / 1e6:.1f} MB".replace(".", ",")


def format_summary(stats: Stats) -> str:
    """Human-readable German summary of a run."""
    lines = []
    verb = "kopiert" if stats.copy else "verschoben"
    if stats.dry_run:
        lines.append("Vorschau – es wurde noch nichts verändert.")
        lines.append(f"Würden {verb}: {stats.transferred} Dateien "
                     f"({_fmt_size(stats.bytes_transferred)})")
    else:
        lines.append("Fertig!")
        lines.append(f"{verb.capitalize()}: {stats.transferred} Dateien "
                     f"({_fmt_size(stats.bytes_transferred)})")
    lines.append(f"Schon vorhanden (übersprungen): {stats.skipped_duplicate}")
    lines.append(f"Fehler: {stats.errors}")
    if stats.per_folder:
        lines.append("")
        lines.append("Ziel nach Ordner:")
        for folder in sorted(stats.per_folder):
            lines.append(f"  {folder}: {stats.per_folder[folder]}")
    if stats.ignored:
        lines.append("")
        parts = ", ".join(f"{n}× {ext}" for ext, n in stats.ignored.most_common())
        lines.append(f"Nicht unterstützt und liegen gelassen: {parts}")
    if stats.error_messages:
        lines.append("")
        lines.append("Fehlermeldungen:")
        lines.extend(f"  {m}" for m in stats.error_messages[:20])
        if len(stats.error_messages) > 20:
            lines.append(f"  … und {len(stats.error_messages) - 20} weitere")
    return "\n".join(lines)


def run_transfer(args: argparse.Namespace,
                 progress: ProgressFn | None = None) -> Stats:
    """Do the actual work. Raises ValueError for bad input, ExiftoolMissing
    if exiftool can't be started. Used by both the CLI and the GUI."""
    if not args.source:
        raise ValueError("Kein Quellordner angegeben.")
    if not args.dest:
        raise ValueError("Kein Zielordner angegeben.")
    src_folder = Path(args.source).expanduser()
    dst_root = Path(args.dest).expanduser()
    if not src_folder.is_dir():
        raise ValueError(f"Quellordner nicht gefunden: {src_folder}")
    if _norm(src_folder) == _norm(dst_root):
        raise ValueError("Quell- und Zielordner sind identisch.")
    dst_root.mkdir(parents=True, exist_ok=True)
    month_style = getattr(args, "month_style", "name") or "name"
    quiet = args.quiet or progress is not None

    # Cache setup
    cache_path = (Path(args.cache_file).expanduser() if args.cache_file
                  else dst_root / DEFAULT_CACHE_NAME)
    if args.no_cache:
        cache = HashCache(dst_root, cache_path)
    else:
        cache = HashCache.load(dst_root, cache_path)
    cache.reconcile(SUPPORTED_EXTENSIONS, no_progress=args.quiet, progress=progress)

    stats = Stats(dry_run=args.dry_run, copy=args.copy)

    # Source scan
    log.info("Durchsuche Quelle: %s", src_folder)
    if progress:
        progress("Quelle wird durchsucht…", 0, 0)
    sources, stats.ignored = scan_source(src_folder, SUPPORTED_EXTENSIONS)
    log.info("%d Fotos/Videos in der Quelle gefunden.", len(sources))
    if not sources:
        if not args.no_cache and not args.dry_run:
            cache.save()
        return stats

    # Batch metadata read
    log.info("Lese Aufnahmedaten mit exiftool…")
    dates = read_dates_batch(sources, progress=progress)

    # CSV log
    log_file = log_writer = None
    if args.log_file:
        log_file, log_writer = open_log_writer(Path(args.log_file).expanduser())

    # Dry run only: nothing lands in the cache, so remember what was planned
    # to get the same names and the same duplicate decisions as a real run.
    planned: set[str] = set()                        # target paths
    planned_by_size: dict[int, list[str | Path]] = {}  # size -> sha1 or path
    total = len(sources)
    try:
        for i, src in enumerate(tqdm(sources, desc="Sorting media",
                                     unit="file", disable=quiet)):
            if progress:
                progress("Dateien werden übertragen…" if not args.dry_run
                         else "Vorschau wird erstellt…", i, total)
            try:
                size = src.stat().st_size
            except OSError as e:
                log.warning("Kann %s nicht lesen: %s", src, e)
                stats.errors += 1
                stats.error_messages.append(f"{src.name}: {e}")
                continue

            # Size prefilter: if no destination file has this exact size, the
            # source can't be a duplicate of anything we have.
            potential_dupe_hashes = cache.hashes_for_size(size)

            sha1: str | None = None
            planned_same_size = args.dry_run and size in planned_by_size
            if potential_dupe_hashes or planned_same_size:
                planned_hashes: set[str] = set()
                try:
                    sha1 = compute_hash(src)
                    if planned_same_size:
                        planned_hashes = {h if isinstance(h, str) else compute_hash(h)
                                          for h in planned_by_size[size]}
                        planned_by_size[size] = list(planned_hashes)
                except OSError as e:
                    log.warning("Kann %s nicht lesen: %s", src, e)
                    stats.errors += 1
                    stats.error_messages.append(f"{src.name}: {e}")
                    continue
                if sha1 in potential_dupe_hashes or sha1 in planned_hashes:
                    stats.skipped_duplicate += 1
                    if log_writer:
                        log_writer.writerow([
                            datetime.now().isoformat(timespec="seconds"),
                            "skipped-duplicate", str(src), "", sha1,
                            size, "", "", "already in destination",
                        ])
                    continue

            # Compute destination path.
            dt, src_key = dates.get(src, (None, None))
            if dt is None:
                # Should be unreachable — read_dates_batch always returns something.
                dt = datetime.fromtimestamp(src.stat().st_mtime)
                src_key = "FS:mtime"

            year_folder = f"{dt.year}_{args.year_suffix}"
            month_folder = month_folder_name(dt.month, month_style)
            target_dir = dst_root / year_folder / month_folder
            target = unique_destination(target_dir, src.name, planned)
            folder_label = os.path.join(year_folder, month_folder)

            if args.dry_run:
                action = "would-copy" if args.copy else "would-move"
                log.debug("%s: %s -> %s  [date: %s, source: %s]",
                          action, src, target, dt.isoformat(), src_key)
                if log_writer:
                    log_writer.writerow([
                        datetime.now().isoformat(timespec="seconds"),
                        action, str(src), str(target), sha1 or "",
                        size, dt.isoformat(), src_key or "", "",
                    ])
                planned.add(_norm(target))
                planned_by_size.setdefault(size, []).append(sha1 or src)
                stats.transferred += 1
                stats.bytes_transferred += size
                stats.per_folder[folder_label] += 1
                continue

            try:
                copied_sha1 = safe_transfer(
                    src, target, copy=args.copy, expected_hash=sha1,
                )
            except Exception as e:
                log.error("Übertragung fehlgeschlagen für %s: %s", src, e)
                stats.errors += 1
                stats.error_messages.append(f"{src.name}: {e}")
                if log_writer:
                    log_writer.writerow([
                        datetime.now().isoformat(timespec="seconds"),
                        "error", str(src), str(target), sha1 or "",
                        size, dt.isoformat(), src_key or "", str(e),
                    ])
                continue

            stats.transferred += 1
            stats.bytes_transferred += size
            stats.per_folder[folder_label] += 1

            # Update cache so the new file is seen on subsequent runs.
            try:
                rel = str(target.relative_to(dst_root))
                cache.add(rel, size, target.stat().st_mtime, copied_sha1)
            except (OSError, ValueError):
                pass

            action = "copied" if args.copy else "moved"
            if log_writer:
                log_writer.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    action, str(src), str(target), copied_sha1,
                    size, dt.isoformat(), src_key or "", "",
                ])
        if progress:
            progress("Fertig", total, total)
    finally:
        if log_file is not None:
            log_file.close()
        if not args.dry_run and not args.no_cache:
            cache.save()

    return stats


def transfer(args: argparse.Namespace) -> int:
    """CLI entry: run, print the German summary, return an exit code."""
    try:
        stats = run_transfer(args)
    except ValueError as e:
        log.error("%s", e)
        return 1
    except ExiftoolMissing as e:
        log.error("%s", e)
        return 2

    print()
    print(format_summary(stats))
    if getattr(args, "open", False) and not args.dry_run:
        open_folder(Path(args.dest).expanduser())
    return 0 if stats.errors == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser(settings: dict | None = None) -> argparse.ArgumentParser:
    """Defaults come from folderbuddy.ini, so all options are optional once
    the destination is saved there (see --save-settings)."""
    st = settings if settings is not None else load_settings()
    p = argparse.ArgumentParser(
        prog="folderbuddy",
        description="Sort photos and videos into <year>_<suffix>/<Month>/ folders. "
                    f"Defaults are read from {CONFIG_PATH.name}.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", "-s", default=None,
                   help="Source folder (e.g., DCIM dump). If omitted: an attached "
                        "camera card with a DCIM folder, else the saved source.")
    p.add_argument("--dest", "-d", default=st["dest"] or None,
                   help="Destination root folder.")
    p.add_argument("--year-suffix", default=st["year_suffix"],
                   help="Appended to year folder name: <year>_<suffix>.")
    p.add_argument("--copy", action=argparse.BooleanOptionalAction,
                   default=st["copy"],
                   help="Copy instead of moving the files (--no-copy = move).")
    p.add_argument("--month-style", choices=MONTH_STYLES, default=st["month_style"],
                   help="Month folders as 'March' (name) or '03_March' (number).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would happen but don't touch any files.")
    p.add_argument("--log-file", default=st["log_file"] or None,
                   help="CSV log of every action (created/appended).")
    p.add_argument("--cache-file",
                   help="Hash cache JSON path "
                        f"(default: <dest>/{DEFAULT_CACHE_NAME}).")
    p.add_argument("--no-cache", action="store_true",
                   help="Ignore the persistent cache and rehash from scratch.")
    p.add_argument("--open", action="store_true",
                   help="Open the destination folder when done.")
    p.add_argument("--save-settings", action="store_true",
                   help=f"Store source, dest, suffix, copy, month style and log "
                        f"file in {CONFIG_PATH.name} as new defaults.")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Suppress progress bars.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Verbose logging (DEBUG level).")
    return p


def resolve_source(args: argparse.Namespace, settings: dict) -> None:
    """Fill args.source if it wasn't given: a single attached camera card
    wins, otherwise the saved source folder."""
    if args.source:
        return
    cards = find_camera_sources()
    if len(cards) == 1:
        log.info("Kamera/Speicherkarte gefunden: %s", cards[0])
        args.source = str(cards[0])
    elif len(cards) > 1:
        log.info("Mehrere Karten gefunden (%s) — bitte mit --source eine wählen.",
                 ", ".join(str(c) for c in cards))
    elif settings.get("source"):
        log.info("Nutze gespeicherten Quellordner: %s", settings["source"])
        args.source = settings["source"]


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    args = build_parser(settings).parse_args(argv)
    configure_logging(args.verbose)
    resolve_source(args, settings)
    if args.save_settings:
        save_settings({
            "source": args.source or settings["source"],
            "dest": args.dest,
            "year_suffix": args.year_suffix,
            "copy": args.copy,
            "month_style": args.month_style,
            "log_file": args.log_file,
        })
        log.info("Einstellungen gespeichert in %s", CONFIG_PATH)
    return transfer(args)


if __name__ == "__main__":
    sys.exit(main())
