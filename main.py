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

Requires: Python 3.9+, `tqdm`, and the `exiftool` binary on PATH.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from tqdm import tqdm


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    # images
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".heic", ".heif",
    # video
    ".mp4", ".mov", ".avi", ".mkv", ".hevc", ".webm", ".3gp", ".wmv", ".m4v",
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

HASH_BLOCK_SIZE = 1 << 20      # 1 MiB
EXIFTOOL_BATCH_SIZE = 500      # files per exiftool invocation
DEFAULT_CACHE_NAME = ".folderbuddy_cache.json"

log = logging.getLogger("folderbuddy")


def _norm(p: Path | str) -> str:
    """Normalize a path for case-insensitive equality on Windows."""
    return os.path.normcase(os.path.normpath(str(p)))


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
                log.info("Loaded %d cache entries from %s",
                         len(cache.entries), cache_path)
            except (json.JSONDecodeError, OSError, TypeError, KeyError) as e:
                log.warning("Cache file unreadable (%s) — starting fresh", e)
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

    def reconcile(self, supported_exts: set[str], no_progress: bool = False) -> None:
        log.info("Scanning destination: %s", self.dest_root)

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
            log.info("Removed %d stale cache entries (files no longer on disk).",
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
            log.info("Cache up to date — %d files indexed.", len(self.entries))
            self.rebuild_size_index()
            return

        log.info("Hashing %d new/changed files…", len(to_hash))
        for rel in tqdm(to_hash, desc="Indexing destination",
                        unit="file", disable=no_progress):
            full = self.dest_root / rel
            try:
                sha1 = compute_hash(full)
                size, mtime = on_disk[rel]
                self.add(rel, size, mtime, sha1)
            except OSError as e:
                log.warning("Could not hash %s: %s", full, e)

        self.rebuild_size_index()
        log.info("Destination indexed: %d files.", len(self.entries))


# ---------------------------------------------------------------------------
# Date extraction (batched exiftool)
# ---------------------------------------------------------------------------

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


def read_dates_batch(paths: list[Path]) -> dict[Path, tuple[datetime, str]]:
    """Read capture dates for many files in one or a few exiftool calls.

    Falls back to filesystem mtime for any file exiftool can't extract
    a date from.
    """
    result: dict[Path, tuple[datetime, str]] = {}
    if not paths:
        return result

    for chunk_start in range(0, len(paths), EXIFTOOL_BATCH_SIZE):
        chunk = paths[chunk_start:chunk_start + EXIFTOOL_BATCH_SIZE]
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
                "exiftool",
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
                log.error("exiftool not found on PATH. "
                          "Install it from https://exiftool.org/")
                raise SystemExit(2)

            if r.returncode != 0:
                log.warning("exiftool exited with code %d: %s",
                            r.returncode, r.stderr.strip()[:500])

            if r.stdout.strip():
                try:
                    items = json.loads(r.stdout)
                except json.JSONDecodeError as e:
                    log.warning("exiftool JSON parse error: %s", e)
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
            log.warning("Copy succeeded but could not remove source %s: %s", src, e)

    return sha1


def unique_destination(dst_folder: Path, filename: str) -> Path:
    """Return a path inside dst_folder that doesn't exist yet, suffixing _1, _2 …"""
    base, ext = os.path.splitext(filename)
    candidate = dst_folder / filename
    counter = 1
    while candidate.exists():
        candidate = dst_folder / f"{base}_{counter}{ext}"
        counter += 1
    return candidate


# ---------------------------------------------------------------------------
# Main transfer
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    transferred: int = 0
    skipped_duplicate: int = 0
    errors: int = 0
    bytes_transferred: int = 0


def collect_source_files(src_folder: Path, exts: set[str]) -> list[Path]:
    files: list[Path] = []
    for path in src_folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in exts:
            files.append(path)
    return files


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


def transfer(args: argparse.Namespace) -> int:
    src_folder = Path(args.source).expanduser()
    dst_root = Path(args.dest).expanduser()
    if not src_folder.is_dir():
        log.error("Source is not a directory: %s", src_folder)
        return 1
    dst_root.mkdir(parents=True, exist_ok=True)

    # Cache setup
    cache_path = (Path(args.cache_file).expanduser() if args.cache_file
                  else dst_root / DEFAULT_CACHE_NAME)
    if args.no_cache:
        cache = HashCache(dst_root, cache_path)
    else:
        cache = HashCache.load(dst_root, cache_path)
    cache.reconcile(SUPPORTED_EXTENSIONS, no_progress=args.quiet)

    # Source scan
    log.info("Scanning source: %s", src_folder)
    sources = collect_source_files(src_folder, SUPPORTED_EXTENSIONS)
    log.info("Found %d source files.", len(sources))
    if not sources:
        if not args.no_cache and not args.dry_run:
            cache.save()
        return 0

    # Batch metadata read
    log.info("Reading metadata via exiftool…")
    dates = read_dates_batch(sources)

    # CSV log
    log_file = log_writer = None
    if args.log_file:
        log_file, log_writer = open_log_writer(Path(args.log_file).expanduser())

    stats = Stats()
    try:
        for src in tqdm(sources, desc="Sorting media",
                        unit="file", disable=args.quiet):
            try:
                size = src.stat().st_size
            except OSError as e:
                log.warning("Cannot stat %s: %s", src, e)
                stats.errors += 1
                continue

            # Size prefilter: if no destination file has this exact size, the
            # source can't be a duplicate of anything we have.
            potential_dupe_hashes = cache.hashes_for_size(size)

            sha1: str | None = None
            if potential_dupe_hashes:
                try:
                    sha1 = compute_hash(src)
                except OSError as e:
                    log.warning("Cannot hash %s: %s", src, e)
                    stats.errors += 1
                    continue
                if sha1 in potential_dupe_hashes:
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
            month_folder = ENGLISH_MONTHS[dt.month - 1]
            target_dir = dst_root / year_folder / month_folder
            target = unique_destination(target_dir, src.name)

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
                stats.transferred += 1
                continue

            try:
                copied_sha1 = safe_transfer(
                    src, target, copy=args.copy, expected_hash=sha1,
                )
            except Exception as e:
                log.error("Transfer failed for %s: %s", src, e)
                stats.errors += 1
                if log_writer:
                    log_writer.writerow([
                        datetime.now().isoformat(timespec="seconds"),
                        "error", str(src), str(target), sha1 or "",
                        size, dt.isoformat(), src_key or "", str(e),
                    ])
                continue

            stats.transferred += 1
            stats.bytes_transferred += size

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
    finally:
        if log_file is not None:
            log_file.close()
        if not args.dry_run and not args.no_cache:
            cache.save()

    log.info("")
    if args.dry_run:
        log.info("Dry-run summary (no files were touched):")
        log.info("  Would transfer: %d files (%.1f MB)",
                 stats.transferred, stats.bytes_transferred / 1e6)
    else:
        log.info("Done.")
        log.info("  Transferred: %d files (%.1f MB)",
                 stats.transferred, stats.bytes_transferred / 1e6)
    log.info("  Duplicates skipped: %d", stats.skipped_duplicate)
    log.info("  Errors: %d", stats.errors)
    return 0 if stats.errors == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="folderbuddy",
        description="Sort photos and videos into <year>_<suffix>/<Month>/ folders.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", "-s", required=True,
                   help="Source folder (e.g., DCIM dump).")
    p.add_argument("--dest", "-d", required=True,
                   help="Destination root folder.")
    p.add_argument("--year-suffix", default="Daniel",
                   help="Appended to year folder name: <year>_<suffix>.")
    p.add_argument("--copy", action="store_true",
                   help="Copy instead of moving the files.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would happen but don't touch any files.")
    p.add_argument("--log-file",
                   help="CSV log of every action (created/appended).")
    p.add_argument("--cache-file",
                   help="Hash cache JSON path "
                        f"(default: <dest>/{DEFAULT_CACHE_NAME}).")
    p.add_argument("--no-cache", action="store_true",
                   help="Ignore the persistent cache and rehash from scratch.")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Suppress progress bars.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Verbose logging (DEBUG level).")
    return p


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    return transfer(args)


if __name__ == "__main__":
    sys.exit(main())
