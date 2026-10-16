/* ==========================================================================
   qcgui 网页界面 —— 前端逻辑
   零依赖（本机无外网）。所有数据来自 /api/*，日志走 SSE。
   ========================================================================== */
'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  molecule: null,        // 当前结构 {symbols, coords, name, charge, multiplicity, formula}
  jobs: [],
  selectedJob: null,
  result: null,
  viewer: null,
  currentJobId: null,    // 正在跑的任务
  batchId: null,
  jobTypes: [],
  recipes: [],
  recipe: null,          // 当前选中的配方
  moleculePath: null,    // 当前结构的来源文件（工具栏外部工具要用）
  busy: false,
  // —— 轨道成分（feat/orbcomp-ui）：最近一次分析结果，切任务时要清掉 ——
  orbComp: null,
  orbCompPartition: 'mulliken',
  orbCompOrbital: 'homo',
  // 结构叠加（feat/overlay-rmsd）：最近一次叠合结果与被叠加的任务 id
  overlay: null,
  overlayOther: null,
  // 红外谱图（feat/ir-spectrum）：线型与半高全宽是用户可调的，记住上次选择
  spectrumKind: 'gaussian',
  spectrumFwhm: 20,
  spectrumData: null,
  // —— 几何参数表（feat/geom-table）：数据来自 /api/geometry/parameters（纯几何，不碰引擎）。
  // geomSource 记住"这张表是哪套几何算出来的" —— 点行高亮前要拿它跟 3D 里
  // 显示的几何比对，否则原子编号会指到别的原子上（本项目踩过很多次）——
  geometry: null,
  geomSource: null,
  geomKind: 'bonds',
  geomSig: null,
  geomToken: 0,
};

// ------------------------------------------------------------------ 小工具

function esc(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function toast(message, kind) {
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = message;
  $('toast-wrap').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 320);
  }, kind === 'err' ? 6500 : 3800);
}

async function api(path, options) {
  const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options));
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const detail = (data && (data.error || data.message)) || res.statusText;
    throw new Error(detail);
  }
  return data;
}

function logLine(text, cls) {
  const pre = $('log');
  const span = document.createElement('span');
  if (cls) span.className = cls;
  span.textContent = text + '\n';
  pre.appendChild(span);
  while (pre.childNodes.length > 4000) pre.removeChild(pre.firstChild);
  if ($('input-autoscroll').checked) pre.scrollTop = pre.scrollHeight;
}

// ------------------------------------------------------------------ 初始化

async function init() {
  state.viewer = new MoleculeViewer($('viewer'), { autoRotate: false, bondTolerance: 1.3 });
  state.viewer.onFrame = (pos, frame) => {
    $('label-frame').textContent = (Math.floor(pos) + 1) + ' / ' + state.viewer.frameCount;
    $('label-frame-energy').textContent = frame && frame.energy !== null && frame.energy !== undefined
      ? 'E = ' + frame.energy.toFixed(6) + ' Ha' : '';
    profile.current = pos;
    drawProfile();
  };
  window.addEventListener('resize', () => { state.viewer.resize(); drawProfile(); });

  bindUi();

  // ?nosse=1 关闭 EventSource：自动化（无头浏览器）时长连接会让页面永远
  // 处于"加载中"，--virtual-time-budget 等不到，截图/dump 就出不来。
  const params0 = new URLSearchParams(location.search);
  if (params0.get('nosse') !== '1') {
    connectEvents();
  } else {
    $('conn-state').textContent = '已关闭（nosse）';
    $('conn-state').className = 'conn';
  }

  // 取服务状态。失败时**不能装作没事**：任务类型、溶剂等下拉都是从它填的，
  // 拿不到就全是空的，此时点「运行」必然失败、而且报的是
  // `'' is not a valid JobType` 这种看不懂的错（审计 F4）。
  // 所以先重试几次，仍失败就明确禁用提交并告诉用户怎么办 ——
  // "看起来能用但一定失败"比"明确说不能用"糟糕得多。
  try {
    applyStatus(await loadStatusWithRetry());
  } catch (err) {
    $('btn-run').disabled = true;
    $('btn-run').title = '服务状态获取失败，暂时无法提交';
    $('chip-engine').className = 'chip off';
    $('chip-engine').querySelector('b').textContent = '状态获取失败';
    logLine('✖ 无法获取服务状态：' + err.message
      + ' —— 已禁用「运行」（任务类型等选项来自服务状态，现在都是空的）。'
      + '请点右上角「刷新」重试。', 'l-err');
    toast('无法获取服务状态，已禁用「运行」—— 点「刷新」重试', 'err');
  }
  await loadExamples();
  await loadRecipes();
  await refreshJobs();
  // 页面刚打开时如果有任务在跑（例如刷新前就在跑），也要开始兜底轮询
  startJobWatch();
  logLine('界面已就绪。', 'l-dim');
  await applyUrlParams();
}

/**
 * 设置状态芯片的 on/off 类时**必须先把另一个清掉**。
 *
 * 以前是 `chip.classList.add('off')` 这种写法 —— 只加不减，两个类会同时存在。
 * 后果：引擎从"不可用"变成"可用"后，`off` 还挂在上面，**红点永远消不掉**，
 * 用户以为服务出问题了（审计）。所以统一走这个函数。
 */
function setChipState(chip, on) {
  chip.classList.remove('on', 'off');
  chip.classList.add(on ? 'on' : 'off');
}

/**
 * 取服务状态，失败时退避重试。
 *
 * 服务刚起来、或瞬时抖动时，一次失败不该把界面打成半初始化状态
 * （审计 F4：芯片永远显示"检测中…"、下拉全空、提交必然失败）。
 */
async function loadStatusWithRetry(attempts = 3) {
  let lastErr;
  for (let i = 1; i <= attempts; i++) {
    try {
      return await api('/api/status');
    } catch (err) {
      lastErr = err;
      if (i < attempts) await new Promise((r) => setTimeout(r, 400 * i));
    }
  }
  throw lastErr;
}

// ------------------------------------------------------------------ 配方

async function loadRecipes() {
  try {
    const data = await api('/api/recipes');
    state.recipes = data.recipes || [];
  } catch (err) {
    state.recipes = [];
    logLine('配方加载失败: ' + err.message, 'l-err');
  }
  const select = $('select-recipe');
  const keep = select.value;
  select.innerHTML = '<option value="">自定义（不用配方）</option>' +
    state.recipes.map((r) =>
      `<option value="${esc(r.id)}">${r.favorite ? '★ ' : ''}${esc(r.name)}</option>`
    ).join('');
  if (keep && state.recipes.some((r) => r.id === keep)) select.value = keep;
}

/** 选中配方后，把它带出的方法/基组/溶剂填进控件，并显示 ORCA 关键字。 */
function applyRecipe() {
  const id = $('select-recipe').value;
  const recipe = state.recipes.find((r) => r.id === id) || null;
  state.recipe = recipe;

  const favBtn = $('btn-fav');
  if (!recipe) {
    $('recipe-keywords').textContent = '';
    $('recipe-note').textContent = '';
    favBtn.disabled = true;
    favBtn.style.opacity = '0.4';
    return;
  }

  $('select-jobtype').value = recipe.job_type;
  $('input-method').value = recipe.method;
  $('input-basis').value = recipe.basis || '';
  $('select-solvent').value = recipe.solvent || '';
  $('recipe-keywords').textContent = recipe.orca_keyword_line + ' ' +
    (recipe.blocks || []).map((b) => '%' + b[0]).join(' ');

  const warn = recipe.parsing === 'partial' ? '　⚠ 能跑通，但结果解析尚未实现' : '';
  $('recipe-note').textContent = (recipe.note || '') + warn;

  favBtn.disabled = false;
  favBtn.style.opacity = recipe.favorite ? '1' : '0.4';
  favBtn.textContent = recipe.favorite ? '★' : '☆';
}

// ------------------------------------------------------------------ 势能面扫描

// 块的拼法在 scanblock.js 里（不碰 DOM，便于 Node 下测试）

function _scanNumbers(id) {
  return parseScanNumbers($(id).value);
}

/** 从界面读扫描设置并拼块；没启用扫描就返回 null。 */
function scanBlock() {
  const kind = $('select-scan').value;
  if (!kind) return null;
  return buildScanBlock(kind, $('input-scan-atoms').value, $('input-scan-range').value);
}

/** 扫描开关变化时更新提示，并处理"任务类型必须是几何优化"这条约束。 */
function updateScanUI() {
  const kind = $('select-scan').value;
  const jobSelect = $('select-jobtype');
  $('scan-options').hidden = !kind;

  if (!kind) {
    jobSelect.disabled = false;
    $('scan-hint').textContent = '';
    return;
  }

  const spec = SCAN_KINDS[kind];
  const notes = [`${spec.how}。编号从 0 起，顺序就是结构文件里的顺序。`];
  $('input-scan-atoms').placeholder = kind === 'bond' ? '0 1'
    : kind === 'angle' ? '0 1 2' : '3 0 1 2';
  $('input-scan-range').placeholder = kind === 'bond' ? '1.2 2.2 11' : '0 120 13';

  // 扫描靠"每一步做一次约束下的几何优化"实现，所以底子必须是几何优化。
  // 配方会决定 job_type，若它给的不是 opt，就退回自定义。
  if (state.recipe && state.recipe.job_type !== 'opt') {
    notes.push(`⚠ 配方「${state.recipe.name}」不是几何优化，已切成自定义 —— 扫描需要自己指定方法/基组。`);
    $('select-recipe').value = '';
    applyRecipe();
  }
  jobSelect.value = 'opt';
  jobSelect.disabled = true;
  notes.push('任务类型固定为几何优化。');

  const mol = state.molecule;
  if (mol && mol.n_atoms !== undefined) {
    for (const n of _scanNumbers('input-scan-atoms')) {
      if (n >= mol.n_atoms) {
        notes.push(`⚠ 编号 ${n} 超出范围（当前结构只有 ${mol.n_atoms} 个原子，编号 0–${mol.n_atoms - 1}）。`);
        break;
      }
    }
  }

  // 几何层面的检查：实际值对不对得上、区间有没有跨 180° 奇点。
  // 这些是打字时就能看出来的 —— 等 ORCA 跑完（或崩掉）再看就太晚了。
  let html = esc(notes.join(' '));
  const atoms = _scanNumbers('input-scan-atoms');
  const range = _scanNumbers('input-scan-range');
  if (mol && mol.coords && mol.coords.length && atoms.length && range.length >= 2) {
    const issues = scanIssues(kind, atoms, range, mol.symbols || [], mol.coords);
    if (issues.length) {
      const mark = { warn: '⚠ ', info: 'ℹ ', ok: '✓ ' };
      html += '<br>' + issues.map((it) => `<span class="${it.level}">`
        + `${mark[it.level] || ''}${esc(it.text)}</span>`).join('<br>');
    }
  }
  $('scan-hint').innerHTML = html;
}

async function toggleFavorite() {
  // 没选配方时以前是**静默 return** —— 按钮看着能点，点了什么也不发生（审计）。
  if (!state.recipe) {
    toast('请先在上方选一个配方，★ 用来收藏它', 'warn');
    return;
  }
  const id = state.recipe.id;
  try {
    const res = await api('/api/recipes/' + encodeURIComponent(id) + '/favorite', {
      method: 'POST',
      body: JSON.stringify({ favorite: !state.recipe.favorite }),
    });
    await loadRecipes();
    $('select-recipe').value = id;
    applyRecipe();
    // 按钮的视觉状态要跟着变，否则看不出到底收藏上没有
    const fav = !!res.favorite;
    const btn = $('btn-fav');
    btn.textContent = fav ? '★ 已收藏' : '☆ 收藏';
    btn.classList.toggle('active', fav);
    toast(fav ? '已收藏' : '已取消收藏', 'ok');
  } catch (err) {
    toast('收藏失败: ' + err.message, 'err');
  }
}

/** 几何来源下拉：列出所有已完成任务，供"用它的优化后几何"起算。 */
function renderGeometryOptions() {
  const select = $('select-geometry');
  const keep = select.value;
  const finished = state.jobs.filter((j) => j.status === 'done');
  select.innerHTML = '<option value="">当前载入的结构</option>' +
    finished.map((j) =>
      `<option value="${esc(j.job_id)}">${esc(j.molecule)} · ${esc(j.job_type)} · ${esc(j.method || '')}</option>`
    ).join('');
  if (keep && finished.some((j) => j.job_id === keep)) select.value = keep;
}

/**
 * 支持用 URL 参数直接进入某个状态，便于分享链接与自动化截图：
 *   ?load=<结构文件路径>   载入该结构
 *   ?job=<任务 id>         选中该任务
 *   ?demo=1                载入自带的示例分子
 */
async function applyUrlParams() {
  const params = new URLSearchParams(location.search);

  const load = params.get('load');
  if (load) await loadStructureFromPath(load);

  if (params.get('demo')) {
    const select = $('example-select');
    const option = Array.from(select.options).find((o) => /caffeine|benzene/.test(o.textContent));
    if (option) {
      select.value = option.value;
      await loadStructureFromPath(option.value);
    }
  }

  const job = params.get('job');
  // 扫描曲线的显示模式要在 selectJob 之前定好，否则渲染出来的是默认视图
  if (params.get('scanmode')) state.scanMode = params.get('scanmode');
  if (job) await selectJob(job);

  // 视图预设：便于分享"我看到的这张图"，也便于自动化截图
  const style = params.get('style');
  if (style) {
    const button = document.querySelector(`[data-style="${style}"]`);
    if (button) button.click();
  }
  const colorMode = params.get('color');
  if (colorMode) {
    const button = document.querySelector(`[data-color="${colorMode}"]`);
    if (button) button.click();
  }
  if (params.get('labels') === '1') $('btn-labels').click();
  if (params.get('scene')) {
    state.viewer.setStyle(params.get('scene').split(',')[0] === 'spacefill' ? 'spacefill' : state.viewer.style);
  }

  const recipeId = params.get('recipe');
  if (recipeId) {
    $('select-recipe').value = recipeId;
    applyRecipe();
  }

  // 直接打开某个批次的相对能量表（便于分享链接）
  const batchId = params.get('batch');
  if (batchId) {
    try {
      const data = await api('/api/batch/' + encodeURIComponent(batchId));
      if (data.result) renderBatch(data.result);
      else logLine('该批次状态：' + data.status, 'l-dim');
    } catch (err) {
      toast('批次加载失败: ' + err.message, 'err');
    }
  }

  if (params.get('viewertest') === '1') {
    await runViewerInteractionTest();
  }
  if (params.get('scantest') === '1') {
    await runScanDragTest();
  }
  if (params.get('scancheck') === '1') {
    await runScanHintTest(params.get('scancheck-kind') || 'dihedral');
  }
  if (params.get('fixtest')) {
    await runInteractionFixTest(params.get('fixtest'));
  }
  if (params.get('botest')) {
    await runBondOrderUiTest(params.get('botest'), params.get('bokind') || 'mayer');
  }
  // 两个自检钩子都要保留（feat/orbcomp-ui 与 feat/geom-table 各加了一个）
  if (params.get('octest')) {
    await runOrbCompUiTest(params.get('octest'));
  }
  if (params.get('geomtest') === '1') {
    await runGeometryTableTest();
  }
  // ?overlay=<另一个任务 id>：把它叠到 ?job= 选中的任务上（便于分享与截图）
  if (params.get('overlay') && job) {
    await applyOverlay(job, params.get('overlay'),
                       document.getElementById('overlay-section'));
  }
  // ?overtest=<任务 id>：叠加界面自检；?overbad=<不一致的任务 id> 顺带验失败路径
  if (params.get('overtest')) {
    await runOverlayUiTest(params.get('overtest'), params.get('overbad'));
  }
  // ?sptest=<频率任务 id>：红外谱图区自检（真浏览器里验图不是空白）
  if (params.get('sptest')) {
    await runSpectrumUiTest(params.get('sptest'));
  }
  // ?sptestfail=<任务 id>：谱图加载失败时的可读原因（不能只留一个破图）
  if (params.get('sptestfail')) {
    await runSpectrumFailUiTest(params.get('sptestfail'));
  }
}

/**
 * 真浏览器验证键级界面（P2 的 Multiwfn 集成）。
 *
 * 后端早就有测试了，但**界面是新写的、必须真点一遍** ——
 * 本项目已经因为"DOM 里有但用户看不见"吃过一次亏（结论横幅被扫描区挤下去）。
 */
async function runBondOrderUiTest(jobId, kind) {
  const out = document.createElement('pre');
  out.id = 'botest-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;'
    + 'color:#0f0;background:#000;padding:4px;max-width:96vw;white-space:pre-wrap';
  document.body.appendChild(out);
  const lines = [];
  try {
    const section = document.getElementById('bondorder-section');
    lines.push(`section=${section ? 'PASS' : 'FAIL'}`);
    if (section) {
      // 位置：必须排在结论横幅（和扫描区）**之后**，但仍在面板可见区内
      const body = $('result-body');
      const kids = Array.from(body.children).map((n) => n.id || n.className);
      lines.push(`order=${kids.join('>')}`);
      const rect = section.getBoundingClientRect();
      lines.push(`visible=${rect.width > 40 && rect.height > 10 ? 'PASS' : 'FAIL'}`
        + ` w=${Math.round(rect.width)} h=${Math.round(rect.height)}`);

      $('bo-kind').value = kind;
      $('bo-run').click();
      const table = await new Promise((resolve) => {
        const deadline = setTimeout(() => resolve(null), 180000);
        const poll = setInterval(() => {
          const t = section.querySelector('.bo-table');
          const e = section.querySelector('.bo-error');
          if (t || e) { clearInterval(poll); clearTimeout(deadline); resolve(t || e); }
        }, 150);
      });
      if (!table) {
        lines.push('table=TIMEOUT');
      } else if (table.classList.contains('bo-error')) {
        lines.push(`table=ERROR ${table.textContent.slice(0, 120)}`);
      } else {
        const rows = table.querySelectorAll('tbody tr');
        const first = rows.length ? rows[0].textContent.replace(/\s+/g, ' ').trim() : '';
        lines.push(`table=PASS rows=${rows.length} first="${first}"`);
        const bar = table.querySelector('.bo-bar i');
        lines.push(`bar=${bar && parseFloat(bar.style.width) > 0 ? 'PASS' : 'FAIL'}`);
        lines.push(`summary="${(section.querySelector('.bo-summary') || {}).textContent || ''}"`);

        // 3D 数值标注：点了「显示数值」之后，查看器必须真的进入数值模式，
        // 而且要把键级喂进去（原子值缺了也要给 bondValues）
        $('btn-values').click();
        lines.push(`values_active=${$('btn-values').classList.contains('active') ? 'PASS' : 'FAIL'}`);
        lines.push(`viewer_valueMode=${state.viewer.valueMode || 'null'}`
          + ` atomValues=${state.viewer.atomValues ? state.viewer.atomValues.length : 0}`
          + ` bondValues=${state.viewer.bondValues ? state.viewer.bondValues.length : 0}`);
        // 画出来的才算数：数一下画布上确实有文字（用一次离屏重绘来探测太绕，
        // 这里检查数据到位 + 模式正确即可，位置正确性由 _drawValues 的单元测试守）
        lines.push(`bond0=${state.viewer.bondValues && state.viewer.bondValues[0]
          ? JSON.stringify(state.viewer.bondValues[0]) : 'none'}`);
      }
    }
  } catch (err) {
    lines.push('ERROR ' + (err && err.message ? err.message : String(err)));
  }
  out.textContent = '__BOTEST__' + lines.join(' | ') + '__END__';
}

/**
 * 真浏览器验证「几何参数」表（键长/键角/二面角）。
 *
 * 本项目已经栽过三次"DOM 里有但屏幕上没有"，所以这里不只看数据对不对：
 *  * 表格真的渲染出行、行的值与排序对不对
 *  * 区块真的在**可见区**内（getBoundingClientRect），且排在结论横幅之后
 *  * 点一行之后，查看器里被高亮的原子数与行里的原子一致，
 *    并且**真的画出来了**（highlightDrawCount = 画了几个高亮环）
 *  * 切换标签会清掉旧高亮；再点同一行能取消高亮
 *
 * 配合 `?geomtest=1`：有 `?job=` 时用任务结果几何，否则用 `?load=` 载入的结构 ——
 * 正好覆盖"结果几何优先、其次当前结构"这两条来源。
 */
