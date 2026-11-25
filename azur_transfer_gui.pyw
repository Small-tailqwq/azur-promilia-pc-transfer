import os
import tkinter as tk
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
from time import monotonic
from tkinter import filedialog, messagebox, ttk

from azur_transfer import RELEASE_VERSION, TARGETS, UserError, find_game, prepare


LABELS = {label: channel for channel, label in TARGETS.items()}
NAMES = {"official": "AzurPromilia-Official", "bilibili": "AzurPromilia-Bilibili"}


class Cancelled(Exception):
    pass


def size_label(size):
    if size >= 10**9:
        return f"{size / 10**9:.2f} GB"
    return f"{size / 10**6:.2f} MB"


class Window:
    def __init__(self, root):
        self.root = root
        self.events = SimpleQueue()
        self.cancelled = Event()
        self.running = False
        self.output = None
        self.inputs = []

        self.source = tk.StringVar()
        self.target = tk.StringVar(value=TARGETS["official"])
        self.destination = tk.StringVar()
        self.hardlink = tk.BooleanVar(value=True)
        self.verify_hashes = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="生成后，在目标启动器中定位新目录，再运行一键修复。")
        self.progress = tk.DoubleVar()

        root.title("蓝色星原 · 启动器导入")
        root.geometry("770x515")
        root.minsize(660, 495)
        root.configure(background="#edf3f7")
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), foreground="#152b3a", background="white")
        style.configure("Outer.TFrame", background="#edf3f7")
        style.configure("Heading.TLabel", background="#edf3f7", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("Status.TLabel", background="#edf3f7", foreground="#405967")
        style.configure("TEntry", padding=(8, 6), bordercolor="#bfd0db")
        style.configure("TCombobox", padding=(8, 6))
        style.configure("TButton", padding=(12, 7), background="#e7eff4")
        style.configure("Primary.TButton", background="#126783", foreground="white", padding=(17, 8))
        style.map("Primary.TButton", background=[("disabled", "#c9dbe3"), ("active", "#0c536d")],
                  foreground=[("disabled", "#496775")])
        style.configure("Horizontal.TProgressbar", background="#126783", troughcolor="#dce7ee",
                        bordercolor="#dce7ee", lightcolor="#126783", darkcolor="#126783", thickness=8)

        outer = ttk.Frame(root, padding=(26, 20), style="Outer.TFrame")
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="蓝色星原客户端转换", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(outer, text=f"已验证客户端版本 {RELEASE_VERSION}", style="Status.TLabel").pack(
            anchor="w", pady=(4, 14)
        )
        form = ttk.Frame(outer, padding=15)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.path_row(form, 0, "已有客户端", self.source, self.pick_source)
        ttk.Label(form, text="目标启动器").grid(row=1, column=0, sticky="w", padx=(0, 12))
        target = ttk.Combobox(form, textvariable=self.target, state="readonly", values=tuple(LABELS))
        target.grid(row=1, column=1, sticky="ew", pady=4)
        target.bind("<<ComboboxSelected>>", self.refresh_destination)
        self.inputs.append(target)
        self.path_row(form, 2, "新目录", self.destination, self.pick_destination)

        toggle = ttk.Checkbutton(form, text="同卷硬链接复用主资源", variable=self.hardlink)
        toggle.grid(row=3, column=1, sticky="w", pady=(5, 2))
        self.inputs.append(toggle)
        ttk.Label(form, text="共享文件被原地改写时，两个目录都会看到变化。",
                  foreground="#526777").grid(row=4, column=1, sticky="w", pady=(0, 4))

        verify = ttk.Checkbutton(form, text="逐文件 SHA-256 校验（较慢）",
                                 variable=self.verify_hashes)
        verify.grid(row=5, column=1, sticky="w", pady=(4, 0))
        self.inputs.append(verify)

        ttk.Progressbar(outer, variable=self.progress, maximum=100).pack(fill="x", pady=(16, 3))
        ttk.Label(outer, textvariable=self.status, style="Status.TLabel", wraplength=600).pack(
            anchor="w", pady=(4, 10), fill="x"
        )
        actions = ttk.Frame(outer, style="Outer.TFrame")
        actions.pack(fill="x")
        self.start = ttk.Button(actions, text="生成目录", style="Primary.TButton", command=self.begin)
        self.start.pack(side="right")
        self.cancel = ttk.Button(actions, text="取消", state="disabled", command=self.request_cancel)
        self.cancel.pack(side="right", padx=(0, 8))
        self.open_output = ttk.Button(actions, text="打开新目录", state="disabled", command=self.open_directory)
        self.open_output.pack(side="left")
        root.after(100, self.poll)

    def path_row(self, frame, row, label, variable, command):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12))
        entry = ttk.Entry(frame, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        browse = ttk.Button(frame, text="选择…", command=command)
        browse.grid(row=row, column=2, padx=(8, 0), pady=4)
        self.inputs.extend((entry, browse))

    def pick_source(self):
        path = filedialog.askdirectory(title="选择包含 AzurPromilia.exe 的客户端目录", parent=self.root)
        if not path:
            return
        try:
            source = find_game(Path(path))
        except UserError as error:
            self.status.set(str(error))
            return
        self.source.set(str(source))
        self.refresh_destination()

    def pick_destination(self):
        parent = filedialog.askdirectory(title="选择存放新客户端的位置", parent=self.root)
        if parent:
            self.destination.set(str(Path(parent) / NAMES[LABELS[self.target.get()]]))

    def refresh_destination(self, _event=None):
        existing = self.destination.get().strip()
        if existing and Path(existing).name not in NAMES.values():
            return
        source = self.source.get().strip()
        if source:
            parent = Path(existing).parent if existing else Path(Path(source).anchor)
            self.destination.set(str(parent / NAMES[LABELS[self.target.get()]]))

    def begin(self):
        selected, destination = self.source.get().strip(), self.destination.get().strip()
        if not selected or not destination:
            self.status.set("请选择已有客户端和新目录。")
            return
        self.running = True
        self.cancelled.clear()
        self.output = None
        self.progress.set(0)
        self.status.set("正在核对客户端…")
        for widget in (*self.inputs, self.start, self.open_output):
            widget.state(["disabled"])
        self.cancel.state(["!disabled"])
        Thread(target=self.worker, args=(Path(selected), LABELS[self.target.get()],
                                         Path(destination), self.hardlink.get(), self.verify_hashes.get()),
               daemon=True).start()

    def worker(self, source, channel, destination, hardlink, verify_hashes):
        last = 0.0

        def progress(status, number, total):
            nonlocal last
            if self.cancelled.is_set():
                raise Cancelled
            now = monotonic()
            if not total or number >= total or now - last >= 0.15:
                last = now
                self.events.put(("progress", (status, number, total)))

        try:
            result = prepare(source, channel, destination, hardlink=hardlink,
                             verify_hashes=verify_hashes, progress=progress)
        except Cancelled:
            self.events.put(("cancelled", None))
        except (OSError, ValueError, KeyError) as error:
            self.events.put(("error", str(error)))
        else:
            self.events.put(("done", result))

    def poll(self):
        while True:
            try:
                event, data = self.events.get_nowait()
            except Empty:
                break
            if event == "progress":
                if not self.cancelled.is_set():
                    status, number, total = data
                    if total:
                        self.progress.set(number / total * 100)
                        status += f"  {number:,} / {total:,}"
                    self.status.set(status)
            else:
                self.finish(event, data)
        self.root.after(100, self.poll)

    def finish(self, event, data):
        self.running = False
        for widget in (*self.inputs, self.start):
            widget.state(["!disabled"])
        self.cancel.state(["disabled"])
        if event == "done":
            self.output = data.path
            self.open_output.state(["!disabled"])
            self.progress.set(100)
            reuse = f"已硬链接复用 {size_label(data.linked_bytes)}。" if data.linked_bytes else "已生成。"
            self.status.set(reuse + f"请定位新目录并一键修复，已知待补 {size_label(data.missing_bytes)}。")
        elif event == "cancelled":
            self.progress.set(0)
            self.status.set("已取消。")
        else:
            self.progress.set(0)
            self.status.set("目录生成失败。")
            messagebox.showerror("蓝色星原客户端转换", data, parent=self.root)

    def open_directory(self):
        try:
            os.startfile(self.output)
        except OSError as error:
            messagebox.showerror("无法打开新目录", str(error), parent=self.root)

    def request_cancel(self):
        self.cancelled.set()
        self.cancel.state(["disabled"])
        self.status.set("正在取消…")

    def close(self):
        if self.running:
            self.request_cancel()
        else:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    Window(root)
    root.mainloop()
