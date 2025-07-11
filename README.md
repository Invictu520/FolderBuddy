# 📁 FolderBuddy

**FolderBuddy** is a Python script that automatically sorts your photos and videos into folders based on their creation date — keeping your digital memories neat and organized.

No more chaos in your DCIM dump — FolderBuddy moves each file into a year/month structure like:

```
D:\Bilder-Name\
├── 2023_Name\
│   ├── January\
│   ├── February\
│   └── ...
├── 2024_Name\
│   ├── March\
│   └── ...
```

And yes, it **skips duplicates** and respects your manually curated folders like `2025_Name\Paris`!

---

## 🚀 Features

✅ Sorts **photos and videos** by creation date  
✅ Automatically creates folders named like `2024_Daniel/May`  
✅ Skips files that already exist anywhere in the destination (via file hash check)  
✅ Prevents overwriting by adding suffixes  
✅ Shows a **progress bar** while processing  

---

## 🖼️ Supported File Types

- **Images:** `.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`, `.gif`
- **Videos:** `.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`, `.3gp`, `.wmv`, `.m4v`, `.hevc`

---

## ⚙️ Requirements

- Python 3.7+
- Install dependencies:
  ```bash
  pip install pillow tqdm
  ```

---

## 📂 Usage

1. **Download** or clone this repo.
2. **Set your paths** at the bottom of `main.py`:

   ```python
   source_folder = r'C:\Users\yourname\DCIM_dump'
   destination_root = r'D:\Bilder-Name'
   ```

3. **Run the script**:

   ```bash
   python main.py
   ```

📦 All files from `source_folder` will be **moved** to their destination based on timestamp.

---

## 🧠 How It Works

- Uses **EXIF data** for image dates (if available).
- Falls back to **file modification time** for videos or missing EXIF.
- Computes **SHA-1 hashes** of files in `destination_root` to avoid duplicates — even if you manually placed them somewhere like `2024_Daniel\Roadtrip`.

---

## ✏️ Customize Folder Naming

To change how the **year folder** is named (e.g., `Name_2024` instead of `2024_Name`), go to this line in `main.py`:

```python
# === Specify naming convention of folders ===
year = f'{date.year}_Name'
```

Change it to any format you like, e.g.:

```python
year = f'Name_{date.year}'
```

---

## 📸 Example Folder Structure

```
Before:
📁 DCIM
├── IMG_001.jpg
├── VID_002.mp4
└── IMG_003.jpg

After:
📁 Bilder-Name
├── 2023_Name
│   └── March
│       ├── IMG_001.jpg
│       └── IMG_003.jpg
└── 2024_Name
    └── May
        └── VID_002.mp4
```

---

## 💡 Tips

- Want to **copy** instead of move? Replace:
  ```python
  shutil.move(src_path, dst_path)
  ```
  with:
  ```python
  shutil.copy2(src_path, dst_path)
  ```

- Want a **log file** or a **dry-run preview**? Ask the author 😄

---

## 🧼 Project Vibe

This is **FolderBuddy** – your chill digital assistant that doesn’t ask questions, just sorts your files with minimal fuss. 🤖📂✨