async function runGeometryTableTest() {
  const out = document.createElement('pre');
  out.id = 'geomtest-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;'
    + 'color:#0f0;background:#000;padding:4px;max-width:96vw;white-space:pre-wrap';
  document.body.appendChild(out);
  const lines = [];
  // 注意：这里**不用 requestAnimationFrame 等重绘**。无头 Chrome 的虚拟时间
  // 与 rAF 对不上（本项目踩过：钩子永远写不出结果），所以每次点完直接调
  // render() 同步重绘 —— 高亮环画没画出来，照样能数清楚。
  const section = () => document.getElementById('geometry-section');
  const readRows = () => Array.from(section().querySelectorAll('tbody tr.geom-row')).map((tr) => ({
    atoms: tr.dataset.atoms,
    label: tr.dataset.label,
    value: Number(tr.dataset.value),
    unit: tr.dataset.unit,
  }));
  const isSorted = (rows) => rows.every((r, i) => i === 0 || rows[i - 1].value <= r.value);
  const clickRow = (index) => {
    const rows = section().querySelectorAll('tbody tr.geom-row');
    if (!rows[index]) return null;
    rows[index].dispatchEvent(new MouseEvent('click', {
      bubbles: true, cancelable: true, view: window,
    }));
    state.viewer.render();
    return rows[index];
  };
  const clickTab = (name) => {
    const btn = section().querySelector(`[data-geom="${name}"]`);
    if (btn) btn.click();
    return btn;
  };
  try {
    // 表是异步请求回来后填的 —— 等 state.geometry 到位（那之后 DOM 已同步填好）
    for (let i = 0; i < 100 && !state.geometry; i++) {
      await new Promise((r) => setTimeout(r, 50));
    }
    if (!section() || !state.geometry) throw new Error('没有渲染出几何参数表');

    lines.push(`source=${state.geomSource ? state.geomSource.source : 'none'}`);
    lines.push(`atoms=${state.geomSource ? state.geomSource.symbols.length : 0}`);
    lines.push(`counts=bonds:${(state.geometry.bonds || []).length}`
      + `,angles:${(state.geometry.angles || []).length}`
      + `,dihedrals:${(state.geometry.dihedrals || []).length}`);

    const rect = section().getBoundingClientRect();
    const visible = rect.width > 60 && rect.height > 30 && rect.bottom > 0 && rect.top < innerHeight;
    lines.push(`visible=${visible ? 'PASS' : 'FAIL'}`
      + ` rect=${Math.round(rect.left)},${Math.round(rect.top)},${Math.round(rect.width)}x${Math.round(rect.height)}`);
    const body = $('result-body');
    // 用逗号连接：dump-dom 会把文本里的 ">" 转义成 "&gt;"，断言会很难写
    lines.push(`order=${Array.from(body.children).map((n) => n.id || n.className).join(',')}`);
    const verdict = body.querySelector('.res-verdict');
    if (verdict) {
      lines.push('after_verdict='
        + ((verdict.compareDocumentPosition(section()) & Node.DOCUMENT_POSITION_FOLLOWING) ? 'PASS' : 'FAIL'));
    }

    // --- 键长页：行数 + 按值升序 + 数值与单位 ---
    let rows = readRows();
    lines.push(`bonds_rows=${rows.length} sorted=${isSorted(rows) ? 'PASS' : 'FAIL'}`);
    lines.push(`bonds_first=${rows.length ? `${rows[0].label}=${rows[0].value.toFixed(3)}${rows[0].unit}` : 'none'}`);
    lines.push(`bonds_last=${rows.length ? `${rows[rows.length - 1].label}=${rows[rows.length - 1].value.toFixed(3)}${rows[rows.length - 1].unit}` : 'none'}`);
    const counts = {};
    section().querySelectorAll('[data-count]').forEach((c) => { counts[c.dataset.count] = c.textContent; });
    lines.push(`tab_counts=${GEOM_KINDS.map(([key]) => `${key}:${counts[key]}`).join(' ')}`);

    // --- 点第一行：高亮原子数与行一致，而且要真画出来 ---
    let picked = clickRow(0);
    lines.push(`pick=${picked ? picked.dataset.label : 'none'}`
      + ` highlight=${state.viewer.highlightedAtoms.join(',')}`
      + ` n=${state.viewer.highlightedAtoms.length}`);
    lines.push(`draws_forced=${state.viewer.highlightDrawCount}`);

    // --- 切标签：计数要对，旧高亮必须清掉 ---
    if (clickTab('angles')) {
      const arows = readRows();
      lines.push(`angles_rows=${arows.length} sorted=${isSorted(arows) ? 'PASS' : 'FAIL'}`
        + ` cleared=${state.viewer.highlightedAtoms.length === 0 ? 'PASS' : 'FAIL'}`);
      if (arows.length) {
        picked = clickRow(0);
        lines.push(`angle_pick=${picked.dataset.label}`
          + ` n=${state.viewer.highlightedAtoms.length} drawn=${state.viewer.highlightDrawCount}`);
      }
    }
    if (clickTab('dihedrals')) {
      const drows = readRows();
      lines.push(`dihedrals_rows=${drows.length}`);
      if (drows.length) {
        picked = clickRow(0);
        lines.push(`dihedral_pick=${picked.dataset.label}`
          + ` value=${drows[0].value.toFixed(2)}`
          + ` n=${state.viewer.highlightedAtoms.length} drawn=${state.viewer.highlightDrawCount}`);
      } else {
        lines.push('dihedrals_none=PASS');
      }
    }

    // --- 再点同一行 = 取消高亮 ---
    if (clickTab('bonds') && readRows().length) {
      clickRow(0);
      const on = state.viewer.highlightedAtoms.length;
      clickRow(0);
      lines.push(`toggle_off=${on > 0 && state.viewer.highlightedAtoms.length === 0 ? 'PASS' : 'FAIL'}`);
    }

    // --- 表与 3D 不是同一套几何时，必须先把查看器切过去（否则高亮会指错原子）---
    // 这是真实会发生的状态：拖过扫描曲线、或停在轨迹某一帧时，查看器里显示的
    // 并不是 result.geometry。这里把查看器换成一个完全不相干的分子来制造错位，
    // 点一行后应当看到查看器被切回表的那套几何、且高亮画在**正确的**原子上。
    state.viewer.setMolecule({ symbols: ['Ar'], coords: [[0, 0, 0]] });
    if (clickTab('bonds') && readRows().length) {
      const want = readRows()[0].atoms;
      clickRow(0);
      const switched = state.viewer.symbols.length === state.geomSource.symbols.length;
      const got = state.viewer.highlightedAtoms.join(',');
      lines.push(`mismatch_switch=${switched ? 'PASS' : 'FAIL'}`
        + ` highlight=${got} want=${want}`
        + ` drawn=${state.viewer.highlightDrawCount}`
        + ` ok=${switched && got === want && state.viewer.highlightDrawCount > 0 ? 'PASS' : 'FAIL'}`);
    }

    // 最后留在"高亮着"的状态：同一条 URL 用 --screenshot 拍到的就是用户看到的样子。
    // 上面的错位检查点过一次行，可能刚好把高亮又切回"已取消"，所以按状态补一次
    // ——不能无条件再点一次，那样反而会把已经亮着的行点灭（截图证据就没了）。
    if (!state.viewer.highlightedAtoms.length) clickRow(0);
    const finalRow = section().querySelector('tbody tr.geom-row.active');
    lines.push(`final_pick=${finalRow ? finalRow.dataset.label : 'none'}`
      + ` n=${state.viewer.highlightedAtoms.length} drawn=${state.viewer.highlightDrawCount}`);
    lines.push('GEOMTEST RESULT: '
      + (lines.some((l) => l.includes('FAIL')) ? 'FAIL' : 'PASS'));
  } catch (err) {
    lines.push('ERROR ' + (err && err.message ? err.message : String(err)));
  }
  out.textContent = '__GEOMTEST__' + lines.join(' | ') + '__END__';
}

/**
 * 真浏览器验证两处**交互修复**（审计 F1 / F2）：
 *
 * * F1 「运行」按钮在 SSE 断开后永久禁用 —— 看门狗要能把它恢复
 * * F2 快速切换任务时结果面板串台 —— 过期的响应要被丢弃
 *
 * 两处都是"接线"层面的问题，纯逻辑测试证明不了，必须在真页面里点。
 */
async function runInteractionFixTest(jobId) {
  const out = document.createElement('pre');
  out.id = 'fixtest-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;'
    + 'color:#0f0;background:#000;padding:4px;max-width:96vw;white-space:pre-wrap';
  document.body.appendChild(out);
  const lines = [];
  try {
    // --- F1：按钮兜底恢复 ---
    $('btn-run').disabled = true;
    $('btn-cancel').disabled = false;
    state._runWatchPolls = 0;
    // 用一个**已经结束**的任务，间隔调短便于测试
    watchRunButton(jobId, 150);
    const t0 = Date.now();
    const restored = await new Promise((resolve) => {
      const deadline = setTimeout(() => resolve(false), 15000);
      const poll = setInterval(() => {
        if (!$('btn-run').disabled) { clearInterval(poll); clearTimeout(deadline); resolve(true); }
      }, 50);
    });
    lines.push(`F1_button_restored=${restored ? 'PASS' : 'FAIL'}`);
    lines.push(`F1_cancel_disabled=${$('btn-cancel').disabled ? 'PASS' : 'FAIL'}`);
    lines.push(`F1_polls=${state._runWatchPolls || 0} elapsed=${Date.now() - t0}ms`);
    lines.push(`F1_has_watch=${state._runWatch ? 'yes' : 'no'}`);

    // --- F2：过期响应必须被丢弃 ---
    // 先确认这次请求真的成功了 —— 否则 state.result 不变，
    // 断言会因为"请求失败"而**假通过**
    await selectJob(jobId);
    lines.push(`F2_selected=${state.selectedJob === jobId ? 'PASS' : 'FAIL'}`
      + ` result_loaded=${state.result ? 'yes' : 'no'}`);
    const settled = state.result;
    const stale = selectJob(jobId);      // 不 await
    state.jobToken += 1;                 // 模拟"期间用户又点了别的任务"
    await stale;
    lines.push(`F2_stale_discarded=${state.result === settled ? 'PASS' : 'FAIL'}`);
    lines.push(`F2_token=${state.jobToken}`);

    // --- P0：结论横幅真的渲染出来了（三档取一）---
    const banner = document.querySelector('.res-verdict');
    lines.push(`verdict_banner=${banner ? 'PASS' : 'FAIL'}`
      + ` class=${banner ? banner.className.replace('res-verdict', '').trim() : '—'}`);
    lines.push(`verdict_text="${banner ? banner.textContent.replace(/\s+/g, ' ').trim().slice(0, 90) : ''}"`);
  } catch (err) {
    lines.push('ERROR ' + (err && err.message ? err.message : String(err)));
  }
  out.textContent = '__FIXTEST__' + lines.join(' | ') + '__END__';
}

/**
 * 真浏览器验证：扫描提示区有没有把「实际值」和「180° 奇点」显示出来。
 *
 * 纯逻辑已经在 Node 里测过了（scanblock.js 的数学对着 ORCA 的 7 个已知
 * 扫描点校准过），这里验的是**接线** —— 算对了但没渲染出来一样是白搭。
 */
async function runScanHintTest(kind) {
  const out = document.createElement('pre');
  out.id = 'scancheck-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;'
    + 'color:#0f0;background:#000;padding:4px;max-width:96vw;white-space:pre-wrap';
  document.body.appendChild(out);
  const lines = [];
  try {
    // 这里**不用任何 await**：结构在 applyUrlParams 里已经加载完，
    // updateScanUI 本身是同步的。加 setTimeout/rAF 会让无头 Chrome 的
    // 虚拟时间与钩子对不上，钩子就永远写不出结果（踩过）。
    const select = $('select-scan');
    select.value = kind;
    $('input-scan-atoms').value = '1 2 3 4';
    $('input-scan-range').value = '90 270 19';
    updateScanUI();

    const hint = $('scan-hint');
    lines.push('hint_hidden=' + $('scan-options').hidden);
    lines.push('has_actual_value=' + /实际值/.test(hint.textContent));
    lines.push('has_180=' + /180/.test(hint.textContent) + '/' + /奇点/.test(hint.textContent));
    lines.push('warn_span=' + hint.querySelectorAll('span.warn').length);
    lines.push('info_span=' + hint.querySelectorAll('span.info').length);
    lines.push('text=' + hint.textContent.replace(/\s+/g, ' ').trim().slice(0, 240));

    // 再试一个应当"没有风险提示"的设置：区间不跨 180°
    $('input-scan-range').value = '110 170 7';
    updateScanUI();
    lines.push('safe_warn_span=' + hint.querySelectorAll('span.warn').length);
    lines.push('safe_info_span=' + hint.querySelectorAll('span.info').length);
  } catch (err) {
    lines.push('ERROR ' + (err && err.message ? err.message : String(err)));
  }
  out.textContent = '__SCANCHECK__' + lines.join(' | ') + '__END__';
}

/**
 * 在**真实浏览器**里用合成指针事件驱动扫描曲线的拖动，验证：
 * 拖了之后读数变了、中栏显示的结构真的换成了那个点的几何。
 *
 * 需要配合 `?job=<扫描任务id>&scantest=1`。结果写进 DOM 供 `--dump-dom` 读取。
 */
async function runScanDragTest() {
  const lines = [];
  const out = document.createElement('pre');
  out.id = 'scantest-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;'
    + 'color:#0f0;background:#000;padding:4px';
  document.body.appendChild(out);

  try {
    // 扫描区是 renderResult 里异步插进来的，这里等它出现
    let section = document.getElementById('scan-section');
    for (let i = 0; i < 60 && !section; i++) {
      await new Promise((r) => setTimeout(r, 100));
      section = document.getElementById('scan-section');
    }
    if (!section) throw new Error('页面上没有扫描区（这任务是扫描任务吗？）');
    const svg = section.querySelector('.scan-svg');
    if (!svg) throw new Error('扫描区里没有曲线 SVG');
    const profile = state.scanProfile || {};
    const rect = svg.getBoundingClientRect();
    lines.push(`chart=${Math.round(rect.width)}x${Math.round(rect.height)}`
      + ` points=${(profile.points || []).length} geoms=${(profile.geometries || []).length}`);
    if (!(profile.geometries || []).length) throw new Error('曲线数据里没有逐点几何');

    const snapshot = () => JSON.stringify(state.viewer.coords);
    const before = snapshot();
    const readoutBefore = (section.querySelector('#scan-readout') || {}).textContent || '';

    const dispatch = (type, clientX) => svg.dispatchEvent(new PointerEvent(type, {
      bubbles: true, cancelable: true, view: window, pointerId: 1, isPrimary: true,
      clientX, clientY: rect.top + rect.height * 0.5,
    }));

    // 从左侧 15% 拖到右侧 85%
    dispatch('pointerdown', rect.left + rect.width * 0.15);
    const afterDown = JSON.stringify(state.viewer.coords);
    const idxDown = state.scanIndex;
    dispatch('pointermove', rect.left + rect.width * 0.85);
    dispatch('pointerup', rect.left + rect.width * 0.85);

    const after = snapshot();
    const readoutAfter = (section.querySelector('#scan-readout') || {}).textContent || '';
    const last = (profile.points || []).length - 1;

    lines.push(`pointerdown  index=${idxDown}  coords_changed=${afterDown !== before ? 'PASS' : 'FAIL'}`);
    lines.push(`pointermove  index=${state.scanIndex}  coords_changed=`
      + `${after !== afterDown ? 'PASS' : 'FAIL'}`);
    lines.push(`readout  "${readoutAfter.replace(/\s+/g, ' ').trim()}"`);
    lines.push(`readout_changed=${readoutAfter !== readoutBefore ? 'PASS' : 'FAIL'}`);
    lines.push(`viewer_atoms=${state.viewer.symbols.length}`
      + ` matches_profile=${state.viewer.symbols.join(',') === (profile.symbols || []).join(',') ? 'PASS' : 'FAIL'}`);

    // 游标也要跟着动
    const cursor = section.querySelector('.scan-cursor');
    lines.push(`cursor_visible=${cursor && !cursor.hidden ? 'PASS' : 'FAIL'}`
      + ` cx=${cursor ? cursor.querySelector('circle').getAttribute('cx') : '—'}`);

    // --- F5：切到「键长响应」模式后，游标圆点必须落在**键长那条曲线**上 ---
    // 以前无论哪种模式都用 rel_kcal 换算 y，键长模式下圆点会飞到曲线之外
    // （实测偏离最近数据点 37px）。
    const bondBtn = section.querySelector('[data-scan-mode="bond"]');
    if (bondBtn) {
      bondBtn.click();
      await new Promise((r) => setTimeout(r, 50));
      const c2 = section.querySelector('.scan-cursor');
      const dot = c2 && c2.querySelector('circle');
      const cy = dot ? parseFloat(dot.getAttribute('cy')) : NaN;
      // 图的 y 轴绘图区是 viewBox 的 12 ~ 154（见 SCAN_CHART）
      const inside = Number.isFinite(cy) && cy >= 10 && cy <= 156;
      lines.push(`F5_bond_cursor_cy=${Number.isFinite(cy) ? cy.toFixed(1) : '—'}`
        + ` inside_plot=${inside ? 'PASS' : 'FAIL'}`);
    } else {
      lines.push('F5_bond_cursor=SKIP(无键长响应)');
    }
  } catch (err) {
    lines.push('ERROR ' + (err && err.message ? err.message : String(err)));
  }
  out.textContent = '__SCANTEST__' + lines.join(' | ') + '__END__';
}

/**
 * 在**真实浏览器**里用合成鼠标事件驱动查看器的事件处理器，
 * 验证拖拽旋转、滚轮缩放、双击复位确实生效，并把结果写进 DOM，
 * 供无头 Chrome 的 --dump-dom 读取。
 *
 * 之所以要这个：无头逻辑测试只能证明投影数学对，证明不了事件绑定对。
 */
async function runViewerInteractionTest() {
  const lines = [];
  const out = document.createElement('pre');
  out.id = 'viewertest-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;color:#0f0;background:#000;padding:4px';
  document.body.appendChild(out);

  const canvas = $('viewer');
  const fire = (type, opts) => canvas.dispatchEvent(new MouseEvent(type, Object.assign({
    bubbles: true, cancelable: true, view: window,
  }, opts)));

  try {
    if (!state.molecule) {
      // 优先用 ?load= 指定的结构；否则取第一个内置示例（避免写死本机路径）
      const data = await api('/api/examples');
      const first = (data.examples || [])[0];
      if (!first) throw new Error('没有可用的示例结构');
      await loadStructureFromPath(first.path);
    }
    // 等一帧，让 setMolecule 生效
    await new Promise((r) => requestAnimationFrame(r));

    const v = state.viewer;
    const rect = canvas.getBoundingClientRect();
    lines.push(`canvas=${Math.round(rect.width)}x${Math.round(rect.height)} atoms=${v.symbols.length}`);

    // --- 拖拽旋转 ---
    const rotBefore = [v.rotX, v.rotY];
    fire('mousedown', { clientX: rect.left + 200, clientY: rect.top + 150, button: 0 });
    fire('mousemove', { clientX: rect.left + 300, clientY: rect.top + 210, button: 0 });
    fire('mouseup', { clientX: rect.left + 300, clientY: rect.top + 210, button: 0 });
    const rotAfter = [v.rotX, v.rotY];
    const rotated = Math.abs(rotAfter[0] - rotBefore[0]) > 1e-6 || Math.abs(rotAfter[1] - rotBefore[1]) > 1e-6;
    lines.push(`rotate before=(${rotBefore.map((x) => x.toFixed(3))}) after=(${rotAfter.map((x) => x.toFixed(3))}) ${rotated ? 'PASS' : 'FAIL'}`);

    // --- 滚轮缩放 ---
    const zoomBefore = v.zoom;
    canvas.dispatchEvent(new WheelEvent('wheel', { deltaY: -200, bubbles: true, cancelable: true }));
    const zoomAfter = v.zoom;
    const zoomed = zoomAfter > zoomBefore * 1.01;
    lines.push(`zoom before=${zoomBefore.toFixed(3)} after=${zoomAfter.toFixed(3)} ${zoomed ? 'PASS' : 'FAIL'}`);

    // --- 右键平移 ---
    const panBefore = [v.panX, v.panY];
    fire('mousedown', { clientX: rect.left + 200, clientY: rect.top + 150, button: 2 });
    fire('mousemove', { clientX: rect.left + 240, clientY: rect.top + 170, button: 2 });
    fire('mouseup', { clientX: rect.left + 240, clientY: rect.top + 170, button: 2 });
    const panned = v.panX !== panBefore[0] && v.panY !== panBefore[1];
    lines.push(`pan before=(${panBefore}) after=(${[v.panX, v.panY]}) ${panned ? 'PASS' : 'FAIL'}`);

    // --- 双击复位 ---
    fire('dblclick', { clientX: rect.left + 200, clientY: rect.top + 150 });
    const reset = Math.abs(v.zoom - 1) < 1e-9 && Math.abs(v.panX) < 1e-9 && Math.abs(v.panY) < 1e-9;
    lines.push(`reset zoom=${v.zoom.toFixed(3)} pan=(${v.panX},${v.panY}) ${reset ? 'PASS' : 'FAIL'}`);

    // --- 样式与着色切换 ---
    document.querySelector('[data-style="spacefill"]').click();
    const styleOk = v.style === 'spacefill';
    document.querySelector('[data-color="charge"]').click();
    const colorOk = v.colorMode === 'charge';
    lines.push(`toolbar style=${v.style} color=${v.colorMode} ${styleOk && colorOk ? 'PASS' : 'FAIL'}`);

    // --- 无轨迹时轨迹栏必须真的不占位 ---
    // 注意要查计算样式：作者 CSS 里的 display 会盖掉 hidden 属性的 UA 规则
    const trajDisplay = getComputedStyle($('trajectory')).display;
    lines.push(`trajectory display=${trajDisplay} ${trajDisplay === 'none' ? 'PASS' : 'FAIL'}`);

    const allPass = lines.every((l) => !l.includes('FAIL'));
    lines.push('VIEWERTEST RESULT: ' + (allPass ? 'PASS' : 'FAIL'));
  } catch (err) {
    lines.push('VIEWERTEST RESULT: FAIL (' + (err && err.message) + ')');
  }

  out.textContent = lines.join('\n');
  // 同步一份到 console，便于人工排查
  lines.forEach((l) => console.log('VIEWERTEST ' + l));
}

