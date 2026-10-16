/**
 * 轨道成分界面的纯函数测试（Node，无浏览器）。
 *
 * 为什么单独测 `orbitalIndexLabel`：Multiwfn 的轨道编号是 1 起、ORCA 是 0 起，
 * 直接显示 `orbital_index` 会让同一轨道在「分子轨道」表和「轨道成分」表里
 * 出现两个号 —— 本项目反复踩过这类"编号对不上"的坑，所以把它做成纯函数钉住。
 *
 * 从 app.js 里按函数名抽出来跑（与 tests/viewer_harness.js 同一套路），
 * 不引入任何依赖。
 *
 *   node tests/orbcomp_harness.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const APP = path.join(__dirname, '..', 'qcgui', 'web', 'static', 'app.js');
const src = fs.readFileSync(APP, 'utf8');

/** 按函数名抽取 `function name(...) { ... }`（靠大括号配平，遇到字符串里的花括号按朴素方式容忍）。 */
function extractFunction(code, name) {
  const start = code.indexOf(`function ${name}(`);
  if (start < 0) return null;
  const open = code.indexOf('{', start);
  if (open < 0) return null;
  let depth = 0;
  for (let i = open; i < code.length; i += 1) {
    const ch = code[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return code.slice(start, i + 1);
    }
  }
  return null;
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail === undefined ? '' : '  ' + detail}`);
}

const labelSrc = extractFunction(src, 'orbitalIndexLabel');
check('extract orbitalIndexLabel', !!labelSrc);
if (!labelSrc) {
  console.log('\nORBCOMP RESULT: FAIL (function not found)');
  process.exit(1);
}

// 在隔离作用域里求值，只暴露被测函数
// eslint-disable-next-line no-new-func
const orbitalIndexLabel = new Function(`${labelSrc}\n return orbitalIndexLabel;`)();

const orb = { homo: 50, lumo: 51 };

// 实测依据（E8）：咖啡因 HOMO Multiwfn 报 51、ORCA 报 50；LUMO 报 52 对 51
const homo = orbitalIndexLabel({ orbital_index: 51 }, orb);
check('HOMO 显示 ORCA 编号 50', /#50\b/.test(homo), homo);
check('HOMO 不再把 51 当 ORCA 编号', !/#51\b/.test(homo), homo);
check('HOMO 标出 Multiwfn 编号 51', /Multiwfn\s*编号\s*51/.test(homo), homo);
check('HOMO 带 HOMO 标记', /HOMO/.test(homo), homo);

const lumo = orbitalIndexLabel({ orbital_index: 52 }, orb);
check('LUMO 显示 ORCA 编号 51', /#51\b/.test(lumo), lumo);
check('LUMO 标出 Multiwfn 编号 52', /Multiwfn\s*编号\s*52/.test(lumo), lumo);

const other = orbitalIndexLabel({ orbital_index: 39 }, orb);
check('非前线轨道只给编号不带 HOMO/LUMO 标记',
  /#38\b/.test(other) && !/HOMO|LUMO/.test(other), other);

check('orbital_index 为 null 时给出占位符', orbitalIndexLabel({ orbital_index: null }, orb) === '—');
check('orb 缺失时不抛异常', typeof orbitalIndexLabel({ orbital_index: 51 }, null) === 'string');

// 结论横幅必须在轨道成分区之前 —— 渲染函数不该把 section 塞到横幅上面。
// 这里用静态检查：loadOrbCompSection 的 anchor 选择里必须包含结论横幅类名。
const loadSrc = extractFunction(src, 'loadOrbCompSection');
check('loadOrbCompSection 存在', !!loadSrc);
if (loadSrc) {
  check('挂载点显式避让结论横幅',
    /res-verdict/.test(loadSrc) && /\.after\(node\)/.test(loadSrc));
  check('键级区之后插入（同级补充分析）', /bondorder-section/.test(loadSrc));
  check('失败时按钮一定恢复可点', /finally/.test(loadSrc) && /disabled = false/.test(loadSrc));
}

// count=0 时必须走错误块，且 problems 不能丢（留痕失败是唯一线索）
const renderSrc = extractFunction(src, 'renderOrbComp');
check('renderOrbComp 存在', !!renderSrc);
if (renderSrc) {
  check('空结果走错误块', /bo-error/.test(renderSrc));
  check('空结果分支也带上 problems', /return `[^`]*bo-error[\s\S]{0,200}\$\{warn\}/.test(renderSrc));
}

const failed = results.filter((r) => !r.ok);
console.log(`\n共 ${results.length} 项，失败 ${failed.length} 项`);
console.log(`ORBCOMP RESULT: ${failed.length ? 'FAIL' : 'PASS'}`);
process.exit(failed.length ? 1 : 0);
