# qcgui — 量化计算工作台
> **想直接用工具、不想看架构？看 [`使用手册.md`](使用手册.md)**
> （也有 Word 版 `使用手册.docx` 和 PDF 版 `使用手册.pdf`；手册由
> `tools/md_to_docx.py` 从 markdown 生成，改手册只改 `.md` 再重跑一次。）

以 **ORCA 为主引擎**、Psi4 为备用引擎、Gaussian 作为对照引擎的量化计算工作台。目标是替代日常的高斯工作流：**结果必须可验证地与高斯一致**。
界面是**网页版**（本地服务 + 浏览器），带一个自写的零依赖 3D 分子查看器。原来的桌面界面保留，用 `python main.py --tk` 启动。
## 架构（两条硬规矩）
> **1. 界面进程永远不 import 任何量化引擎。**
> **2. 结果只从结构化输出取**（ORCA 走 `orca_2json`），日志文本仅用于展示与判定终止。
```
浏览器 (HTML/JS/Canvas)
   ↕ REST + SSE
   ↕ Flask 服务 ── 路由 / 事件总线        ✗ import 引擎
   ↕    ↕ JobManager ── 队列 / 状态机 / SQLite 持久化 / 取消
   ↕ subprocess
   ↕ Worker 进程 ── EngineRegistry        ✓ 全项目唯一 import 引擎的地方
                 ├── OrcaEngine   .inp → orca.exe → orca_2json → Result
                 └── Psi4Engine   （备，待实现）
```

- 引擎跑在独立进程里：引擎崩溃不影响界面，取消就是杀进程树。
- 界面要用的引擎能力，通过 `python -m qcgui.worker --capabilities` 子进程查询，
  而不是在服务进程里 import 引擎。
- 桌面界面（`--tk`）与网页界面共用同一套 `JobManager` / `Result`，互不影响。
## 目录

```
qcgui/
├── main.py              入口：默认网页版，--tk 用桌面版
├── qcgui/
│   ├── web/             网页界面
│   │   ├── app.py       Flask 应用：REST/SSE 路由
│   │   └── static/      index.html / app.css / app.js / viewer.js
│   ├── model/           Molecule / JobSpec / Result / trajectory（引擎无关）
│   ├── engines/         Engine 抽象 + 注册表；orca.py 是唯一的引擎适配
│   ├── jobs/            JobManager（子进程调度）/ JobStore（SQLite）/ batch.py
│   ├── worker/          独立进程入口（**全项目唯一 import 引擎的地方**）
│   ├── ui/              桌面界面（旧，保留）
│   ├── external.py      外部工具调用：Avogadro2 / Multiwfn / obabel
│   ├── orcatools.py     ORCA 配套工具：orca_plot 出 cube、orca_2mkl 出 Molden
│   ├── recipes.py       计算配方库（参考 ACH-Orca-Workbench）
│   ├── config.py        外部工具路径：自动探测 + 配置文件覆盖
│   ├── cheminfo.py      SMILES → 三维结构（rdkit，惰性导入）
│   ├── units.py         单位换算常量（全项目唯一来源）
│   ├── paths.py         应用数据目录
│   ├── procs.py         跨平台进程终止（Windows 下连 MPI 子进程）
│   └── cli.py           命令行入口
├── examples/            water / benzene / caffeine / acetic_acid
├── tools/make_icon.py   生成应用图标（可重复运行）
├── tools/plot_scan.py   把扫描曲线导出成图片（界面里也能直接看）
├── docs/qcgui.ico       应用图标
├── tests/               11 个测试文件，共 107 项
├── MIGRATION.md         旧版本 → 本仓库的迁移对照
└── environment.yml
```

## 用法

```bash
# 网页界面（默认）：启动本地服务并自动打开浏览器
python main.py

python main.py --port 8800        # 指定端口（被占用时自动顺延）
python main.py --no-browser       # 不自动开浏览器
python main.py --tk               # 原来的桌面界面
```

