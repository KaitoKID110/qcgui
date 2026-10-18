"""NMR 参考物屏蔽常数的磁盘缓存。

为什么要落盘
------------
参考物（TMS）必须用**当前的方法/基组**现算，否则 δ 会系统性偏移。但现算
要跑"优化 + NMR"两条任务 —— 对稍大的基组就是几分钟。同一个实验室通常反复
用同一套方法，所以按 ``(参考物, 方法, 基组, 溶剂)`` 缓存起来，算一次复用多次。

缓存键与失效
------------
* 键 = ``参考物方案 + 方法 + 基组 + 溶剂 + 参考物几何版本``；
* ``REFERENCE_VERSION`` 在参考物几何改动时必须加一，否则会拿旧几何的
  参考值去算新结果的 δ —— 这种错误很难被发现；
* 方法/基组做去空格处理（``"B3LYP "`` 与 ``"B3LYP"`` 视作同一个）；
* 溶剂用 ``None``/小写归一。

落盘格式
--------
``<jobs_root>/nmr-references/<key 的短哈希>.json``，一条一个文件：
 * 只写不读改，天然避免"边写边读"的半截文件；
 * 文件里带**来源任务 id** 与时间戳 —— 复用缓存时能追溯"这个参考值是谁算的"，
   而不是一个来历不明的数字。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

#: 参考物几何/算法版本。改了内置几何、或改变了缓存键的构成，就加一，
#: 让旧缓存自动失效（否则会拿旧条件下的参考值算新条件下的 δ）。
#: 2：键里加入 extra_keywords / engine。
REFERENCE_VERSION = 2


def _normalise_extra_keywords(extra_keywords) -> tuple[str, ...]:
    """把 extra_keywords 归一成可比较的元组。

    去空、去重、**统一大写**：ORCA 的关键字大小写不敏感，
    如果 ``("ri",)`` 和 ``("RI",)`` 当成两套，缓存会白白翻倍。
    """
    if not extra_keywords:
        return ()
    if isinstance(extra_keywords, str):
        extra_keywords = extra_keywords.split()
    out = {str(k).strip().upper() for k in extra_keywords if str(k).strip()}
    return tuple(sorted(out))


def _normalise(scheme: str, method: str, basis: str, solvent: str | None,
               extra_keywords=(), engine: str = "orca") -> tuple:
    """缓存键。

    必须包含所有会影响屏蔽常数的输入：参考物、方法、基组、溶剂、
    以及额外关键字（``! RI`` / ``! TightSCF`` / DFT 网格等都会改变 σ 的绝对值）。
    """
    return (
        (scheme or "").strip().lower(),
        (method or "").strip(),
        (basis or "").strip(),
        (solvent or "").strip().lower() or None,
        _normalise_extra_keywords(extra_keywords),
        (engine or "orca").strip().lower(),
        int(REFERENCE_VERSION),
    )


def _key_for_json(key: tuple) -> list:
    """把键转成 JSON 往返后仍相等的形式。

    坑：键里含 `extra_keywords` 元组，`json.dumps(list(key))` 会把它写成
    JSON 数组，读回来是 `list`。若直接拿 `data["key"] != list(key)` 比较，
    永远是不相等，导致**刚写进去的缓存立刻失效**（实测踩过）。
    所以两边都过一遍 JSON 归一。
    """
    return json.loads(json.dumps(list(key), ensure_ascii=False))


class NmrReferenceCache:
    """参考物 σ 的磁盘缓存（线程安全，落盘失败不影响计算）。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---------- 内部 ----------

    def _path_for(self, key: tuple) -> Path:
        raw = json.dumps(list(key), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return self.root / f"{digest}.json"

    # ---------- 对外 ----------

    def get(self, scheme: str, method: str, basis: str,
            solvent: str | None = None, extra_keywords=(),
            engine: str = "orca") -> dict | None:
        """取缓存。命中返回完整条目（含 ``references`` 与 ``source_jobs``）。"""
        key = _normalise(scheme, method, basis, solvent, extra_keywords, engine)
        path = self._path_for(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 坏文件当成没缓存：宁可重算，也不要拿半截数据算 δ
            return None
        # 双重校验：文件名是哈希，内容里也存了一份键，防止哈希碰撞/误改
        if data.get("key") != _key_for_json(key):
            return None
        refs = data.get("references")
        if not isinstance(refs, dict) or not refs:
            return None
        try:
            data["references"] = {str(k): float(v) for k, v in refs.items()}
        except (TypeError, ValueError):
            return None
        return data

    def put(self, scheme: str, method: str, basis: str,
            references: dict[str, float], source_jobs: list[str] | None = None,
            solvent: str | None = None, extra_keywords=(),
            engine: str = "orca") -> dict:
        """写入缓存。返回写入的条目（调用方可以直接用它的 provenance）。"""
        key = _normalise(scheme, method, basis, solvent, extra_keywords, engine)
        entry = {
            "key": _key_for_json(key),
            "scheme": key[0],
            "method": key[1],
            "basis": key[2],
            "solvent": key[3],
            "extra_keywords": list(key[4]),
            "engine": key[5],
            "reference_version": key[6],
            "references": {str(k): float(v) for k, v in (references or {}).items()},
            "source_jobs": [str(j) for j in (source_jobs or [])],
            "created_at": time.time(),
        }
        path = self._path_for(key)
        with self._lock:
            try:
                # 先写临时文件再替换：避免读到写了一半的 JSON
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                tmp.replace(path)
            except OSError:
                # 缓存写不进去不该让工作流失败；下次重算就是了
                pass
        return entry

    def list_entries(self) -> list[dict]:
        """列出所有缓存条目（新的在前）。给界面/排查用。"""
        out: list[dict] = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("references"):
                out.append(data)
        out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
        return out

    def clear(self) -> int:
        """清空缓存，返回删除的条数。"""
        removed = 0
        with self._lock:
            for path in list(self.root.glob("*.json")):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed
