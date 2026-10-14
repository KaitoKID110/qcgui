/*!
 * scanblock.js —— 把界面上的扫描设置拼成 ORCA 的 %geom Scan 块。
 *
 * 单独一个文件、不碰 DOM，是为了能在 Node 里直接测（见 tests/viewer_harness.js）。
 * 拼错一个原子编号或坐标字母，ORCA 要么报错要么算出一个不是你要的东西，
 * 所以这段逻辑值得单独验证。
 */
'use strict';

/** ORCA 的扫描坐标写法：字母 + 原子编号（0 起）+ 起点/终点/点数 */
const SCAN_KINDS = {
  bond: { letter: 'B', atoms: 2, label: '键长', how: '两个原子：B i j' },
  angle: { letter: 'A', atoms: 3, label: '键角', how: '三个原子：A i j k' },
  dihedral: { letter: 'D', atoms: 4, label: '二面角', how: 'D i j k l' },
};

/** 把 "3 0 1 2" / "3,0,1,2" 这类输入切成数字数组。 */
function parseScanNumbers(text) {
  return String(text == null ? '' : text)
    .trim()
    .split(/[\s,]+/)
    .filter(Boolean)
    .map(Number);
}

/**
 * 拼出 %geom Scan 块（作为 extra_blocks 的一项）。
 *
 * @param {string} kind   'bond' | 'angle' | 'dihedral'
 * @param {string} atomText  原子编号，空白或逗号分隔，从 0 起
 * @param {string} rangeText 起点 终点 点数
 * @returns {Array<Array<string>>} 形如 [['geom', 'Scan\n...\nend']]
 * @throws {Error} 输入不合法时抛出可直接展示给用户的中文说明
 */
function buildScanBlock(kind, atomText, rangeText) {
  const spec = SCAN_KINDS[kind];
  if (!spec) throw new Error('未知的扫描类型: ' + kind);

  const atoms = parseScanNumbers(atomText);
  if (atoms.length !== spec.atoms) {
    throw new Error(`${spec.label}需要 ${spec.atoms} 个原子编号（从 0 起），`
      + `现在填了 ${atoms.length} 个`);
  }
  // 数量对了但值不对时要说清楚是哪个值不行 —— 否则会误导成"个数不对"
  const badAtom = atoms.find((n) => !Number.isInteger(n) || n < 0);
  if (badAtom !== undefined) {
    throw new Error(`原子编号要是从 0 起的非负整数，「${badAtom}」不行`);
  }

  const range = parseScanNumbers(rangeText);
  if (range.length !== 3 || range.some((n) => !Number.isFinite(n))) {
    throw new Error('扫描范围要填三个数：起点 终点 点数');
  }
  const [from, to, steps] = range;
  if (!Number.isInteger(steps) || steps < 2) {
    throw new Error(`点数要是 ≥ 2 的整数，现在是 ${steps}`);
  }
  if (from === to) {
    throw new Error('起点和终点不能相同，否则扫不出曲线');
  }

  // 步长判据放宽 + 提高迭代上限：扫描的每一步都是在约束下做几何优化，
  // 用默认的严格判据很容易卡在第 1 步不动（实测踩过）。
  const body = [
    'Scan',
    `  ${spec.letter} ${atoms.join(' ')} = ${from}, ${to}, ${steps}`,
    'end',
    'MaxIter 250',
    'TolRMSD 0.010',
    'TolMaxD 0.030',
  ].join('\n');

  return [['geom', body]];
}

// ---------------------------------------------------------------- 几何量测