function applyStatus(status) {
  state.jobTypes = status.job_types || [];

  const select = $('select-jobtype');
  select.innerHTML = state.jobTypes
    .map((t) => `<option value="${esc(t.value)}">${esc(t.label)}</option>`).join('');

  const solvent = $('select-solvent');
  solvent.innerHTML = '<option value="">气相</option>' +
    (status.solvents || []).map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('');

  const engines = status.engines || [];
  const orca = engines.find((e) => e.name === 'orca') || engines[0];
  const chipEngine = $('chip-engine');
  if (orca) {
    setChipState(chipEngine, !!orca.available);
    chipEngine.querySelector('b').textContent = orca.display_name + (orca.available ? '' : '（不可用）');
    chipEngine.title = orca.detail || '';
    if (!orca.available) toast('引擎不可用：' + orca.detail, 'err');
  } else {
    setChipState(chipEngine, false);
    chipEngine.querySelector('b').textContent = '未检测到';
  }

  const tools = status.tools || {};
  const names = { multiwfn: 'Multiwfn', avogadro: 'Avogadro2', obabel: 'obabel' };
  const available = Object.keys(tools).filter((k) => tools[k].available);
  const chipTools = $('chip-tools');
  setChipState(chipTools, available.length > 0);
  chipTools.querySelector('b').textContent = available.length + ' / ' + Object.keys(tools).length + ' 可用';
  chipTools.title = Object.keys(tools)
    .map((k) => `${names[k] || k}: ${tools[k].available ? tools[k].path : '未找到'}`).join('\n');

  $('chip-jobs').querySelector('b').textContent = status.jobs_root || '—';
  $('chip-jobs').title = status.jobs_root || '';

  const cfg = status.config || {};
  $('cfg-orca').value = cfg.orca_path || '';
  $('cfg-multiwfn').value = cfg.multiwfn_path || '';
  $('cfg-avogadro').value = cfg.avogadro_path || '';
  $('cfg-obabel').value = cfg.obabel_path || '';
  $('cfg-web_port').value = cfg.web_port || 8760;
  $('cfg-auto_open_browser').checked = !!cfg.auto_open_browser;

  // 换机器时最需要看的一句：ORCA 到底找到没有、是从哪找到的。
  // 只显示"未找到"不够 —— 用户需要知道下一步该做什么。
  // （orca 变量在上面已经取过了，这里直接用。）
  const hint = $('orca-hint');
  if (hint) {
    hint.innerHTML = orca && orca.available
      ? `<span class="ok">✓ 已找到：${esc(orca.exe || orca.detail || '')}</span>`
        + (orca.source ? `（来源：${esc(orca.source)}）` : '')
      : '<span class="warn">⚠ 没找到 ORCA。填上 orca.exe 的完整路径，'
        + '或把它所在目录加入 PATH。</span>';
  }
}

async function loadExamples() {
  try {
    const data = await api('/api/examples');
    const select = $('example-select');
    select.innerHTML = '<option value="">示例结构…</option>' +
      (data.examples || []).map((e) => `<option value="${esc(e.path)}">${esc(e.name)}</option>`).join('');
  } catch (err) { /* 示例加载失败不致命 */ }
}

// ------------------------------------------------------------------ 结构

function setMolecule(molecule) {
  state.molecule = molecule;
  showStructureInfo({
    name: molecule.name,
    formula: molecule.formula && molecule.formula !== '—'
      ? molecule.formula : formulaOf(molecule.symbols),
    n_atoms: molecule.n_atoms || (molecule.symbols || []).length,
    charge: molecule.charge,
    multiplicity: molecule.multiplicity,
  });
  $('input-charge').value = molecule.charge ?? 0;
  $('input-mult').value = molecule.multiplicity ?? 1;

  $('formula-display').textContent = '';

  state.viewer.setMolecule({ symbols: molecule.symbols, coords: molecule.coords });
  $('viewer-empty').classList.add('hide');
  $('viewer-caption').textContent =
    `${molecule.name || '结构'} · ${molecule.formula || ''} · ${(molecule.symbols || []).length} 原子`;
  $('trajectory').hidden = true;
  logLine(`载入结构: ${molecule.name || ''} ${molecule.formula || ''} (${(molecule.symbols || []).length} 原子)`, 'l-dim');
  // 没有任务结果时，几何参数表就用这个刚载入的结构（"退而用当前载入的结构"）
  loadGeometrySection();
}

async function loadStructureFromPath(path) {
  try {
    const data = await api('/api/structure/load', { method: 'POST', body: JSON.stringify({ path }) });
    state.moleculePath = data.path || path;
    setMolecule(data.molecule);
  } catch (err) {
    toast('载入失败: ' + err.message, 'err');
    logLine('载入失败: ' + err.message, 'l-err');
  }
}

function readLocalFile(file) {
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const data = await api('/api/structure/text', {
        method: 'POST',
        body: JSON.stringify({ text: reader.result, name: file.name.replace(/\.[^.]+$/, '') }),
      });
      state.moleculePath = null;   // 来自拖放的文本，没有后端路径
      setMolecule(data.molecule);
    } catch (err) {
      toast('解析失败: ' + err.message, 'err');
      logLine('解析失败: ' + err.message, 'l-err');
    }
  };
  reader.readAsText(file);
}

// ------------------------------------------------------------------ 任务

async function refreshJobs() {
  try {
    const data = await api('/api/jobs?limit=80');
    state.jobs = data.jobs || [];
    renderJobs();
  } catch (err) {
    logLine('刷新任务失败: ' + err.message, 'l-err');
  }
}

/**
 * 任务列表的**兜底轮询**。
 *
 * 界面原来只靠 SSE 推送来刷新任务列表。SSE 一断（服务重启、网络抖动、休眠唤醒），
 * `finished` 事件就永远收不到，列表会一直停在"运行中" —— 看起来就像任务卡住了。
 * 实测踩过：重启服务后，用户看到的就是"算完了还显示在运行"。
 *
 * 所以加一层兜底：只在**确实有任务没跑完**时每 5 秒拉一次，全部结束就自动停，
 * 既不会长期打扰服务，也不会漏掉状态变化。
 */
function startJobWatch(intervalMs = 5000) {
  if (state._jobWatch) return;
  const pending = () => state.jobs.some((j) => j.status === 'running' || j.status === 'queued');
  if (!pending()) return;
  state._jobWatch = setInterval(async () => {
    await refreshJobs();
    if (!pending()) {
      clearInterval(state._jobWatch);
      state._jobWatch = null;
    }
  }, intervalMs);
}

function renderJobs() {
  const list = $('jobs-list');
  $('jobs-count').textContent = state.jobs.length;
  if (!state.jobs.length) {
    list.innerHTML = '<p class="empty">还没有任务</p>';
    return;
  }
  list.innerHTML = state.jobs.map((job) => {
    const when = job.created_at ? new Date(job.created_at * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '';
    const energy = job.final_energy !== null && job.final_energy !== undefined ? job.final_energy.toFixed(8) : '';
    const duration = job.duration ? job.duration.toFixed(1) + 's' : '';
    const selected = job.job_id === state.selectedJob ? ' selected' : '';
    // 结果有问题时 store 会把结论写进 message 并加前缀：
    //   ⛔ = 结果不可用（算出来的分子都变了）  ⚠ = 可用但有限制
    // 卡片上必须带出来 —— 否则列表里是个绿点 DONE，用户不点开就永远发现不了。
    const msg = job.message || '';
    const unusable = job.status === 'done' && msg.startsWith('⛔');
    const warned = job.status === 'done' && msg.startsWith('⚠');
    const badge = unusable
      ? '<span class="badge warned" title="' + esc(msg) + '">⛔ 结果不可用</span>'
      : warned
        ? '<span class="badge warned" title="' + esc(msg) + '">⚠ 有限制</span>'
        : '';
    return `<div class="job-card${selected}" data-job="${esc(job.job_id)}" data-status="${esc(job.status)}">
      <div class="line1">
        <span class="name">${esc(job.molecule)}</span>
        <span class="badge ${esc(job.status)}">${esc(job.status)}</span>
        ${badge}
        <span class="when">${esc(when)}</span>
      </div>
      <div class="line2">
        <span>${esc(job.job_type)}</span><span>${esc(job.method)}</span>
        <span class="energy">${esc(energy)}</span><span>${esc(duration)}</span>
      </div>
    </div>`;
  }).join('');

  list.querySelectorAll('.job-card').forEach((card) => {
    card.addEventListener('click', () => selectJob(card.dataset.job));
  });
  renderGeometryOptions();
}

/** 由元素符号算化学式（C、H 在前，符合有机惯例）。 */
function formulaOf(symbols) {
  const counts = {};
  (symbols || []).forEach((s) => { counts[s] = (counts[s] || 0) + 1; });
  const order = Object.keys(counts).sort((a, b) => {
    const rank = (s) => (s === 'C' ? 0 : s === 'H' ? 1 : 2);
    return rank(a) - rank(b) || a.localeCompare(b);
  });
  return order.map((s) => s + (counts[s] > 1 ? counts[s] : '')).join('');
}

/** 左侧结构面板：既用于载入的结构，也用于任务结果里的几何。 */
function showStructureInfo(info) {
  $('info-name').textContent = info.name || '—';
  $('info-formula').textContent = info.formula || '—';
  $('info-atoms').textContent = info.n_atoms ?? '—';
  $('info-charge').textContent = `${info.charge ?? 0} / ${info.multiplicity ?? 1}`;
}

async function selectJob(jobId) {
  state.selectedJob = jobId;
  renderJobs();
  $('result-job').textContent = jobId.slice(0, 15);
  // 请求序号：快速点两个任务时，**慢的那个响应后到会把面板覆盖掉** ——
  // 标题和高亮是后点的那个，结果区却是先点的那个（审计 F2，实测复现）。
  // 回来时对不上序号就整段丢弃。
  const token = ++state.jobToken;
  state.bondOrders = null;          // 换任务就清掉上一个任务的键级，免得串到 3D 图上
  try {
    const data = await api('/api/jobs/' + encodeURIComponent(jobId));
    if (token !== state.jobToken) return;
    state.result = data.result;
    renderResult(data.job, data.result);
    refreshViewerValues();          // 「显示数值」开着时用新任务的电荷刷新
    if (data.result && data.result.geometry) {
      state.viewer.setMolecule({
        symbols: data.result.geometry.symbols,
        coords: data.result.geometry.coords,
        charges: (data.result.loewdin_charges && data.result.loewdin_charges.length)
          ? data.result.loewdin_charges : null,
      });
      $('viewer-empty').classList.add('hide');
      $('viewer-caption').textContent = `${data.job.molecule} · ${data.job.job_type} · ${data.result.status}`;
      // 用了几何血缘时左侧面板本来是空的，这里用结果几何补上
      showStructureInfo({
        name: data.job.molecule || '—',
        formula: formulaOf(data.result.geometry.symbols),
        n_atoms: data.result.geometry.symbols.length,
        charge: data.result.charge,
        multiplicity: data.result.multiplicity,
      });
    }
    if (data.result && data.result.irc && data.result.irc.files && data.result.irc.files.full_trajectory) {
      await loadTrajectory(jobId);
      // 轨迹是异步的，回来时也要确认没被后点的任务顶掉（否则会把 A 的曲线插进 B 的面板）
      if (token !== state.jobToken) return;
    } else {
      $('trajectory').hidden = true;
    }
  } catch (err) {
    // 请求失败时**必须把面板清成明确的错误态**：以前只弹一句 toast，
    // 结果区仍旧显示**上一个任务**的内容，而标题已经是新任务了 ——
    // 用户会把别人的数据当成这次的（审计）。这和 F2 是同一类问题：
    // 面板显示的不是你以为的那次任务。
    if (token !== state.jobToken) return;
    state.result = null;
    $('result-body').innerHTML =
      '<div class="res-verdict unusable">'
      + '<div class="res-verdict-head"><span class="res-verdict-icon">✕</span>'
      + '<b>读不出这个任务的结果</b></div>'
      + `<div class="res-verdict-why">${esc(err.message)}</div>`
      + '<ul class="res-verdict-list"><li>面板里原本显示的是<b>上一个</b>任务的内容，'
      + '已经清掉了 —— 免得把它当成这次的。</li></ul></div>';
    toast('读取结果失败: ' + err.message, 'err');
    logLine('✖ 读取结果失败：' + err.message, 'l-err');
  }
}

function currentSpecPayload() {
  const geometryFrom = $('select-geometry').value || null;
  const charges = {
    charge: parseInt($('input-charge').value || '0', 10),
    multiplicity: parseInt($('input-mult').value || '1', 10),
  };
  // 用了几何血缘时可以不载入结构 —— 坐标由上游任务的结果给出
  if (!state.molecule && !geometryFrom) throw new Error('请先载入结构，或选择一个几何来源');

  const scan = scanBlock();        // 没启用扫描时是 null；填错了会抛
  const shared = {
    method: $('input-method').value.trim() || 'HF',
    basis: $('input-basis').value.trim(),
    solvent: $('select-solvent').value || null,
    nprocs: parseInt($('input-nprocs').value || '1', 10),
    maxcore_mb: parseInt($('input-maxcore').value || '2000', 10),
    geometry_from_job: geometryFrom,
    // 电荷/多重度必须**同时发在顶层**：走「几何来源」时后端用的是上游任务
    // 的结果几何 + 上游那次计算的电荷/多重度，嵌在 molecule 里的那份根本不被采用
    // —— 只发 molecule 的话，用户改的值会被静默忽略（审计 F3）。
    charge: charges.charge,
    multiplicity: charges.multiplicity,
  };
  if (scan) shared.extra_blocks = scan;

  if (state.recipe) {
    // 配方负责 job_type / 关键词 / 块设置；界面上的方法基组只作覆盖
    return Object.assign({
      recipe_id: state.recipe.id,
      molecule: state.molecule ? Object.assign({}, state.molecule, charges) : null,
    }, shared);
  }

  return Object.assign({
    molecule: Object.assign({}, state.molecule, charges),
    // 扫描时任务类型由扫描面板锁定为几何优化，不看下拉（下拉此时也是禁用的）
    job_type: scan ? 'opt' : $('select-jobtype').value,
    calc_hess: $('input-calc-hess').checked,
  }, shared);
}

/**
 * 「运行」按钮的**兜底恢复**（审计 F1，高危）。
 *
 * 提交后按钮靠 SSE 的 `finished` 事件恢复。SSE 一断（服务重启、网络抖动、
 * 休眠唤醒），事件就永远收不到 —— 按钮**永久灰掉，唯一自救是刷新页面**。
 * 实测：`?nosse=1` 下任务已经 failed，按钮仍是 disabled。
 *
 * 这里加一层与 SSE 无关的兜底：轮询任务状态，看到它结束就恢复。
 * 连不上服务超过一定次数也恢复 —— 宁可让用户能再点一次，
 * 也别把界面变成只读的。
 */
function watchRunButton(jobId, intervalMs = 4000) {
  if (!jobId) return;
  if (state._runWatch) clearInterval(state._runWatch);
  const MAX_UNREACHABLE = 15;      // 约 1 分钟
  let unreachable = 0;
  const restore = (why) => {
    if (state._runWatch) { clearInterval(state._runWatch); state._runWatch = null; }
    $('btn-run').disabled = false;
    $('btn-cancel').disabled = true;
    if (why) logLine('· ' + why, 'l-dim');
  };
  state._runWatch = setInterval(async () => {
    state._runWatchPolls = (state._runWatchPolls || 0) + 1;
    try {
      const data = await api('/api/jobs/' + encodeURIComponent(jobId));
      unreachable = 0;
      const status = (data.job || {}).status;
      if (status && status !== 'running' && status !== 'queued') {
        restore(`任务已结束（${status}），按钮已恢复`);
      }
    } catch (err) {
      unreachable += 1;
      if (unreachable >= MAX_UNREACHABLE) {
        restore('连不上服务，先恢复「运行」按钮避免界面卡死');
      }
    }
  }, intervalMs);
}

async function runJob() {
  let payload;
  try { payload = currentSpecPayload(); }
  catch (err) { toast(err.message, 'err'); return; }

  $('btn-run').disabled = true;
  try {
    const data = await api('/api/jobs', { method: 'POST', body: JSON.stringify(payload) });
    state.currentJobId = data.job.job_id;
    $('btn-cancel').disabled = false;
    logLine('');
    logLine('▶ 提交任务 ' + data.job.job_id + '  ·  ' + data.summary, 'l-head');
    // 提交时后端已经算过扫描设置了，有问题当场说出来 —— 算力花出去就晚了
    for (const w of (data.warnings || [])) {
      logLine('⚠ 扫描设置: ' + w, 'l-err');
      toast('扫描设置有问题，见日志', 'err');
    }
    await refreshJobs();
    startJobWatch();
    watchRunButton(data.job.job_id);
  } catch (err) {
    $('btn-run').disabled = false;
    toast('提交失败: ' + err.message, 'err');
    logLine('提交失败: ' + err.message, 'l-err');
  }
}

async function cancelJob() {
  const jobId = state.batchId || state.currentJobId;
  if (!jobId) return;
  try {
    const path = state.batchId
      ? '/api/batch/' + encodeURIComponent(jobId) + '/cancel'
      : '/api/jobs/' + encodeURIComponent(jobId) + '/cancel';
    await api(path, { method: 'POST' });
    logLine('■ 已请求取消 ' + jobId, 'l-dim');
  } catch (err) {
    toast('取消失败: ' + err.message, 'err');
  }
}

async function runBatch() {
  let payload;
  try { payload = currentSpecPayload(); }
  catch (err) { toast(err.message, 'err'); return; }

  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.accept = '.xyz,.cjson,.json';
  input.addEventListener('change', async () => {
    const files = Array.from(input.files || []);
    if (files.length < 2) { toast('批量至少需要两个结构', 'err'); return; }
    try {
      const molecules = await Promise.all(files.map((file) => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = async () => {
          try {
            const r = await api('/api/structure/text', {
              method: 'POST',
              body: JSON.stringify({ text: reader.result, name: file.name.replace(/\.[^.]+$/, '') }),
            });
            resolve(r.molecule);
          } catch (e) { reject(e); }
        };
        reader.onerror = () => reject(new Error('读取失败: ' + file.name));
        reader.readAsText(file);
      })));

      const body = Object.assign({}, payload, { molecules });
      delete body.molecule;
      delete body.geometry_from_job;
      const data = await api('/api/batch', { method: 'POST', body: JSON.stringify(body) });
      state.batchId = data.batch_id;
      $('btn-cancel').disabled = false;
      logLine('');
      logLine(`▶ 批量提交 ${molecules.length} 个结构 (${data.batch_id})`, 'l-head');
      toast('批量任务已提交，完成后会显示相对能量表', 'ok');
      await refreshJobs();
      startJobWatch();
    } catch (err) {
      toast('批量提交失败: ' + err.message, 'err');
    }
  });
  input.click();
}

// ------------------------------------------------------------------ 结果渲染

/**
 * 任务结论横幅：这个结果能不能用？
 *
 * 判据全部在后端算（``Result.verdict`` / ``verdict_reason`` / ``issues``）——
 * 前端**不重新推导一套**，否则两边迟早就判得不一致。
 */
const VERDICT_META = {
  ok: { icon: '✓', cls: 'ok' },
  limited: { icon: '!', cls: 'limited' },
  unusable: { icon: '✕', cls: 'unusable' },
};
const SEVERITY_ORDER = { blocking: 0, major: 1, minor: 2 };
const SEVERITY_LABEL = { blocking: '不可用', major: '需注意', minor: '提示' };

function verdictBanner(result) {
  // 后端没给结论（老的结果文件）时按状态兜底
  const verdict = result.verdict || (result.status === 'done' ? 'limited' : 'unusable');
  const meta = VERDICT_META[verdict] || VERDICT_META.limited;
  // 展示时去掉 markdown 的星号：这里是纯文本渲染，留着会显示成字面量
  const plain = (text) => esc(String(text || '').replace(/\*\*/g, ''));
  const issues = (result.issues || [])
    .slice()
    .sort((a, b) => (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9));

  // 理由往往**就是**第一条严重问题的原文（verdict_reason 取的正是它），
  // 两条都渲染就会出现一模一样的重复段落 —— 去重后再决定要不要列清单
  const reason = String(result.verdict_reason || '');
  const extra = issues.filter((i) => {
    const text = String(i.message || '').replace(/\*\*/g, '');
    return text.replace(/\s/g, '') !== reason.replace(/\*\*/g, '').replace(/\s/g, '');
  });
  const detail = extra.length
    ? `<ul class="res-verdict-list">${extra.map((i) =>
      `<li><b>${esc(SEVERITY_LABEL[i.severity] || i.severity)}</b> ${plain(i.message)}</li>`
    ).join('')}</ul>`
    : '';

  return `<div class="res-verdict ${meta.cls}">
    <div class="res-verdict-head">
      <span class="res-verdict-icon">${meta.icon}</span>
      <strong>${esc(result.verdict_label || '任务结论')}</strong>
    </div>
    <p class="res-verdict-why">${plain(result.verdict_reason)}</p>
    ${detail}
  </div>`;
}

