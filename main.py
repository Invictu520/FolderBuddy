import os
import shutil
from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS
import hashlib
from tqdm import tqdm
import subprocess

def get_file_date(file_path):
    """
    Robust capture-date detection:
    - Query multiple EXIF/QuickTime tags in one exiftool call
    - Accept optional timezone offsets
    - Fall back to filesystem times only if all EXIF reads fail
    """
    # Ask for many candidates; videos often need the QuickTime tags
    tags = [
        "-DateTimeOriginal",
        "-CreateDate",
        "-DateTimeDigitized",
        "-ModifyDate",
        "-MediaCreateDate",
        "-TrackCreateDate",
        "-FileModifyDate",  # last resort before FS times
    ]

    # Force a parseable format; %z adds +HHMM or +HH:MM depending on exiftool version
    # Both are handled below.
    cmd = ["exiftool", "-s3", "-d", "%Y-%m-%d %H:%M:%S%z", *tags, file_path]

    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode == 0:
            lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            for s in lines:
                # Try with timezone first
                for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
                    try:
                        dt = datetime.strptime(s, fmt)
                        # If tz-aware, convert to local *naive* for folder naming
                        if dt.tzinfo is not None:
                            dt = dt.astimezone().replace(tzinfo=None)
                        return dt
                    except ValueError:
                        continue
    except Exception:
        pass

    # Fallback: creation/modification date (copy time on Windows!)
    try:
        timestamp = (
            os.path.getctime(file_path) if os.name == "nt" else os.path.getmtime(file_path)
        )
        return datetime.fromtimestamp(timestamp)
    except Exception:
        return datetime.now()


def compute_hash(file_path, block_size=65536):
    hasher = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while chunk := f.read(block_size):
            hasher.update(chunk)
    return hasher.hexdigest()

def build_existing_hashes(bilder_root, supported_ext):
    hashes = set()
    for root, _, files in os.walk(bilder_root):
        for file in files:
            if os.path.splitext(file)[1].lower() in supported_ext:
                file_path = os.path.join(root, file)
                try:
                    file_hash = compute_hash(file_path)
                    hashes.add(file_hash)
                except Exception:
                    pass
    return hashes

def transfer_files(src_folder, dst_root):
    supported_ext = {
        '.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.gif',
        '.mp4', '.mov', '.avi', '.mkv', '.hevc', '.webm',
        '.3gp', '.wmv', '.m4v',
        '.cr2', '.nef', '.arw', '.dng', '.rw2', '.orf', '.raf'
    }

    existing_hashes = build_existing_hashes(dst_root, supported_ext)

    all_files = []
    for root, _, files in os.walk(src_folder):
        for file in files:
            if os.path.splitext(file)[1].lower() in supported_ext:
                all_files.append(os.path.join(root, file))

    for src_path in tqdm(all_files, desc="Sorting media", unit="file"):
        try:
            file_hash = compute_hash(src_path)
        except Exception:
            print(f"Could not read file: {src_path}")
            continue

        if file_hash in existing_hashes:
            continue  # skip duplicate

        date = get_file_date(src_path)
        year = f'{date.year}_Daniel'
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

# === Usage ===
source_folder = r'C:\Users\danie\Desktop\100MSDCF'  # Replace with your actual folder
destination_root = r'D:\Bilder-Daniel'  # Replace with your actual folder

transfer_files(source_folder, destination_root)