桌面快捷方式 **qcgui 量化计算工作台** 用 `pythonw.exe` 启动网页版（无控制台窗口）；
停止服务用界面里的 **设置 → 关闭服务**。
重复启动不会起第二个实例：启动时先探测目标端口，若已有 qcgui 在跑，直接打开
已有实例的页面并退出（连点两次快捷方式是常见操作）。端口探测用排他语义 —— Windows 下 `SO_REUSEADDR` 的含义是“允许抢占已被占用的端口”，用它做探测会在
端口已被监听时仍然成功，曾因此出现两个实例同时监听 8760、共用同一个任务库。
命令行：

```bash
python -m qcgui.cli engines                       # 查看引擎能力
python -m qcgui.cli run --xyz examples/water.xyz --job opt_freq --method HF --basis def2-SVP
python -m qcgui.cli run --xyz examples/water.xyz --job energy --solvent water
python -m qcgui.cli batch a.xyz b.xyz c.xyz --method HF --basis def2-SVP --outdir runs
```

## 分子可视化
`qcgui/web/static/viewer.js` 是**完全自写、零依赖**的 3D 查看器（只用 Canvas 2D）。之所以不用 3Dmol.js / NGL 这类成熟库：**本机没有外网**，任何 CDN 依赖都加载不了。
- 四种显示模式：球棍 / 空间填充 / 棍状 / 线框
- 两种着色：按元素（CPK）或按原子电荷（负电偏红、正电偏蓝）
- 交互：拖拽旋转 · 滚轮缩放 · 右键平移 · 双击复位 · 可选自转与元素标注
- 成键按共价半径之和 × 1.3 自动推断
- 轨迹播放：IRC 轨迹在多帧之间线性插值，可拖动进度条或连续播放
- **IRC 能量剖面图**：纯 Canvas 绘制的相对能量曲线，标出过渡态与当前帧，
  点击/拖动即可跳转到任意一帧
- 渲染用画家算法：原子与「半根键」一起按深度排序，遮挡关系正确
原子数据（共价半径、范德华半径、CPK 配色）内嵌在 `viewer.js` 里，
碳的颜色特意提亮了 —— 纯黑的碳在深色背景上看不见。
## 分子轨道

三件事：**看能级、导 cube、交给 Multiwfn**。
- **能级与占据数**直接来自 `orca_2json` 的结构化输出（`Molecule.MolecularOrbitals`），
  **不需要额外算一次**。结果面板里给出能级图（占据实线 + ↑↓、空轨道虚线、
  HOMO/LUMO 高亮、HOMO–LUMO 间隙标注）与前线轨道表。
- **cube 导出**基于已有的 `.gbw` 走 `orca_plot`，**不重跑 SCF**，通常一两秒。
  命令序列：`2=轨道 → 3=自旋 → 4=网格 → 11=生成 → 12=退出`。
- **Multiwfn 路线**：`orca_2mkl <base> -molden` 得到 Molden，再交给 Multiwfn。
  界面上一个按钮完成，与你原来的习惯一致。
几个踩过的坑，都写在代码注释里：

| 坑 | 表现 | 处理 |
|---|---|---|
| `orca_plot` 的轨道编号 | 越界时打印 `Invalid MO requested`，**但仍然留下一个无意义的文件** | 不能只看文件在不在，必须查它自己打印的文字 |
| `orca_2mkl` 的参数 | 传 `xxx.gbw` 会去找 `xxx.gbw.gbw` | 必须传**不带后缀的基名** |
| `orca_plot` 的交互模式 | 喂到非法输入会**死循环**（EOF 也不行） | 每个数字都必须有兜底 + 超时兜底 |
| 80 个轨道全画进 250px | 挤成一条黑线 | 只画价层窗口，并注明省略了多少个深层轨道 |

编号口径已实测确认：**`orca_plot` 和 JSON 都是 0 起的**
（水的 STO-3G 有 7 个轨道，MO=6 可导、MO=7 报越界）。
## 参考实现：ACH-Orca-Workbench