function renderResult(job, result) {
  const body = $('result-body');
  if (!result) {
    body.innerHTML = `<p class="empty">任务状态：${esc(job.status)}${job.message ? ' — ' + esc(job.message) : ''}</p>`;
    return;
  }

  const parts = [];

  // **结论横幅放在最前面** —— 这是用户唯一一定会看到的地方。
  // 本项目已有三次教训都属于同一类：算错了却显示绿色成功
  // （静默跳过的 NBO、消除成丁二烯却报 done 的扫描、跑满 50 圈没收敛的过渡态）。
  // 以前只列"有分析被跳过"，把"分子已经变了"这种更严重的事也混在同一档里。
  parts.push(verdictBanner(result));

  parts.push(`<div class="res-section"><h4>概要</h4><dl class="res-grid">
    <dt>状态</dt><dd class="${result.status === 'done' ? 'hi' : ''}">${esc(result.status)}</dd>
    <dt>引擎</dt><dd>${esc(result.engine)} ${esc(result.engine_version)}</dd>
    <dt>方法</dt><dd>${esc(result.method)}</dd>
    <dt>能量 (Ha)</dt><dd class="hi">${result.final_energy !== null && result.final_energy !== undefined ? result.final_energy.toFixed(10) : '—'}</dd>
    <dt>基函数 / 电子</dt><dd>${result.n_basis_functions ?? '—'} / ${result.n_electrons ?? '—'}</dd>
    ${result.timings && result.timings.SUM !== undefined ? `<dt>耗时</dt><dd>${Number(result.timings.SUM).toFixed(1)} s</dd>` : ''}
  </dl></div>`);

  const t = result.thermo || {};
  if (Object.keys(t).length) {
    const kjm = 2625.4996394799, jmolk = 4.3597447222071e-18 * 6.02214076e23;
    const row = (label, key, factor, unit) => (t[key] !== undefined
      ? `<dt>${label}</dt><dd>${Number(t[key]).toFixed(6)} Ha · ${(t[key] * factor).toFixed(2)} ${unit}</dd>` : '');
    parts.push(`<div class="res-section"><h4>热化学 ${t.temperature_K ? '· ' + t.temperature_K + ' K' : ''}</h4><dl class="res-grid">
      ${row('零点能 ZPE', 'zpe_hartree', kjm, 'kJ/mol')}
      ${row('内能 U', 'inner_energy_U_hartree', kjm, 'kJ/mol')}
      ${row('焓 H', 'enthalpy_H_hartree', kjm, 'kJ/mol')}
      ${row('自由能 G', 'gibbs_free_energy_G_hartree', kjm, 'kJ/mol')}
      ${t.entropy_S_hartree_per_K !== undefined
        ? `<dt>熵 S</dt><dd>${(t.entropy_S_hartree_per_K * jmolk).toFixed(2)} J/(mol·K)</dd>` : ''}
    </dl></div>`);
  }

  if (result.frequencies && result.frequencies.length) {
    const real = result.frequencies.filter((f) => Math.abs(f) > 1e-6);
    parts.push(`<div class="res-section"><h4>振动频率 (cm⁻¹) · 虚频 ${result.n_imaginary_frequencies ?? 0}</h4>
      <div class="freq-list">${real.map((f) => `<span class="${f < 0 ? 'imag' : ''}">${f.toFixed(1)}</span>`).join('')}</div></div>`);

    // 红外谱图区：仅当任务有频率时才出现（没有频率的任务不占位置）。
    // 只有一列数字看不出谱形，而"算完频率直接看谱图"正是 GaussView 最常见的用法。
    // 真实数据由 /spectrum 异步取（图由后端 Pillow 画，前端不引任何绘图库）。
    parts.push(spectrumSectionShell());
  }

  const irc = result.irc || {};
  if (irc.n_frames) {
    parts.push(`<div class="res-section"><h4>IRC 反应路径</h4><dl class="res-grid">
      <dt>轨迹帧数</dt><dd>${irc.n_frames}</dd>
      ${irc.ts_frame_index !== undefined ? `<dt>过渡态帧</dt><dd>#${irc.ts_frame_index}</dd>` : ''}
      ${(irc.barriers_kcal || []).length === 2
        ? `<dt>两端能垒 (kcal/mol)</dt><dd class="hi">${irc.barriers_kcal[0].toFixed(2)} / ${irc.barriers_kcal[1].toFixed(2)}</dd>` : ''}
    </dl></div>`);
  }

  const charges = (result.loewdin_charges && result.loewdin_charges.length)
    ? result.loewdin_charges : (result.mulliken_charges || []);
  if (charges.length && result.geometry) {
    // 限高滚动：24 个原子的电荷表会把下面真正重要的轨道/偶极推到看不见
    parts.push(`<div class="res-section"><h4>原子电荷 (Löwdin)</h4><div class="table-scroll"><table class="data">
      <tr><th>原子</th><th style="text-align:right">电荷</th></tr>
      ${result.geometry.symbols.map((s, i) => `<tr><td>${i + 1} ${esc(s)}</td><td class="num">${charges[i] !== undefined ? charges[i].toFixed(4) : '—'}</td></tr>`).join('')}
    </table></div></div>`);
  }

  if (result.dipole && result.dipole.magnitude !== null && result.dipole.magnitude !== undefined) {
    parts.push(`<div class="res-section"><h4>偶极矩</h4><dl class="res-grid">
      <dt>总偶极 (D)</dt><dd class="hi">${Number(result.dipole.magnitude).toFixed(4)}</dd></dl></div>`);
  }

  if (result.orbitals && result.orbitals.count) {
    parts.push(orbitalSection(result, job.job_id));
  }

  if (result.files && Object.keys(result.files).length) {
    parts.push(`<div class="res-section"><h4>产出文件</h4><table class="data">
      ${Object.keys(result.files).map((role) => `<tr><td>${esc(role)}</td>
        <td style="text-align:right"><a class="link" href="/api/jobs/${encodeURIComponent(job.job_id)}/file/${encodeURIComponent(role)}" target="_blank">查看</a>
        · <a class="link" href="/api/jobs/${encodeURIComponent(job.job_id)}/file/${encodeURIComponent(role)}?download=1">下载</a></td></tr>`).join('')}
    </table></div>`);
  }

  body.innerHTML = parts.join('');
  bindOrbitalButtons();
  loadScanSection(job.job_id);      // 扫描任务才有数据，其它任务静默跳过
  loadGeometrySection();            // 几何参数表（结果几何优先，其次载入的结构）
  loadBondOrderSection(job.job_id); // 键级要手动点「开始分析」才跑
  loadOrbCompSection(job.job_id);   // 轨道成分同样要手动点（Multiwfn 读波函数）
  loadOverlaySection(job.job_id);   // 叠加要手动点「与当前结构叠加」才跑
  loadSpectrumSection(job.job_id);  // 频率/谱图区，其它任务静默跳过
}

function renderBatch(payload) {
  const body = $('result-body');
  if (!payload) return;
  const items = (payload.items || []).slice().sort((a, b) => {
    const av = a.relative_kj === null || a.relative_kj === undefined ? Infinity : a.relative_kj;
    const bv = b.relative_kj === null || b.relative_kj === undefined ? Infinity : b.relative_kj;
    return av - bv;
  });
  const minV = Math.min.apply(null, items.map((i) => (i.relative_kj === null || i.relative_kj === undefined ? Infinity : i.relative_kj)));
  body.innerHTML = `<div class="res-section"><h4>批量相对能量 · ${esc(payload.quantity_label || payload.quantity || '')}</h4>
    <dl class="res-grid">
      <dt>结构数</dt><dd>${items.length}</dd>
      <dt>参考（最低）</dt><dd class="hi">${esc(payload.reference_name || '—')}</dd>
    </dl>
    <table class="data" style="margin-top:8px">
      <tr><th>结构</th><th style="text-align:right">Δ kJ/mol</th><th style="text-align:right">Δ kcal/mol</th><th>状态</th></tr>
      ${items.map((it) => `<tr class="${it.relative_kj === minV ? 'min' : ''}">
        <td>${esc(it.name)}</td>
        <td class="num">${it.relative_kj !== null && it.relative_kj !== undefined ? it.relative_kj.toFixed(2) : '—'}</td>
        <td class="num">${it.relative_kcal !== null && it.relative_kcal !== undefined ? it.relative_kcal.toFixed(2) : '—'}</td>
        <td>${esc(it.status)}</td></tr>`).join('')}
    </table></div>`;
}

// ------------------------------------------------------------------ 轨迹

async function loadTrajectory(jobId) {
  try {
    const data = await api('/api/jobs/' + encodeURIComponent(jobId) + '/irc');
    const frames = data.frames || [];
    if (!frames.length) { $('trajectory').hidden = true; return; }
    state.viewer.setTrajectory(frames);
    const range = $('range-frame');
    range.max = String(frames.length - 1);
    range.value = '0';
    $('label-frame').textContent = '1 / ' + frames.length;
    $('trajectory').hidden = false;

    // 能量剖面：优先用后端算好的逐帧能量
    profile.energies = (data.frame_energies && data.frame_energies.length)
      ? data.frame_energies
      : frames.map((f) => f.energy);
    profile.tsIndex = (data.ts_frame_index === undefined || data.ts_frame_index === null)
      ? null : data.ts_frame_index;
    profile.current = 0;
    $('irc-profile').hidden = false;
    drawProfile();

    logLine(`已载入 IRC 轨迹：${frames.length} 帧` +
      (profile.tsIndex !== null ? `，过渡态在第 ${profile.tsIndex} 帧` : ''), 'l-dim');
  } catch (err) {
    $('trajectory').hidden = true;
  }
}

// ------------------------------------------------------------------ IRC 能量剖面

const HARTREE_TO_KCAL = 627.5094740631;
const HARTREE_TO_EV = 27.211386245988;
const profile = { energies: [], tsIndex: null, current: 0 };

function drawProfile() {
  const canvas = $('irc-profile');
  if (!canvas || canvas.hidden) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 400;
  const h = canvas.clientHeight || 92;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const values = profile.energies;
  if (!values.length) return;

  const padL = 46, padR = 12, padT = 12, padB = 16;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);
  const min = Math.min.apply(null, values);
  const rel = values.map((v) => (v - min) * HARTREE_TO_KCAL);
  const maxRel = Math.max(1e-6, Math.max.apply(null, rel));

  const X = (i) => padL + (values.length === 1 ? plotW / 2 : (i / (values.length - 1)) * plotW);
  const Y = (v) => padT + plotH - (v / maxRel) * plotH;

  // 面积
  ctx.beginPath();
  ctx.moveTo(X(0), padT + plotH);
  rel.forEach((v, i) => ctx.lineTo(X(i), Y(v)));
  ctx.lineTo(X(rel.length - 1), padT + plotH);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
  grad.addColorStop(0, 'rgba(47,143,224,.45)');
  grad.addColorStop(1, 'rgba(47,143,224,.03)');
  ctx.fillStyle = grad;
  ctx.fill();

  // 曲线
  ctx.beginPath();
  rel.forEach((v, i) => (i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v))));
  ctx.strokeStyle = '#59b0f5';
  ctx.lineWidth = 1.6;
  ctx.stroke();

  // 过渡态标记
  if (profile.tsIndex !== null && profile.tsIndex < rel.length) {
    const tx = X(profile.tsIndex), ty = Y(rel[profile.tsIndex]);
    ctx.strokeStyle = 'rgba(232,178,58,.7)';
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(tx, padT); ctx.lineTo(tx, padT + plotH); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#e8b23a';
    ctx.beginPath(); ctx.arc(tx, ty, 3.2, 0, Math.PI * 2); ctx.fill();
    ctx.font = '10px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('TS', tx, padT + 9);
  }

  // 当前帧游标
  const ci = Math.round(profile.current);
  if (ci >= 0 && ci < rel.length) {
    ctx.strokeStyle = 'rgba(255,255,255,.9)';
    ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.moveTo(X(ci), padT); ctx.lineTo(X(ci), padT + plotH); ctx.stroke();
  }

  // 坐标轴
  ctx.fillStyle = '#64758a';
  ctx.font = '10px ui-monospace, monospace';
  ctx.textAlign = 'right';
  ctx.fillText(maxRel.toFixed(1), padL - 6, padT + 8);
  ctx.fillText('0.0', padL - 6, padT + plotH);
  ctx.textAlign = 'left';
  ctx.font = '10px system-ui, sans-serif';
  ctx.fillText('kcal/mol', 5, padT + 8);
  ctx.textAlign = 'center';
  ctx.fillText('反应坐标 →', padL + plotW / 2, h - 4);
}

function profileFrameAt(clientX) {
  const canvas = $('irc-profile');
  const rect = canvas.getBoundingClientRect();
  const padL = 46, padR = 12;
  const plotW = Math.max(10, rect.width - padL - padR);
  const t = (clientX - rect.left - padL) / plotW;
  return Math.max(0, Math.min(profile.energies.length - 1, Math.round(t * (profile.energies.length - 1))));
}

// ------------------------------------------------------------------ 分子轨道

/** 能级图（SVG）：占据轨道实线、空轨道虚线，标出 HOMO/LUMO 与间隙。

 *  只画价层窗口 —— 把 80 个轨道连内层一起塞进 250px 会挤成一条黑带，
 *  而化学上要看的本来就是前线区域。
 */
function orbitalDiagram(orb) {
  const all = (orb.orbitals || []).filter((o) => o.energy !== null);
  if (!all.length) return '';

  let list = all;
  let omitted = 0;
  if (orb.homo_ev !== undefined && orb.lumo_ev !== undefined) {
    const lo = orb.homo_ev - 16;
    const hi = orb.lumo_ev + 9;
    const kept = all.filter((o) => {
      const ev = o.energy * HARTREE_TO_EV;
      return ev >= lo && ev <= hi;
    });
    if (kept.length >= 3 && kept.length < all.length) {
      list = kept;
      omitted = all.length - kept.length;
    }
  }

  const W = 330;
  const H = Math.max(200, Math.min(520, 60 + list.length * 11));
  const pad = { l: 10, r: 74, t: 16, b: 22 };
  const plotH = H - pad.t - pad.b;
  const energies = list.map((o) => o.energy * HARTREE_TO_EV);
  const emin = Math.min.apply(null, energies);
  const emax = Math.max.apply(null, energies);
  const span = Math.max(0.5, emax - emin);
  const Y = (ev) => pad.t + (1 - (ev - emin) / span) * plotH;

  const homoIndex = orb.homo, lumoIndex = orb.lumo;
  // 窗口裁剪后 HOMO/LUMO 理论上一定还在，但仍要防住 null，否则整个结果面板会崩
  const homoOrb = list.find((o) => o.index === homoIndex);
  const lumoOrb = list.find((o) => o.index === lumoIndex);
  const parts = [];
  parts.push(`<svg class="orb-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}">`);

  // 间隙底纹
  if (homoOrb && lumoOrb) {
    const yh = Y(homoOrb.energy * HARTREE_TO_EV);
    const yl = Y(lumoOrb.energy * HARTREE_TO_EV);
    parts.push(`<rect x="0" y="${Math.min(yh, yl).toFixed(1)}" width="${pad.l + 8}" height="${Math.abs(yl - yh).toFixed(1)}" fill="rgba(232,178,58,.10)"/>`);
  }

  for (const o of list) {
    const ev = o.energy * HARTREE_TO_EV;
    const y = Y(ev).toFixed(1);
    const occupied = (o.occupancy || 0) > 1e-6;
    let color = occupied ? '#59b0f5' : '#7f8fa6';
    if (o.index === homoIndex) color = '#e8b23a';
    if (o.index === lumoIndex) color = '#7ee2b8';
    const dash = occupied ? '' : ' stroke-dasharray="5 3"';
    parts.push(`<line x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}" stroke="${color}" stroke-width="${o.index === homoIndex || o.index === lumoIndex ? 2.6 : 1.6}"${dash}/>`);
    // 占据示意：闭壳层画一对箭头
    if (occupied) {
      parts.push(`<text x="${pad.l + 6}" y="${(parseFloat(y) - 3).toFixed(1)}" fill="${color}" font-size="9">↑↓</text>`);
    }
    parts.push(`<text x="${W - pad.r + 6}" y="${(parseFloat(y) + 3.5).toFixed(1)}" fill="#94a6bb" font-size="10" font-family="ui-monospace, monospace">${ev.toFixed(2)}</text>`);
    parts.push(`<text x="${pad.l - 0}" y="${(parseFloat(y) - 3).toFixed(1)}" fill="#64758a" font-size="9">#${o.index}${o.index === homoIndex ? ' HOMO' : o.index === lumoIndex ? ' LUMO' : ''}</text>`);
  }

  // HOMO-LUMO 间隙标注
  if (orb.gap_ev !== undefined && homoOrb && lumoOrb) {
    const yh = Y(homoOrb.energy * HARTREE_TO_EV);
    const yl = Y(lumoOrb.energy * HARTREE_TO_EV);
    const xm = W - pad.r - 24;
    parts.push(`<line x1="${xm}" y1="${yh.toFixed(1)}" x2="${xm}" y2="${yl.toFixed(1)}" stroke="#e8b23a" stroke-width="1"/>`);
    parts.push(`<text x="${xm - 4}" y="${((yh + yl) / 2 + 3).toFixed(1)}" fill="#e8b23a" font-size="9.5" text-anchor="end">${orb.gap_ev.toFixed(2)} eV</text>`);
  }

  const note = omitted
    ? `轨道能级 (eV)　价层窗口内 ${list.length} 个，另有 ${omitted} 个深层/高位轨道未画出`
    : `轨道能级 (eV)　共 ${orb.count} 个，占据 ${orb.n_electrons} 电子`;
  parts.push(`<text x="${pad.l}" y="${H - 6}" fill="#64758a" font-size="9.5">${note}</text>`);
  parts.push('</svg>');
  return parts.join('');
}

/** 只列前线轨道附近，避免上百个轨道把面板撑爆。 */
function frontierOrbitals(orb, span = 4) {
  const list = orb.orbitals || [];
  const homo = orb.homo, lumo = orb.lumo;
  if (homo === null && lumo === null) return list.slice(0, 12);
  const lo = Math.max(0, (homo === null ? lumo : homo) - span);
  const hi = Math.min(list.length - 1, (lumo === null ? homo : lumo) + span);
  return list.slice(lo, hi + 1);
}

function orbitalSection(result, jobId) {
  const orb = result.orbitals || {};
  if (!orb.count) return '';

  const rows = frontierOrbitals(orb).map((o) => {
    const occupied = (o.occupancy || 0) > 1e-6;
    const tag = o.index === orb.homo ? 'HOMO' : o.index === orb.lumo ? 'LUMO' : '';
    const cls = o.index === orb.homo ? 'min' : '';
    return `<tr class="${cls}">
      <td>#${o.index} <span class="muted">${tag}</span></td>
      <td class="num">${o.energy === null ? '—' : (o.energy * HARTREE_TO_EV).toFixed(3)}</td>
      <td class="num">${o.occupancy === null ? '—' : o.occupancy.toFixed(2)}</td>
      <td>${esc(o.symmetry || '')}</td>
      <td style="text-align:right">
        <button class="btn ghost small" data-orbital="${o.index}" data-job="${esc(jobId)}">导出 cube</button>
      </td></tr>`;
  }).join('');

  const gap = orb.gap_ev !== undefined
    ? `<dt>HOMO–LUMO 间隙</dt><dd class="hi">${orb.gap_ev.toFixed(2)} eV</dd>` : '';
  const wf = (result.files && result.files.wavefunction)
    ? `<p class="hint mono">波函数: ${esc(String(result.files.wavefunction).split(/[\\/]/).pop())}</p>`
    : '<p class="hint">该任务未保存波函数（.gbw），无法导出轨道 cube。</p>';

  return `<div class="res-section"><h4>分子轨道</h4>
    <dl class="res-grid">
      <dt>轨道数</dt><dd>${orb.count}</dd>
      <dt>电子数</dt><dd>${orb.n_electrons}</dd>
      <dt>点群</dt><dd>${esc(orb.point_group || '—')}</dd>
      ${gap}
    </dl>
    ${orbitalDiagram(orb)}
    <table class="data" style="margin-top:6px">
      <tr><th>轨道</th><th style="text-align:right">能量 (eV)</th><th style="text-align:right">占据</th><th>对称</th><th></th></tr>
      ${rows}
    </table>
    ${wf}
    <div class="row" style="margin-top:8px">
      <button class="btn ghost small" id="btn-open-multiwfn" data-job="${esc(jobId)}">用 Multiwfn 打开轨道</button>
      <button class="btn ghost small" id="btn-open-avogadro-mo" data-job="${esc(jobId)}">用 Avogadro2 打开 cube</button>
    </div>
    <p class="hint">cube 由 <code>orca_plot</code> 从已有 .gbw 生成，不重跑 SCF；网格默认 50³，可在导出后调整。</p>
  </div>`;
}

// ------------------------------------------------------------------ 扫描曲线

// 曲线数据不在 result.json 里（解析要读整个输出文件），按需向后端要一次。
// 只有真是扫描任务才画，其它任务静默不显示这一节。
state.scanMode = 'energy';
state.scanIndex = null;      // 当前预览的是第几个扫描点（null = 没在预览）
state.scanProfile = null;
//: selectJob 的请求序号：防止慢响应后到把结果面板覆盖成旧任务
state.jobToken = 0;

function scanSeries(profile, mode) {
  const pts = profile.points || [];
  if (mode === 'bond') {
    return (profile.series || []).map((s) => ({
      label: s.label,
      data: pts.map((p, i) => [p.value, s.values[i]])
        .filter((d) => d[1] !== null && d[1] !== undefined),
    })).filter((s) => s.data.length);
  }
  return [{ label: '相对能量', data: pts.map((p) => [p.value, p.rel_kcal]) }];
}

// 图表几何：拖动时要靠这几个常量把鼠标位置换算回数据坐标，所以提出来共用
const SCAN_CHART = { W: 330, H: 186, L: 46, R: 10, T: 12, B: 32 };
const SCAN_IW = SCAN_CHART.W - SCAN_CHART.L - SCAN_CHART.R;

