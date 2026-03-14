#!/usr/bin/env python3
"""PDF Compressor — macOS drag-and-drop GUI."""

import sys
import threading
import queue
import subprocess
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_TKDND_IMPORT = True
except ImportError:
    _HAS_TKDND_IMPORT = False
HAS_DND = False  # set to True only if TkinterDnD.Tk() succeeds

DEFAULT_DISTANCE = 7.0

W, H   = 380, 215
PAD    = 20
BG     = "systemWindowBackgroundColor"  # matches native button/window bg
GRAY   = "#8E8E93"
BORDER = "#D1D1D6"
ACCENT = "#007AFF"           # macOS blue


def _parse_drop(data: str) -> str | None:
    data = data.strip()
    if data.startswith("{"):
        end = data.find("}")
        path = data[1:end] if end > 0 else data[1:]
    else:
        path = data.split()[0]
    return path if path.lower().endswith(".pdf") else None


def _draw_dashed_rect(canvas, x1, y1, x2, y2, color, r=10, dash=(6, 5)):
    w = 1.5
    canvas.create_line(x1+r, y1, x2-r, y1, dash=dash, fill=color, width=w)
    canvas.create_line(x1+r, y2, x2-r, y2, dash=dash, fill=color, width=w)
    canvas.create_line(x1, y1+r, x1, y2-r, dash=dash, fill=color, width=w)
    canvas.create_line(x2, y1+r, x2, y2-r, dash=dash, fill=color, width=w)
    for ox, oy, s in ((x1, y1, 90), (x2-2*r, y1, 0),
                      (x1, y2-2*r, 180), (x2-2*r, y2-2*r, 270)):
        canvas.create_arc(ox, oy, ox+2*r, oy+2*r, start=s, extent=90,
                          style="arc", outline=color, width=w)


def _truncate(name: str, max_chars: int = 42) -> str:
    if len(name) <= max_chars:
        return name
    keep = (max_chars - 3) // 2
    return name[:keep] + "…" + name[-keep:]


def _lbl(parent, **kw):
    """tk.Label with the window background pre-set."""
    return tk.Label(parent, bg=BG, **kw)


def _frm(parent, **kw):
    """tk.Frame with the window background pre-set."""
    return tk.Frame(parent, bg=BG, **kw)


