import os
import shutil
from datetime import datetime
import json
import hashlib
from tqdm import tqdm
import subprocess

def get_file_date(file_path, debug=False):
    """
    Prefer true capture timestamps for all formats (HEIC/HEIF/AVIF/JPEG/RAW/MP4/MOV).
    Falls back to Modify/File times only if no capture time exists.
    """


    # Strict capture-first priority (no ModifyDate unless last resort)
    priority_keys = [
        # EXIF
        "DateTimeOriginal",
        "DateTimeDigitized",
        "CreateDate",
        # QuickTime / ISO-BMFF (used by HEIC/MOV)
        "QuickTime:CreateDate",
        "MediaCreateDate",
        "TrackCreateDate",
        # XMP / IPTC (exports/edits may carry original capture here)
        "XMP:DateCreated",
        "XMP:CreateDate",
        "IPTC:DateCreated",
        "IPTC:DigitalCreationDate",
        # only if nothing above exists:
        "ModifyDate",
        "FileModifyDate",
    ]

    cmd = [
        "exiftool",
        "-api", "QuickTimeUTC",
        "-api", "LargeFileSupport=1",
        "-s",
        "-json",
        "-d", "%Y-%m-%d %H:%M:%S%z",
        # EXIF
        "-EXIF:DateTimeOriginal",
        "-EXIF:DateTimeDigitized",
        "-EXIF:CreateDate",
        # QuickTime / HEIC
        "-QuickTime:CreateDate",
        "-QuickTime:MediaCreateDate",
        "-QuickTime:TrackCreateDate",
        # XMP / IPTC
        "-XMP:DateCreated",
        "-XMP:CreateDate",
        "-IPTC:DateCreated",
        "-IPTC:DigitalCreationDate",
        # last-resort metadata
        "-EXIF:ModifyDate",
        "-FileModifyDate",
        file_path,
    ]

    picked_key = None
    picked_val = None

    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)[0]

            # Normalize: if XMP-only present, mirror into generic keys so the priority list works
            if not data.get("DateTimeOriginal") and data.get("XMP:DateCreated"):
                data["DateTimeOriginal"] = data["XMP:DateCreated"]
            if not data.get("CreateDate"):
                if data.get("XMP:CreateDate"):
                    data["CreateDate"] = data["XMP:CreateDate"]
                elif data.get("QuickTime:CreateDate"):
                    data["CreateDate"] = data["QuickTime:CreateDate"]

            # Pick first available in priority order
            for key in priority_keys:
                val = data.get(key)
                if not val:
                    continue
                for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
                    try:
                        dt = datetime.strptime(val, fmt)
                        if dt.tzinfo is not None:
                            dt = dt.astimezone().replace(tzinfo=None)
                        picked_key, picked_val = key, val
                        if debug:
                            print(f"[{os.path.basename(file_path)}] used {picked_key} -> {picked_val}")
                        return dt
                    except ValueError:
                        continue
    except Exception:
        pass

    # Filesystem fallback (Windows mtime is safer than ctime)
    try:
        ts = os.path.getmtime(file_path)
        dt = datetime.fromtimestamp(ts)
        picked_key, picked_val = "FS:getmtime", dt.isoformat(sep=" ")
        if debug:
            print(f"[{os.path.basename(file_path)}] fallback {picked_key} -> {picked_val}")
        return dt
    except Exception:
        if debug:
            print(f"[{os.path.basename(file_path)}] fallback NOW")
        return datetime.now()

def compute_hash(file_path, block_size=65536):
    hasher = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(block_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

def build_existing_hashes(bilder_root, supported_ext):
    """
    Scan the destination drive and build a set of hashes for all media files.
    Shows a tqdm progress bar so you see that something is happening.
    """
    print(f"\n[1/3] Scanning destination for existing media: {bilder_root}")
    media_files = []

    # First collect all candidate files
    for root, _, files in os.walk(bilder_root):
        for file in files:
            if os.path.splitext(file)[1].lower() in supported_ext:
                media_files.append(os.path.join(root, file))

    hashes = set()

    # Now hash them with progress bar
    for file_path in tqdm(media_files, desc="Indexing existing media", unit="file"):
        try:
            file_hash = compute_hash(file_path)
            hashes.add(file_hash)
        except Exception:
            # You could also log this to a file if you want
            continue

    print(f"  → Indexed {len(hashes)} existing files by hash.\n")
    return hashes

def transfer_files(src_folder, dst_root):
    supported_ext = {
        '.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.gif',
        '.mp4', '.mov', '.avi', '.mkv', '.hevc', '.webm',
        '.3gp', '.wmv', '.m4v',
        '.cr2', '.nef', '.arw', '.dng', '.rw2', '.orf', '.raf'
    }

    # Step 1: build hash index of destination
    existing_hashes = build_existing_hashes(dst_root, supported_ext)

    # Step 2: collect all source files with progress info
    print(f"[2/3] Scanning source folder for media: {src_folder}")
    all_files = []
    for root, _, files in os.walk(src_folder):
        for file in files:
            if os.path.splitext(file)[1].lower() in supported_ext:
                all_files.append(os.path.join(root, file))
    print(f"  → Found {len(all_files)} files to process.\n")

    # Step 3: process/move files with tqdm progress bar
    print("[3/3] Sorting and moving files...")
    for src_path in tqdm(all_files, desc="Sorting media", unit="file"):
        try:
            file_hash = compute_hash(src_path)
        except Exception:
            print(f"Could not read file: {src_path}")
            continue

        # Skip duplicates
        if file_hash in existing_hashes:
            continue

        date = get_file_date(src_path)
        year = f'{date.year}_Daniel'  # Adjust as needed
        month = date.strftime('%B')
        dst_folder = os.path.join(dst_root, year, month)
        os.makedirs(dst_folder, exist_ok=True)

        filename = os.path.basename(src_path)
        dst_path = os.path.join(dst_folder, filename)

        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dst_path):
            dst_path = os.path.join(dst_folder, f"{base}_{counter}{ext}")
            counter += 1

        shutil.move(src_path, dst_path)
        existing_hashes.add(file_hash)

    print("\n✅ Done! All files processed.")


# === Usage ===
source_folder = r'C:\Users\danie\Desktop\Foto Transfer'
destination_root = r'D:\Bilder-Daniel'
transfer_files(source_folder, destination_root)