function scanDiagram(profile, mode, active) {
  const series = scanSeries(profile, mode);
  if (!series.length) return '<p class="hint">没有可画的数据</p>';

  const { W, H, L, R, T, B } = SCAN_CHART;
  const iw = SCAN_IW, ih = H - T - B;
  const all = series.flatMap((s) => s.data.map((d) => d[1]));
  const xs = (profile.points || []).map((p) => p.value);
  if (!all.length || !xs.length) return '';

  let y0 = Math.min.apply(null, all), y1 = Math.max.apply(null, all);
  if (y1 - y0 < 1e-9) { y0 -= 0.5; y1 += 0.5; }
  const pad = (y1 - y0) * 0.14;
  y0 -= pad; y1 += pad;
  const x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);

  const px = (v) => L + (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0)) * iw;
  const py = (v) => T + ih - (v - y0) / (y1 - y0) * ih;
  const isBond = mode === 'bond';
  const fmt = (v) => (isBond ? v.toFixed(3) : v.toFixed(2));

  let svg = `<svg class="scan-svg" viewBox="0 0 ${W} ${H}" role="img">`;

  // 横向网格 + y 刻度
  for (let k = 0; k <= 3; k++) {
    const v = y0 + (y1 - y0) * k / 3, y = py(v);
    svg += `<line x1="${L}" y1="${y.toFixed(1)}" x2="${L + iw}" y2="${y.toFixed(1)}"
      stroke="#dfe3e8" stroke-width="1" stroke-dasharray="3 4"/>`;
    svg += `<text x="${L - 5}" y="${(y + 3).toFixed(1)}" class="scan-tick"
      text-anchor="end">${fmt(v)}</text>`;
  }
  // x 刻度（最多 6 个）
  const step = Math.max(1, Math.ceil(xs.length / 6));
  for (let i = 0; i < xs.length; i += step) {
    const x = px(xs[i]);
    svg += `<line x1="${x.toFixed(1)}" y1="${T + ih}" x2="${x.toFixed(1)}" y2="${T + ih + 4}"
      stroke="#98a2b0" stroke-width="1"/>`;
    svg += `<text x="${x.toFixed(1)}" y="${T + ih + 15}" class="scan-tick"
      text-anchor="middle">${xs[i]}</text>`;
  }
  svg += `<line x1="${L}" y1="${T}" x2="${L}" y2="${T + ih}" stroke="#98a2b0" stroke-width="1.2"/>`;
  svg += `<line x1="${L}" y1="${T + ih}" x2="${L + iw}" y2="${T + ih}" stroke="#98a2b0" stroke-width="1.2"/>`;

  // 曲线
  const colors = ['#2f6fd0', '#8e44ad', '#2ea078'];
  series.forEach((s, k) => {
    const d = s.data.map((p, i) => (i ? 'L' : 'M') + px(p[0]).toFixed(1) + ' ' + py(p[1]).toFixed(1)).join(' ');
    svg += `<path d="${d}" fill="none" stroke="${colors[k % 3]}" stroke-width="2"
      ${k ? 'stroke-dasharray="5 3"' : ''} stroke-linejoin="round"/>`;
    s.data.forEach((p) => {
      svg += `<circle cx="${px(p[0]).toFixed(1)}" cy="${py(p[1]).toFixed(1)}" r="2.2"
        fill="${colors[k % 3]}"/>`;
    });
    if (isBond) {
      const last = s.data[s.data.length - 1];
      svg += `<text x="${(px(last[0]) - 4).toFixed(1)}" y="${(py(last[1]) - 5).toFixed(1)}"
        class="scan-tag" text-anchor="end" fill="${colors[k % 3]}">${esc(s.label)}</text>`;
    }
  });

  // 势垒标注
  if (!isBond && profile.barrier_kcal !== undefined) {
    const x = px(profile.max_value);
    svg += `<line x1="${x.toFixed(1)}" y1="${py(0).toFixed(1)}" x2="${x.toFixed(1)}"
      y2="${py(profile.barrier_kcal).toFixed(1)}" stroke="#c0392b" stroke-width="1.4"/>`;
    svg += `<text x="${(x + 5).toFixed(1)}" y="${((py(0) + py(profile.barrier_kcal)) / 2).toFixed(1)}"
      class="scan-barrier" fill="#c0392b">${profile.barrier_kcal.toFixed(2)}</text>`;
  }

  // 当前构象的游标（拖动时更新，不重画整个图）
  const hasActive = Number.isInteger(active) && active >= 0 && active < xs.length;
  const ax = hasActive ? px(xs[active]) : 0;
  const ay = hasActive ? py(profile.points[active].rel_kcal) : 0;
  svg += `<g class="scan-cursor" ${hasActive ? '' : 'hidden'}>
    <line x1="${ax.toFixed(1)}" y1="${T}" x2="${ax.toFixed(1)}" y2="${T + ih}"
      stroke="#e8b23a" stroke-width="1.4"/>
    <circle cx="${ax.toFixed(1)}" cy="${ay.toFixed(1)}" r="4.4"
      fill="#e8b23a" stroke="#1b2430" stroke-width="1.2"/>
  </g>`;

  svg += `<text x="${L + iw / 2}" y="${H - 3}" class="scan-tick" text-anchor="middle">${
    esc(profile.coordinate || '扫描坐标')} (${esc(profile.unit || '')})</text>`;
  svg += `</svg>`;
  return svg;
}

function scanSection(profile, jobId) {
  const bonds = profile.series || [];
  const modes = [`<button class="btn ghost small" data-scan-mode="energy" data-job="${esc(jobId)}">能量曲线</button>`];
  if (bonds.length) {
    modes.push(`<button class="btn ghost small" data-scan-mode="bond" data-job="${esc(jobId)}">键长响应</button>`);
  }
  const warn = (profile.warnings || []).map((w) => `<p class="hint warn">${esc(w)}</p>`).join('');
  return `<div class="res-section" id="scan-section">
    <h4>扫描曲线 · ${esc(profile.coordinate || '')}</h4>
    <div class="row scan-modes">${modes.join('')}</div>
    <div id="scan-chart">${scanDiagram(profile, state.scanMode)}</div>
    <p class="hint" id="scan-readout">在曲线上<b>按住拖动</b>可以逐点预览构象，中栏会跟着变。</p>
    <dl class="res-grid">
      <dt>扫描点数</dt><dd>${(profile.points || []).length}</dd>
      <dt>势垒</dt><dd class="hi">${(profile.barrier_kcal ?? 0).toFixed(3)} kcal/mol · ${(profile.barrier_kj ?? 0).toFixed(2)} kJ/mol</dd>
      <dt>极小 / 极大</dt><dd>${profile.min_value}${esc(profile.unit || '')} / ${profile.max_value}${esc(profile.unit || '')}</dd>
    </dl>
    <div class="row" style="margin-top:6px">
      <button class="btn ghost small" id="btn-scan-restore" data-job="${esc(jobId)}">恢复任务结构</button>
    </div>
    <p class="hint">能量取自 <code>${esc(profile.energy_file || '')}</code>；曲线不重跑计算，直接解析已有输出。</p>
    ${warn}
  </div>`;
}

/**
 * 把扫描曲线上的某一点显示出来：移动游标、更新读数、把该点几何送进 3D 查看器。
 *
 * 这是"拖动势能图看构象怎么变"的核心 —— 和 IRC 的逐帧播放是同一个思路，
 * 只是扫描的点少（十几到几十个），直接 drag 比进度条更顺手。
 */
function showScanFrame(profile, index, section) {
  const pts = profile.points || [];
  const geoms = profile.geometries || [];
  if (!pts.length) return;
  index = Math.max(0, Math.min(index, pts.length - 1));
  state.scanIndex = index;      // 切换图表模式时要能恢复到这个点

  // 游标
  const svg = section.querySelector('.scan-svg');
  const xs = pts.map((p) => p.value);
  const x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
  const iw = SCAN_IW;
  const px = (v) => SCAN_CHART.L + (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0)) * iw;
  // 游标 y 必须取**当前模式对应的那条曲线**的值。
  // 以前一律用 pts[index].rel_kcal —— 在「键长响应」模式下图的 y 轴是键长，
  // 拿能量去换算会让圆点落到所有曲线之外（实测偏离最近数据点 37px，审计 F5）。
  const series = scanSeries(profile, state.scanMode);
  const yvals = series.flatMap((s) => s.data.map((d) => d[1]));
  let y0 = Math.min.apply(null, yvals), y1 = Math.max.apply(null, yvals);
  if (!yvals.length || y1 - y0 < 1e-9) { y0 -= 0.5; y1 += 0.5; }
  const pad = (y1 - y0) * 0.14;
  y0 -= pad; y1 += pad;
  const py = (v) => SCAN_CHART.T + (SCAN_CHART.H - SCAN_CHART.T - SCAN_CHART.B)
    - (v - y0) / (y1 - y0) * (SCAN_CHART.H - SCAN_CHART.T - SCAN_CHART.B);

  if (svg) {
    const cursor = svg.querySelector('.scan-cursor');
    if (cursor) {
      // 取当前模式第一条曲线在这个 x 上的值；找不到就退回能量
      const first = series[0];
      const hit = first ? first.data.find((d) => d[0] === pts[index].value) : null;
      const yValue = hit ? hit[1] : pts[index].rel_kcal;
      const x = px(pts[index].value), y = py(yValue);
      const line = cursor.querySelector('line');
      const dot = cursor.querySelector('circle');
      line.setAttribute('x1', x.toFixed(1)); line.setAttribute('x2', x.toFixed(1));
      dot.setAttribute('cx', x.toFixed(1)); dot.setAttribute('cy', y.toFixed(1));
      cursor.hidden = false;
    }
  }

  // 读数
  const out = section.querySelector('#scan-readout');
  if (out) {
    out.innerHTML = `第 <b>${index + 1}/${pts.length}</b> 点 · ${esc(profile.coordinate || '坐标')} = `
      + `<b>${pts[index].value}${esc(profile.unit || '')}</b> · 相对能量 `
      + `<b>${pts[index].rel_kcal.toFixed(3)} kcal/mol</b>（${(pts[index].rel_kcal * 4.184).toFixed(2)} kJ/mol）`;
  }

  // 3D：把该点的几何换上去
  const geom = geoms[index];
  if (geom && profile.symbols && profile.symbols.length && state.viewer) {
    state.viewer.setMolecule({ symbols: profile.symbols, coords: geom.coords });
    state.viewer.render();
  }
}

/** 在曲线上按下/拖动 → 选最近的那个扫描点。 */
function attachScanDrag(section, profile) {
  const svg = section.querySelector('.scan-svg');
  const pts = profile.points || [];
  if (!svg || pts.length < 2 || !(profile.geometries || []).length) return;

  const xs = pts.map((p) => p.value);
  const x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
  let dragging = false;

  const indexAt = (clientX) => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return 0;
    // 先把客户端坐标换算回 viewBox 坐标，再换算成数据值
    const vx = (clientX - rect.left) * (SCAN_CHART.W / rect.width);
    const t = Math.max(0, Math.min(1, (vx - SCAN_CHART.L) / SCAN_IW));
    const value = x0 + t * (x1 - x0);
    let best = 0, bestD = Infinity;
    xs.forEach((v, i) => {
      const d = Math.abs(v - value);
      if (d < bestD) { bestD = d; best = i; }
    });
    return best;
  };

  const move = (ev) => { if (dragging) showScanFrame(profile, indexAt(ev.clientX), section); };

  svg.addEventListener('pointerdown', (ev) => {
    dragging = true;
    if (svg.setPointerCapture) { try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* 忽略 */ } }
    svg.classList.add('dragging');
    showScanFrame(profile, indexAt(ev.clientX), section);
    ev.preventDefault();
  });
  svg.addEventListener('pointermove', move);
  const stop = () => { dragging = false; svg.classList.remove('dragging'); };
  svg.addEventListener('pointerup', stop);
  svg.addEventListener('pointercancel', stop);
  svg.addEventListener('pointerleave', stop);
}

// ------------------------------------------------------------------ 几何参数表

/** 三张表的定义。顺序就是界面上的切换顺序。 */
const GEOM_KINDS = [
  ['bonds', '键长', 'Å'],
  ['angles', '键角', '°'],
  ['dihedrals', '二面角', '°'],
];

/**
 * 该拿哪套几何来列表：**优先当前任务的结果几何**，其次当前载入的结构。
 *
 * 为什么是这个优先级：用户点开一个任务，想看的就是**这次算出来的**键长键角
 * （优化前后差多少正是要比较的东西）；没有任务结果时才退回左栏载入的结构。
 * 表头会把来源写出来，免得用户以为看的是另一套。
 */
function geometryForTable() {
  const geom = state.result && state.result.geometry;
  if (geom && geom.symbols && geom.symbols.length && geom.coords) {
    return { symbols: geom.symbols, coords: geom.coords, source: 'result' };
  }
  const mol = state.molecule;
  if (mol && mol.symbols && mol.symbols.length && mol.coords) {
    return { symbols: mol.symbols, coords: mol.coords, source: 'structure' };
  }
  return null;
}

/** 一套几何的指纹：避免同一套几何被重复算两遍、插出两块表。 */
function geometrySignature(source) {
  let hash = 2166136261;
  const push = (text) => {
    for (let i = 0; i < text.length; i++) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
  };
  push(source.source + '|' + source.symbols.join(','));
  for (const c of source.coords) {
    push(`${Number(c[0]).toFixed(4)},${Number(c[1]).toFixed(4)},${Number(c[2]).toFixed(4)}`);
  }
  return (hash >>> 0).toString(16);
}

function geometryShell(source) {
  const tabs = GEOM_KINDS.map(([key, label]) =>
    `<button data-geom="${key}" class="${key === state.geomKind ? 'active' : ''}">${label}`
    + `<i class="geom-count" data-count="${key}">…</i></button>`).join('');
  const from = source.source === 'result' ? '任务结果' : '当前载入的结构';
  return `<div class="res-section" id="geometry-section">
    <h4>几何参数 <span class="muted" id="geom-source">来源：${esc(from)}</span></h4>
    <div class="segmented geom-tabs" id="geom-tabs">${tabs}</div>
    <div class="table-scroll geom-scroll"><table class="data geom-table">
      <thead><tr><th>#</th><th>原子</th><th class="geom-value-head">值</th></tr></thead>
      <tbody id="geom-rows"><tr class="geom-none"><td colspan="3">计算中…</td></tr></tbody>
    </table></div>
    <p class="hint" id="geom-pick">点一行 → 在 3D 里高亮这几个原子</p>
  </div>`;
}

/** 把（已经拿到或刚拿到的）参数画进表里。切换标签、排序都走这里。 */
function renderGeometryTable() {
  const section = document.getElementById('geometry-section');
  const data = state.geometry;
  if (!section || !data) return;

  GEOM_KINDS.forEach(([key]) => {
    const counter = section.querySelector(`[data-count="${key}"]`);
    if (counter) counter.textContent = String((data[key] || []).length);
  });
  section.querySelectorAll('[data-geom]').forEach((btn) =>
    btn.classList.toggle('active', btn.dataset.geom === state.geomKind));

  const kind = GEOM_KINDS.find(([key]) => key === state.geomKind) || GEOM_KINDS[0];
  const rows = (data[kind[0]] || []).slice().sort((a, b) => a.value - b.value);
  const tbody = section.querySelector('#geom-rows');
  const pick = section.querySelector('#geom-pick');

  if (!rows.length) {
    tbody.innerHTML = `<tr class="geom-none"><td colspan="3">这套几何里没有${kind[1]}</td></tr>`;
    if (pick) pick.textContent = `共 0 条${kind[1]}`;
  } else {
    // 按值排序（后端已经排好，这里再排一次是按值显示这件事的直接保证：
    // 用户看到的第一行必须是最小的那个值）
    tbody.innerHTML = rows.map((row, index) => {
      const digits = row.unit === 'Å' ? 3 : 2;
      return `<tr class="geom-row" tabindex="0" data-atoms="${row.atoms.join(',')}"`
        + ` data-label="${esc(row.label)}" data-value="${row.value}" data-unit="${esc(row.unit)}">`
        + `<td class="geom-idx">${index + 1}</td>`
        + `<td class="geom-atoms">${esc(row.label)}</td>`
        + `<td class="geom-value">${row.value.toFixed(digits)}`
        + `<span class="geom-unit">${esc(row.unit)}</span></td></tr>`;
    }).join('');
    if (pick) {
      pick.textContent = `共 ${rows.length} 条，按值从小到大 · 点一行在 3D 里高亮涉及的原子`;
    }
  }
  // 表换了内容，上一行的高亮就不再有对应关系了
  state.viewer.highlightAtoms([]);
}

/** 表里的这套几何，3D 里现在显示的**就是**它吗？（编号对不对得上的前提） */
function viewerShowsGeometry(source) {
  const v = state.viewer;
  if (!v || v.symbols.length !== source.symbols.length) return false;
  for (let i = 0; i < source.coords.length; i++) {
    const a = v.coords[i];
    const b = source.coords[i];
    if (!a || !b) return false;
    if (Math.abs(a[0] - b[0]) > 1e-6
      || Math.abs(a[1] - b[1]) > 1e-6
      || Math.abs(a[2] - b[2]) > 1e-6) return false;
  }
  return true;
}

/**
 * 点一行：在 3D 查看器里高亮这几个原子。
 *
 * 两个必须处理的细节：
 *  * 表里的原子编号只有在**表与 3D 是同一套几何**时才成立。不是同一套
 *    （例如表用的是任务结果、查看器还停在载入的结构）就先把查看器切过去 ——
 *    否则高亮会指到别的原子上，而那正是本项目反复栽的一类错误。
 *  * 再点一次同一行 = 取消高亮，不用再去别处找"清除"按钮。
 */
function selectGeometryRow(row) {
  const source = state.geomSource;
  if (!source || !row) return;
  if (!viewerShowsGeometry(source)) {
    state.viewer.setMolecule({ symbols: source.symbols, coords: source.coords });
    $('viewer-empty').classList.add('hide');
    $('viewer-caption').textContent =
      `${source.source === 'result' ? '任务结果几何' : (state.molecule && state.molecule.name) || '结构'}`
      + ` · ${source.symbols.length} 原子`;
  }
  const active = row.classList.contains('active');
  const label = row.dataset.label || '';
  const digits = row.dataset.unit === 'Å' ? 3 : 2;
  const value = `${Number(row.dataset.value).toFixed(digits)} ${row.dataset.unit}`;
  if (active) {
    state.viewer.highlightAtoms([]);
    row.classList.remove('active');
    $('geom-pick').textContent = `已取消高亮（${label} = ${value}）· 点一行重新高亮`;
    return;
  }
  const atoms = (row.dataset.atoms || '').split(',').filter((s) => s !== '').map(Number);
  const drawn = state.viewer.highlightAtoms(atoms);
  row.parentNode.querySelectorAll('tr.geom-row.active')
    .forEach((other) => other.classList.remove('active'));
  row.classList.add('active');
  $('geom-pick').textContent =
    `已高亮 ${label} = ${value}（${drawn} 个原子，青色圈）· 再点一次取消`;
  logLine(`几何参数：高亮 ${label} = ${value}`, 'l-dim');
}

function bindGeometryTable(node) {
  node.querySelectorAll('[data-geom]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.geomKind = btn.dataset.geom;
      renderGeometryTable();
    });
  });
  // 绑在 tbody 上：行是整批 innerHTML 换掉的，绑行本身会在切换标签后失效
  const tbody = node.querySelector('#geom-rows');
  tbody.addEventListener('click', (ev) => {
    const row = ev.target.closest('tr.geom-row');
    if (row) selectGeometryRow(row);
  });
  tbody.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    const row = ev.target.closest('tr.geom-row');
    if (!row) return;
    ev.preventDefault();
    selectGeometryRow(row);
  });
}

/**
 * 把「几何参数」区挂到结果面板上。
 *
 * 位置：结论横幅（和扫描曲线）之后、其它概要信息之前 —— 这是用户最常看的
 * 一张表，不能像结论横幅那次一样被推到滚动区下面（"DOM 里有但看不见"）。
 *
 * 数据是**后端算的**：键长/键角/二面角用的是与引擎同一套、已经校准过的几何
 * 实现（`qcgui/model/geometry.py`），前端不另写一份数学 —— 两边各算一遍
 * 迟早会不一致。这里只负责把哪套几何发过去、把结果画出来。
 */
async function loadGeometrySection() {
  const body = $('result-body');
  const old = document.getElementById('geometry-section');
  const source = geometryForTable();
  if (!source) {
    if (old) old.remove();
    return;
  }
  const signature = geometrySignature(source);
  // 同一套几何已经算过、表还在 → 不重复请求（selectJob 里结果几何会被设两次）
  if (old && state.geomSig === signature && old.querySelector('tr.geom-row')) return;

  const token = ++state.geomToken;
  if (old) old.remove();
  const holder = document.createElement('div');
  holder.innerHTML = geometryShell(source);
  const node = holder.firstElementChild;
  // 有结果时紧跟结论横幅；只有载入的结构时，让"选中一个任务查看结果"那句
  // 留在上面（先说明面板是干什么的，再给表），不要抢到它前面去
  const anchor = body.querySelector('.res-verdict, .res-warning')
    || body.querySelector('p.empty');
  if (anchor) anchor.after(node);
  else body.prepend(node);
  bindGeometryTable(node);

  let data;
  try {
    data = await api('/api/geometry/parameters', {
      method: 'POST',
      body: JSON.stringify({ symbols: source.symbols, coords: source.coords }),
    });
  } catch (err) {
    // 明确报出来，而不是留一块空表 —— "点了没反应"是本项目最不想要的
    node.querySelector('#geom-rows').innerHTML =
      `<tr class="geom-none"><td colspan="3">几何参数计算失败：${esc(err.message)}</td></tr>`;
    logLine('✖ 几何参数计算失败：' + err.message, 'l-err');
    return;
  }
  // 期间用户可能已经切到别的任务（同 F2：过期的响应必须丢掉，
  // 否则面板里会出现另一个任务的键长）
  if (token !== state.geomToken) return;
  state.geometry = data;
  state.geomSource = source;
  state.geomSig = signature;
  renderGeometryTable();
}

