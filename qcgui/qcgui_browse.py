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
import subprocess
import sys
import time
from pathlib import Path


#: 按纯文本处理的扩展名 —— 记事本是系统程序，在本环境里能正常起。
_TEXT_EXTS = (".cif", ".txt", ".xyz", ".out", ".inp", ".json", ".csv", ".log",
              ".gjf", ".com", ".molden", ".molden.input")

#: 启动后观察多久，确认程序没有立刻崩掉。
_LAUNCH_GRACE = 1.2


def _notepad_path():
    root = os.environ.get("SystemRoot") or "C:\\Windows"
    p = os.path.join(root, "System32", "notepad.exe")
    return p if os.path.isfile(p) else None


def _launch_alive(argv):
    r"""启动并观察：活着 True，立刻退出 False。

    为什么要观察：本机由受限进程启动 VESTA.exe 会**立刻以 0xFFFFFFFF 退出**，
    Popen 本身不报错 —— 只看 Popen 会把"崩了"当成"已打开"。
    """
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        proc.wait(timeout=_LAUNCH_GRACE)
    except subprocess.TimeoutExpired:
        return True
    return proc.returncode == 0


def _registry_progid(ext):
    try:
        import winreg
    except ImportError:
        return None
    path = (r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
            + "\\" + ext + r"\UserChoice")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            progid, _ = winreg.QueryValueEx(k, "ProgId")
            if progid:
                return progid
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ext) as k:
            value, _ = winreg.QueryValueEx(k, None)
            return value or None
    except OSError:
        return None


def _assoc_exe(progid):
    if not progid:
        return None
    try:
        import winreg
    except ImportError:
        return None
    path = progid + r"\shell\open\command"
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as k:
            cmd, _ = winreg.QueryValueEx(k, None)
    except OSError:
        return None
    if not cmd:
        return None
    cmd = cmd.strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        exe = cmd[1:end] if end > 0 else None
    else:
        parts = cmd.split()
        exe = parts[0] if parts else None
    return exe if exe and os.path.isfile(exe) else None


def open_path(path):
    r"""打开一个文件，返回 (成功?, 说明)。"""
    path = os.path.abspath(str(path))
    if not os.path.exists(path):
        return False, "路径不存在：" + path
    ext = os.path.splitext(path)[1].lower()

    if ext in _TEXT_EXTS:
        nb = _notepad_path()
        if nb and _launch_alive([nb, path]):
            return True, "已用记事本打开（受环境限制，暂不能用关联程序）"

    exe = _assoc_exe(_registry_progid(ext)) if ext else None
    if exe and _launch_alive([exe, path]):
        return True, "已用关联程序打开"

    nb = _notepad_path()
    if nb and _launch_alive([nb, path]):
        return True, "已改用记事本打开"

    return False, "没有可用的程序能打开这种文件"


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
        ok, why = open_path(target)
        if ok:
            status.set(f"{why}: {target.name}")
        else:
            # 打不开时至少把路径给到用户（可复制）
            status.set(f"{why}: {target}")
            messagebox.showwarning(
                "打不开文件",
                f"{why}\n\n路径（可复制）：\n{target}")

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