class App:
    def __init__(self):
        global HAS_DND
        if _HAS_TKDND_IMPORT:
            try:
                self.root = TkinterDnD.Tk()
                HAS_DND = True
            except Exception:
                self.root = tk._default_root or tk.Tk()
        else:
            self.root = tk.Tk()

        self.root.title("PDF Compressor")
        self.root.geometry(f"{W}x{H}")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        try:
            ttk.Style().theme_use("aqua")
        except Exception:
            pass

        self._output_path: Path | None = None
        self._total_images: int = 0
        self._q: queue.Queue = queue.Queue()

        self._build_ui()
        self._show("idle")

        if HAS_DND:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)

        self.root.after(50, self._poll)
        self.root.mainloop()

    # ── build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = _frm(self.root)
        outer.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        self._outer = outer

        # ── Idle ──────────────────────────────────────────────────────────────
        idle = _frm(outer)

        cw = W - PAD * 2
        ch = 100
        canvas = tk.Canvas(idle, width=cw, height=ch,
                           bg=BG, highlightthickness=0)
        canvas.pack()
        _draw_dashed_rect(canvas, 6, 6, cw - 6, ch - 6, color=ACCENT)
        canvas.create_text(cw // 2, ch // 2 - 18, text="📄",
                           font=("Helvetica Neue", 28))
        canvas.create_text(cw // 2, ch // 2 + 18, text="Drop a PDF here",
                           font=("Helvetica Neue", 13, "bold"), fill="#3A3A3C")

        # Native macOS button for "choose"
        ttk.Button(idle, text="or choose a file…",
                   command=self._choose_file).pack(pady=(10, 0))

        self._idle_frame = idle

        # ── Compressing ───────────────────────────────────────────────────────
        comp = _frm(outer)

        _frm(comp).pack(expand=True, fill="both")   # top spacer

        _lbl(comp, text="Compressing…",
             font=("Helvetica Neue", 22, "bold")).pack()

        self._file_lbl = _lbl(comp, text="",
                              font=("Helvetica Neue", 11), fg=GRAY)
        self._file_lbl.pack(pady=(2, 10))

        self._progress_bar = ttk.Progressbar(comp, length=W - PAD * 2,
                                             mode="indeterminate")
        self._progress_bar.pack()

        self._status_lbl = _lbl(comp, text="",
                                font=("Helvetica Neue", 11), fg=GRAY)
        self._status_lbl.pack(pady=(6, 0))

        _frm(comp).pack(expand=True, fill="both")   # bottom spacer

        self._comp_frame = comp

        # ── Done ──────────────────────────────────────────────────────────────
        done = _frm(outer)

        # inner block: vertically centred, full width
        inner = _frm(done)
        inner.place(relx=0.5, rely=0.5, anchor="center",
                    relwidth=1.0)

        # Size comparison is the hero line
        self._size_lbl = _lbl(inner, text="",
                              font=("Helvetica Neue", 22, "bold"))
        self._size_lbl.pack()

        # Reduction % is the supporting line
        self._done_lbl = _lbl(inner, text="",
                              font=("Helvetica Neue", 13), fg=GRAY)
        self._done_lbl.pack(pady=(4, 0))

        # Row 1: Show in Finder | Open PDF  (each 50%)
        row1 = _frm(inner)
        row1.pack(fill="x", pady=(14, 0))

        self._finder_btn = ttk.Button(row1, text="Show in Finder",
                                      command=self._show_in_finder)
        self._finder_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))

        self._open_btn = ttk.Button(row1, text="Open PDF",
                                    command=self._open_pdf)
        self._open_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        # Row 2: Compress Another (full width)
        row2 = _frm(inner)
        row2.pack(fill="x", pady=(6, 0))

        ttk.Button(row2, text="Compress Another",
                   command=lambda: self._show("idle")).pack(fill="x")

        self._done_frame = done

    # ── state ─────────────────────────────────────────────────────────────────

    def _show(self, state: str):
        for w in self._outer.winfo_children():
            w.pack_forget()
        if state == "idle":
            self._idle_frame.pack(fill="both", expand=True)
        elif state == "compressing":
            self._comp_frame.pack(fill="both", expand=True)
        elif state == "done":
            self._done_frame.pack(fill="both", expand=True)

    # ── events ────────────────────────────────────────────────────────────────

    def _on_drop(self, event):
        path = _parse_drop(event.data)
        if path:
            self._start(Path(path))

    def _choose_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("PDF files", "*.pdf")],
            title="Choose a PDF to compress",
        )
        if path:
            self._start(Path(path))

    def _start(self, input_path: Path):
        self._output_path = (
            input_path.parent / (input_path.stem + "_compressed.pdf")
        )
        self._file_lbl.config(text=_truncate(input_path.name))
        self._status_lbl.config(text="Scanning…")
        self._progress_bar.config(mode="indeterminate", value=0)
        self._progress_bar.start(10)
        self._show("compressing")

        def run():
            try:
                import compress_pdf as cli

                def on_progress(done, total):
                    self._q.put(("img", done, total))

                stats = cli.compress_pdf(
                    input_path,
                    self._output_path,
                    distance=DEFAULT_DISTANCE,
                    progress_callback=on_progress,
                )
                self._q.put(("done", {
                    "in_mb": stats["in_mb"],
                    "out_mb": stats["out_mb"],
                    "reduction_pct": stats["reduction_pct"],
                }))

            except Exception as e:
                self._q.put(("error", str(e)))

        threading.Thread(target=run, daemon=True).start()

    # ── queue polling ─────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                msg = self._q.get_nowait()
                kind = msg[0]
                if kind == "img":
                    done, total = msg[1], msg[2]
                    if total and not self._total_images:
                        self._total_images = total
                        self._progress_bar.stop()
                        self._progress_bar.config(
                            mode="determinate", maximum=total, value=0
                        )
                    self._progress_bar.config(value=done)
                    t = self._total_images
                    lbl = f"Image {done} of {t}" if t else f"Image {done}…"
                    self._status_lbl.config(text=lbl)
                elif kind == "done":
                    self._on_done(msg[1])
                elif kind == "error":
                    self._on_error(msg[1])
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _on_done(self, stats: dict):
        self._progress_bar.stop()
        self._size_lbl.config(
            fg="systemTextColor",
            text=f"{stats['in_mb']:.1f} MB  →  {stats['out_mb']:.1f} MB",
        )
        self._done_lbl.config(
            fg=GRAY,
            text=f"{stats['reduction_pct']:.0f}% smaller",
        )
        self._finder_btn.config(state="normal")
        self._open_btn.config(state="normal")
        self._show("done")

    def _on_error(self, msg: str):
        self._progress_bar.stop()
        self._size_lbl.config(fg="systemTextColor", text="Compression failed")
        self._done_lbl.config(fg=GRAY, text=msg[:80])
        self._finder_btn.config(state="disabled")
        self._open_btn.config(state="disabled")
        self._show("done")

    def _show_in_finder(self):
        if self._output_path:
            subprocess.run(["open", "-R", str(self._output_path)])

    def _open_pdf(self):
        if self._output_path:
            subprocess.run(["open", str(self._output_path)])


if __name__ == "__main__":
    App()
