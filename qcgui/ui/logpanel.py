"""日志面板。

线程安全的关键：引擎日志来自 worker 的读取线程，**绝不能直接写 Tk 控件**。
这里用队列缓冲，由主线程的定时器批量取出渲染。
"""

from __future__ import annotations

import queue
import tkinter as tk
from typing import Iterable

import customtkinter as ctk

_MAX_LINES = 5000


class LogPanel(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._queue: queue.Queue[str] = queue.Queue()
        self._lines = 0

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(header, text="运行日志", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="清空", width=56, height=22, command=self.clear).pack(side="right")
        self.autoscroll = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            header, text="自动滚动", variable=self.autoscroll, checkbox_width=16,
            checkbox_height=16, font=ctk.CTkFont(size=11),
        ).pack(side="right", padx=8)

        self.text = ctk.CTkTextbox(self, wrap="none", font=ctk.CTkFont(family="Consolas", size=11))
        self.text.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.text.configure(state="disabled")

    # ---------- 线程安全写入 ----------

    def put(self, message: str) -> None:
        """任意线程可调用。"""
        self._queue.put(message)

    def put_many(self, messages: Iterable[str]) -> None:
        for message in messages:
            self._queue.put(message)

    # ---------- 仅主线程调用 ----------

    def drain(self, max_batch: int = 400) -> int:
        """把队列里的日志渲染到控件。返回本次渲染的行数。"""
        batch: list[str] = []
        for _ in range(max_batch):
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0

        self.text.configure(state="normal")
        self.text.insert("end", "\n".join(batch) + "\n")
        self._lines += len(batch)
        if self._lines > _MAX_LINES:
            excess = self._lines - _MAX_LINES
            self.text.delete("1.0", f"{excess + 1}.0")
            self._lines = _MAX_LINES
        self.text.configure(state="disabled")
        if self.autoscroll.get():
            self.text.see("end")
        return len(batch)

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._lines = 0
