/*!
 * viewer_harness.js —— 在 Node 里无头驱动 viewer.js，供 tests/test_viewer_js.py 断言。
 *
 * 通过一个最小的 DOM/Canvas 桩，把查看器的**计算逻辑**（成键推断、居中、
 * 轨迹插值、颜色表）跑起来并打印 JSON。渲染本身不在此验证。
 *
 * 用法：node tests/viewer_harness.js <case>
 */
'use strict';

const fs = require('fs');
const path = require('path');

const VIEWER = path.join(__dirname, '..', 'qcgui', 'web', 'static', 'viewer.js');

function makeCanvas() {
  const listeners = {};
  const calls = {};
  const gradient = { addColorStop() {} };
  const ctx = new Proxy({}, {
    get: (target, prop) => {
      if (prop === 'createRadialGradient' || prop === 'createLinearGradient') {
        return () => gradient;
      }
      if (prop === 'canvas') return undefined;
      return (...args) => { calls[prop] = (calls[prop] || 0) + 1; };
    },
    set: () => true,
  });
  return {
    clientWidth: 800, clientHeight: 600, width: 800, height: 600,
    getContext: () => ctx,
    addEventListener: (k, fn) => { listeners[k] = fn; },
    _listeners: listeners,
    _calls: calls,
  };
}

function loadViewer() {
  const src = fs.readFileSync(VIEWER, 'utf8');
  global.window = global;
  global.devicePixelRatio = 1;
  global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  eval(src);                       // eslint-disable-line no-eval
  return global.MoleculeViewer;
}

const WATER = {
  symbols: ['O', 'H', 'H'],
  coords: [[0, 0, 0.1173], [0, 0.7572, -0.4692], [0, -0.7572, -0.4692]],
};

