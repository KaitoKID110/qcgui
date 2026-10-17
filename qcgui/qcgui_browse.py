# -*- coding: utf-8 -*-
r"""独立的结果文件夹浏览器。

由 qcgui 用独立进程启动（pythonw qcgui_browse.py <目录>）。
为什么独立进程：tkinter 的事件循环必须在**主线程**运行；如果从 Flask 的
请求线程里调 tk.Tk() + mainloop()，窗口会静默不出现（接口还返回成功）。
放到独立进程后就正常了 —— 而且不依赖 explorer.exe（本机它会 0xc0000142）。

用法：
    pythonw qcgui_browse.py "D:\path\to\folder" [窗口标题]
"""
import os
import sys
import time
from pathlib import Path


def main(argv):
    if len(argv) < 2:
        sys.exit("用法: qcgui_browse.py <目录> [标题]")

    folder = Path(argv[1])
    title = argv[2] if len(argv) > 2 else "结果文件"

    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title(f"{title} — {folder}")
    root.geometry("880x540")

    top = ttk.Frame(root, padding=8)
    top.pack(fill=tk.X)
    path_var = tk.StringVar(value=str(folder))
    ttk.Label(top, textvariable=path_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
    ttk.Button(top, text="上一级", command=lambda: go_up()).pack(side=tk.RIGHT, padx=(4, 0))
    ttk.Button(top, text="刷新", command=lambda: reload()).pack(side=tk.RIGHT)

    frame = ttk.Frame(root)
    frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
    tree = ttk.Treeview(frame, columns=("name", "size", "mtime"), show="headings")
    tree.heading("name", text="名称")
    tree.heading("size", text="大小")
    tree.heading("mtime", text="修改时间")
    tree.column("name", width=460, anchor="w")
    tree.column("size", width=110, anchor="e")
    tree.column("mtime", width=170, anchor="w")
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(0, weight=1)

    status = tk.StringVar(value="")
    ttk.Label(root, textvariable=status, padding=(8, 0, 8, 8),
              anchor="w").pack(fill=tk.X)

    def reload():
        tree.delete(*tree.get_children())
        try:
            entries = sorted(folder.iterdir(),
                             key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            status.set(f"读不了目录: {exc}")
            return
        for entry in entries:
            try:
                st = entry.stat()
                mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
                size = "文件夹" if entry.is_dir() else f"{st.st_size:,}"
            except OSError:
                size, mtime = "", ""
            tree.insert("", tk.END, iid=str(entry), values=(entry.name, size, mtime))
        status.set(f"{len(entries)} 项　双击文件夹进入；双击文件交给系统打开")

    def open_selected(_event=None):
        nonlocal folder
        sel = tree.selection()
        if not sel:
            return
        target = Path(sel[0])
        if target.is_dir():
            folder = target
            path_var.set(str(folder))
            root.title(f"{title} — {folder}")
            reload()
            return
        try:
            os.startfile(str(target))       # noqa: S606
            status.set(f"已交给系统打开: {target.name}")
        except OSError as exc:
            # 本机 explorer/关联程序可能起不来，如实说明而不是装成功
            messagebox.showwarning(
                "打不开文件",
                f"系统没法打开这个文件。\n\n{type(exc).__name__}: {exc}\n\n"
                f"路径（可复制）：\n{target}")

    def go_up():
        nonlocal folder
        parent = folder.parent
        if parent != folder and parent.is_dir():
            folder = parent
            path_var.set(str(folder))
            root.title(f"{title} — {folder}")
            reload()

    tree.bind("<Double-1>", open_selected)
    reload()
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv)
