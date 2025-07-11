import os
import shutil
from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS
import hashlib
from tqdm import tqdm

def get_file_date(file_path):
    # Try to extract date from EXIF for images
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in {'.jpg', '.jpeg', '.png', '.tiff', '.bmp'}:
            image = Image.open(file_path)
            exif_data = image._getexif()
            if exif_data:
                for tag, value in exif_data.items():
                    tag_name = TAGS.get(tag)
                    if tag_name == 'DateTimeOriginal':
                        return datetime.strptime(value, '%Y:%m:%d %H:%M:%S')
    except Exception:
        pass

    # Fallback to file modification time
    try:
        timestamp = os.path.getmtime(file_path)
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
        '.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.gif',  # images
        '.mp4', '.mov', '.avi', '.mkv', '.hevc', '.webm',  # videos
        '.3gp', '.wmv', '.m4v'
    }

    existing_hashes = build_existing_hashes(dst_root, supported_ext)

    # Collect all files first
    all_files = []
    for root, _, files in os.walk(src_folder):
        for file in files:
            if os.path.splitext(file)[1].lower() in supported_ext:
                all_files.append(os.path.join(root, file))

    for src_path in tqdm(all_files, desc="Sorting media", unit="file"):
        ext = os.path.splitext(src_path)[1].lower()
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

        # Prevent overwriting in same folder
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dst_path):
            dst_path = os.path.join(dst_folder, f"{base}_{counter}{ext}")
            counter += 1

        shutil.move(src_path, dst_path)
        existing_hashes.add(file_hash)

# === Usage ===
source_folder = r'C:\Users\danie\Desktop'
destination_root = r'D:\Bilder-Daniel'

transfer_files(source_folder, destination_root)