/**
 * 键级分析（Multiwfn）—— 补上"化学解释"这块能力。
 *
 * Mayer / Wiberg / Mulliken / FBO / LBO 键级**都不需要买 NBO**，
 * 用已装好的 Multiwfn 就能做。键级能直接看出成键强弱、离域程度、
 * 哪些键在反应中变弱。
 *
 * 刻意**不自动跑**：Multiwfn 要读波函数、建积分，大分子上并不便宜，
 * 而多数时候用户并不需要它。给一个明确的按钮，点了才算。
 */
function bondOrderSection() {
  const kinds = [['mayer', 'Mayer 键级'], ['wiberg', 'Wiberg 键级（Löwdin）'],
                 ['mulliken', 'Mulliken 键级'], ['fuzzy', '模糊键级 FBO'],
                 ['lbo', 'Laplacian 键级 LBO']];
  return `<div class="res-section" id="bondorder-section">
    <div class="res-section-head">键级分析 <span class="muted">Multiwfn</span></div>
    <div class="bo-controls">
      <select id="bo-kind">${kinds.map(([v, t]) =>
        `<option value="${v}">${esc(t)}</option>`).join('')}</select>
      <button class="btn small" id="bo-run">开始分析</button>
      <span class="muted" id="bo-hint">键级看的是成键强弱与离域，不需要 NBO</span>
    </div>
    <div id="bo-body"></div>
  </div>`;
}

/** 把键级表画出来。空结果不该走到这里（失败会先渲染成错误态）。 */
function renderBondOrders(data) {
  const top = data.orders[0];
  const rows = data.orders.map((o) => {
    // 相对最强键的比例条，扫一眼就能看出哪根键弱
    const pct = Math.max(2, Math.round((o.order / Math.max(0.001, top.order)) * 100));
    return `<tr><td class="bo-label">${esc(o.label)}</td>`
      + `<td class="bo-num">${o.order.toFixed(3)}</td>`
      + `<td class="bo-bar"><i style="width:${pct}%"></i></td></tr>`;
  }).join('');
  const warn = data.problems && data.problems.length
    ? `<div class="bo-note">${data.problems.map((p) => esc(p)).join('；')}</div>` : '';
  return `<div class="bo-summary">共 <b>${data.count}</b> 条（阈值 ${data.threshold ?? '—'}）`
    + `，最强 <b>${esc(top.label)}</b> = ${top.order.toFixed(3)}</div>`
    + `<table class="bo-table"><thead><tr><th>键</th><th>键级</th><th>相对强弱</th></tr></thead>`
    + `<tbody>${rows}</tbody></table>${warn}`;
}

/**
 * 把键级区挂到结果面板上。
 *
 * 插入顺序必须是 **结论横幅 → 扫描曲线 → 几何参数 → 键级**：
 * 结论横幅永远最上面（那是"这个结果能不能用"），扫描曲线是扫描任务的头号结果，
 * 几何参数表是最常看的通用信息。
 * 这几个顺序都踩过坑（结论横幅被扫描区挤到滚动区下面），所以这里显式避让。
 */
function loadBondOrderSection(jobId) {
  const body = $('result-body');
  const old = document.getElementById('bondorder-section');
  if (old) old.remove();

  const holder = document.createElement('div');
  holder.innerHTML = bondOrderSection();
  const node = holder.firstElementChild;
  const anchor = document.getElementById('geometry-section')
    || document.getElementById('scan-section')
    || body.querySelector('.res-verdict, .res-warning');
  if (anchor) anchor.after(node);
  else body.prepend(node);

  const runBtn = node.querySelector('#bo-run');
  const kindSel = node.querySelector('#bo-kind');
  const out = node.querySelector('#bo-body');

  const run = async () => {
    const kind = kindSel.value;
    runBtn.disabled = true;
    out.innerHTML = '<div class="bo-loading">Multiwfn 分析中…（要读波函数、建积分，'
      + '大分子可能要几十秒）</div>';
    try {
      const data = await api('/api/jobs/' + encodeURIComponent(jobId)
        + '/bondorders?kind=' + encodeURIComponent(kind));
      out.innerHTML = renderBondOrders(data);
      // 记下来供 3D 数值标注用；如果「显示数值」正开着，立刻画上去
      state.bondOrders = data.orders;
      refreshViewerValues();
      logLine(`键级分析完成：${data.kind_label} ${data.count} 条`, 'l-dim');
    } catch (err) {
      // 失败就说清楚为什么（缺 .gbw / 没装 Multiwfn / 格式不认识），
      // 不要留一个"点了没反应"的按钮
      out.innerHTML = `<div class="bo-error">分析失败：${esc(err.message)}</div>`;
      logLine('✖ 键级分析失败：' + err.message, 'l-err');
    } finally {
      runBtn.disabled = false;
    }
  };
  runBtn.addEventListener('click', run);
  kindSel.addEventListener('change', () => {
    if (out.querySelector('.bo-table, .bo-error')) run();
  });
  return node;
}

// ------------------------------------------------------- 轨道成分（Multiwfn 菜单 8）

/**
 * 轨道编号翻译 —— **本功能最容易出错的一处**。
 *
 * Multiwfn 的轨道编号是 **1 起**，ORCA 的 `orca_2json` 是 **0 起**：
 * 咖啡因（80 MO / 102 电子 / 51 个双占据）的 HOMO，Multiwfn 报 `51`、
 * ORCA 报 `homo=50`；LUMO 报 `52` 对 `51`。
 *
 * 直接把 `orbital_index: 51` 当 ORCA 轨道号显示，同一轨道就会在「分子轨道」
 * 表和「轨道成分」表里出现两个号 —— 本项目反复踩过这类"编号对不上"的问题。
 * 所以这里既给出 ORCA 口径的编号，也把 Multiwfn 自己的编号写出来。
 */
function orbitalIndexLabel(orbComp, orb) {
  const mw = orbComp && orbComp.orbital_index;
  if (mw === null || mw === undefined) return '—';
  const orca = mw - 1;
  const tag = orb
    ? (orca === orb.homo ? '（HOMO）' : orca === orb.lumo ? '（LUMO）' : '')
    : '';
  return `#${orca}${tag} <span class="muted">Multiwfn 编号 ${mw}</span>`;
}

function orbCompSection() {
  const partitions = [
    ['mulliken', 'Mulliken 布居'],
    ['stout', 'Stout-Politzer 布居'],
    ['scpa', 'Ros-Schuit (SCPA) 布居'],
  ];
  const orbitals = [['homo', 'HOMO'], ['lumo', 'LUMO'],
                    ['homo-1', 'HOMO−1'], ['lumo+1', 'LUMO+1']];
  return `<div class="res-section" id="orbcomp-section">
    <div class="res-section-head">轨道成分 <span class="muted">Multiwfn</span></div>
    <div class="bo-controls">
      <select id="oc-partition">${partitions.map(([v, t]) =>
        `<option value="${v}">${esc(t)}</option>`).join('')}</select>
      <select id="oc-orbital">${orbitals.map(([v, t]) =>
        `<option value="${v}">${esc(t)}</option>`).join('')}</select>
      <button class="btn small" id="oc-run">分析轨道成分</button>
      <span class="muted" id="oc-hint">看的是这个轨道主要在哪几个原子上，不需要 NBO</span>
    </div>
    <div id="oc-body" class="muted">尚未分析</div>
  </div>`;
}

/**
 * 把轨道成分画出来。
 *
 * 两条必须写清楚的话：
 *  1. Multiwfn 只打印贡献大于阈值的原子，所以百分比**合计不是 100%** ——
 *     不说明的话用户会以为数据缺了一块；
 *  2. `problems` 非空时必须显示出来（例如原始输出没落盘），那是留痕失败的提示，不能藏。
 */
function renderOrbComp(data, orb) {
  const atoms = data.atoms || [];
  const warn = data.problems && data.problems.length
    ? `<div class="bo-note">${data.problems.map((p) => esc(p)).join('；')}</div>` : '';
  if (!atoms.length) {
    // 宁可交白卷也不给半份数据；但 problems 必须一起显示 ——
    // 它是"原始输出没落盘"这类留痕失败的唯一线索，藏起来用户就只剩一句"没结果"。
    return `<div class="bo-error">没有解析出任何原子贡献 —— 宁可交白卷也不给半份数据</div>${warn}`;
  }
  const top = atoms[0];
  const total = atoms.reduce((s, a) => s + (a.percent || 0), 0);
  const rows = atoms.map((a) => {
    // 相对最强原子的比例条：扫一眼就知道第二个是谁
    const pct = Math.max(2, Math.round((a.percent / Math.max(0.001, top.percent)) * 100));
    const shells = (a.shells || []).map((s) => `${esc(s.type)}${s.shell} ${s.percent.toFixed(1)}%`).join('、');
    return `<tr><td class="bo-label">${esc(a.label)}</td>`
      + `<td class="bo-num">${a.percent.toFixed(2)}%</td>`
      + `<td class="bo-bar"><i style="width:${pct}%"></i></td>`
      + `<td class="muted">${shells || '—'}</td></tr>`;
  }).join('');
  const approx = total < 99.99
    ? `，因此合计 ${total.toFixed(2)}%，不是 100%` : '';
  return `<div class="bo-summary">`
    + `${esc(data.request_label)} · 轨道 ${orbitalIndexLabel(data, orb)}`
    + `${data.energy_ev === null || data.energy_ev === undefined ? '' : ` · ${data.energy_ev.toFixed(2)} eV`}`
    + `<br>主要在这几个原子上：<b>${esc(data.top_label)}</b></div>`
    + `<table class="bo-table"><thead><tr><th>原子</th><th>贡献</th><th>相对最强</th><th>shell 明细</th></tr></thead>`
    + `<tbody>${rows}</tbody></table>`
    + `<p class="hint">只列出贡献 &gt; 阈值 ${data.threshold ?? '—'}% 的原子${approx}。</p>`
    + `${warn}`;
}

/**
 * 把轨道成分区挂到结果面板上。
 *
 * 与键级区一样**刻意不自动跑**：Multiwfn 要读波函数、建积分；本次实测咖啡因
 * 只要 0.12 s，但大分子与 `orbital=all` 会明显变慢，不替用户做这个决定。
 */
function loadOrbCompSection(jobId) {
  const body = $('result-body');
  const old = document.getElementById('orbcomp-section');
  if (old) old.remove();
  state.orbComp = null;

  const holder = document.createElement('div');
  holder.innerHTML = orbCompSection();
  const node = holder.firstElementChild;
  // 结论横幅永远最上面；其次扫描曲线、键级，轨道成分排在它们之后
  const anchor = document.getElementById('bondorder-section')
    || document.getElementById('scan-section')
    || body.querySelector('.res-verdict, .res-warning');
  if (anchor) anchor.after(node);
  else body.prepend(node);

  const runBtn = node.querySelector('#oc-run');
  const partSel = node.querySelector('#oc-partition');
  const orbSel = node.querySelector('#oc-orbital');
  const out = node.querySelector('#oc-body');
  partSel.value = state.orbCompPartition;
  orbSel.value = state.orbCompOrbital;

  const run = async () => {
    const partition = partSel.value;
    const orbital = orbSel.value;
    state.orbCompPartition = partition;
    state.orbCompOrbital = orbital;
    runBtn.disabled = true;
    out.innerHTML = '<div class="bo-loading">Multiwfn 分析中…（要读波函数、建积分，'
      + '大分子可能要几十秒）</div>';
    try {
      const data = await api('/api/jobs/' + encodeURIComponent(jobId)
        + '/orbital-composition?partition=' + encodeURIComponent(partition)
        + '&orbital=' + encodeURIComponent(orbital));
      const orb = (state.result && state.result.orbitals) || null;
      out.innerHTML = renderOrbComp(data, orb);
      state.orbComp = data;
      logLine(`轨道成分：${data.partition_label} ${data.request_label} `
        + `${data.count} 个原子（${data.top_label}）`, 'l-dim');
    } catch (err) {
      // 缺 .gbw / 没装 Multiwfn / 格式不认识 —— 说清原因，别留一个"点了没反应"的按钮
      out.innerHTML = `<div class="bo-error">分析失败：${esc(err.message)}</div>`;
      logLine('✖ 轨道成分分析失败：' + err.message, 'l-err');
    } finally {
      runBtn.disabled = false;
    }
  };
  runBtn.addEventListener('click', run);
  [partSel, orbSel].forEach((sel) => sel.addEventListener('change', () => {
    if (out.querySelector('.bo-table, .bo-error')) run();
  }));
  return node;
}

/** `?octest=<job>`：真浏览器里的自检钩子，比照 `runBondOrderUiTest`。 */
async function runOrbCompUiTest(jobId) {
  const out = document.createElement('pre');
  out.id = 'octest-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;'
    + 'color:#0f0;background:#000;padding:4px;max-width:96vw;white-space:pre-wrap';
  document.body.appendChild(out);
  const lines = [];
  try {
    const node = loadOrbCompSection(jobId);
    lines.push(`section=${node ? 'PASS' : 'FAIL'}`);
    if (node) {
      // 位置：结论横幅必须在轨道成分**之前**（本项目为这件事吃过一次亏）
      const verdict = document.querySelector('.res-verdict, .res-warning');
      lines.push(`verdict_before_orbcomp=${
        verdict && (verdict.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING)
          ? 'PASS' : 'FAIL'}`);
      const body = $('result-body');
      lines.push(`order=${Array.from(body.children).map((n) => n.id || n.className).join('>')}`);
      const rect = node.getBoundingClientRect();
      lines.push(`visible=${rect.width > 40 && rect.height > 10 ? 'PASS' : 'FAIL'}`
        + ` w=${Math.round(rect.width)} h=${Math.round(rect.height)}`);

      $('oc-run').click();
      const table = await new Promise((resolve) => {
        const deadline = setTimeout(() => resolve(null), 180000);
        const poll = setInterval(() => {
          const t = node.querySelector('.bo-table');
          const e = node.querySelector('.bo-error');
          if (t || e) { clearInterval(poll); clearTimeout(deadline); resolve(t || e); }
        }, 150);
      });
      if (!table) {
        lines.push('table=TIMEOUT');
      } else if (table.classList.contains('bo-error')) {
        lines.push(`table=ERROR ${table.textContent.slice(0, 160)}`);
      } else {
        const rows = table.querySelectorAll('tbody tr');
        lines.push(`table=PASS rows=${rows.length}`);
        lines.push(`summary="${(node.querySelector('.bo-summary') || {}).textContent || ''}"`
          .replace(/\s+/g, ' '));
        const hint = node.querySelector('.hint');
        lines.push(`hint="${hint ? hint.textContent.replace(/\s+/g, ' ').slice(0, 140) : ''}"`);
        const bar = table.querySelector('.bo-bar i');
        lines.push(`bar=${bar && parseFloat(bar.style.width) > 0 ? 'PASS' : 'FAIL'}`);
      }
      lines.push(`button_reenabled=${$('oc-run').disabled ? 'FAIL' : 'PASS'}`);
    }
  } catch (err) {
    lines.push(`ERROR ${err && err.message}`);
  }
  out.textContent = '__OCTEST__' + lines.join(' | ') + '__END__';
}

/**
 * 「显示数值」：把电荷 / 键级**直接画在 3D 图上**。
 *
 * 为什么值得做：表格里有数据，但你得按编号去和 3D 图对 ——
 * 而"编号对错"正是本项目反复踩过的一类问题（原子顺序、扫描下标）。
 * 数字直接画在原子上，就不用对编号了。
 *
 * 两个来源：
 *  * 原子电荷来自 ``state.result``（优先 Löwdin，其次 Mayer）；
 *  * 键级来自最近一次键级分析 —— 没分析过就没有键级，只画电荷。
 *    Multiwfn 给的是 **1 起**编号，这里要减一（查看器是 0 起）。
 */
function refreshViewerValues() {
  const btn = $('btn-values');
  if (!btn.classList.contains('active')) {
    state.viewer.setValueDisplay(null);
    return;
  }
  const charges = state.result
    ? (state.result.loewdin_charges && state.result.loewdin_charges.length
        ? state.result.loewdin_charges : state.result.mayer_charges) : null;

  const bonds = (state.bondOrders || []).map((o) => ({
    i: o.i - 1, j: o.j - 1, text: o.order.toFixed(2),
  }));

  if (!charges || !charges.length) {
    // 没有电荷就只画键级；两个都没有时要明确告诉用户为什么没东西
    if (bonds.length) {
      state.viewer.setValueDisplay('bondorder', null, bonds);
    } else {
      state.viewer.setValueDisplay(null);
      toast('这个任务没有可显示的数值（没有电荷数据，也还没做键级分析）', 'warn');
    }
    return;
  }
  state.viewer.setValueDisplay('charge', charges, bonds.length ? bonds : null);
}

async function loadScanSection(jobId) {
  const body = $('result-body');
  let data;
  try {
    data = await api('/api/jobs/' + encodeURIComponent(jobId) + '/scan');
  } catch (err) {
    return;                                   // 不是扫描任务，静默跳过
  }
  if (!data || !data.points || !data.points.length) return;

  // 放在结果面板**最前面**：扫描任务里曲线就是头号结果，
  // 追加到末尾的话会被挤到滚动区下面，等于没显示。
  //
  // 但要**避让结论横幅**：`.res-verdict` 是用户唯一一定会看的东西
  // （这个结果到底能不能用），必须留在最上面。以前只避让 `.res-warning`，
  // 结果扫描区把结论横幅压到了滚动区下面 —— 截图才发现。
  const holder = document.createElement('div');
  holder.innerHTML = scanSection(data, jobId);
  const node = holder.firstElementChild;
  const top = body.querySelector('.res-verdict, .res-warning');
  if (top) top.after(node);
  else body.prepend(node);

  // 切模式只重画图，游标和拖动都要重新挂（innerHTML 换掉了 SVG）
  const redraw = () => {
    $('scan-chart').innerHTML = scanDiagram(data, state.scanMode, state.scanIndex);
    attachScanDrag(node, data);
    if (Number.isInteger(state.scanIndex)) showScanFrame(data, state.scanIndex, node);
  };
  node.querySelectorAll('[data-scan-mode]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.scanMode === state.scanMode);
    btn.addEventListener('click', () => {
      state.scanMode = btn.dataset.scanMode;
      node.querySelectorAll('[data-scan-mode]').forEach((b) =>
        b.classList.toggle('active', b.dataset.scanMode === state.scanMode));
      redraw();
    });
  });

  const restore = node.querySelector('#btn-scan-restore');
  if (restore) {
    restore.addEventListener('click', async () => {
      state.scanIndex = null;
      await selectJob(jobId);               // 重新载入任务自带的几何
      toast('已恢复任务结构');
    });
  }

  state.scanProfile = data;
  state.scanIndex = null;
  attachScanDrag(node, data);
}

async function exportOrbital(button) {
  const jobId = button.dataset.job;
  const mo = button.dataset.orbital;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = '导出中…';
  try {
    const res = await api(`/api/jobs/${encodeURIComponent(jobId)}/orbitals/${mo}/cube`,
      { method: 'POST', body: JSON.stringify({ grid: 60 }) });
    toast(res.message, 'ok');
    logLine('✔ ' + res.message, 'l-ok');
    button.textContent = '下载 cube';
    button.disabled = false;
    button.onclick = () => window.open(res.url, '_blank');
  } catch (err) {
    toast('导出失败: ' + err.message, 'err');
    logLine('✖ 轨道导出失败: ' + err.message, 'l-err');
    button.textContent = original;
    button.disabled = false;
  }
}

/** 用 Multiwfn 打开：先把 .gbw 转成 Molden（Multiwfn 能读），再交给它。 */
async function openOrbitalsInMultiwfn(jobId) {
  try {
    const made = await api(`/api/jobs/${encodeURIComponent(jobId)}/molden`, { method: 'POST' });
    if (!made.ok) { toast(made.message, 'err'); return; }
    const res = await api('/api/external', {
      method: 'POST',
      body: JSON.stringify({ tool: 'multiwfn', job_id: jobId, role: 'molden' }),
    });
    toast(res.message, res.ok ? 'ok' : 'err');
    logLine((res.ok ? '✔ ' : '✖ ') + res.message, res.ok ? 'l-ok' : 'l-err');
  } catch (err) {
    toast('Multiwfn 调用失败: ' + err.message, 'err');
  }
}

/** 用 Avogadro2 打开轨道 cube（Avogadro 能直接渲染等值面）。 */
async function openOrbitalsInAvogadro(jobId) {
  const res0 = await api(`/api/jobs/${encodeURIComponent(jobId)}/orbitals`);
  const cubes = Object.keys(res0.cubes || {});
  if (!cubes.length) { toast('还没有导出任何轨道 cube，请先点某个轨道的「导出 cube」', 'err'); return; }
  const role = cubes[cubes.length - 1];
  try {
    const res = await api('/api/external', {
      method: 'POST',
      body: JSON.stringify({ tool: 'avogadro', job_id: jobId, role }),
    });
    toast(res.message, res.ok ? 'ok' : 'err');
  } catch (err) {
    toast('Avogadro2 调用失败: ' + err.message, 'err');
  }
}

