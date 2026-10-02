#!/usr/bin/env python3
"""
FolderBuddy — simple window on top of main.py.

Pick a source (attached camera cards are offered automatically) and a
destination, press "Vorschau" to see what would happen, then "Übertragen".
Settings are remembered in folderbuddy.ini next to this script.

Start with a double-click on FolderBuddy.bat, or: pythonw gui.py
"""

from __future__ import annotations

import argparse
import logging
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import main as fb

LOG_FILE_NAME = "folderbuddy_protokoll.csv"


class QueueHandler(logging.Handler):
    """Forward log records from the worker thread to the window."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", self.format(record)))


class FolderBuddyApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.settings = fb.load_settings()

        root.title("FolderBuddy – Fotos & Videos sortieren")
        root.minsize(640, 520)

        self.source_var = tk.StringVar(value=self.settings["source"])
        self.dest_var = tk.StringVar(value=self.settings["dest"])
        self.suffix_var = tk.StringVar(value=self.settings["year_suffix"])
        # The window speaks in terms of "delete originals"; the core in
        # terms of "copy". Same setting, inverted.
        self.delete_var = tk.BooleanVar(value=not self.settings["copy"])
        self.numbered_var = tk.BooleanVar(value=self.settings["month_style"] == "number")
        self.log_var = tk.BooleanVar(value=bool(self.settings["log_file"]))
        self.status_var = tk.StringVar(value="Bereit.")
        self.example_var = tk.StringVar()

        self._build()
        for var in (self.suffix_var, self.numbered_var, self.dest_var):
            var.trace_add("write", lambda *_: self._update_example())
        self._update_example()
        self.refresh_cards(initial=True)

        handler = QueueHandler(self.q)
        handler.setFormatter(logging.Formatter("%(message)s"))
        fb.log.addHandler(handler)
        fb.log.setLevel(logging.INFO)

        self.root.after(100, self._poll_queue)

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Von:").grid(row=0, column=0, sticky="w", **pad)
        self.source_box = ttk.Combobox(frm, textvariable=self.source_var)
        self.source_box.grid(row=0, column=1, sticky="ew", **pad)
        src_btns = ttk.Frame(frm)
        src_btns.grid(row=0, column=2, sticky="e")
        ttk.Button(src_btns, text="Durchsuchen…",
                   command=self.pick_source).pack(side="left", padx=2)
        ttk.Button(src_btns, text="Karten suchen",
                   command=self.refresh_cards).pack(side="left", padx=2)
        self.cards_hint = ttk.Label(frm, foreground="#666")
        self.cards_hint.grid(row=1, column=1, columnspan=2, sticky="w", padx=8)

        ttk.Label(frm, text="Nach:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.dest_var).grid(
            row=2, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Durchsuchen…", command=self.pick_dest).grid(
            row=2, column=2, sticky="e", **pad)

        ttk.Label(frm, text="Name im Jahresordner:").grid(
            row=3, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.suffix_var, width=20).grid(
            row=3, column=1, sticky="w", **pad)
        ttk.Label(frm, textvariable=self.example_var, foreground="#666").grid(
            row=4, column=1, columnspan=2, sticky="w", padx=8)

        opts = ttk.Frame(frm)
        opts.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 4))
        ttk.Checkbutton(
            opts, variable=self.delete_var,
            text="Originale nach dem Übertragen löschen (verschieben statt kopieren)",
        ).pack(anchor="w")
        ttk.Checkbutton(
            opts, variable=self.numbered_var,
            text="Monatsordner nummerieren (03_March), damit sie richtig sortiert werden",
        ).pack(anchor="w")
        ttk.Checkbutton(
            opts, variable=self.log_var,
            text=f"Protokoll im Zielordner speichern ({LOG_FILE_NAME})",
        ).pack(anchor="w")

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="ew", pady=8)
        self.preview_btn = ttk.Button(btns, text="Vorschau",
                                      command=lambda: self.start(dry_run=True))
        self.preview_btn.pack(side="left")
        self.run_btn = ttk.Button(btns, text="Übertragen",
                                  command=lambda: self.start(dry_run=False))
        self.run_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(btns, text="Zielordner öffnen",
                                   command=self.open_dest)
        self.open_btn.pack(side="right")

        self.progress = ttk.Progressbar(frm, mode="determinate")
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew", padx=8)
        ttk.Label(frm, textvariable=self.status_var).grid(
            row=8, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 6))

        out = ttk.Frame(frm)
        out.grid(row=9, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(9, weight=1)
        self.output = tk.Text(out, height=14, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(out, command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        self.output.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _update_example(self) -> None:
        now = datetime.now()
        style = "number" if self.numbered_var.get() else "name"
        suffix = self.suffix_var.get().strip() or "…"
        self.example_var.set(
            f"Beispiel: ein Foto von heute landet in "
            f"{now.year}_{suffix}\\{fb.month_folder_name(now.month, style)}")

    def _write(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def _clear_output(self) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    # ------------------------------------------------------------- actions

    def refresh_cards(self, initial: bool = False) -> None:
        cards = [str(c) for c in fb.find_camera_sources()]
        values = list(cards)
        if self.settings["source"] and self.settings["source"] not in values:
            values.append(self.settings["source"])
        self.source_box["values"] = values
        if cards:
            # A freshly inserted card is almost always what you want to import.
            self.source_var.set(cards[0])
            if len(cards) == 1:
                self.cards_hint.configure(text=f"Speicherkarte/Kamera erkannt: {cards[0]}")
            else:
                self.cards_hint.configure(
                    text=f"{len(cards)} Karten erkannt – Auswahl über den Pfeil.")
        elif not initial:
            self.cards_hint.configure(
                text="Keine Karte mit DCIM-Ordner gefunden. Handys bitte erst "
                     "in einen Ordner kopieren und den hier auswählen.")
        else:
            self.cards_hint.configure(text="")

    def pick_source(self) -> None:
        path = filedialog.askdirectory(title="Quellordner wählen",
                                       initialdir=self.source_var.get() or None)
        if path:
            self.source_var.set(str(Path(path)))

    def pick_dest(self) -> None:
        path = filedialog.askdirectory(title="Zielordner wählen",
                                       initialdir=self.dest_var.get() or None)
        if path:
            self.dest_var.set(str(Path(path)))

    def open_dest(self) -> None:
        dest = self.dest_var.get().strip()
        if dest and Path(dest).is_dir():
            fb.open_folder(Path(dest))
        else:
            messagebox.showinfo("FolderBuddy", "Der Zielordner existiert noch nicht.")

    def _save_settings(self) -> None:
        self.settings.update({
            "source": self.source_var.get().strip(),
            "dest": self.dest_var.get().strip(),
            "year_suffix": self.suffix_var.get().strip(),
            "copy": not self.delete_var.get(),
            "month_style": "number" if self.numbered_var.get() else "name",
            "log_file": LOG_FILE_NAME if self.log_var.get() else "",
        })
        try:
            fb.save_settings(self.settings)
        except OSError as e:
            self._write(f"Einstellungen konnten nicht gespeichert werden: {e}")

    def start(self, dry_run: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        source = self.source_var.get().strip()
        dest = self.dest_var.get().strip()
        suffix = self.suffix_var.get().strip()
        if not source or not Path(source).is_dir():
            messagebox.showwarning("FolderBuddy", "Bitte einen gültigen Quellordner wählen.")
            return
        if not dest:
            messagebox.showwarning("FolderBuddy", "Bitte einen Zielordner wählen.")
            return
        if not suffix:
            messagebox.showwarning("FolderBuddy", "Bitte einen Namen für die Jahresordner eingeben.")
            return
        if not fb.find_exiftool():
            messagebox.showerror("FolderBuddy", str(fb.ExiftoolMissing()))
            return
        if not dry_run and self.delete_var.get():
            if not messagebox.askyesno(
                    "FolderBuddy",
                    "Die Originale werden nach erfolgreichem Übertragen aus\n"
                    f"{source}\ngelöscht. Dateien, die schon im Ziel liegen, bleiben "
                    "auf der Quelle.\n\nFortfahren?"):
                return

        self._save_settings()
        args = argparse.Namespace(
            source=source, dest=dest, year_suffix=suffix,
            copy=not self.delete_var.get(),
            month_style=self.settings["month_style"],
            dry_run=dry_run,
            log_file=str(Path(dest) / LOG_FILE_NAME) if self.log_var.get() else None,
            cache_file=None, no_cache=False, quiet=True,
        )

        self._clear_output()
        self._write("Vorschau läuft…" if dry_run else "Übertragung läuft…")
        self.progress.configure(value=0, maximum=1)
        self._set_busy(True)
        self.worker = threading.Thread(target=self._work, args=(args,), daemon=True)
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for b in (self.preview_btn, self.run_btn):
            b.configure(state=state)

    # -------------------------------------------------------------- worker

    def _work(self, args: argparse.Namespace) -> None:
        def progress(phase: str, done: int, total: int) -> None:
            self.q.put(("progress", phase, done, total))
        try:
            stats = fb.run_transfer(args, progress=progress)
            self.q.put(("done", stats))
        except (ValueError, fb.ExiftoolMissing) as e:
            self.q.put(("fail", str(e)))
        except Exception as e:  # keep the window alive whatever happens
            fb.log.exception("Unerwarteter Fehler")
            self.q.put(("fail", f"Unerwarteter Fehler: {e}"))

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._write(msg[1])
                elif kind == "progress":
                    _, phase, done, total = msg
                    if total:
                        self.progress.configure(mode="determinate",
                                                maximum=total, value=done)
                        self.status_var.set(f"{phase} {done}/{total}")
                    else:
                        self.status_var.set(phase)
                elif kind == "done":
                    stats = msg[1]
                    self._write("")
                    self._write(fb.format_summary(stats))
                    self.status_var.set("Vorschau fertig – nichts wurde verändert."
                                        if stats.dry_run else "Fertig.")
                    self._set_busy(False)
                elif kind == "fail":
                    self._write("")
                    self._write(msg[1])
                    self.status_var.set("Abgebrochen.")
                    self._set_busy(False)
                    messagebox.showerror("FolderBuddy", msg[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    FolderBuddyApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
