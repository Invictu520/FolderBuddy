# 📁 FolderBuddy

**FolderBuddy** is a Python script that automatically sorts your photos and videos into folders based on their capture date — keeping your digital memories neat and organized.

No more chaos in your DCIM dump — FolderBuddy moves each file into a year/month structure like:

```
D:\Bilder-Name\
├── 2025_Name\
│   ├── January\
│   ├── February\
│   └── ...
├── 2026_Name\
│   ├── March\
│   └── ...
```

It **skips duplicates** anywhere in the destination tree (so manually curated folders like `2025_Name\Paris` work fine), keeps a persistent hash cache so re-runs are fast, and never leaves a half-written file behind if you Ctrl+C in the middle.

---

## 🚀 Features

- Sorts **photos and videos** by true capture date (EXIF / QuickTime / XMP / IPTC) with filesystem mtime as a last-resort fallback
- **Persistent hash cache** of the destination — only new or changed files get re-hashed on the next run
- **Size prefilter** — source files with no size match in the destination are never hashed at all
- **Batched exiftool** — one process for hundreds of files (huge speedup on Windows)
- **Atomic transfers** — copy to `<dst>.partial`, hash-verify, rename, then delete the source
- **Locale-stable** — month folders are always English (no `März` accidentally living next to `March`)
- **Dry-run mode** + **CSV log** of every action
- Detects duplicates anywhere in the destination tree, including manually curated subfolders

---

## 🖼️ Supported File Types

- **Images:** `.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`, `.tif`, `.gif`, `.heic`, `.heif`
- **Videos:** `.mp4`, `.mov`, `.avi`, `.mkv`, `.hevc`, `.webm`, `.3gp`, `.wmv`, `.m4v`
- **RAW:** `.cr2`, `.cr3`, `.nef`, `.arw`, `.dng`, `.rw2`, `.orf`, `.raf`

---

## ⚙️ Requirements

- Python 3.9+
- `tqdm` (progress bars):
  ```bash
  pip install tqdm
  ```
- **`exiftool`** binary on your `PATH` — download from <https://exiftool.org/>. On Windows, rename `exiftool(-k).exe` to `exiftool.exe` and drop it in a folder that's on `PATH` (e.g. `C:\Windows\` or anywhere in your user `PATH`).

---

## 📂 Usage

```bash
python main.py --source "C:\Users\daniel\Desktop\Bilder" --dest "D:\Bilder-Daniel"
```

All options:

```
--source, -s       Source folder (e.g., DCIM dump).            [required]
--dest, -d         Destination root folder.                    [required]
--year-suffix      Appended to year folder: <year>_<suffix>.   [default: Daniel]
--copy             Copy instead of moving the files.
--dry-run          Print what would happen without touching any files.
--log-file         CSV log of every action (created/appended).
--cache-file       Hash cache JSON path. [default: <dest>/.folderbuddy_cache.json]
--no-cache         Ignore the persistent cache and rehash from scratch.
--quiet, -q        Suppress progress bars.
--verbose, -v      Verbose logging.
```

**Recommended first run** (after upgrading from the old version):

```bash
python main.py -s "C:\Users\daniel\Desktop\Bilder" -d "D:\Bilder-Daniel" \
    --dry-run --log-file run.csv
```

This walks your destination, builds the hash cache (slow once, fast forever after), and writes a CSV showing exactly what *would* be moved. Inspect `run.csv`, then re-run without `--dry-run` to actually move files.

---

## 🧠 How It Works

1. **Index destination.** Walks `--dest`, validates the cache against on-disk size + mtime, and only hashes files that are new or changed since the last run. The cache lives at `<dest>\.folderbuddy_cache.json` by default.
2. **Read source dates.** All source files go through a single (or few) `exiftool` invocation in batched mode.
3. **Triage.** For each source file:
   - Look up its size in the destination size index. If no match → it's not a duplicate; ship it.
   - If a match → compute its SHA-1 and compare. If hash matches → skip (it's already in the destination, anywhere in the tree).
4. **Transfer.** Stream-copy to `<target>.partial`, computing SHA-1 during the copy. Verify the hash, atomically rename, then delete the source (unless `--copy` was passed).

---

## 🧱 Folder Naming

- Year folder: `<year>_<suffix>` (e.g. `2026_Daniel`). Change with `--year-suffix`.
- Month folder: full English name (`January`, `February`, …). This is hardcoded so the same Windows install will produce the same folder names regardless of system locale.

If you have legacy folders from previous tools (e.g. `2010-Daniel\1005xx\Konfirmation`), FolderBuddy will happily read them for duplicate detection but never write into them — new files always go into the modern `<year>_<suffix>\<Month>\` structure.

---

## 📓 Log Format

With `--log-file run.csv` you get one row per file processed:

| Column | Meaning |
|---|---|
| `timestamp` | When this row was written |
| `action` | `moved`, `copied`, `skipped-duplicate`, `would-move`, `would-copy`, `error` |
| `src` | Source path |
| `dst` | Destination path (empty for skipped/error rows) |
| `sha1` | SHA-1 of the file contents |
| `size_bytes` | File size |
| `date_used` | The capture date FolderBuddy chose |
| `date_source` | Which metadata field that came from (e.g. `DateTimeOriginal`, `FS:mtime`) |
| `note` | Reason / error message |

---

## 🛟 What If Something Goes Wrong?

- **Interrupted mid-run:** Any file being copied at the moment of interruption leaves only a `<name>.partial` in the destination, which is harmless and gets overwritten next run. The original on the source drive is untouched.
- **Bad cache:** Delete `<dest>\.folderbuddy_cache.json` (or run with `--no-cache`) and the next run will rebuild it.
- **Wrong sort:** Use `--dry-run --log-file plan.csv` first; if you don't like what it plans to do, nothing has been touched.

---

## 💡 Tips

- Run with `--copy` the first few times to build confidence before letting it move files off your camera card.
- Pair with Windows Task Scheduler to auto-import whenever a specific drive is connected.
- The cache is portable — moving the destination drive between machines just works as long as the path layout is preserved relative to `--dest`.

---

## 📸 Example

```
Before:                                    After:
📁 DCIM                                    📁 Bilder-Daniel
├── IMG_001.jpg  (taken 2025-03-15)        ├── 2025_Daniel
├── VID_002.mp4  (taken 2026-04-12)        │   └── March
└── IMG_003.jpg  (taken 2025-03-20)        │       ├── IMG_001.jpg
                                           │       └── IMG_003.jpg
                                           └── 2026_Daniel
                                               └── April
                                                   └── VID_002.mp4
```