function bindOrbitalButtons() {
  document.querySelectorAll('[data-orbital]').forEach((button) => {
    button.addEventListener('click', () => exportOrbital(button));
  });
  const mw = $('btn-open-multiwfn');
  if (mw) mw.addEventListener('click', () => openOrbitalsInMultiwfn(mw.dataset.job));
  const av = $('btn-open-avogadro-mo');
  if (av) av.addEventListener('click', () => openOrbitalsInAvogadro(av.dataset.job));
}

// ------------------------------------------------------------------ SSE

function connectEvents() {
  const source = new EventSource('/api/events');
  // SSE 重连说明中间漏过事件（服务重启/网络抖动），立刻补一次，
  // 否则任务列表会停在旧状态。
  source.onopen = () => {
    $('conn-state').textContent = '实时连接中';
    $('conn-state').className = 'conn on';
    refreshJobs().then(startJobWatch);
  };
  source.onerror = () => {
    $('conn-state').textContent = '连接断开，重试中…';
    $('conn-state').className = 'conn off';
    // SSE 断了之后 finished 事件就收不到了 —— 「运行」按钮会永久禁用。
    // 这里用与 SSE 无关的轮询兜底恢复（审计 F1）。
    if (state.currentJobId) watchRunButton(state.currentJobId);
  };

  source.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    const kind = data.event;

    if (kind === 'hello') return;

    if (kind === 'log') {
      const line = data.line || '';
      let cls = '';
      if (/ERROR|Error:|错误|失败|❌/.test(line)) cls = 'l-err';
      else if (/✅|完成|NORMALLY/.test(line)) cls = 'l-ok';
      else if (/^\s*$/.test(line)) cls = '';
      logLine(line, cls);
      return;
    }

    if (kind === 'running') {
      logLine('  worker 已启动 · ' + (data.job_dir || ''), 'l-dim');
      refreshJobs();
      return;
    }

    if (kind === 'finished') {
      logLine((data.status === 'done' ? '✔ 任务完成' : '✖ 任务结束: ' + data.status), data.status === 'done' ? 'l-ok' : 'l-err');
      $('btn-run').disabled = false;
      $('btn-cancel').disabled = true;
      state.currentJobId = null;
      refreshJobs().then(() => selectJob(data.job_id));
      return;
    }

    if (kind === 'error' || kind === 'failed') {
      logLine('✖ ' + (data.message || ''), 'l-err');
      $('btn-run').disabled = false;
      $('btn-cancel').disabled = true;
      refreshJobs();
      return;
    }

    if (kind === 'batch_item') {
      const item = data.item || {};
      logLine(`    [${item.status}] ${item.name}` + (item.value !== null && item.value !== undefined ? '  ' + item.value.toFixed(8) + ' Ha' : ''), 'l-dim');
      return;
    }

    if (kind === 'batch_done') {
      logLine('✔ 批量计算完成', 'l-ok');
      $('btn-run').disabled = false;
      $('btn-cancel').disabled = true;
      state.batchId = null;
      renderBatch(data.batch);
      refreshJobs();
      if (data.batch && data.batch.saved && data.batch.saved.csv) {
        logLine('  表格: ' + data.batch.saved.csv, 'l-dim');
      }
      return;
    }

    if (kind === 'batch_failed') {
      logLine('✖ 批量失败: ' + (data.message || ''), 'l-err');
      $('btn-run').disabled = false;
      $('btn-cancel').disabled = true;
      state.batchId = null;
      return;
    }
  };
}

// ------------------------------------------------------------------ 交互绑定

function bindUi() {
  $('btn-run').addEventListener('click', runJob);
  $('btn-cancel').addEventListener('click', cancelJob);
  $('btn-batch').addEventListener('click', runBatch);
  $('select-recipe').addEventListener('change', () => { applyRecipe(); updateScanUI(); });
  $('select-scan').addEventListener('change', updateScanUI);
  $('input-scan-atoms').addEventListener('input', updateScanUI);
  $('btn-fav').addEventListener('click', toggleFavorite);
  $('btn-values').addEventListener('click', (e) => {
    e.currentTarget.classList.toggle('active');
    refreshViewerValues();
  });
  $('btn-refresh').addEventListener('click', async () => {
    try { applyStatus(await api('/api/status')); } catch (e) { /* ignore */ }
    await refreshJobs();
    toast('已刷新');
  });

  $('btn-open').addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.xyz,.cjson,.json';
    input.addEventListener('change', () => { if (input.files[0]) readLocalFile(input.files[0]); });
    input.click();
  });

  $('example-select').addEventListener('change', (e) => {
    if (e.target.value) loadStructureFromPath(e.target.value);
  });

  $('btn-smiles').addEventListener('click', async () => {
    const smiles = prompt('输入 SMILES：');
    if (!smiles) return;
    try {
      const data = await api('/api/structure/smiles', { method: 'POST', body: JSON.stringify({ smiles }) });
      state.moleculePath = null;
      setMolecule(data.molecule);
    } catch (err) { toast('生成失败: ' + err.message, 'err'); }
  });

  // 拖放
  const dz = $('dropzone');
  dz.addEventListener('click', () => $('btn-open').click());
  ['dragenter', 'dragover'].forEach((evt) => dz.addEventListener(evt, (e) => {
    e.preventDefault(); dz.classList.add('hover');
  }));
  ['dragleave', 'drop'].forEach((evt) => dz.addEventListener(evt, (e) => {
    e.preventDefault(); dz.classList.remove('hover');
  }));
  dz.addEventListener('drop', (e) => {
    const file = e.dataTransfer.files[0];
    if (file) readLocalFile(file);
  });
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer && e.dataTransfer.files[0];
    if (file) readLocalFile(file);
  });

  // 查看器控制
  $('seg-style').addEventListener('click', (e) => {
    const btn = e.target.closest('button'); if (!btn) return;
    $('seg-style').querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
    state.viewer.setStyle(btn.dataset.style);
  });
  $('seg-color').addEventListener('click', (e) => {
    const btn = e.target.closest('button'); if (!btn) return;
    $('seg-color').querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
    state.viewer.setColorMode(btn.dataset.color);
  });
  $('btn-labels').addEventListener('click', (e) => {
    const on = !e.target.classList.contains('active');
    e.target.classList.toggle('active', on);
    state.viewer.setLabels(on);
  });
  $('btn-autorotate').addEventListener('click', (e) => {
    const on = !e.target.classList.contains('active');
    e.target.classList.toggle('active', on);
    state.viewer.setAutoRotate(on);
    if (on) spin(); else cancelAnimationFrame(state._spin);
  });
  $('btn-reset').addEventListener('click', () => state.viewer.resetView());

  // 轨迹
  $('range-frame').addEventListener('input', (e) => {
    state.viewer.pause();
    $('btn-play').textContent = '▶ 播放轨迹';
    state.viewer.setFrame(parseFloat(e.target.value));
  });
  // 能量剖面：点击/拖动直接跳帧
  const prof = $('irc-profile');
  let scrubbing = false;
  const scrubTo = (clientX) => {
    const index = profileFrameAt(clientX);
    $('range-frame').value = String(index);
    state.viewer.pause();
    $('btn-play').textContent = '▶ 播放轨迹';
    state.viewer.setFrame(index);
  };
  prof.addEventListener('mousedown', (e) => { scrubbing = true; scrubTo(e.clientX); e.preventDefault(); });
  window.addEventListener('mousemove', (e) => { if (scrubbing) scrubTo(e.clientX); });
  window.addEventListener('mouseup', () => { scrubbing = false; });
  $('btn-play').addEventListener('click', () => {
    if (state.viewer.playing) {
      state.viewer.pause();
      $('btn-play').textContent = '▶ 播放轨迹';
    } else {
      $('btn-play').textContent = '⏸ 暂停';
      state.viewer.play(14);
      const sync = () => {
        if (!state.viewer.playing) return;
        $('range-frame').value = String(state.viewer.framePos);
        requestAnimationFrame(sync);
      };
      sync();
    }
  });

  // 外部工具
  document.querySelectorAll('[data-external]').forEach((btn) => {
    btn.addEventListener('click', () => openExternal(btn.dataset.external));
  });
  $('btn-open-folder').addEventListener('click', () => openExternal('explorer'));

  // 日志
  $('btn-clear-log').addEventListener('click', () => { $('log').textContent = ''; });

  // 设置
  const dialog = $('dialog-settings');
  $('btn-settings').addEventListener('click', () => dialog.showModal());
  $('btn-save-settings').addEventListener('click', async (e) => {
    e.preventDefault();
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({
          orca_path: $('cfg-orca').value.trim(),
          multiwfn_path: $('cfg-multiwfn').value.trim(),
          avogadro_path: $('cfg-avogadro').value.trim(),
          obabel_path: $('cfg-obabel').value.trim(),
          web_port: parseInt($('cfg-web_port').value || '8760', 10),
          auto_open_browser: $('cfg-auto_open_browser').checked,
        }),
      });
      dialog.close();
      applyStatus(await api('/api/status'));
      toast('设置已保存', 'ok');
    } catch (err) { toast('保存失败: ' + err.message, 'err'); }
  });

  $('btn-shutdown').addEventListener('click', async () => {
    if (!confirm('确定关闭本地服务吗？正在运行的任务会被取消。')) return;
    try { await api('/api/shutdown', { method: 'POST' }); } catch (e) { /* 服务正在退出 */ }
    document.body.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;height:100vh;' +
      'color:#94a6bb;font-family:system-ui;font-size:14px">服务已关闭，可以关闭此标签页。</div>';
  });
}

function spin() {
  const step = () => {
    state.viewer.rotY += 0.006;
    state.viewer._schedule();
    state._spin = requestAnimationFrame(step);
  };
  cancelAnimationFrame(state._spin);
  step();
}

async function openExternal(tool) {
  const payload = { tool };
  // 后端按工具挑它真能读的文件（Multiwfn 读不了 .gbw、Avogadro 读不了 .inp，
  // 以前这里硬传 role 正是"点了没反应"的原因）
  if (state.selectedJob) {
    payload.job_id = state.selectedJob;
  } else if (state.moleculePath) {
    payload.path = state.moleculePath;
  }
  if (!payload.job_id && !payload.path) {
    toast('请先选中一个任务，或从文件载入一个结构', 'err');
    return;
  }

  try {
    const res = await api('/api/external', { method: 'POST', body: JSON.stringify(payload) });
    if (res.ok) { toast(res.message, 'ok'); logLine('✔ ' + res.message, 'l-ok'); }
    else { toast(res.message, 'err'); logLine('✖ ' + res.message, 'l-err'); }
  } catch (err) {
    toast('调用失败: ' + err.message, 'err');
  }
}

document.addEventListener('DOMContentLoaded', init);

// ---------------------------------------------------------------- 结构叠加 RMSD

/**
 * 选一个已完成任务，叠到**当前显示的结构**上，并报对齐前 / 对齐后两个 RMSD。
 *
 * 为什么两个数都要看：对齐前大、对齐后小 → 两个结构其实一样，只是摆放不同；
 * 对齐后仍然大 → 构象真的不同（这才是"优化前后差多少"的答案）。
 * 只给一个数的话，用户没法分辨这两种完全不同的情况。
 *
 * 计算全在后端（``GET /api/jobs/<id>/overlay/<other>``），前端只负责画 ——
 * Kabsch 只能有一份实现，前端再写一遍迟早和后面报的数不一致。
 */
function overlaySection(jobId) {
  const options = state.jobs
    .filter((j) => j.status === 'done' && j.job_id !== jobId)
    .slice(0, 80)
    .map((j) => `<option value="${esc(j.job_id)}">${esc(j.molecule || j.job_id)}`
      + ` · ${esc(j.job_type)} · ${esc(j.job_id.slice(0, 15))}</option>`)
    .join('');
  return `<div class="res-section" id="overlay-section">
    <div class="res-section-head">结构叠加 RMSD <span class="muted">Kabsch 最优叠加</span></div>
    <div class="ov-controls">
      <select id="ov-other" class="grow">${options || '<option value="">没有其它已完成任务</option>'}</select>
      <button class="btn small" id="ov-run"${options ? '' : ' disabled'}>与当前结构叠加</button>
      <button class="btn ghost small" id="ov-clear" disabled>取消叠加</button>
    </div>
    <div id="ov-body">
      <p class="hint">把另一个已完成任务的结构叠到当前结构上。对齐前 / 对齐后两个
      RMSD 一起看：对齐后明显变小说明差别只是摆放不同；对齐后仍很大说明构象真的不同。
      两个结构的原子数必须相同、元素顺序必须一致（本功能不会自动找对应原子）。</p>
    </div>
  </div>`;
}

function overlayReadout(data) {
  const f = (v) => (v === null || v === undefined ? '—' : Number(v).toFixed(4));
  const drop = data.rmsd_before > 1e-9
    ? (1 - data.rmsd_after / data.rmsd_before) * 100 : 0;
  return `<dl class="res-grid ov-grid">
      <dt>原子数</dt><dd>${data.n_atoms}</dd>
      <dt>对齐前 RMSD</dt><dd class="ov-before">${f(data.rmsd_before)} Å</dd>
      <dt>对齐后 RMSD</dt><dd class="ov-after hi">${f(data.rmsd_after)} Å</dd>
    </dl>
    <p class="hint">参考 <b>${esc(data.a.label)}</b>（原色，不动）· 叠加
      <b>${esc(data.b.label)}</b>（品红半透明、虚线键）</p>
    <p class="hint">${drop > 1
      ? `最优叠加把 RMSD 从 ${f(data.rmsd_before)} Å 降到 ${f(data.rmsd_after)} Å（降 ${drop.toFixed(1)}%）`
      : '两个结构本来就在同一个摆放上，叠加前后没有差别'}</p>`;
}

function updateOverlayLegend(data) {
  const line = $('viewer-overlay-legend');
  if (!line) return;
  if (!data) {
    line.hidden = true;
    line.innerHTML = '';
    return;
  }
  // 用查看器导出的同一个混色函数取色，图例和画布才不会各说各话
  const c = MoleculeViewer.overlayColor([190, 190, 190]);
  line.hidden = false;
  line.innerHTML = `<span class="ov-swatch ref"></span>${esc(data.a.label)}`
    + `　<span class="ov-swatch ghost" style="background:rgb(${c[0]},${c[1]},${c[2]})"></span>`
    + `${esc(data.b.label)} · 对齐前 <b>${Number(data.rmsd_before).toFixed(4)}</b> Å`
    + ` → 对齐后 <b class="hi">${Number(data.rmsd_after).toFixed(4)}</b> Å`;
}

function clearOverlayUi(notify) {
  state.overlay = null;
  if (state.viewer) state.viewer.clearOverlay();
  const line = $('viewer-overlay-legend');
  if (line) { line.hidden = true; line.innerHTML = ''; }
  const body = $('ov-body');
  if (body) body.innerHTML = '<p class="hint">已取消叠加，当前只显示一个结构。</p>';
  const btn = $('ov-clear');
  if (btn) btn.disabled = true;
  if (notify) toast('已取消叠加');
}

async function applyOverlay(jobId, otherId, node) {
  const holder = node || document.getElementById('overlay-section');
  const out = holder ? holder.querySelector('#ov-body') : null;
  if (!otherId) {
    toast('没有可叠加的其它已完成任务', 'warn');
    return null;
  }
  if (out) out.innerHTML = '<div class="bo-loading">读取两个结构并做最优叠加…</div>';
  try {
    const data = await api('/api/jobs/' + encodeURIComponent(jobId)
      + '/overlay/' + encodeURIComponent(otherId));
    state.overlay = data;
    state.viewer.setOverlay({
      symbols: data.b.symbols,
      coords: data.b.aligned_coords,
      label: data.b.label,
    });
    if (out) out.innerHTML = overlayReadout(data);
    updateOverlayLegend(data);
    const clear = holder && holder.querySelector('#ov-clear');
    if (clear) clear.disabled = false;
    logLine(`叠加对比 ${data.a.label} ↔ ${data.b.label}：对齐前 `
      + `${Number(data.rmsd_before).toFixed(4)} Å → 对齐后 `
      + `${Number(data.rmsd_after).toFixed(4)} Å`, 'l-dim');
    return data;
  } catch (err) {
    state.overlay = null;
    state.viewer.clearOverlay();
    updateOverlayLegend(null);
    if (out) out.innerHTML = `<div class="bo-error">叠加失败：${esc(err.message)}</div>`;
    toast('叠加失败: ' + err.message, 'err');
    logLine('✖ 叠加失败：' + err.message, 'l-err');
    return null;
  }
}

/**
 * 把叠加区挂到结果面板上。
 *
 * 插入顺序：结论横幅 → 扫描曲线 → 键级 → 叠加。
 * 结论横幅永远在最上面（那是"这个结果能不能用"），这条顺序本项目已经踩过坑
 * （扫描区把结论横幅挤到滚动区下面，截图才发现），所以这里显式避让。
 */
function loadOverlaySection(jobId) {
  const body = $('result-body');
  const old = document.getElementById('overlay-section');
  if (old) old.remove();

  const holder = document.createElement('div');
  holder.innerHTML = overlaySection(jobId);
  const node = holder.firstElementChild;
  const anchor = document.getElementById('bondorder-section')
    || document.getElementById('scan-section')
    || body.querySelector('.res-verdict, .res-warning');
  if (anchor) anchor.after(node);
  else body.prepend(node);

  node.querySelector('#ov-run').addEventListener('click', () => {
    applyOverlay(jobId, node.querySelector('#ov-other').value, node);
  });
  node.querySelector('#ov-clear').addEventListener('click', () => clearOverlayUi(true));
  return node;
}

/**
 * 真浏览器验证「结构叠加 RMSD」这条链路。
 *
 * 为什么非要真浏览器：本项目已经三次栽在"DOM 里有但屏幕上没有"。这里要证明的
 * 恰恰是**两个结构真的同时画在画布上**，所以除了读数还在 DOM 里，
 * 还要：
 *   * 数画布上实际发出了多少个 ``arc``（每个原子一个球）；
 *     叠加后必须等于"主结构原子数 + 被叠加结构原子数"
 *   * 比较开启叠加前后的画布像素，确认画面**真的变了**
 *   * 把两个结构的投影点都算出来，确认都落在画布可见区内
 *
 * ``badId`` 给了就再跑一遍**失败路径**：选一个原子数/元素顺序不一致的结构
 * 点「与当前结构叠加」，必须显示红色错误，而且不能留下任何叠加数据/图例/
 * 多画的球 —— 项目规矩里最坏的情况就是"算错了还显示绿色成功"。
 */