function _sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
function _dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
function _cross(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
function _norm(a) { return Math.sqrt(_dot(a, a)); }

/** 两点距离（Å）。 */
function distance3(p, q) { return Math.sqrt(_dot(_sub(p, q), _sub(p, q))); }

/** 键角 p0-p1-p2，度。 */
function angle3(p0, p1, p2) {
  const v1 = _sub(p0, p1), v2 = _sub(p2, p1);
  const denom = _norm(v1) * _norm(v2);
  if (denom < 1e-12) return 0;
  return Math.acos(Math.max(-1, Math.min(1, _dot(v1, v2) / denom))) * 180 / Math.PI;
}

/**
 * 二面角 p0-p1-p2-p3，度，范围 (-180, 180]。
 *
 * 用标准（IUPAC / ORCA）约定：顺式 = 0°，反式 = ±180°。
 * `b0 = p0 - p1` 这个负号是必须的 —— 写成 `p1 - p0` 会让结果整体偏 180°，
 * 把 Z 构型判成 E（本项目真的写错过一次，而且错了很久才被发现）。
 */
function dihedral3(p0, p1, p2, p3) {
  const b0 = _sub(p0, p1), b1 = _sub(p2, p1), b2 = _sub(p3, p2);
  const n = _norm(b1);
  if (n < 1e-12) return 0;
  const b1u = [b1[0] / n, b1[1] / n, b1[2] / n];
  const d0 = _dot(b0, b1u), d2 = _dot(b2, b1u);
  const v = _sub(b0, [b1u[0] * d0, b1u[1] * d0, b1u[2] * d0]);
  const w = _sub(b2, [b1u[0] * d2, b1u[1] * d2, b1u[2] * d2]);
  return Math.atan2(_dot(_cross(b1u, v), w), _dot(v, w)) * 180 / Math.PI;
}

/** 按 kind（键长/键角/二面角）量一个坐标；编号越界返回 null。 */
function measure3(kind, atoms, coords) {
  if (!Array.isArray(coords) || !atoms.length) return null;
  if (atoms.some((i) => !Number.isInteger(i) || i < 0 || i >= coords.length)) return null;
  const p = atoms.map((i) => coords[i]);
  if (kind === 'bond' && p.length === 2) return distance3(p[0], p[1]);
  if (kind === 'angle' && p.length === 3) return angle3(p[0], p[1], p[2]);
  if (kind === 'dihedral' && p.length === 4) return dihedral3(p[0], p[1], p[2], p[3]);
  return null;
}

/** 扫描区间是否包含 180°（内坐标奇点）。 */
function crosses180(kind, from, to) {
  if (kind !== 'dihedral' && kind !== 'angle') return false;
  const lo = Math.min(from, to), hi = Math.max(from, to);
  if (hi - lo >= 360) return true;
  const base = Math.floor((lo - 180) / 360) * 360;
  return [base, base + 360, base + 720].some((s) => lo <= 180 + s && 180 + s <= hi);
}

/**
 * 检查界面上的扫描设置，返回要给用户看的问题清单。
 *
 * 这几条都是**实测踩出来的**，不是理论担忧：
 * - 起点离分子实际值太远 → 受约束优化的第一步要硬扳，实测见过醇被直接消成烯 + 水
 * - 区间跨过 180° → ORCA 内坐标奇点，实测 exit 2
 *
 * @param {string} kind 'bond' | 'angle' | 'dihedral'
 * @param {number[]} atoms 原子编号（0 起）
 * @param {number[]} range [起点, 终点, 点数]
 * @param {string[]} symbols 元素符号
 * @param {number[][]} coords 坐标（Å）
 * @returns {Array<{level: string, text: string}>}
 */
function scanIssues(kind, atoms, range, symbols, coords) {
  const issues = [];
  if (!coords || !coords.length || !Array.isArray(range) || range.length < 2) return issues;

  const actual = measure3(kind, atoms, coords);
  const unit = kind === 'bond' ? ' Å' : '°';
  const label = (SCAN_KINDS[kind] || {}).label || kind;

  if (actual !== null) {
    const from = Number(range[0]);
    let diff;
    if (kind === 'dihedral') {
      diff = Math.abs(((from - actual + 180) % 360 + 360) % 360 - 180);
    } else {
      diff = Math.abs(from - actual);
    }
    // 只**如实汇报**实际值，不拿它报警。
    // 直觉上"起点离实际值远"该报警，但实测不支持：实际值 -117.9° 时，
    // 起点 0°（差 118°）和 90°（差 152°）都跑歪了，而 110°（差 132°）
    // 那组 7 个点全部完好 —— 差距最大的不是失败的那次。三个数据点推不出因果，
    // 硬报只会制造误报、让用户学会忽略警告。
    issues.push({
      level: diff > 10 ? 'info' : 'ok',
      text: `${label}当前实际值 ${actual.toFixed(1)}${unit}（起点填的是 ${from}${unit}`
        + `，相差 ${diff.toFixed(0)}${unit}）。起点越贴近实际值，第一步的约束冲击越小。`,
    });
  }

  if (range.length >= 2 && crosses180(kind, Number(range[0]), Number(range[1]))) {
    issues.push({
      level: 'warn',
      text: `扫描区间 ${range[0]}→${range[1]} 包含 180°。二面角/键角在 180° 处是`
        + ` ORCA 内坐标的奇点，实测会直接崩（ORCA_GSTEP ... Aborting run，退出码 2）。`
        + `建议改成不跨 180° 的区间，或拆成两段。`,
    });
  }
  return issues;
}

// 浏览器里按 <script> 加载（此时没有 module）；Node 下供测试 require
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    SCAN_KINDS, buildScanBlock, parseScanNumbers,
    distance3, angle3, dihedral3, measure3, crosses180, scanIssues,
  };
}