const CASES = {
  /** 成键推断：水应当有 2 根 O–H，且没有 H–H */
  bonds() {
    const V = loadViewer();
    const v = new V(makeCanvas(), {});
    v.setMolecule(WATER);
    return {
      n_atoms: v.symbols.length,
      bonds: v.bonds.map((b) => [b[0], b[1]]),
      radius: v.radius,
    };
  },

  /** 距离很近的两个原子必须成键；人为拉远则不应成键 */
  bond_cutoff() {
    const V = loadViewer();
    const v = new V(makeCanvas(), {});
    v.setMolecule({ symbols: ['C', 'C'], coords: [[0, 0, 0], [0, 0, 1.54]] });
    const bonded = v.bonds.length;
    v.setMolecule({ symbols: ['C', 'C'], coords: [[0, 0, 0], [0, 0, 6.0]] });
    const far = v.bonds.length;
    return { bonded, far };
  },

  /** 轨迹插值：0.5 帧应是两帧的中点 */
  interpolate() {
    const V = loadViewer();
    const v = new V(makeCanvas(), {});
    v.setTrajectory([
      { symbols: ['O', 'H', 'H'], coords: [[0, 0, 0], [0, 1.0, 0], [0, -1.0, 0]], energy: -75.0 },
      { symbols: ['O', 'H', 'H'], coords: [[0, 0, 0], [0, 1.2, 0], [0, -1.2, 0]], energy: -75.1 },
    ]);
    const n = v.frameCount;
    v.setFrame(0.5);
    return { n_frames: n, y_half: v.coords[1][1], y_first: 1.0, y_last: 1.2 };
  },

  /** 元素表：查得到常见元素，查不到的有兜底 */
  elements() {
    const V = loadViewer();
    const E = V.ELEMENTS;
    return {
      has: ['H', 'C', 'N', 'O', 'S', 'Fe'].map((s) => [s, !!E.covalent[s] && !!E.colors[s]]),
      carbon_color: E.colors.C,
      unknown_covalent: E.covalent.Xx === undefined,
    };
  },

  /** 空数据不应抛异常 */
  empty() {
    const V = loadViewer();
    const v = new V(makeCanvas(), {});
    v.setMolecule(null);
    v.setMolecule({});
    v.setFrame(3);
    v.setStyle('spacefill');
    v.setColorMode('charge');
    v.resetView();
    return { ok: true, atoms: v.symbols.length };
  },

  /** 电荷着色：正电偏蓝、负电偏红 */
  charge_colors() {
    const V = loadViewer();
    const v = new V(makeCanvas(), {});
    v.setMolecule(WATER);
    v.setCharges([-0.5, 0.25, 0.25]);
    v.setColorMode('charge');
    const neg = v._colorFor(0);
    const pos = v._colorFor(1);
    return { neg, pos };
  },

  /** 旋转：改变欧拉角必须改变投影后的坐标 */
  rotation() {
    const V = loadViewer();
    const v = new V(makeCanvas(), {});
    v.setMolecule(WATER);
    v.rotX = 0; v.rotY = 0; v.zoom = 1; v.panX = 0; v.panY = 0;
    const before = v._rotate(v.coords[1], v._project());
    v.rotY = Math.PI / 2;
    const afterY = v._rotate(v.coords[1], v._project());
    v.rotY = 0; v.rotX = Math.PI / 3;
    const afterX = v._rotate(v.coords[1], v._project());
    return { before, afterY, afterX };
  },

  /** 缩放：zoom 加倍，投影比例尺随之加倍 */
  zoom() {
    const V = loadViewer();
    const v = new V(makeCanvas(), {});
    v.setMolecule(WATER);
    v.zoom = 1;
    const s1 = v._project().scale;
    v.zoom = 2;
    const s2 = v._project().scale;
    // 平移也要生效
    v.panX = 50; v.panY = -20;
    const pr = v._project();
    return { s1, s2, cx: pr.cx, cy: pr.cy };
  },

  /** 四种显示模式的原子半径应有明确大小关系 */
  styles() {
    const V = loadViewer();
    const v = new V(makeCanvas(), {});
    const out = {};
    for (const s of ['spacefill', 'ballstick', 'stick', 'wireframe']) {
      v.setStyle(s);
      out[s] = v._atomStyle('C', 0).radius;
    }
    out.vdw_carbon = V.ELEMENTS.vdw.C;
    return out;
  },

  /** 渲染管线：确实发出了绘制调用，且原子数与弧的条数对得上 */
  render_draw_calls() {
    const V = loadViewer();
    const canvas = makeCanvas();
    const v = new V(canvas, {});
    v.setMolecule(WATER);
    v.render();
    const calls = canvas._calls;
    return {
      arcs: calls.arc || 0,
      strokes: calls.stroke || 0,
      fills: calls.fill || 0,
      n_atoms: v.symbols.length,
      n_bonds: v.bonds.length,
    };
  },

  /** 空结构渲染时不应抛异常（只画一行提示文字） */
  render_empty() {
    const V = loadViewer();
    const canvas = makeCanvas();
    const v = new V(canvas, {});
    v.render();
    return { ok: true, fills: canvas._calls.fill || 0 };
  },

  /**
   * 原子编号标注：文字必须**带描边**。
   *
   * 实测踩过：原来用"黑字打底、白字错开半像素"的假阴影，压在跟文字同色系的
   * 球面上完全读不出来 —— 看着像"编号功能没做"。
   */
  labels_have_outline() {
    const V = loadViewer();
    const molecule = { symbols: ['C', 'O', 'H'], coords: [[0, 0, 0], [0, 0, 1.4], [0, 1, 1.4]] };

    const onCanvas = makeCanvas();
    const on = new V(onCanvas, {});
    on.setMolecule(molecule);
    on.setLabels(true);
    on.render();

    const offCanvas = makeCanvas();
    const off = new V(offCanvas, {});
    off.setMolecule(molecule);
    off.setLabels(false);
    off.render();

    return {
      on_stroke: onCanvas._calls.strokeText || 0,
      on_fill: onCanvas._calls.fillText || 0,
      off_stroke: offCanvas._calls.strokeText || 0,
      off_fill: offCanvas._calls.fillText || 0,
    };
  },

  /** 播放/暂停状态机 */
  playback_state() {
    const V = loadViewer();
    const v = new V(makeCanvas(), {});
    v.setTrajectory([
      { symbols: ['O', 'H', 'H'], coords: [[0, 0, 0], [0, 1, 0], [0, -1, 0]], energy: -75.0 },
      { symbols: ['O', 'H', 'H'], coords: [[0, 0, 0], [0, 1.2, 0], [0, -1.2, 0]], energy: -75.1 },
    ]);
    const idle = v.playing;
    v.play(1000);                 // 极慢，避免测试期间真的推进
    const playing = v.playing;
    const framesSeen = v.onFrame !== null;
    v.pause();
    return { idle, playing, paused: v.playing, has_callback: framesSeen };
  },

  /** 扫描块拼装：正常输入要拼对，坏输入要给出可读的中文报错 */
  scan_block() {
    const sb = require(path.join(__dirname, '..', 'qcgui', 'web', 'static', 'scanblock.js'));
    const buildScanBlock = sb.buildScanBlock;

    const ok = buildScanBlock('dihedral', '3 0 1 2', '0 120 13');
    const block = ok[0][1];
    const commas = buildScanBlock('bond', '0,1', '1.2 2.2 11');

    // 每一种坏输入都必须抛错，且错误信息要能直接给用户看
    const bad = {};
    const attempts = {
      '原子太少': () => buildScanBlock('dihedral', '3 0 1', '0 120 13'),
      '原子太多': () => buildScanBlock('bond', '0 1 2', '1 2 5'),
      '编号为负': () => buildScanBlock('bond', '0 -1', '1 2 5'),
      '编号非整数': () => buildScanBlock('bond', '0 1.5', '1 2 5'),
      '范围只给两个数': () => buildScanBlock('bond', '0 1', '1 2'),
      '点数不是整数': () => buildScanBlock('bond', '0 1', '1 2 5.5'),
      '点数小于 2': () => buildScanBlock('bond', '0 1', '1 2 1'),
      '起点等于终点': () => buildScanBlock('bond', '0 1', '1.5 1.5 5'),
      '未知类型': () => buildScanBlock('nope', '0 1', '1 2 5'),
    };
    Object.keys(attempts).forEach((key) => {
      try {
        attempts[key]();
        bad[key] = '没有报错';
      } catch (err) {
        bad[key] = String(err.message || err);
      }
    });

    return {
      block_name: ok[0][0],
      lines: block.split('\n'),
      comma_ok: commas[0][1].split('\n')[1].trim(),
      bad,
    };
  },
  /**
   * 高亮一组原子（几何参数表点一行时用）。
   *
   * 要验三件事：
   *  * 设/清高亮后 ``highlightedAtoms`` 与 ``highlightDrawCount`` 一致
   *    —— **数据到位 ≠ 真的画出来**（本项目栽过三次）
   *  * 越界/非法下标被丢掉（宁可少画，不要画到别的原子上）
   *  * 换了分子之后旧下标作废（否则会高亮不存在的原子）
   */
  highlight() {
    const V = loadViewer();
    const canvas = makeCanvas();
    const v = new V(canvas, {});
    v.setMolecule(WATER);

    const on = v.highlightAtoms([0, 2]);
    const setAfter = v.highlightedAtoms;
    const strokesBefore = canvas._calls.stroke || 0;
    v.render();
    const drawn = v.highlightDrawCount;
    const strokesAfter = canvas._calls.stroke || 0;

    const cleared = v.highlightAtoms([]);
    v.render();
    const drawnAfterClear = v.highlightDrawCount;

    v.highlightAtoms([0, 1]);
    v.setMolecule({ symbols: ['C', 'C'], coords: [[0, 0, 0], [1.54, 0, 0]] });
    const afterSwap = v.highlightedAtoms.length;

    const filtered = v.highlightAtoms([0, 99, -3, 'x']);
    v.render();
    return {
      on, setAfter, drawn, drawnAfterClear, cleared, afterSwap, filtered,
      drawnFiltered: v.highlightDrawCount,
      strokes_added: strokesAfter - strokesBefore,
    };
  },

};

const name = process.argv[2];
if (!name || !CASES[name]) {
  console.error('用法: node tests/viewer_harness.js <' + Object.keys(CASES).join('|') + '>');
  process.exit(2);
}
try {
  console.log(JSON.stringify(CASES[name]()));
} catch (err) {
  console.log(JSON.stringify({ error: String(err && err.stack || err) }));
  process.exit(1);
}
