"""应用配置：外部工具路径等。

设计要点：
- **路径全部自动探测**，探测结果可被配置文件覆盖。旧版把某个用户桌面的绝对路径
  写死在 constants.py 里（形如 ``C:\\Users\\<用户名>\\Desktop\\Multiwfn_...``），
  换了使用者或换了机器就失效。
- 配置文件放在用户数据目录（``paths.data_root()/config.json``），不污染仓库。
"""

from __future__ import annotations

import glob
import json
import os
import shutil
from pathlib import Path

from .paths import data_root, ensure

DEFAULTS: dict = {
    #: ORCA 可执行文件（orca.exe）。**留空自动探测**；换一台机器时最常要填的就是这一项。
    #: 放在最前面是因为"能不能算"取决于它。
    "orca_path": "",
    "multiwfn_path": "",
    "avogadro_path": "",
    "obabel_path": "",
    #: 网页界面端口；被占用时服务会自动顺延
    "web_port": 8760,
    "auto_open_browser": True,
}

def _build_tool_patterns() -> dict[str, tuple[str, ...]]:
    """按"可能装着工具的根目录"生成 glob 模式。

    以前这里是一串写死的 ``D:\\multiwfn\\*\\Multiwfn.exe`` —— 那是**这台机器**
    的布局，换一台机器就全失效。改成按 :func:`qcgui.paths.search_roots`
    （盘符根 + 用户目录）拼出来，反而比原来覆盖得更广。
    """
    from .paths import search_roots

    roots = search_roots()
    patterns: dict[str, list[str]] = {"multiwfn": [], "avogadro": [], "obabel": []}
    for root in roots:
        # Multiwfn 常见形态：Multiwfn_2026.2\Multiwfn.exe（多一层版本目录）
        patterns["multiwfn"] += [
            os.path.join(root, "Multiwfn.exe"),
            os.path.join(root, "Multiwfn*", "Multiwfn.exe"),
            os.path.join(root, "Multiwfn*", "*", "Multiwfn.exe"),
            os.path.join(root, "multiwfn*", "*", "Multiwfn.exe"),
        ]
        patterns["avogadro"] += [
            os.path.join(root, "Avogadro*", "bin", "avogadro2.exe"),
            os.path.join(root, "Avogadro*", "avogadro2.exe"),
            os.path.join(root, "Programs", "Avogadro*", "bin", "avogadro2.exe"),
        ]
        patterns["obabel"] += [
            os.path.join(root, "Avogadro*", "bin", "obabel.exe"),
            os.path.join(root, "OpenBabel*", "obabel.exe"),
            os.path.join(root, "OpenBabel*", "bin", "obabel.exe"),
        ]
    return {name: tuple(items) for name, items in patterns.items()}


#: 各工具的候选位置（glob 模式，按优先级排序）
_TOOL_PATTERNS: dict[str, tuple[str, ...]] = _build_tool_patterns()

#: 环境变量覆盖
_ENV_OVERRIDES = {
    "multiwfn": "QCGUI_MULTIWFN",
    "avogadro": "QCGUI_AVOGADRO",
    "obabel": "QCGUI_OBABEL",
}

#: PATH 里的可执行文件名
_PATH_NAMES = {
    "multiwfn": ("Multiwfn", "multiwfn"),
    "avogadro": ("avogadro2", "avogadro"),
    "obabel": ("obabel",),
}

_CONFIG_KEY = {
    "multiwfn": "multiwfn_path",
    "avogadro": "avogadro_path",
    "obabel": "obabel_path",
}


def config_path() -> Path:
    return data_root() / "config.json"


def sanitize_config(config: dict) -> dict:
    """按 ``DEFAULTS`` 的类型把配置值收敛到合法形状。

    以前配置是**原样读写**的：``POST /api/config {"multiwfn_path": {"a": 1}}``
    会被持久化，之后 ``/api/status`` 每次都在 ``resolve_tool()`` 里抛
    ``AttributeError`` → 顶栏状态永久 500，而且**重启也不恢复**（坏值已经落盘）。
    ``web_port: null`` 更糟：下次启动 ``int(None)`` 抛 TypeError，服务直接起不来。

    所以两头都收敛：写的时候拒绝坏值，读的时候也清洗一遍（救已经落盘的坏配置）。
    """
    out: dict = {}
    for key, default in DEFAULTS.items():
        if key not in config:
            continue
        value = config[key]
        if isinstance(default, bool):
            if isinstance(value, bool):
                out[key] = value
            elif isinstance(value, str):
                out[key] = value.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(value, (int, float)):
                out[key] = bool(value)
            # 其它类型（dict/list/None）直接丢弃，保留默认值
        elif isinstance(default, int):
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if key == "web_port" and not (1 <= number <= 65535):
                continue
            out[key] = number
        elif isinstance(default, str):
            if isinstance(value, str):
                out[key] = value
            # 非字符串的属性值（例如 PowerShell 序列化出来的对象）直接丢弃
    return out


def load_config() -> dict:
    """读取配置；缺失或损坏时回退到默认值（不抛异常）。"""
    config = dict(DEFAULTS)
    path = config_path()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                # 清洗：磁盘上可能已经存着坏值（老版本写进去的）
                config.update(sanitize_config(loaded))
        except (OSError, ValueError):
            pass
    return config


def save_config(config: dict) -> Path:
    path = config_path()
    ensure(path.parent)
    merged = dict(DEFAULTS)
    merged.update(sanitize_config(config))
    path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def detect_tool(name: str) -> str | None:
    """自动探测工具路径：环境变量 → PATH → 候选安装位置。"""
    env_key = _ENV_OVERRIDES.get(name)
    if env_key:
        explicit = os.environ.get(env_key)
        if explicit and Path(explicit).is_file():
            return str(Path(explicit))

    for exe in _PATH_NAMES.get(name, ()):
        found = shutil.which(exe)
        if found:
            return found

    for pattern in _TOOL_PATTERNS.get(name, ()):
        matches = sorted(glob.glob(pattern))
        for match in matches:
            if Path(match).is_file():
                return match
    return None


def resolve_tool(name: str, config: dict | None = None) -> tuple[str | None, str]:
    """返回 ``(路径, 来源说明)``。配置里指定但不存在时会说明原因并回退探测。"""
    config = config if config is not None else load_config()
    key = _CONFIG_KEY.get(name)
    configured = (config.get(key) or "").strip() if key else ""

    if configured:
        if Path(configured).is_file():
            return configured, "配置文件"
        detected = detect_tool(name)
        if detected:
            return detected, f"配置里的路径不存在（{configured}），已回退到自动探测"
        return None, f"配置里的路径不存在且未探测到：{configured}"

    detected = detect_tool(name)
    if detected:
        return detected, "自动探测"
    return None, "未找到"


def tool_status(config: dict | None = None) -> dict[str, dict]:
    """所有外部工具的状态，供界面展示。"""
    config = config if config is not None else load_config()
    status: dict[str, dict] = {}
    for name in _TOOL_PATTERNS:
        path, source = resolve_tool(name, config)
        status[name] = {
            "name": name,
            "path": path or "",
            "available": bool(path),
            "source": source,
        }
    return status