本机装有 **ACH-Orca-Workbench 1.6.0**（Christian Nelle，TU Dortmund，MIT 协议，[github.com/ACH-Repo/ACH-Orca-Workbench](https://github.com/ACH-Repo/ACH-Orca-Workbench)）—— 一个成熟的 ORCA Tkinter 桌面 GUI（Molecules / Recipes / Calculations / Report / Workflow
五个标签页，含 SLURM 集群支持）。它的**界面功能组织**是本项目的重要参考，已采纳：

| 参考它的设计 | 本项目实现 |
|---|---|
| **配方（Recipes）**：命名预设，可收藏置顶，含 ORCA 输入模板 | `qcgui/recipes.py`：19 个内置配方 + 用户配方目录，★ 收藏（含取消预置收藏） |
| **几何血缘**：`Geometry: From parent's optimised geometry` | `JobSpec.parent_job` + `POST /api/jobs` 的 `geometry_from_job`，子任务直接沿用父任务的结果几何，血缘记入结果与 CSV |
| **Report 标签页**：扫描已完成计算并导出 | `GET /api/report.csv`：一行一个任务，含能量 / ZPE / H / G / S / 虚频数 / 耗时 / 上游任务 / 配方 |
| 配方里的方法关键词组合 | 采纳 `RIJCOSX def2/J`、`D4`、`TightOpt`、`VeryTightSCF`、`AUTOAUX`、`pcSseg-2` 等实测可用的 |
| 计算类型覆盖 | 采纳 TD-DFT / NMR / EPR 配方（标注为 `partial`：能跑通但结果解析未实现，绝不假装有结果） |

**没有照搬**：SLURM 集群提交、工作流图编辑器、3D 结构编辑 —— 这些超出当前阶段的范围，且依赖该项目的项目文件格式。
## 配方（Recipes）
`qcgui/recipes.py` 把方法 + 基组 + 关键词 + 块设置打包成可复用预设。
```python
from qcgui.recipes import get_recipe
spec = get_recipe("optfreq_b3lyp").build_spec(molecule)
# -> JobType.OPT_FREQ, B3LYP/def2-TZVPP,
#    keywords=RIJCOSX def2/J D4 TightOpt Freq VeryTightSCF
```

- **内置配方**随仓库分发，覆盖 SP / OPT / FREQ / OPT+FREQ / TS+FREQ / IRC，
  含 r2SCAN-3c、B3LYP-D4、wB97X-D4、DLPNO-MP2、DLPNO-CCSD(T)、溶剂化、TD-DFT、NMR
- **用户配方**保存在 `data_root()/recipes/*.json`
- **收藏**存在 `recipe_favorites.json`，是“覆盖表”而非列表 ——
  因为内置配方本身带默认收藏，列表格式只能加不能减
- 标了 `parsing="partial"` 的配方（TD-DFT / NMR）**能跑通但结果尚未解析**，
  界面上会显式提示，不会让人误以为有数据

`qcgui/external.py` 移植自旧版的 `psi4gui/core/external.py`，修掉了三个问题：
| 旧版问题 | 现在 |
|---|---|
| 路径硬编码到某台机器的用户名目录下（换机器即失效） | 自动探测 + 配置文件覆盖；配置写错会回退探测并在提示里说明 |
| `proc.wait()` 一直等 Multiwfn 退出（当年在界面线程调，界面会卡死） | 一律立即返回，不阻塞 |
| 靠固定 `sleep` 盲喂 stdin 给 Multiwfn | 实测 Multiwfn 支持命令行直接带文件，改为传参 + 独立控制台，可正常交互 |

界面上「用外部工具打开当前结构」提供 Avogadro2 和 Multiwfn 两个入口。另外 `run_multiwfn_batch()` 保留非交互用法，供批量分析与测试使用。
## 测试

```bash
python tests/test_orca_engine.py    # 引擎层：单点/优化/频率/热化学/TS/IRC/SMD
python tests/test_job_manager.py    # 进程隔离、任务落盘、取消
python tests/test_batch.py          # 批量相对能量
python tests/test_recipes.py        # 配方库、收藏、JobSpec 生成
python tests/test_orbitals.py       # 分子轨道解析、cube 导出、Molden、Web 接口
python tests/test_web_api.py        # 网页 API、SSE、配方、几何血缘、报告导出
python tests/test_web_browser.py    # 真实浏览器里的前端交互（无头 Chrome）
python tests/test_external.py       # 外部工具（真调用 Multiwfn / Avogadro2）
python tests/test_viewer_js.py      # 3D 查看器逻辑（Node 无头驱动）
python tests/test_gui_selftest.py   # 桌面界面端到端
```

- `test_web_api.py` 用 werkzeug 在随机端口起真实服务，用 urllib 打真 HTTP 接口。
- `test_web_browser.py` 用无头 Chrome 打开页面，**用合成鼠标事件驱动真实的事件处理器**
  （拖拽旋转 / 滚轮缩放 / 右键平移 / 双击复位 / 工具栏切换），再用 `--dump-dom` 取回结果断言。
  这覆盖的是事件绑定，是无头逻辑测试够不到的部分。找不到 Chrome 时自动跳过。
- `test_gui_selftest.py` 会真的开窗口、真的跑 ORCA，并对**界面实际显示的文字**做断言。
- 慢速用例（IRC，约 2–3 分钟）默认跳过，用 `QCGUI_SLOW_TESTS=1` 开启。
- 无图形环境时用 `QCGUI_SKIP_GUI_TESTS=1` 跳过桌面界面测试。
- 查看器测试需要 Node，找不到时自动跳过（可用 `QCGUI_NODE` 指定）。
### 前端自动化钩子
给无头测试留了两个 URL 参数（正常使用不受影响）：
| 参数 | 作用 |
|---|---|
| `?nosse=1` | 不建立 SSE 长连接。无头浏览器下长连接会让页面永远“加载中”，`--virtual-time-budget` 等不到 |
| `?viewertest=1` | 跑一遍合成鼠标交互自检，把结果写进 `<pre id="viewertest-output">`，供 `--dump-dom` 读取 |
| `?load=<路径>` | 直接载入指定结构 |
| `?job=<任务 id>` | 直接打开某个任务的结果 |
| `?demo=1` | 载入内置示例分子 |

## 环境

二选一（都需要 **Python 3.10+**）：

```bash
# 方式 A：conda
conda env create -f environment.yml
conda activate qcgui

# 方式 B：pip（任意虚拟环境）
python -m pip install -r requirements.txt
```

只有 **Flask / Werkzeug** 是必需的；`rdkit`（SMILES 生成结构）与 `Pillow`（扫描曲线导图片）
缺失时只少对应功能，不影响启动。

**ORCA 要自己装**（学术免费授权，但不开源、不可再分发，因此本仓库不含它）。
装好后任选一种让程序找到它，优先级从高到低：

| 方式 | 做法 | 适合 |
|---|---|---|
| 环境变量 | `set QCGUI_ORCA_EXE=D:\orca_6_1_1\orca.exe` | 临时覆盖、脚本化 |
| **界面设置** | 顶栏「设置」→ 填 ORCA 的完整路径 | **换机器时最省事**，填完立即生效 |
| PATH | 把 `orca.exe` 所在目录加进 PATH | 长期使用 |
| 自动探测 | 什么都不做 | 它会在各盘符根、Program Files、桌面上找 `orca*\orca.exe` |

### 换一台机器：先跑诊断

```bash
python main.py --doctor
```

它会逐项检查 Python 版本、依赖、ORCA（含**是从哪儿找到的**）、外部工具、
数据目录是否可写，并对每个缺失项给出可直接照做的处理办法。

```
【量化引擎】
  [✓] ORCA: <你的 ORCA 目录>\orca.exe（来源：PATH）
【Python 依赖】
  [✓] Flask: 已安装
  [·] rdkit: 缺失 —— 「从 SMILES 生成结构」不可用
        → python -m pip install 'rdkit'
结论：**可以算**。可选功能缺失：rdkit（上面标 · 的项）。
```

**可移植性相关的设计**（换机器时不用改代码）：

- 全项目只有 `qcgui/orcatools.py` 决定"ORCA 装在哪"，其余地方一律调它
- 搜索位置**按当前机器的盘符与用户目录动态生成**，不写死某个具体目录
- 配置里留着的旧机器路径失效时会**自动回退探测**，不会把程序卡死
- 数据目录可用 `QCGUI_HOME` 覆盖（便携部署 / U 盘）

## 已知前提（实测踩坑）

- ORCA 的 `.inp` **绝不能带 BOM**，否则报 `non-ASCII character(s) found` 并以退出码 50 终止。本项目一律用 ASCII 无 BOM 写入。
- `orca_2json` 必须显式传入 `.gbw` 文件（传基名会报 `Cannot open GBW file`）。
- ORCA JSON 里的坐标单位是 **Bohr**，本项目统一换算成 Å 后再进入 `Result`。
- ORCA 的 `entropyS` 字段实际是 **T·S**（Hartree），不是熵本身；适配器据此换算出真熵。
- `PYTHONIOENCODING` 环境变量对**当前进程**的 stdout 无效（解释器启动时就定死了）。
  `pythonw.exe` 下 stdout 会落成 GBK，打印非 ASCII 会抛异常；本项目用
  `qcgui.configure_stdio()` 显式 `reconfigure(encoding="utf-8", errors="replace")`。
## 已验证（本机实测，ORCA 6.1.1）
| 项目 | 结果 |
|---|---|
| 引擎发现 | 自动定位到 `orca.exe`（本机在 `D:\orca1\`，走 PATH） |
| 单点能 | HF/def2-SVP 水：E = **−75.960983978583 Ha** |
| 几何优化 | 收敛：O–H **0.9450 Å**、H–O–H **105.13°** |
| 频率 + 热化学 | 1790.7 / 3972.6 / 4062.0 cm⁻¹；ZPE 0.022384 Ha，**S = 188.5 J/(mol·K)**（文献 188.8） |
| 过渡态搜索 | 乙二醛顺反异构 TS（r2SCAN-3c）：**恰好 1 个虚频（−177.8 cm⁻¹）**，12 个模式 = 3N−6 |
| IRC 反应路径 | 从同一 TS 出发（HF/def2-SVP）：20 帧，TS 位于最高点，两端能量 4.63 / 5.02 kcal/mol |
| 批量相对能量 | 水三个 O–H 键长：**0.96 Å 最低**，0.90 Å +12.15、1.10 Å +99.87 kJ/mol |
| SMD 溶剂化 | 水相单点 −75.9744144807 Ha |
| 结构化输出 | `orca_2json` 一次给全：能量、几何、偶极、Löwdin/Mulliken/Mayer 电荷、Mayer 键级、频率、热化学 |
| 进程隔离 | 真实的界面进程跑完任务后，`qcgui.engines` **不在 sys.modules 里** |
| 取消 | 杀 worker 进程树，**无 ORCA 孤儿进程残留** |
| 网页界面 | 真实浏览器渲染确认；REST/SSE 全通；`/api/shutdown` 可停服；批量相对能量的 API 与 CLI 结果一致 |
| 前端交互 | 无头 Chrome 里用合成鼠标事件驱动：拖拽旋转 / 滚轮缩放 / 右键平移 / 双击复位 / 工具栏切换，**全部 PASS** |
| 外部工具 | Multiwfn 非交互载入回报 `Loaded ... successfully!`；Avogadro2 真实启动并回报 |
| 自测 | 11 个测试文件共 **107 项** |

> 结论：**热力学不再需要正则解析日志文本** —— 全部来自 JSON，且已用物理量校验过。
> IRC 的能量剖面取自 ORCA 写的多帧轨迹文件（每帧注释行带能量），同样不解析人读表格。
> `capabilities()` 只声明上述已实测的任务类型；NEB-TS 与扫描尚未验证，故明确不声明。
### 复合方法

`r2SCAN-3c`、`PBEh-3c`、`B97-3c` 这类复合方法自带基组，把 `basis` 留空即可。
```python
JobSpec(method="r2SCAN-3c", basis="")
```