async function runOverlayUiTest(otherId, badId) {
  const out = document.createElement('pre');
  out.id = 'overtest-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;'
    + 'color:#0f0;background:#000;padding:4px;max-width:96vw;white-space:pre-wrap';
  document.body.appendChild(out);
  const lines = [];
  const canvas = $('viewer');

  /** 统计下一次重绘会画几个球（arc 调用次数）。 */
  const countArcs = () => {
    const proto = Object.getPrototypeOf(canvas.getContext('2d'));
    const original = proto.arc;
    let n = 0;
    proto.arc = function (...args) { n += 1; return original.apply(this, args); };
    try { state.viewer.render(); } finally { proto.arc = original; }
    return n;
  };
  /** 取画布像素快照（用于"画面真的变了"）。 */
  const snapshot = () => {
    state.viewer.render();
    try { return canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data; }
    catch (err) { return null; }
  };
  const changedPixels = (a, b) => {
    if (!a || !b) return -1;
    let n = 0;
    for (let i = 0; i < a.length; i += 4) {
      if (a[i] !== b[i] || a[i + 1] !== b[i + 1] || a[i + 2] !== b[i + 2]) n += 1;
    }
    return n;
  };

  try {
    const section = document.getElementById('overlay-section');
    lines.push(`section=${section ? 'PASS' : 'FAIL'}`);
    if (section) {
      const rect = section.getBoundingClientRect();
      lines.push(`visible=${rect.width > 40 && rect.height > 10 ? 'PASS' : 'FAIL'}`
        + ` w=${Math.round(rect.width)} h=${Math.round(rect.height)}`);
      const bodyKids = Array.from($('result-body').children)
        .map((n) => n.id || n.className);
      lines.push(`order=${bodyKids.join('>')}`);
    }

    const primaryAtoms = state.viewer.symbols.length;
    const arcsBefore = countArcs();
    const shotBefore = snapshot();

    const sel = document.getElementById('ov-other');
    const hasOption = !!(sel && Array.from(sel.options).some((o) => o.value === otherId));
    lines.push(`option_present=${hasOption ? 'PASS' : 'FAIL'}`
      + ` options=${sel ? sel.options.length : 0}`);
    if (sel) sel.value = otherId;
    // 点**真按钮**（不是直接调函数）—— 接线错了这里就会露馅
    document.getElementById('ov-run').click();

    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      const body = document.getElementById('ov-body');
      if (state.overlay && body && /对齐后/.test(body.textContent)) break;
      await new Promise((r) => setTimeout(r, 120));
    }

    const body = document.getElementById('ov-body');
    const text = body ? body.textContent.replace(/\s+/g, ' ').trim() : '';
    lines.push(`readout=${/对齐前/.test(text) && /对齐后/.test(text) ? 'PASS' : 'FAIL'}`);
    lines.push(`readout_text="${text.slice(0, 160)}"`);
    if (!state.overlay) {
      lines.push('ERROR 叠加没有产生数据');
    } else {
      lines.push(`rmsd_before=${Number(state.overlay.rmsd_before).toFixed(6)}`);
      lines.push(`rmsd_after=${Number(state.overlay.rmsd_after).toFixed(6)}`);
      lines.push(`viewer_overlay_atoms=${state.viewer.overlay ? state.viewer.overlay.symbols.length : 0}`);
      lines.push(`viewer_overlay_bonds=${state.viewer.overlayBonds.length}`);
      const legend = document.getElementById('viewer-overlay-legend');
      lines.push(`legend=${legend && !legend.hidden && /对齐后/.test(legend.textContent) ? 'PASS' : 'FAIL'}`);
      lines.push(`legend_text="${legend ? legend.textContent.replace(/\s+/g, ' ').trim().slice(0, 140) : ''}"`);

      const arcsAfter = countArcs();
      const expected = primaryAtoms
        + (state.viewer.overlay ? state.viewer.overlay.symbols.length : 0);
      lines.push(`arcs_before=${arcsBefore} arcs_after=${arcsAfter} expected=${expected}`);
      lines.push(`both_structures_drawn=${arcsAfter === expected && arcsBefore === primaryAtoms
        ? 'PASS' : 'FAIL'}`);

      const shotAfter = snapshot();
      const changed = changedPixels(shotBefore, shotAfter);
      lines.push(`canvas_changed=${changed > 200 ? 'PASS' : 'FAIL'} changed_px=${changed}`);

      // 两个结构的投影点都要落在画布可见区内 —— 这才是"用户看得见"
      const pr = state.viewer._project();
      const inside = (coords) => coords.map((p) => state.viewer._toScreen(
        state.viewer._rotate(p, pr), pr
      )).filter((s) => s[0] >= 0 && s[1] >= 0
        && s[0] <= canvas.clientWidth && s[1] <= canvas.clientHeight).length;
      const inPrimary = inside(state.viewer.coords);
      const inOverlay = inside(state.viewer.overlay.coords);
      lines.push(`on_screen=${inPrimary === primaryAtoms
        && inOverlay === state.viewer.overlay.coords.length ? 'PASS' : 'FAIL'}`
        + ` primary=${inPrimary}/${primaryAtoms} overlay=${inOverlay}/${state.viewer.overlay.coords.length}`);
    }

    // 取消叠加：画面必须回到只有一个结构
    const clearBtn = document.getElementById('ov-clear');
    if (clearBtn) {
      clearBtn.click();
      const arcsCleared = countArcs();
      lines.push(`clear=${state.viewer.overlay === null && arcsCleared === primaryAtoms
        ? 'PASS' : 'FAIL'} arcs=${arcsCleared} expected=${primaryAtoms}`);
    }

    // 失败路径：选一个**不兼容**的结构（原子数或元素顺序不一致）再点一次。
    // 要证明的是"失败时用户看到的是什么"：必须是红色错误，且不能留下任何
    // 叠加数据、图例、或者多画出来的球 —— 否则就是"算错了还显示绿点"。
    if (badId) {
      const sel2 = document.getElementById('ov-other');
      const hasBad = !!(sel2 && Array.from(sel2.options).some((o) => o.value === badId));
      lines.push(`bad_option_present=${hasBad ? 'PASS' : 'FAIL'}`);
      if (sel2) sel2.value = badId;
      document.getElementById('ov-run').click();
      let errNode = null;
      const errDeadline = Date.now() + 30000;
      while (Date.now() < errDeadline) {
        errNode = document.querySelector('#ov-body .bo-error');
        if (errNode) break;
        await new Promise((r) => setTimeout(r, 100));
      }
      const errText = errNode ? errNode.textContent.replace(/\s+/g, ' ').trim() : '';
      lines.push(`error_shown=${errNode ? 'PASS' : 'FAIL'}`);
      lines.push(`error_text="${errText.slice(0, 140)}"`);
      lines.push(`error_mentions_cause=${/原子数|元素顺序/.test(errText) ? 'PASS' : 'FAIL'}`);
      lines.push(`error_no_overlay_data=${state.overlay === null
        && state.viewer.overlay === null ? 'PASS' : 'FAIL'}`);
      const legend2 = document.getElementById('viewer-overlay-legend');
      lines.push(`error_no_legend=${legend2 && legend2.hidden ? 'PASS' : 'FAIL'}`);
      const arcsErr = countArcs();
      lines.push(`error_only_primary_drawn=${arcsErr === primaryAtoms ? 'PASS' : 'FAIL'}`
        + ` arcs=${arcsErr} expected=${primaryAtoms}`);
    }
  } catch (err) {
    lines.push('ERROR ' + (err && err.message ? err.message : String(err)));
  }
  out.textContent = '__OVERTEST__' + lines.join(' | ') + '__END__';
}

// ---------------------------------------------------------------- 红外光谱（IR）

/**
 * 红外谱图区的**空壳**（同步插进结果面板）。
 *
 * 为什么要先插空壳再异步填内容：真数据要从 /spectrum 取（峰表与图共用后端
 * 算出来的同一份数据）。如果等拿到数据才插 DOM，用户点到频率任务时会先看到
 * 面板"闪一下没有谱图"，而且截图/自动化脚本要跟不确定的时序赛跑。
 */
function spectrumSectionShell() {
  return `<div class="res-section" id="spectrum-section">
    <div class="res-section-head">红外谱图 <span class="muted">频率展宽</span></div>
    <div id="spectrum-body"><p class="hint">谱图绘制中…</p></div>
  </div>`;
}

function spectrumSection(data) {
  const rows = (data.peaks || []).map((p) => {
    // 虚频是判断过渡态对不对的头号指标：整行加底色 + 红色"虚频"标签。
    // 缺强度时强度列显示"—"，**不把补出来的 1.0 摆成真实强度**。
    const kindText = (p.imaginary ? '<b class="imag">虚频</b>' : '实频')
      + (p.strongest ? ' · <b class="strong">最强峰</b>' : '');
    return `<tr class="${p.imaginary ? 'imag-row' : ''}">
      <td class="num">${p.wavenumber.toFixed(1)}</td>
      <td class="num">${p.intensity_available ? Number(p.intensity).toFixed(2) : '—'}</td>
      <td>${kindText}</td></tr>`;
  }).join('');

  const unit = data.intensity_unit ? esc(data.intensity_unit) : '—';
  const note = data.note
    ? `<p class="hint warn" id="spec-note">${esc(data.note)}</p>`
    : '<p class="hint" id="spec-note"></p>';
  const imagNote = data.imaginary_count
    ? `<p class="hint warn">检测到 <b>${data.imaginary_count}</b> 个虚频（红色标出）：`
      + '过渡态应当正好 1 个虚频，极小点应当 0 个。</p>'
    : '<p class="hint">没有虚频 —— 这是极小点（过渡态的话应当正好 1 个虚频）。</p>';
  const options = (data.kinds || []).map((k) => `<option value="${esc(k.value)}"`
    + `${k.value === data.kind ? ' selected' : ''}>${esc(k.label)}</option>`).join('');
  const dropped = data.dropped_zero_modes
    ? `（已略去 ${data.dropped_zero_modes} 个平动/转动的 0 cm⁻¹ 模态）` : '';

  return `<div class="spec-controls">
      <label>线型 <select id="spec-kind">${options}</select></label>
      <label>半高全宽 <input type="number" id="spec-fwhm" class="input" min="0.1"
        max="1000" step="1" value="${data.fwhm}"> cm⁻¹</label>
      <a class="link" href="${esc(data.png_url)}" target="_blank" rel="noopener">新窗口看大图</a>
      <span class="muted" id="spectrum-hint">改线型或宽度会重画；展宽<b>不移动峰位</b></span>
    </div>
    <div id="spectrum-figure">
      <a href="${esc(data.png_url)}" target="_blank" rel="noopener"
        title="点开在新窗口看原始尺寸（结果栏较窄，图上的小字要放大才看得清）">
        <img id="spectrum-img" class="spectrum-img" alt="红外谱图"
          src="${esc(data.png_url)}">
      </a>
      <p class="hint warn" id="spectrum-img-error" hidden></p>
    </div>
    ${note}${imagNote}
    <div class="table-scroll spec-table-scroll">
      <table class="data" id="spectrum-table">
        <thead><tr><th>波数 (cm⁻¹)</th>
          <th style="text-align:right">强度 (${unit})</th><th>类型</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <p class="hint">共 ${data.n_peaks} 个振动峰${dropped}；波数窗口
      ${data.x_min} ~ ${data.x_max} cm⁻¹，采样步长 ${data.step} cm⁻¹。</p>`;
}

/**
 * 取红外谱图数据并画到面板上。
 *
 * 图由**后端 Pillow 渲染**（前端零依赖，也不引任何绘图库；matplotlib 在本机
 * 是坏的）。非频率任务在 renderResult 里就不会插这个空壳，这里直接早退。
 */
async function loadSpectrumSection(jobId) {
  const section = document.getElementById('spectrum-section');
  if (!section) return;
  const body = section.querySelector('#spectrum-body');
  state.spectrumData = null;

  const draw = async (kind, fwhm) => {
    state.spectrumKind = kind || 'gaussian';
    state.spectrumFwhm = fwhm;
    body.innerHTML = '<p class="hint">谱图绘制中…</p>';
    try {
      const url = '/api/jobs/' + encodeURIComponent(jobId) + '/spectrum?kind='
        + encodeURIComponent(state.spectrumKind) + '&fwhm=' + encodeURIComponent(fwhm);
      const data = await api(url);
      body.innerHTML = spectrumSection(data);
      state.spectrumData = data;
      const sel = body.querySelector('#spec-kind');
      const inp = body.querySelector('#spec-fwhm');
      // 换线型/宽度重画。峰位不变，只是"峰看起来多宽"，所以不用重新拉频率数据。
      const redraw = () => draw(sel.value, inp.value);
      sel.addEventListener('change', redraw);
      inp.addEventListener('change', redraw);
      // 图是后端画的：没装 Pillow → 503，参数越界 → 400。浏览器只会给一个破图
      // 图标，用户根本不知道出了什么事（这正是硬规矩 3 要防的"坏了还像正常"）。
      const img = body.querySelector('#spectrum-img');
      if (img) img.addEventListener('error', () => spectrumImageFailed(body, img));
    } catch (err) {
      // 失败要说清楚为什么（不是频率任务 / 没装 Pillow / 参数不合法），
      // 不要留一个空白区让人以为"谱图就是这样"
      body.innerHTML = `<p class="hint warn">红外谱图不可用：${esc(err.message)}</p>`;
    }
  };

  await draw(state.spectrumKind, state.spectrumFwhm);
}

/**
 * 谱图 PNG 加载失败时的兜底：**把服务端的原因原话摆出来，别留一个破图**。
 *
 * 为什么必须要这一段：PNG 由后端 Pillow 画（Pillow 是**可选依赖**，见
 * requirements.txt / doctor.py）。没装 Pillow 时 ``/spectrum.png`` 返回 503
 * 加一句中文说明，参数越界返回 400 —— 但浏览器看不懂这些，它只会在 ``<img>``
 * 上画一个破图图标。用户看到"一块坏掉的图"，既不知道是没装依赖、也不知道
 * 频率数字其实完好。最坏的 bug 不是算错，是坏了还显示得像正常（硬规矩 3）。
 *
 * 所以这里把同一个 URL 的 JSON 错误取回来显示；取不到就退到一句通用说明。
 * 保留 ``<img>`` 本身（只隐藏）：重画时它还要用。
 */
async function spectrumImageFailed(body, img) {
  const box = body.querySelector('#spectrum-figure');
  const err = body.querySelector('#spectrum-img-error');
  if (!box || !err) return;
  img.style.display = 'none';
  let why = '谱图 PNG 画不出来（服务端没给出原因）';
  try {
    const res = await fetch(img.getAttribute('src'), {
      headers: { Accept: 'application/json' },
    });
    const data = await res.json();           // 拿到的是 PNG 就会抛，落到 catch
    if (data && data.error) why = data.error;
  } catch (e) { /* 拿不到原因，就用上面那句通用的 */ }
  err.textContent = `红外谱图不可用：${why}`;
  err.hidden = false;
}

/**
 * 真浏览器验证红外谱图区（功能 C）。
 *
 * 为什么非要真浏览器：本项目已经三次栽在"DOM 里有但屏幕上没有"。
 * 谱图是**后端画的 PNG**，所以这里要验的东西比纯逻辑多一层：
 * 1. 区块确实插进了结果面板、有实际尺寸（不是 0×0 或者被挤到滚动区外）；
 * 2. `<img>` 真的**加载成功**（naturalWidth > 0）—— 这一步就证明
 *    /spectrum.png 在真服务里能出图，而不只是单元测试里能出图；
 * 3. 图**不是空白**：把图片画进离屏 canvas 数非白像素。只检查 src 非空
 *    是不够的 —— 一张全白的 PNG 也有 src（这正是本项目反复踩的坑）；
 * 4. 虚频行在表里有醒目标记、缺强度时有"无强度"说明。
 *
 * 需要配合 `?job=<频率任务id>&sptest=<同一个id>`，结果写进 DOM 供 --dump-dom 读。
 */
async function runSpectrumUiTest(jobId) {
  const out = document.createElement('pre');
  out.id = 'sptest-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;'
    + 'color:#0f0;background:#000;padding:4px;max-width:96vw;white-space:pre-wrap';
  document.body.appendChild(out);
  const lines = [];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  try {
    await selectJob(jobId);
    // 谱图区是异步填进去的：等表格出现（最多 20 秒）
    let table = null;
    for (let i = 0; i < 100 && !table; i++) {
      await sleep(200);
      table = document.querySelector('#spectrum-table tbody tr');
    }
    if (!table) {
      const err = document.querySelector('#spectrum-body .warn');
      lines.push('section=FAIL ' + (err ? 'body="' + err.textContent.trim() + '"' : '表格没出现'));
      out.textContent = '__SPTEST__' + lines.join(' | ') + '__END__';
      return;
    }

    const section = document.getElementById('spectrum-section');
    const body = $('result-body');
    lines.push('order=' + Array.from(body.children)
      .map((n) => n.id || n.className).join('>'));
    const rect = section.getBoundingClientRect();
    lines.push(`visible=${rect.width > 200 && rect.height > 120 ? 'PASS' : 'FAIL'}`
      + ` w=${Math.round(rect.width)} h=${Math.round(rect.height)}`);

    // 结论横幅必须仍在谱图区前面（那是"结果能不能用"的第一信息）
    const banner = body.querySelector('.res-verdict, .res-warning');
    lines.push(`after_verdict=${banner && (banner.compareDocumentPosition(section)
      & Node.DOCUMENT_POSITION_FOLLOWING) ? 'PASS' : 'FAIL'}`);
    // 结论本身也要抓下来：无强度谱**必须是"可用但有限制"**，不能是绿色可用
    const why = banner ? banner.querySelector('.res-verdict-why') : null;
    lines.push(`verdict_class=${banner ? banner.className.replace('res-verdict', '').trim() : '—'}`
      + ` verdict_why="${why ? why.textContent.trim() : ''}"`);

    // ---- 图：真的加载出来了吗 ----
    const img = section.querySelector('#spectrum-img');
    for (let i = 0; i < 100 && !(img && img.complete && img.naturalWidth > 0); i++) {
      await sleep(200);
    }
    const loaded = !!(img && img.complete && img.naturalWidth > 0);
    lines.push(`img_loaded=${loaded ? 'PASS' : 'FAIL'}`
      + ` natural=${img ? img.naturalWidth + 'x' + img.naturalHeight : '—'}`
      + ` src=${img ? img.getAttribute('src') : '—'}`);

    // ---- 图不是空白：数非白像素（这一条才是"看得见"的证据）----
    let ink = -1;
    if (loaded) {
      const cv = document.createElement('canvas');
      cv.width = img.naturalWidth;
      cv.height = img.naturalHeight;
      const cx = cv.getContext('2d');
      cx.drawImage(img, 0, 0);
      const px = cx.getImageData(0, 0, cv.width, cv.height).data;
      let colored = 0;
      for (let i = 0; i < px.length; i += 4) {
        if (px[i] < 245 || px[i + 1] < 245 || px[i + 2] < 245) colored++;
      }
      ink = colored / (cv.width * cv.height) * 100;
      lines.push(`nonblank=${ink > 1 ? 'PASS' : 'FAIL'} ink=${ink.toFixed(2)}%`);
      // 蓝色谱线必须真的画上去了：数一下接近 C_CURVE (47,111,208) 的像素
      let blue = 0;
      for (let i = 0; i < px.length; i += 4) {
        if (Math.abs(px[i] - 47) < 26 && Math.abs(px[i + 1] - 111) < 26
            && Math.abs(px[i + 2] - 208) < 26) blue++;
      }
      lines.push(`curve_pixels=${blue > 200 ? 'PASS' : 'FAIL'} n=${blue}`);
    } else {
      lines.push('nonblank=FAIL ink=-1');
    }

    // ---- 峰值表：虚频醒目、缺强度要说明 ----
    const rows = section.querySelectorAll('#spectrum-table tbody tr');
    const imagRows = section.querySelectorAll('#spectrum-table tbody tr.imag-row');
    lines.push(`table=PASS rows=${rows.length} imag_rows=${imagRows.length}`);
    lines.push(`imag_marked=${imagRows.length > 0
      && imagRows[0].querySelector('.imag') ? 'PASS' : 'FAIL'}`);
    const note = section.querySelector('#spec-note');
    lines.push(`note="${note ? note.textContent.trim() : ''}"`);
    lines.push(`strongest_marked=${
      section.querySelector('#spectrum-table tbody tr .strong') ? 'PASS' : 'NONE'}`
      + ` strongest_row=${Array.from(rows).findIndex(
        (r) => r.querySelector('.strong'))}`);

    // ---- 控制条：改 FWHM 要能触发重画（换一张图，且峰位不移动）----
    const fwhmInput = section.querySelector('#spec-fwhm');
    const before = img.getAttribute('src');
    const beforePeaks = (state.spectrumData.peaks || [])
      .map((p) => p.wavenumber.toFixed(1)).join(',');
    fwhmInput.value = String(Number(fwhmInput.value) * 3);
    fwhmInput.dispatchEvent(new Event('change', { bubbles: true }));
    let after = before;
    for (let i = 0; i < 100 && after === before; i++) {
      await sleep(200);
      const cur = section.querySelector('#spectrum-img');
      after = cur ? cur.getAttribute('src') : before;
    }
    lines.push(`fwhm_control=${after !== before ? 'PASS' : 'FAIL'}`
      + ` new_src=${after}`);
    const afterPeaks = (state.spectrumData.peaks || [])
      .map((p) => p.wavenumber.toFixed(1)).join(',');
    lines.push(`peak_positions_kept=${afterPeaks === beforePeaks ? 'PASS' : 'FAIL'}`);
    lines.push(`after_fwhm=${state.spectrumData.fwhm}`);
  } catch (err) {
    lines.push('ERROR ' + (err && err.message ? err.message : String(err)));
  }
  out.textContent = '__SPTEST__' + lines.join(' | ') + '__END__';
}

/**
 * 真浏览器验证**谱图加载失败的兜底**（硬规矩 3：它失败的时候，用户看到的是什么）。
 *
 * 做法刻意不依赖"本机有没有装 Pillow"：把 ``#spectrum-img`` 的 src 换成一条
 * **注定失败**的谱图 URL（``fwhm=99999`` → 服务端 400 + 中文原因），然后看
 * 界面上出现的是不是那句能读懂的原因，而不是一个破图图标。
 *
 * 配合 `?job=<频率任务id>&sptestfail=<同一个id>`。
 */
async function runSpectrumFailUiTest(jobId) {
  const out = document.createElement('pre');
  out.id = 'sptestfail-output';
  out.style.cssText = 'position:fixed;left:0;bottom:0;z-index:9999;font-size:10px;'
    + 'color:#0f0;background:#000;padding:4px;max-width:96vw;white-space:pre-wrap';
  document.body.appendChild(out);
  const lines = [];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const note = () => {
    const el = document.querySelector('#spectrum-img-error');
    return el && !el.hidden ? el.textContent.trim() : '';
  };
  try {
    await selectJob(jobId);
    let img = null;
    for (let i = 0; i < 100 && !img; i++) {
      await sleep(200);
      img = document.querySelector('#spectrum-img');
    }
    if (!img) {
      lines.push('img=FAIL 谱图区的 <img> 都没出现');
      out.textContent = '__SPTF__' + lines.join(' | ') + '__END__';
      return;
    }
    lines.push('img=PASS');

    // 故意指到一条越界的半高全宽上：服务端必然 400，并给出一句中文原因。
    // 加个时间戳避免浏览器复用失败的缓存条目。
    img.style.display = '';
    img.setAttribute('src', '/api/jobs/' + encodeURIComponent(jobId)
      + '/spectrum.png?kind=gaussian&fwhm=99999&t=' + Date.now());
    let msg = '';
    for (let i = 0; i < 60 && !msg; i++) {
      await sleep(200);
      msg = note();
    }
    lines.push(`fail_msg="${msg}"`);
    lines.push(`explained=${msg.indexOf('半高全宽') >= 0 ? 'PASS' : 'FAIL'}`);
    lines.push(`img_hidden=${img.style.display === 'none' ? 'PASS' : 'FAIL'}`);
    // 峰值表不该因为图画不出来就消失 —— 频率数字仍然是有用的结果
    const rows = document.querySelectorAll('#spectrum-table tbody tr');
    lines.push(`table_kept=${rows.length > 0 ? 'PASS' : 'FAIL'} rows=${rows.length}`);
  } catch (err) {
    lines.push('ERROR ' + (err && err.message ? err.message : String(err)));
  }
  out.textContent = '__SPTF__' + lines.join(' | ') + '__END__';
}

