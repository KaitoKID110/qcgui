/*!
 * viewer.js —— 零依赖 3D 分子查看器
 *
 * 只用 Canvas 2D，不引入任何第三方库（本机无外网，CDN 一律不可用）。
 *
 * 能力：
 *   - 球棍 / 空间填充 / 棍状 / 线框 四种显示模式
 *   - 按元素（CPK）或按原子电荷着色
 *   - 旋转（拖拽）、缩放（滚轮）、平移（右键或 Shift 拖拽）、复位（双击）
 *   - 成键自动推断（共价半径之和 × 容差）
 *   - 轨迹播放：多帧之间线性插值，可拖动进度
 *
 * 渲染采用画家算法：所有图元（原子、键的每一半）按深度排序后依次绘制。
 */
(function (global) {
  'use strict';

  // ---------------------------------------------------------------- 元素数据

  // 共价半径 (Å)，用于成键推断
  const COVALENT = {
    H: 0.31, He: 0.28, Li: 1.28, Be: 0.96, B: 0.84, C: 0.76, N: 0.71, O: 0.66,
    F: 0.57, Ne: 0.58, Na: 1.66, Mg: 1.41, Al: 1.21, Si: 1.11, P: 1.07, S: 1.05,
    Cl: 1.02, Ar: 1.06, K: 2.03, Ca: 1.76, Sc: 1.70, Ti: 1.60, V: 1.53, Cr: 1.39,
    Mn: 1.39, Fe: 1.32, Co: 1.26, Ni: 1.24, Cu: 1.32, Zn: 1.22, Ga: 1.22, Ge: 1.20,
    As: 1.19, Se: 1.20, Br: 1.20, Kr: 1.16, Rb: 2.20, Sr: 1.95, Y: 1.90, Zr: 1.75,
    Nb: 1.64, Mo: 1.54, Ru: 1.46, Rh: 1.42, Pd: 1.39, Ag: 1.45, Cd: 1.44, In: 1.42,
    Sn: 1.39, Sb: 1.39, Te: 1.38, I: 1.39, Xe: 1.40, Cs: 2.44, Ba: 2.15, La: 2.07,
    W: 1.62, Pt: 1.36, Au: 1.36, Hg: 1.32, Tl: 1.45, Pb: 1.46, Bi: 1.48,
    U: 1.96, // 常见锕系，给个近似值
  };

  // 范德华半径 (Å)，用于空间填充
  const VDW = {
    H: 1.20, He: 1.40, Li: 1.82, Be: 1.53, B: 1.92, C: 1.70, N: 1.55, O: 1.52,
    F: 1.47, Ne: 1.54, Na: 2.27, Mg: 1.73, Al: 1.84, Si: 2.10, P: 1.80, S: 1.80,
    Cl: 1.75, Ar: 1.88, K: 2.75, Ca: 2.31, Sc: 2.15, Ti: 2.11, V: 2.07, Cr: 2.06,
    Mn: 2.05, Fe: 2.04, Co: 2.00, Ni: 1.97, Cu: 1.96, Zn: 2.01, Ga: 1.87, Ge: 2.11,
    As: 1.85, Se: 1.90, Br: 1.85, Kr: 2.02, Rb: 3.03, Sr: 2.49, Y: 2.32, Zr: 2.23,
    Mo: 2.17, Ru: 2.07, Rh: 2.02, Pd: 2.05, Ag: 2.03, Cd: 2.18, In: 1.93, Sn: 2.17,
    Sb: 2.06, Te: 2.06, I: 1.98, Xe: 2.16, Cs: 3.43, Ba: 2.68, La: 2.43, W: 2.10,
    Pt: 2.13, Au: 2.14, Hg: 2.23, Tl: 1.96, Pb: 2.02, Bi: 2.07,
  };

  // CPK 配色；碳在深色背景上提亮，否则看不见
  const COLORS = {
    H: [255, 255, 255], He: [217, 255, 255], Li: [204, 128, 255], Be: [194, 255, 0],
    B: [255, 181, 181], C: [144, 144, 144], N: [48, 80, 248], O: [255, 13, 13],
    F: [144, 224, 80], Ne: [179, 227, 245], Na: [171, 92, 242], Mg: [138, 255, 0],
    Al: [191, 166, 166], Si: [240, 200, 160], P: [255, 128, 0], S: [255, 255, 48],
    Cl: [31, 240, 31], Ar: [128, 209, 227], K: [143, 64, 212], Ca: [61, 255, 0],
    Sc: [230, 230, 230], Ti: [191, 194, 199], V: [166, 166, 171], Cr: [138, 153, 199],
    Mn: [156, 122, 199], Fe: [224, 102, 51], Co: [240, 144, 160], Ni: [80, 208, 80],
    Cu: [200, 128, 51], Zn: [125, 128, 176], Ga: [194, 143, 143], Ge: [102, 143, 143],
    As: [189, 128, 227], Se: [255, 161, 0], Br: [166, 41, 41], Kr: [92, 184, 209],
    Rb: [112, 46, 176], Sr: [0, 255, 0], Y: [148, 255, 255], Zr: [148, 224, 224],
    Mo: [84, 181, 181], Ru: [36, 143, 143], Rh: [10, 125, 140], Pd: [0, 105, 133],
    Ag: [192, 192, 192], Cd: [255, 217, 143], In: [166, 117, 115], Sn: [102, 128, 128],
    Sb: [158, 99, 181], Te: [212, 122, 0], I: [148, 0, 148], Xe: [66, 158, 176],
    Cs: [87, 23, 143], Ba: [0, 201, 0], La: [112, 212, 255], W: [33, 148, 148],
    Pt: [208, 208, 224], Au: [255, 209, 35], Hg: [184, 184, 208], Tl: [166, 84, 77],
    Pb: [87, 89, 97], Bi: [158, 79, 181],
  };

  const DEFAULT_COLOR = [255, 105, 180];
  const DEFAULT_COVALENT = 1.4;
  const DEFAULT_VDW = 1.8;

  const covalentRadius = (s) => COVALENT[s] || DEFAULT_COVALENT;
  const vdwRadius = (s) => VDW[s] || DEFAULT_VDW;
  const elementColor = (s) => COLORS[s] || DEFAULT_COLOR;

  // ---------------------------------------------------------------- 小工具

  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);

  /** 电荷 → 颜色：负电偏红、正电偏蓝、近中性偏灰。 */
  function chargeColor(q, scale) {
    const t = clamp(q / (scale || 0.5), -1, 1);
    const neutral = 190;
    if (t >= 0) {
      // 正电 → 蓝
      return [
        Math.round(neutral * (1 - t) + 60 * t),
        Math.round(neutral * (1 - t) + 130 * t),
        Math.round(neutral * (1 - t) + 245 * t),
      ];
    }
    const a = -t;
    // 负电 → 红
    return [
      Math.round(neutral * (1 - a) + 245 * a),
      Math.round(neutral * (1 - a) + 70 * a),
      Math.round(neutral * (1 - a) + 70 * a),
    ];
  }

  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a === undefined ? 1 : a})`;

  // ---------------------------------------------------------------- 主类

  class MoleculeViewer {
    constructor(canvas, options) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.options = Object.assign(
        { background: 'transparent', autoRotate: false, bondTolerance: 1.3 },
        options || {}
      );

      this.symbols = [];
      this.coords = [];
      this.charges = null;
      this.bonds = [];
      this.frames = [];
      this.framePos = 0;          // 可为小数，表示两帧之间
      this.playing = false;

      this.style = 'ballstick';   // ballstick | spacefill | stick | wireframe
      this.colorMode = 'element'; // element | charge
      this.showLabels = false;
      this.showAxes = false;

      this.rotX = -0.35;
      this.rotY = 0.6;
      this.zoom = 1;
      this.panX = 0;
      this.panY = 0;
      this.radius = 1;            // 分子特征半径，用于自动缩放

      this._dragging = null;
      this._last = null;
      this._dpr = 1;
      this._raf = null;
      this._autoSpin = this.options.autoRotate;
      this.onFrame = null;

      this._bindEvents();
      this.resize();
    }

    // ------------------------------------------------------------ 数据接口

    setMolecule(data) {
      if (!data || !data.symbols || !data.coords) return;
      this.symbols = data.symbols.slice();
      this.coords = data.coords.map((c) => [c[0], c[1], c[2]]);
      this.charges = data.charges ? data.charges.slice() : null;
      this.frames = [];
      this.framePos = 0;
      this._prepare();
      this._schedule();
    }

    /** frames: [{symbols, coords, energy?}, ...] */
    setTrajectory(frames) {
      if (!frames || !frames.length) return;
      this.frames = frames.map((f) => ({
        symbols: f.symbols.slice(),
        coords: f.coords.map((c) => [c[0], c[1], c[2]]),
        energy: f.energy,
      }));
      this.symbols = this.frames[0].symbols.slice();
      this.coords = this.frames[0].coords.map((c) => c.slice());
      this.framePos = 0;
      this._prepare();
      if (this.onFrame) this.onFrame(0, this.frames[0]);
      this._schedule();
    }

    get frameCount() {
      return this.frames.length;
    }

    setFrame(pos) {
      if (!this.frames.length) return;
      this.framePos = clamp(pos, 0, this.frames.length - 1);
      const i = Math.floor(this.framePos);
      const j = Math.min(i + 1, this.frames.length - 1);
      const t = this.framePos - i;
      const a = this.frames[i].coords;
      const b = this.frames[j].coords;
      const n = Math.min(a.length, b.length, this.symbols.length);
      const out = new Array(n);
      for (let k = 0; k < n; k++) {
        out[k] = [
          a[k][0] + (b[k][0] - a[k][0]) * t,
          a[k][1] + (b[k][1] - a[k][1]) * t,
          a[k][2] + (b[k][2] - a[k][2]) * t,
        ];
      }
      this.coords = out;
      if (this.onFrame) this.onFrame(this.framePos, this.frames[i]);
      this._schedule();
    }

    setCharges(charges) {
      this.charges = charges ? charges.slice() : null;
      this._schedule();
    }

    setStyle(style) {
      this.style = style;
      this._schedule();
    }

    setColorMode(mode) {
      this.colorMode = mode;
      this._schedule();
    }

    setLabels(on) {
      this.showLabels = !!on;
      this._schedule();
    }

    setAutoRotate(on) {
      this._autoSpin = !!on;
      this._schedule();
    }

    resetView() {
      this.rotX = -0.35;
      this.rotY = 0.6;
      this.zoom = 1;
      this.panX = 0;
      this.panY = 0;
      this._schedule();
    }

    play(fps) {
      if (!this.frames.length) return;
      this.playing = true;
      const step = 1 / Math.max(1, fps || 12);
      const tick = () => {
        if (!this.playing) return;
        let next = this.framePos + step;
        if (next >= this.frames.length - 1) next = 0;
        this.setFrame(next);
        this._playTimer = setTimeout(tick, 1000 / Math.max(1, fps || 12));
      };
      tick();
    }

    pause() {
      this.playing = false;
      if (this._playTimer) clearTimeout(this._playTimer);
    }

    // ------------------------------------------------------------ 内部准备

    _prepare() {
      // 居中并计算特征半径
      let cx = 0, cy = 0, cz = 0;
      const n = this.coords.length;
      for (const c of this.coords) {
        cx += c[0]; cy += c[1]; cz += c[2];
      }
      cx /= n || 1; cy /= n || 1; cz /= n || 1;
      this.center = [cx, cy, cz];

      let maxR = 0.1;
      for (const c of this.coords) {
        const d = Math.hypot(c[0] - cx, c[1] - cy, c[2] - cz);
        if (d > maxR) maxR = d;
      }
      this.radius = maxR;
      this.bonds = this._inferBonds();
    }

    _inferBonds() {
      const bonds = [];
      const n = this.coords.length;
      const tol = this.options.bondTolerance;
      for (let i = 0; i < n; i++) {
        const ri = covalentRadius(this.symbols[i]);
        const ci = this.coords[i];
        for (let j = i + 1; j < n; j++) {
          const rj = covalentRadius(this.symbols[j]);
          const limit = (ri + rj) * tol;
          const cj = this.coords[j];
          const dx = ci[0] - cj[0];
          if (dx > limit || dx < -limit) continue;
          const dy = ci[1] - cj[1];
          if (dy > limit || dy < -limit) continue;
          const dz = ci[2] - cj[2];
          if (dz > limit || dz < -limit) continue;
          const d = Math.sqrt(dx * dx + dy * dy + dz * dz);
          if (d > 0.1 && d <= limit) bonds.push([i, j, d]);
        }
      }
      return bonds;
    }

    // ------------------------------------------------------------ 尺寸 / 事件

    resize() {
      const dpr = global.devicePixelRatio || 1;
      const w = this.canvas.clientWidth || 640;
      const h = this.canvas.clientHeight || 420;
      this._dpr = dpr;
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
      this._schedule();
    }

    _bindEvents() {
      const c = this.canvas;
      c.addEventListener('mousedown', (e) => {
        this._dragging = e.button === 2 || e.shiftKey ? 'pan' : 'rotate';
        this._last = [e.clientX, e.clientY];
        e.preventDefault();
      });
      c.addEventListener('mousemove', (e) => {
        if (!this._dragging || !this._last) return;
        const dx = e.clientX - this._last[0];
        const dy = e.clientY - this._last[1];
        this._last = [e.clientX, e.clientY];
        if (this._dragging === 'rotate') {
          this.rotY += dx * 0.01;
          this.rotX = clamp(this.rotX + dy * 0.01, -Math.PI / 2, Math.PI / 2);
        } else {
          this.panX += dx;
          this.panY += dy;
        }
        this._schedule();
      });
      const stop = () => { this._dragging = null; this._last = null; };
      c.addEventListener('mouseup', stop);
      c.addEventListener('mouseleave', stop);
      c.addEventListener('contextmenu', (e) => e.preventDefault());
      c.addEventListener('dblclick', () => this.resetView());
      c.addEventListener(
        'wheel',
        (e) => {
          e.preventDefault();
          const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
          this.zoom = clamp(this.zoom * factor, 0.15, 12);
          this._schedule();
        },
        { passive: false }
      );
    }

    _schedule() {
      if (this._raf) return;
      this._raf = global.requestAnimationFrame(() => {
        this._raf = null;
        this.render();
      });
    }

    // ------------------------------------------------------------ 渲染

    _project() {
      const w = this.canvas.width / this._dpr;
      const h = this.canvas.height / this._dpr;
      const cx = w / 2 + this.panX;
      const cy = h / 2 + this.panY;

      const fit = (Math.min(w, h) / 2) * 0.78 / (this.radius + 1.4);
      const scale = fit * this.zoom;
      const focal = this.radius * 6;

      const cosX = Math.cos(this.rotX), sinX = Math.sin(this.rotX);
      const cosY = Math.cos(this.rotY), sinY = Math.sin(this.rotY);
      const [ox, oy, oz] = this.center;

      return { cx, cy, scale, focal, cosX, sinX, cosY, sinY, ox, oy, oz };
    }

    _rotate(p, pr) {
      const x = p[0] - pr.ox, y = p[1] - pr.oy, z = p[2] - pr.oz;
      // 先绕 Y 再绕 X
      const x1 = x * pr.cosY + z * pr.sinY;
      const z1 = -x * pr.sinY + z * pr.cosY;
      const y2 = y * pr.cosX - z1 * pr.sinX;
      const z2 = y * pr.sinX + z1 * pr.cosX;
      return [x1, y2, z2];
    }

    _toScreen(r, pr) {
      const persp = pr.focal / Math.max(pr.focal - r[2], pr.focal * 0.25);
      return [pr.cx + r[0] * pr.scale * persp, pr.cy - r[1] * pr.scale * persp, r[2], persp];
    }

    _atomStyle(symbol, index) {
      const base = vdwRadius(symbol);
      if (this.style === 'spacefill') return { radius: base * 1.0, bondWidth: 0 };
      if (this.style === 'stick') return { radius: base * 0.22, bondWidth: base * 0.42 };
      if (this.style === 'wireframe') return { radius: base * 0.06, bondWidth: 0.045 };
      return { radius: base * 0.30, bondWidth: base * 0.30 }; // ballstick
    }

    _colorFor(index) {
      if (this.colorMode === 'charge' && this.charges && this.charges[index] !== undefined) {
        return chargeColor(this.charges[index], 0.5);
      }
      return elementColor(this.symbols[index]);
    }

    render() {
      const ctx = this.ctx;
      const dpr = this._dpr;
      const w = this.canvas.width, h = this.canvas.height;

      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (this.options.background && this.options.background !== 'transparent') {
        ctx.fillStyle = this.options.background;
        ctx.fillRect(0, 0, w, h);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';

      if (!this.coords.length) {
        ctx.fillStyle = 'rgba(200,210,225,0.35)';
        ctx.font = '13px system-ui, sans-serif';
        ctx.textAlign = 'center';
        const cw = w / dpr, ch = h / dpr;
        ctx.fillText('尚未载入结构', cw / 2, ch / 2);
        return;
      }

      const pr = this._project();
      const rotated = this.coords.map((p) => this._rotate(p, pr));
      const screen = rotated.map((r) => this._toScreen(r, pr));

      // 组装图元并按深度排序（z 小 = 远，先画）
      const prims = [];
      for (let i = 0; i < this.symbols.length; i++) {
        const st = this._atomStyle(this.symbols[i], i);
        prims.push({
          kind: 'atom',
          z: rotated[i][2],
          screen: screen[i],
          radius: st.radius * pr.scale * screen[i][3],
          color: this._colorFor(i),
          // 带上原子编号：填扫描坐标 / 看轨道成分都要用到它，
          // 光有元素符号根本分不清哪个碳是几号（编号从 0 起，与 ORCA 一致）
          label: this.symbols[i] + i,
        });
      }
      const bondW = this.style === 'wireframe' ? 0.045
        : this.style === 'spacefill' ? 0 : 0.30;
      for (const [i, j] of this.bonds) {
        const a = screen[i], b = screen[j];
        const w2 = bondW * pr.scale * ((a[3] + b[3]) / 2);
        if (w2 <= 0.15) continue;
        const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
        const zi = rotated[i][2], zj = rotated[j][2];
        // 每根键拆成两半，各取一端原子的颜色；深度用各自中点，
        // 这样原子与键的前后遮挡关系才正确
        prims.push({
          kind: 'bond', z: zi * 0.75 + zj * 0.25,
          x1: a[0], y1: a[1], x2: mx, y2: my,
          width: w2, color: this._colorFor(i),
        });
        prims.push({
          kind: 'bond', z: zj * 0.75 + zi * 0.25,
          x1: mx, y1: my, x2: b[0], y2: b[1],
          width: w2, color: this._colorFor(j),
        });
      }
      prims.sort((p, q) => p.z - q.z);

      for (const p of prims) {
        if (p.kind === 'bond') this._drawBond(ctx, p);
        else this._drawAtom(ctx, p);
      }

      if (this.showLabels) this._drawLabels(ctx, prims);
      if (this.showAxes) this._drawAxes(ctx, pr);
    }

    _drawBond(ctx, p) {
      ctx.strokeStyle = rgba(p.color, 0.88);
      ctx.lineWidth = Math.max(1, p.width);
      ctx.beginPath();
      ctx.moveTo(p.x1, p.y1);
      ctx.lineTo(p.x2, p.y2);
      ctx.stroke();
    }

    _drawAtom(ctx, p) {
      const [x, y] = p.screen;
      const r = Math.max(1.2, p.radius);
      const g = ctx.createRadialGradient(
        x - r * 0.35, y - r * 0.35, r * 0.1,
        x, y, r
      );
      g.addColorStop(0, rgba(p.color.map((c) => Math.min(255, c + 70))));
      g.addColorStop(0.55, rgba(p.color));
      g.addColorStop(1, rgba(p.color.map((c) => Math.round(c * 0.45))));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,0.35)';
      ctx.lineWidth = Math.min(1.5, r * 0.08);
      ctx.stroke();
    }

    _drawLabels(ctx, prims) {
      // 白字 + 深色描边。原来用"黑字打底、白字错开半像素"的假阴影，
      // 压在跟文字同色系的球面上根本读不出来（实测就是这么糊的）。
      const atoms = prims.filter((p) => p.kind === 'atom' && p.radius >= 5);
      if (!atoms.length) return;
      // 原子多的时候缩小字号，免得糊成一片
      const size = atoms.length > 60 ? 8 : atoms.length > 30 ? 9 : atoms.length > 12 ? 11 : 12;
      ctx.font = `600 ${size}px ui-monospace, Consolas, monospace`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.lineJoin = 'round';
      ctx.miterLimit = 2;
      const outline = Math.max(2.5, size * 0.36);
      for (const p of atoms) {
        const x = p.screen[0];
        const y = p.screen[1];
        ctx.lineWidth = outline;
        ctx.strokeStyle = 'rgba(0, 0, 0, 0.9)';
        ctx.strokeText(p.label, x, y);
        ctx.fillStyle = '#ffffff';
        ctx.fillText(p.label, x, y);
      }
    }

    _drawAxes(ctx, pr) {
      const origin = this._toScreen(this._rotate([pr.ox, pr.oy, pr.oz], pr), pr);
      const len = this.radius * 1.35;
      const axes = [
        [[len, 0, 0], '#ff6b6b', 'x'],
        [[0, len, 0], '#6bd66b', 'y'],
        [[0, 0, len], '#6b9bff', 'z'],
      ];
      ctx.font = '10px system-ui, sans-serif';
      ctx.textAlign = 'center';
      for (const [vec, color, label] of axes) {
        const tip = this._toScreen(
          this._rotate([pr.ox + vec[0], pr.oy + vec[1], pr.oz + vec[2]], pr), pr
        );
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(origin[0], origin[1]);
        ctx.lineTo(tip[0], tip[1]);
        ctx.stroke();
        ctx.fillStyle = color;
        ctx.fillText(label, tip[0], tip[1] - 6);
      }
    }
  }

  // 导出
  MoleculeViewer.ELEMENTS = { covalent: COVALENT, vdw: VDW, colors: COLORS };
  global.MoleculeViewer = MoleculeViewer;
})(typeof window !== 'undefined' ? window : globalThis);
