"""
Integration tests for the FolderBuddy refactor.

Tests cover the parts that don't depend on exiftool being installed:
- Hash cache persistence and reconciliation
- Size prefilter
- Safe transfer (atomic copy/move with hash verification)
- Unique destination naming
- Filesystem date fallback
- Dry-run mode
- CSV log output
- End-to-end transfer with mocked exiftool
"""

import csv
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# Stub tqdm so the import works
sys.modules.setdefault("tqdm", types.SimpleNamespace(tqdm=lambda x, **k: x))

# Make main importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as fb  # noqa: E402


def make_photo(path: Path, content: bytes, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TestHashCache(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_path = root / "cache.json"

            cache = fb.HashCache(root, cache_path)
            cache.add("a.jpg", 100, 12345.0, "abc")
            cache.add("sub/b.jpg", 200, 67890.0, "def")
            cache.save()

            cache2 = fb.HashCache.load(root, cache_path)
            self.assertEqual(len(cache2.entries), 2)
            self.assertEqual(cache2.entries["a.jpg"].sha1, "abc")
            self.assertEqual(cache2.entries["sub/b.jpg"].size, 200)

    def test_corrupt_cache_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_path = root / "cache.json"
            cache_path.write_text("{not valid json")
            cache = fb.HashCache.load(root, cache_path)
            self.assertEqual(len(cache.entries), 0)

    def test_reconcile_drops_missing_hashes_new(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_path = root / "cache.json"

            # Pre-populate cache with an entry for a file that doesn't exist
            cache = fb.HashCache(root, cache_path)
            cache.add("ghost.jpg", 100, 0, "ghosthash")

            # Add two real files
            make_photo(root / "real1.jpg", b"hello world")
            make_photo(root / "sub" / "real2.jpg", b"different content")

            cache.reconcile(fb.SUPPORTED_EXTENSIONS, no_progress=True)

            # Ghost dropped, real files added
            self.assertNotIn("ghost.jpg", cache.entries)
            self.assertIn("real1.jpg", cache.entries)
            self.assertIn(os.path.join("sub", "real2.jpg"), cache.entries)

            # Size index works
            sz_real1 = (root / "real1.jpg").stat().st_size
            self.assertEqual(len(cache.hashes_for_size(sz_real1)), 1)

    def test_reconcile_rehashes_changed_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_path = root / "cache.json"
            f = root / "file.jpg"
            make_photo(f, b"original")

            cache = fb.HashCache(root, cache_path)
            cache.reconcile(fb.SUPPORTED_EXTENSIONS, no_progress=True)
            original_hash = cache.entries["file.jpg"].sha1

            # Modify the file (size changes) — bump mtime so reconcile sees it
            make_photo(f, b"new much longer content here", mtime=9999999999.0)
            cache.reconcile(fb.SUPPORTED_EXTENSIONS, no_progress=True)

            self.assertNotEqual(cache.entries["file.jpg"].sha1, original_hash)

    def test_reconcile_skips_unchanged_files(self):
        """If size+mtime match, the cache should NOT recompute the hash."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_path = root / "cache.json"
            f = root / "file.jpg"
            make_photo(f, b"unchanged content")

            cache = fb.HashCache(root, cache_path)
            cache.reconcile(fb.SUPPORTED_EXTENSIONS, no_progress=True)

            with patch.object(fb, "compute_hash",
                              side_effect=AssertionError("should not be called")):
                cache.reconcile(fb.SUPPORTED_EXTENSIONS, no_progress=True)


class TestSafeTransfer(unittest.TestCase):
    def test_basic_move(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src.jpg"
            dst = root / "out" / "dst.jpg"
            payload = b"some image data"
            make_photo(src, payload)

            sha1 = fb.safe_transfer(src, dst, copy=False)

            self.assertTrue(dst.exists())
            self.assertFalse(src.exists())
            self.assertEqual(dst.read_bytes(), payload)
            self.assertEqual(fb.compute_hash(dst), sha1)

    def test_basic_copy_keeps_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src.jpg"
            dst = root / "dst.jpg"
            make_photo(src, b"x")
            fb.safe_transfer(src, dst, copy=True)
            self.assertTrue(src.exists())
            self.assertTrue(dst.exists())

    def test_hash_mismatch_aborts_and_leaves_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src.jpg"
            dst = root / "dst.jpg"
            make_photo(src, b"data")

            with self.assertRaises(IOError):
                fb.safe_transfer(src, dst, copy=False, expected_hash="wrong" * 8)

            self.assertTrue(src.exists())
            self.assertFalse(dst.exists())
            # No leftover .partial
            self.assertFalse((dst.parent / "dst.jpg.partial").exists())

    def test_unique_destination_suffixes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "img.jpg").write_bytes(b"a")
            (root / "img_1.jpg").write_bytes(b"b")
            result = fb.unique_destination(root, "img.jpg")
            self.assertEqual(result.name, "img_2.jpg")


class TestEndToEnd(unittest.TestCase):
    """Full transfer with exiftool mocked out."""

    def _setup_dirs(self, td: Path):
        src = td / "source"
        dst = td / "dest"
        src.mkdir()
        dst.mkdir()
        return src, dst

    def _fake_dates(self, *date_pairs):
        """Build a fake read_dates_batch implementation.

        date_pairs: (filename_substring, datetime) tuples — files matching
        any substring get that date; everything else gets fs mtime.
        """
        def fn(paths):
            result = {}
            for p in paths:
                for sub, dt in date_pairs:
                    if sub in p.name:
                        result[p] = (dt, "EXIF:Test")
                        break
                else:
                    result[p] = (datetime.fromtimestamp(p.stat().st_mtime),
                                 "FS:mtime")
            return result
        return fn

    def test_basic_sort(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, dst = self._setup_dirs(td)

            make_photo(src / "a.jpg", b"photo A")
            make_photo(src / "b.jpg", b"photo B")
            make_photo(src / "sub" / "c.mp4", b"video C")

            fake = self._fake_dates(
                ("a.jpg", datetime(2025, 3, 15, 10, 0, 0)),
                ("b.jpg", datetime(2024, 12, 31, 23, 59, 0)),
                ("c.mp4", datetime(2026, 1, 5, 8, 30, 0)),
            )

            args = fb.build_parser().parse_args([
                "--source", str(src), "--dest", str(dst),
                "--year-suffix", "Daniel", "--quiet",
            ])
            with patch.object(fb, "read_dates_batch", fake):
                rc = fb.transfer(args)

            self.assertEqual(rc, 0)
            self.assertTrue((dst / "2025_Daniel" / "March" / "a.jpg").exists())
            self.assertTrue((dst / "2024_Daniel" / "December" / "b.jpg").exists())
            self.assertTrue((dst / "2026_Daniel" / "January" / "c.mp4").exists())
            # Source emptied (move, not copy)
            self.assertFalse((src / "a.jpg").exists())

    def test_duplicate_detection(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, dst = self._setup_dirs(td)

            # Pre-existing file in dest
            existing = dst / "2025_Daniel" / "March" / "old.jpg"
            make_photo(existing, b"identical content")

            # Two source files: one identical to existing, one unique
            make_photo(src / "dupe.jpg", b"identical content")
            make_photo(src / "new.jpg", b"different content")

            fake = self._fake_dates(
                ("dupe.jpg", datetime(2025, 3, 15)),
                ("new.jpg", datetime(2025, 3, 20)),
            )

            args = fb.build_parser().parse_args([
                "--source", str(src), "--dest", str(dst),
                "--year-suffix", "Daniel", "--quiet",
            ])
            with patch.object(fb, "read_dates_batch", fake):
                fb.transfer(args)

            # Existing untouched
            self.assertTrue(existing.exists())
            # Duplicate skipped — still in source
            self.assertTrue((src / "dupe.jpg").exists())
            # New file moved
            self.assertTrue((dst / "2025_Daniel" / "March" / "new.jpg").exists())
            self.assertFalse((src / "new.jpg").exists())

    def test_dry_run_does_not_modify(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, dst = self._setup_dirs(td)
            make_photo(src / "a.jpg", b"photo")
            fake = self._fake_dates(("a.jpg", datetime(2025, 5, 1)))

            args = fb.build_parser().parse_args([
                "--source", str(src), "--dest", str(dst),
                "--dry-run", "--quiet",
            ])
            with patch.object(fb, "read_dates_batch", fake):
                fb.transfer(args)

            self.assertTrue((src / "a.jpg").exists())
            # No year folder created
            self.assertFalse((dst / "2025_Daniel").exists())
            # No cache file written
            self.assertFalse((dst / fb.DEFAULT_CACHE_NAME).exists())

    def test_csv_log(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, dst = self._setup_dirs(td)
            log_path = td / "log.csv"

            make_photo(src / "a.jpg", b"unique")
            existing = dst / "2025_Daniel" / "March" / "old.jpg"
            make_photo(existing, b"shared")
            make_photo(src / "dupe.jpg", b"shared")

            fake = self._fake_dates(
                ("a.jpg", datetime(2025, 5, 10)),
                ("dupe.jpg", datetime(2025, 3, 1)),
            )

            args = fb.build_parser().parse_args([
                "--source", str(src), "--dest", str(dst),
                "--log-file", str(log_path), "--quiet",
            ])
            with patch.object(fb, "read_dates_batch", fake):
                fb.transfer(args)

            with log_path.open() as f:
                rows = list(csv.DictReader(f))

            actions = {r["action"]: r for r in rows}
            self.assertIn("moved", actions)
            self.assertIn("skipped-duplicate", actions)
            self.assertEqual(actions["moved"]["src"].endswith("a.jpg"), True)

    def test_cache_persists_across_runs(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, dst = self._setup_dirs(td)

            make_photo(src / "a.jpg", b"first run")
            fake = self._fake_dates(("a.jpg", datetime(2025, 7, 7)))
            args = fb.build_parser().parse_args([
                "--source", str(src), "--dest", str(dst), "--quiet",
            ])
            with patch.object(fb, "read_dates_batch", fake):
                fb.transfer(args)

            cache_path = dst / fb.DEFAULT_CACHE_NAME
            self.assertTrue(cache_path.exists())
            data = json.loads(cache_path.read_text())
            self.assertEqual(len(data), 1)

            # Second run: same file content shows up in source again — should be
            # detected as duplicate without rehashing the destination.
            make_photo(src / "a_again.jpg", b"first run")  # same content
            with patch.object(fb, "read_dates_batch",
                              self._fake_dates(("a_again.jpg",
                                                datetime(2025, 7, 8)))):
                with patch.object(fb, "compute_hash",
                                  wraps=fb.compute_hash) as spy:
                    fb.transfer(args)

            # The cached destination file should NOT have been rehashed.
            hashed_paths = [c.args[0] for c in spy.call_args_list]
            cached_dst = dst / "2025_Daniel" / "July" / "a.jpg"
            self.assertNotIn(cached_dst, hashed_paths)
            # The source duplicate should not have been moved.
            self.assertTrue((src / "a_again.jpg").exists())

    def test_size_prefilter_avoids_hashing(self):
        """A source file whose size doesn't appear in dest is never hashed."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, dst = self._setup_dirs(td)

            # Dest has one file of size 5
            make_photo(dst / "2024_Daniel" / "March" / "old.jpg", b"AAAAA")
            # Source has a totally different size
            make_photo(src / "huge.jpg", b"X" * 1000)
            # And one matching size, different content
            make_photo(src / "five.jpg", b"BBBBB")

            fake = self._fake_dates(
                ("huge", datetime(2025, 3, 1)),
                ("five", datetime(2025, 3, 2)),
            )
            args = fb.build_parser().parse_args([
                "--source", str(src), "--dest", str(dst), "--quiet",
            ])

            hash_calls = []
            real_hash = fb.compute_hash

            def spy(p):
                hash_calls.append(Path(p))
                return real_hash(p)

            with patch.object(fb, "read_dates_batch", fake), \
                 patch.object(fb, "compute_hash", side_effect=spy):
                fb.transfer(args)

            # huge.jpg has no size match, so should NOT be hashed for dedup
            # (it WILL be hashed during the actual copy via safe_transfer's
            # streaming hash, but safe_transfer uses hashlib directly, not
            # compute_hash, so we're good).
            self.assertNotIn(src / "huge.jpg", hash_calls)
            # five.jpg has a size match, so MUST be hashed for dedup
            self.assertIn(src / "five.jpg", hash_calls)

    def test_existing_year_suffix_inconsistency_handled(self):
        """User has both 2024_Daniel/ (new style) and 2010-Daniel/ (old style)
        in dest. The script should index BOTH for dedup but only WRITE to the
        new style."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, dst = self._setup_dirs(td)

            # Pre-existing photos in legacy folder
            old_photo = dst / "2010-Daniel" / "1005xx" / "Konfirmation" / "img.jpg"
            make_photo(old_photo, b"ancient memory")
            new_photo = dst / "2025_Daniel" / "March" / "newish.jpg"
            make_photo(new_photo, b"newish memory")

            # Source has a duplicate of the OLD photo plus a fresh one
            make_photo(src / "dupe_old.jpg", b"ancient memory")
            make_photo(src / "fresh.jpg", b"brand new content")

            fake = self._fake_dates(
                ("dupe_old", datetime(2010, 5, 9, 12, 0)),
                ("fresh", datetime(2026, 4, 12, 10, 30)),
            )
            args = fb.build_parser().parse_args([
                "--source", str(src), "--dest", str(dst), "--quiet",
            ])
            with patch.object(fb, "read_dates_batch", fake):
                fb.transfer(args)

            # Old-style folders preserved untouched, dupe detected
            self.assertTrue(old_photo.exists())
            self.assertTrue((src / "dupe_old.jpg").exists())
            # New file goes to NEW-style folder
            self.assertTrue((dst / "2026_Daniel" / "April" / "fresh.jpg").exists())

    def test_filename_collision_gets_suffixed(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, dst = self._setup_dirs(td)

            # Existing file with same name but different content
            existing = dst / "2025_Daniel" / "March" / "IMG_001.jpg"
            make_photo(existing, b"existing version")
            make_photo(src / "IMG_001.jpg", b"different version, same name")

            fake = self._fake_dates(("IMG_001", datetime(2025, 3, 15)))
            args = fb.build_parser().parse_args([
                "--source", str(src), "--dest", str(dst), "--quiet",
            ])
            with patch.object(fb, "read_dates_batch", fake):
                fb.transfer(args)

            # Original untouched, new one renamed
            self.assertEqual(existing.read_bytes(), b"existing version")
            renamed = dst / "2025_Daniel" / "March" / "IMG_001_1.jpg"
            self.assertTrue(renamed.exists())
            self.assertEqual(renamed.read_bytes(),
                             b"different version, same name")


if __name__ == "__main__":
    unittest.main(verbosity=2)
