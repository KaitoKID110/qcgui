"""误报验证：新增的两个引擎检查在**真实历史任务**上会不会乱叫。

一个会对正常任务报警的检查比没有更糟 —— 用户会学会忽略警告。
所以这里拿真实任务库全量过一遍，把"报警"分成真问题与误报。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qcgui.engines.orca import _check_connectivity, _check_convergence  # noqa: E402
from qcgui.jobs import JobStore                                        # noqa: E402
from qcgui.paths import jobs_root                                      # noqa: E402

#: 任务库自动发现（尊重 QCGUI_HOME），不写死某一台机器的路径
REAL = Path(os.environ.get("QCGUI_JOBS_ROOT") or jobs_root())
OPT_TYPES = {"opt", "opt_freq", "ts", "ts_freq"}


def tail_text(job_dir: Path, limit: int = 200_000) -> str:
    """只读输出文件的尾部 —— 收敛公告在最后，没必要读整个 17 MB。"""
    outs = sorted(job_dir.glob("*.out"))
    if not outs:
        return ""
    out = outs[0]
    size = out.stat().st_size
    with open(out, "rb") as fh:
        if size > limit:
            fh.seek(size - limit)
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def main() -> int:
    store = JobStore(REAL)
    records = store.list(limit=100000)
    total = len(records)
    checked = 0
    conn_hits: list[tuple[str, str]] = []
    conv_hits: list[tuple[str, str]] = []
    conv_no_text = 0

    for record in records:
        spec = store.load_spec(record.job_id)
        if spec is None or spec.job_type.value not in OPT_TYPES:
            continue
        checked += 1
        result = store.load_result(record.job_id)
        text = tail_text(store.job_dir(record.job_id))
        if not text:
            conv_no_text += 1

        if result is not None:
            conn = _check_connectivity(spec, result)
            if conn:
                conn_hits.append((record.job_id, record.status, conn[0][:110]))
        if text:
            conv = _check_convergence(spec, text)
            if conv:
                conv_hits.append((record.job_id, record.status, record.molecule))

    print(f"  任务总数 {total}，其中几何优化类 {checked} 条")
    print(f"  没有 .out 的 {conv_no_text} 条（跳过收敛检查）")

    print(f"\n=== 成键变化检查：命中 {len(conn_hits)} 条 ===")
    for jid, status, msg in conn_hits:
        print(f"  [{status}] {jid}")
        print(f"        {msg}")

    print(f"\n=== 收敛检查：命中 {len(conv_hits)} 条 ===")
    for jid, status, mol in conv_hits:
        print(f"  [{status}] {jid}  {mol}")

    # 判读
    good_flagged = [h for h in conn_hits if h[1] == "done"]
    print("\n" + "=" * 70)
    print(f"  成键变化：{len(conn_hits)} 条命中，其中状态 done 的 {len(good_flagged)} 条")
    print("  （done 的命中必须逐条核对是不是真变化，不能默认是误报）")
    print(f"  收敛：{len(conv_hits)} 条命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
