#!/usr/bin/env python3
"""Tiny Tkinter dialogs for install-app.sh — used only when zenity
is not installed (Linux Mint has zenity by default, so this is the
fallback, not the main path).

Subcommands:
  confirm|question <title> <text>   -> exit 0 on yes, 1 on no
  info <title> <text>               -> message, exit 0
  error <title> <text>              -> message, exit 0
  progress <title> <text> <log> <done-file>
    -> pulsing progress window; the label follows the last line of
       <log>; the window closes when <done-file> appears. The
       window cannot be closed early (closing it mid-install
       would report a failure while the install still runs).
"""

import os
import sys
import tkinter as tk
from tkinter import messagebox, ttk


def _ask(title, text):
    root = tk.Tk()
    root.withdraw()
    try:
        return messagebox.askyesno(title, text, parent=root)
    finally:
        root.destroy()


def _show(kind, title, text):
    root = tk.Tk()
    root.withdraw()
    try:
        if kind == "info":
            messagebox.showinfo(title, text, parent=root)
        else:
            messagebox.showerror(title, text, parent=root)
    finally:
        root.destroy()


def _progress(title, text, log_path, done_path):
    root = tk.Tk()
    root.title(title)
    root.resizable(False, False)
    root.protocol("WM_DELETE_WINDOW", lambda: None)
    tk.Label(root, text=text, wraplength=420, justify="left").pack(
        padx=16, pady=(16, 8))
    status = tk.Label(root, text="Working...", wraplength=420,
                      justify="left")
    status.pack(padx=16, pady=(0, 8))
    bar = ttk.Progressbar(root, mode="indeterminate", length=380)
    bar.pack(padx=16, pady=(0, 16))
    bar.start(20)

    def tick():
        if os.path.exists(done_path):
            root.destroy()
            return
        try:
            with open(log_path, encoding="utf-8",
                      errors="replace") as handle:
                lines = handle.read().splitlines()
            if lines:
                status.config(text=lines[-1][:120])
        except OSError:
            pass
        root.after(1000, tick)

    root.after(500, tick)
    root.mainloop()


def main(argv):
    if len(argv) < 4:
        print("usage: install-gui.py <mode> <title> <text> ...",
              file=sys.stderr)
        return 2
    mode, title, text = argv[1], argv[2], argv[3]
    if mode in ("confirm", "question"):
        return 0 if _ask(title, text) else 1
    if mode == "info":
        _show("info", title, text)
        return 0
    if mode == "error":
        _show("error", title, text)
        return 0
    if mode == "progress":
        if len(argv) < 6:
            return 2
        _progress(title, text, argv[4], argv[5])
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
