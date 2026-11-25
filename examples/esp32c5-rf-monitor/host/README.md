# 测试上位机（PySide6 + WebView）

一个**桌面壳 + 网页界面**的测试上位机：Python 只做三件事（串口、调度、打包
JSON），界面全部在 `web/` 里的 HTML/CSS/JS。

**换实验 = 换网页**：新增一个实验只加 `web/pages/<id>/page.html` 和
`registry.json` 里一行，`app.py` **不用改**。

```
host/
├── app.py             # Qt 外壳：QWebEngineView + QWebChannel 桥 + 采集线程 + --selftest
├── gain.py            # 顶栏三段增益 + 自动闭环（GainControl，真板命令非界面滤镜）
├── occupancy.py       # 信道占用统计：射频线程里跟着每帧 Snapshot 自己算（判据与固件同源）
├── paths.py           # 定位 Python 分析层（core/ modes/ output/ tests/）的位置
├── port_lock.py       # 同一串口只允许一个上位机实例采集（跨进程互斥）
├── watch.py           # 监测调度：何时扫 WiFi、何时灌热点名单、射频流开多久
├── wifi_env.py        # WiFi 环境扫描：上位机侧拿「有名字的热点名单」
├── link_guard.py      # 串口自保：命令前排空残留帧 + 失效后自动重同步（core/ 不动）
├── session.py         # 纯 Python 逻辑层（无 Qt，可单独测）：配置校验 / 会话 / 频谱打包
├── start.bat          # Windows 双击启动（缺依赖自动装）
├── requirements.txt   # PySide6 + pyserial
├── l10n.py            # 下行文案本地化：中文串骨架匹配 → 英文模板（翻不到原样返回）
├── tests/test_session.py  # 29 条无窗口自测（含全量并行 2 条）
├── tests/test_sweep_session.py # 设备自扫引擎（engine=device）会话级单测，假固件
├── tests/test_gain.py     # gain.py：手动档位 / 串口落地时序 / 闭环参数
├── tests/test_link_guard.py # 串口自保层：排空残留帧 + 失效后重同步
├── tests/test_port_lock.py # port_lock.py：同一串口只允许一个实例
├── tests/probe_occ.py     # 「信道占用」页渲染探针（假桥，15 s 出结果，中英双语）
├── tests/test_l10n.py     # 英文界面零中文（10 场景 × 全部下行出口 + ASCII 页）
├── tests/probe_popout.py  # 弹窗切页行为快速探针（20 s 出结果，迭代弹窗逻辑用它）
├── store_throttle.py  # 落库节流：同窗最大保持合并，换窗/1 s 才落盘（别让一窗 60 帧堵住采集）
└── web/
    ├── index.html     # 唯一入口：顶栏 + 导航 + #page-container + 页脚
    ├── css/app.css    # 暗色仪表盘主题
    ├── assets/vendor/ # three.js r128 + OrbitControls（本地，离线可用，MIT）
    ├── js/
    │   ├── bridge.js  # QWebChannel 封装：信号自动 JSON.parse，调用返回 Promise
    │   ├── util.js    # 单位格式化 + 分档配色 + 热力色带 + 画布适配
    │   ├── three3d.js # 3D 瀑布曲面 / 3D 信道柱状图
    │   ├── router.js  # 按 registry.json 加载片段（XHR，QWebEngine 不支持 file:// 的 fetch）
    │   ├── popout.js  # 双击任意面板 → 全窗口预览 + 自定义缩放 + 全屏 + 独立窗口
    │   ├── scanbar.js # 扫频进度条（2.4G/5G 频段地图 + 回卷游标）
    │   ├── i18n.js    # 中英切换：静态词条；en 缺 key 不回退中文（亮 key 让自检抓）
    │   ├── app.js     # 外壳装配 + 事件路由 + 自检探针 window.probeReport()
    │   └── pages/     # 各实验自己的渲染逻辑
    └── pages/
        ├── registry.json      # 页面注册表（换实验只动这里）
        ├── spectrum/page.html # 实时频谱：2D 曲线 + 3D 瀑布 + 2D 瀑布
        ├── waterfall/page.html# 周期瀑布图：全频段时频热力图 + 出 PNG/HTML 按钮
        ├── wifi/page.html     # WiFi 信道体检：卡片 + 排序表 + 3D 柱状图
        ├── occ/page.html      # 信道占用：38 信道逐条占空比（当前/峰值/平均）+ CSV 导出
        ├── raw/page.html      # 通用兜底：任何模块的 Output 表格 + CLI 同款 ASCII
```

## 怎么跑

```sh
cd examples/esp32c5-rf-monitor

# 1) 真板（COM56 是这块板子的硬规则，见仓库根 AGENTS.md）
python host/app.py --port COM56 --gain-mode MANUAL --gain-index 60
python host/app.py --port COM56 --gain-mode AUTO   --gain-target 4.0   # 主机闭环自己压底噪
python host/app.py --port COM56 --gain-mode HARDWARE                   # 固件硬件 AGC

# 2) 没板也能看：假固件 + 内置场景
python host/app.py --mock --scenario wifi_busy

# 3) 无人工干预自检（跑 10s → 走完六个页面 + 增益控件 + 窗口缩放 + 英文零中文 → 打印 JSON 报告 → 退出码 0/2）
python host/app.py --selftest --seconds 10 --shot out.png
python host/app.py --selftest --real --port COM56 --gain-index 60 --seconds 12
python host/app.py --selftest --seconds 10 --mock --engine device   # 设备自扫引擎
#    → 报告末尾 "checks": 54 项全 true，退出码 0（host / device 两条引擎都验过）
#    ⚠️ 别把 --seconds 压到 8：页面探针没跑完，画布/瀑布那几项会假红
#    ⚠️ 自检会真的切一次语言并逐页扫 DOM：`en_no_cjk` / `en_keys_complete` 红了就是英文界面
#       里漏了中文。三种修法，按现场选：
#         · 普通判词/表格 → 补 host/l10n.py 的 TABLE
#         · 列表长度会变（`ch1, ch2, …`）→ 补 LIST_PATTERNS 正则（TABLE 按长度备条目必漏）
#         · 异常文案（`[RF error] …`）→ 补 ERROR_PHRASES 片段，界面侧走 session.error_text()
#       前端静态词条则补 web/js/i18n.js 的 en 词典。真板才会出现的档位，必须跑 --real 才验得到。

# 4) 串口自保探针（真板；验证「命令前排空」不打坏扫描 + 失效后自动重同步）
cd .. && python testing/probe_resilient.py --windows 24     # 期望 [OK]

# 5) 指定窗口尺寸（验「窗口态也不裁按钮」）
python host/app.py --mock --window-size 1024x680

# 逻辑层自测（不开窗口）
python -m unittest discover -s host/tests -t . -v
python tests/test_l10n.py -v          # 英文下行零中文（10 场景 × 6 出口 + ASCII 页 + 列表句 + 异常文案）
python tests/test_occupancy.py -v     # 占用统计口径（判据 / 帧归属 / EMA / 峰值 / CSV）

# 页面探针（只验渲染，不碰串口）
python tests/probe_occ.py             # 「信道占用」页：38 行/未扫到不显示成 0%/按钮
python tests/probe_occ.py --lang en   # 同上 + 英文界面零中文
```

Windows 上也可以双击 `host/start.bat`（缺依赖会自动 `pip install`）。

## 信道占用页（occ）：统计「不是 WiFi 的协议」

目标不是“信号多强”，而是**这个信道有多少时间被别人占着** —— BLE 广播、图传、遥控、
无绳电话都不在 WiFi 栅格上，看 SSID 列表永远看不到它们。

| 项 | 口径 |
| --- | --- |
| 判据 | 一帧取**中间一半** bin，`p50 − p25 > 15 码` 判「有传输」（与固件 `spectrum.c` 同一个门限） |
| 帧归属 | 帧中心离某信道 ≤ 2.5 MHz 才算该信道的测量；宽窗中心之外的帧不计入任何行 |
| 当前 | 逐帧命中指示的 EMA（α = 1/8，约 1 秒）；板端是逐窗 EMA（τ ≈ 24 s），**两者只比高低不比绝对值** |
| 峰值 | 会话内「当前」的最高值 —— 认**突发**协议（BLE 广播是尖的）靠它 |
| 平均 | 累计命中帧 / 总帧 —— 看“一直发射吗”（图传/视频流） |
| 未扫到 | 显示 `未扫到`/`—`，**不是 0%**；宽窗扫频时很多信道压根没被单独测过 |

三个按钮：`按占用/按频率排序`、`导出 CSV`（写 `host/.data/occ_<时间戳>.csv` 并尝试打开）、
`清零`（只清统计，不动扫描/热力/瀑布）。统计在射频线程里跟着扫描一直跑，切页不会断。

验证分两层：`tests/test_occupancy.py`（口径，无窗口）+ `tests/probe_occ.py`（渲染，假桥）。

## 造一个新实验页（不碰 Python）

1. 建 `web/pages/<id>/page.html`（纯静态结构，`id` 自己起，只写要显示的骨架）。
2. 在 `web/pages/registry.json` 里加一行 `{"id": "<id>", "title": "显示名", "hint": "副标题"}`。
3. 需要实时数据就再写 `web/js/pages/<id>.js`：
   ```js
   window.Pages = window.Pages || {};
   window.Pages["<id>"] = {
     onEnter(root) {},            // 片段注入后调用，root 就是 #page-container
     onLeave() {},                // 切走时清理计时器/事件监听
     onInfo(payload) {},          // 开流/换扫描计划一次（含 plans，画频段条用）
    onSpectrum(payload) {},      // 每帧频谱（~20fps）
     onTables(payload) {},        // 每 1s 的表格式产出
     probe() { return {}; },      // 可选：给 --selftest 的报告里加一段
   };
   ```
   然后在 `index.html` 的 `<script>` 列表里加一行（这是唯一的 Python 之外的手工步骤）。
4. 没写 JS 也能跑：`raw` 页面会把任何模块的 `Output` 表格渲染出来。
5. 不用登记"这一页要算哪些模块"：**所有模块一直并行在算**（见下节）。

## 全功能并行（一份数据，多个消费者）

射频线程每轮 `session.step()` 产出的帧会分发给**所有**模块，`session.tables()` 也每次都
全量产出五个模块的表格；页面切换只影响显示，**不影响采集与计算**。

| 位置 | 做了什么 |
|---|---|
| `web/js/router.js` | `navigate()` 末尾调 `Bridge.call("setView", {page, win})` —— 只用于记录"谁在看哪页"和调试 |
| `host/app.py` `Bridge.setView` | 写进 `HostWindow.view_box`（**普通 dict**，不是 QObject 属性），窗口标签区分主窗口/独立小窗 |
| `host/app.py` `needed_modules()` | **恒返回全部模块**（不再按页面裁剪） |
| `host/app.py` `Runner` | `tables` 每周期全量推；`spectrum` 帧一直推（频谱页与所有浮层共用一份） |
| `host/session.py` `tables(modules)` | 传 `None` = 全量；`restrict_modules()` 仍在，但只给探针/测试用，**不接切页** |

为什么不再按页裁剪：实测切页会顺带改变射频线程的调度与 tables 节拍，吞吐反而掉下去
（真板 `--selftest --engine device` 切页把窗率打到 0.37 窗/秒），而且切回来之前那一页
的数据是断的。`view_gating` / `spectrum_gating` 两条自检就是盯着这个语义：
留档里每条 `modules` 都必须等于模块总数，且停在非频谱页时频谱帧计数仍在涨。

两条注意：

* **跨线程只读普通 dict**：射频线程读 `view_box` / 写 `stats_box`，绝不跨线程读 QObject
  的实例属性（PySide6 那条路会栈溢出崩进程，且没有任何 Python 回溯）。`Runner` 手上的
  `module_ids` 是构造时在主线程取的一份副本。
* 门控只管「算不算」，**不改调度权重**：五个模块的 `slice_weight` 仍是各 1.0，
  自检里 `share` 依旧 5 × 0.20。不想看到某模块的扫描时间被浪费，才需要在固件侧做
  状态机（见 `docs/` 里的方案讨论）。

## 扫频进度条（`web/js/scanbar.js`）

频谱页最上面那条横条就是「扫到哪了」：左边一段是 2.4G、右边一段是 5G，
每一小格 = 扫描计划里的一个 20MHz 窗，游标停在该窗的中心频点上；扫到最右端
**回卷**到最左端继续下一轮，底部细条是本轮已覆盖的窗数。

- **频段边界和窗位置都不是写死的**，来自 `info.plans[<module>]`（Python 侧
  `scan_requests()` 的 `[[中心频点, 带宽], ...]`，按扫描顺序）。前端按「相邻窗
  频率跳跃 > 3 倍窗宽」自动切段并命名（2.4G / 5G / #n），换实验不用改 JS。
- 横轴是**分段线性**的：2.4G 只有 80 MHz、5G 有 665 MHz，按真实比例画 2.4G 会
  变成一根看不见的线，所以每段宽度取 √跨度 归一（实测 2.4G ≈ 26%、5G ≈ 74%）。
- 游标用 `requestAnimationFrame` 插值（不是每帧硬跳）；回卷时用更慢的插值，
  能看见游标一路往左扫回去。已扫过的窗留浅蓝底、当前窗高亮。
- **假固件不模拟驻留时间**（一个窗口 3ms 就跑完），不加节流界面上的游标会闪成
  一片；`Runner._mock_pace()` 在每帧之间补 `dwell_ms/2`，节奏就与真机一致。
  实测：假固件 ≈ 8~10 窗/秒，真机 ≈ 4 窗/秒（串口解析 1024 点才是瓶颈，
  不是 100ms 驻留），一轮 49 窗约 12s。

## 双击弹出预览（`web/js/popout.js`）

**任何一个 `.panel` 双击就弹成全窗口预览**，右上角也有个 `⤢` 按钮（双击不显眼，
所以两个入口都给）。工具栏能做的事：

- `−` / `+`：**自定义缩放**，75% → 100% → 125% → 150% → 200% → 300%（12 档太密，
  按这 6 档走）；`重置` 回 100%；`独立窗口` 把这块面板丢到**另一个窗口**里
  （见下节）；`全屏` / `退出全屏` 见下；`✕`/`Esc` 关闭。
- 浮层是**同一文档内的 fixed 覆盖层 + 原地搬 DOM**，不是 `window.open`：
  canvas / WebGL 上下文跟着 DOM 走，搬进搬出都不丢内容，也不用给 Python 侧再加
  一个窗口和桥。
- 缩放是**按新尺寸重画**，不是把小图拉伸放大：面板宽度 = 倍率 × 100%，
  同时在面板上设 `--fit-scale: 倍率`，`U.fitCanvas()` 与 3D 容器高度
  （`#wf-3d` 的 `calc(300px * var(--fit-scale, 1))`）都乘这个倍率，
  然后派发一次 `resize` 让页面自己的钩子重算。
- 尺寸一变，2D 瀑布**保留历史**（把旧缓冲按新宽度重采样，`resizeWaterfall()`），
  不再像以前那样清空重来。
- 换页时不把面板「搬回去」而是**先安顿好浮层**：调 `Popout.beforePageChange(下一页)`
  判定该关还是该暂停（见下节「切页时弹窗怎么办」），并先把浮层从文档里摘下来，
  否则会被 `host.innerHTML = html` 连根清掉。

新实验页不用写任何代码就有这个功能——`Popout.attach(#page-container)` 是委托到
`.panel` 上的，按钮也是自动加的（`MutationObserver` 兜住运行时新插入的面板）。

### 切页时弹窗怎么办（实时流暂停 / 汇总型关掉）

面板分两类，切页时**待遇不同**：

| 面板种类 | 判据 | 切走后 | 切回来 |
|---|---|---|---|
| 实时流（画布、随帧刷新的表） | 不是 `STATIC_PANELS` 里登记的 | **不关**，停在最后一帧 + 顶部琥珀条「模式已切换：本面板已暂停刷新」 | 自动继续刷新，提示条消失 |
| 汇总型（判读说明、门槛表、一键体检结论、出图按钮） | 在 `STATIC_PANELS` 登记 | **直接关掉**（它只是「当时那份快照的结论」，留着会误导） | —— |

- 汇总型**只在一张表里登记**（`popout.js` 的 `STATIC_PANELS`，值是 `.panel` 的下标）：
  `waterfall: [0,1,3]` / `wifi: [2]` / `ble: [4,5]` / `capture: [1,4,5]`。新页面加面板只改这张表。
- 面板的归属页 + 是否汇总型写在 `data-panel-owner` / `data-panel-static` 上，
  由 `decorate()` 统一盖章。**已经弹出去的面板不再盖章**（它被搬进浮层，而浮层就挂在
  `#page-container` 里，切页后会再被遍历到；照改会把 wifi 页的面板标成 ble 页的，
  导致切回来永远恢复不了）。
- 「暂停」几乎不用写代码：`Router.dispatch` 只把 `tables`/`spectrum` 派给**当前页**的模块，
  面板留在浮层里就自然收不到新帧。
- 暂停状态下的 `Popout.state()`：`{open:true, paused:true, banner:true, owner:"<原页>"}`；
  恢复后 `paused:false, banner:false`。

### 弹出后高度自适应（别留一大片空白）

弹出来只是「变宽」不够，2D 瀑布那种固定 `150px` 高的画布放大后下面全是空白。
现在的做法是**两阶段量高度**：

1. 面板上 `--fit-grow` 归零 → 派发一次 `resize`，页面按倍率把画布重画成「自然高度」；
2. 260ms 后量 `浮层可用高度 − 面板实际高度`，把这段余量除以面板里标了
   `data-fit-grow="1"` 的个数，写进 `--fit-grow`，再派发一次 `resize`。

`U.fitCanvas()` 里的高度 = `cssHeight × fitScale + fitGrow()`，3D 容器高度 =
`calc(300px * var(--fit-scale, 1) + var(--fit-grow, 0px))`。窗口被拉大/全屏时
（Python 侧 `showFullScreen()` 也会触发真正的 `resize`）会重新走一遍这个流程；
自己派发的合成 `resize` 用一个 `suppress` 计数屏蔽，不会自我递归。

实测：普通窗口浮层 stage 865px / 面板 825px（95%），全屏 stage 1032 / 面板 993（96%）。

### 全屏为什么不走 `requestFullscreen()`

自动化（`element.click()`）和脚本触发的全屏**没有用户手势**，Chromium 直接拒，
表现就是「点了全屏没反应、布局跟普通一模一样」——真机第一次就是这么翻车的。
现在 `全屏` 按钮调 `Bridge.call("setFullscreen", {"on":..,"target":"main"|"popout"})`，
由 Python 侧 `win.showFullScreen()/showNormal()` 做，不需要手势；纯浏览器打开网页
（没有 QWebChannel）时才退回 DOM Fullscreen API，再不行退化成「铺满窗口」。

### 窗口外弹窗（`独立窗口`）

`Popout.openDocked()` → `window.open('index.html?solo=<页面:序号>')`，Python 侧
`ConsolePage.createWindow()` 接住并返回一个新页：

- 新窗口是**真正的窗口**（`QWebEngineView` 顶层窗口，可拖到别的显示器、可自己全屏）；
- 加载的还是同一套 `web/index.html`，只是带 `?solo=` 参数，网页自己
  `body.solo` 隐藏外壳、只把那一个面板铺满（隐藏「关闭」和「独立窗口」按钮）；
- **共用同一个 `Bridge`**：`QWebChannel` 是每个页面一个，但注册的是同一个
  `backend`，所以信号天然广播给所有窗口；晚连上的窗口靠 `runtime()` 里带回的
  最后一帧 `spectrum`/`tables` 立刻有画面（`HostWindow._cache_spectrum`）；
- 主窗口关掉时一起关掉（`HostWindow.shutdown()`）；浮层挂进 `#page-container`
  而不是 `body`，是为了让被搬走的面板仍然是页面 `root` 的后代
  （否则页面 resize 钩子 `root.querySelector("#spec-canvas")` 拿到 `null` 直接抛错，
  画面停止重绘——这个坑现场踩过）。

## 2D 瀑布的频率轴

`#wf-canvas` 底部有一条**简易频率轴**：轴线 + 5 个刻度（含两端）+ MHz 标签 +
单位，中间那个刻度标亮一点，代表本窗中心频点。两条边界要说清楚：

- 轴描述的是**最新一行**（当前窗）；扫频时上面旧行可能来自别的窗，所以轴不是
  「整张图的绝对横轴」，`wf2dAxis` 也只报最新一帧的 `lo/hi/center/ticks`。
- 刻度是按 `payload.freq_lo_hz..freq_hi_hz` 均分算的，不经 `modes/` 二次处理，
  界面与 `python -m app.live` 的横轴一致。

## 数据契约（Python → 前端）

所有信号都传**一个 JSON 字符串**（QWebChannel 会把 `QVariantList` 降级成 JS
`object`，字符串最稳）：

| 信号 | 频率 | 内容 |
|---|---|---|
| `info` | 开流时一次 | `{kind:"info", identity, port, baudrate, mock, scenario, gain_index, lang, profiles, plugins, plans:{id:[[center_hz,bw_hz]...]}, notes}` |
| `spectrum` | ≤20fps | `{kind:"spectrum", ts, module, center_hz, bw_hz, freq_lo_hz, freq_hi_hz, raw_bins, bins:[0..255]}` |
| `tables` | 1s | `{kind:"tables", plugins:[{id, name, weight, table:{title, columns, rows}, payload, error}]}` |
| `stats` | 1s | `{kind:"stats", slices, snapshots, elapsed_s, source_errors, last_source_error}` |
| `state` / `log` | 事件 | 状态机与日志文本 |

页面里 `probe()` 额外回给自检的键（不影响运行）：`frames`、`scanbar`、
`wf2d`（含 `paintedRows/distinctRows/topMean/bottomMean`）、`wf2dAxis`
（`{lo, hi, center, ticks}`，单位 MHz）、`three`。

自检另外收两路只有宿主才看得见的证据（`popout_pause` / `popout_resume` /
`popout_static_closes` 三项专验上面那张表，`view_gating` / `spectrum_gating` 验页面门控）：`Popout.selftest()`（浮层开 / 1.5× 缩放 /
归位、`--fit-grow`、面板高度 vs 浮层高度）与 Python 侧 `popout_window`（独立窗口
里真有画布、窗口标题、截图）、`popout_fullscreen`（`win.isFullScreen()` 为真、
全屏下面板仍吃满高度）。这三项是「弹出预览 + 窗口外弹窗」的验收凭据。

JS → Python 的入口：`listPorts()`、`scenarios()`、`renderText()`、
`start(configJson)` / `stop()`、`setFullscreen({on,target})`、`uiDefaults()`、
`runtime()`、`renderWaterfall()`（强制重渲瀑布图 PNG+HTML，返回产物名）、
`openFile(path)`（只在系统浏览器打开 `.data/` 下的产物，其它路径直接拒）、
`uiReport(json)`（都在 `bridge.js` 里封装成 Promise）。

## 窗口缩放（窗口态 / 最大化都不裁按钮）

外壳是 CSS Grid：顶栏 `auto` 行、侧栏 `clamp(124px, 11vw, 176px)`、页面列
`minmax(0, 1fr)`、页脚 `auto` 且 `max-height: 26vh`。三条硬约定：

1. **顶栏允许换行**（`.top { flex-wrap: wrap; max-height: 42vh; overflow-y: auto }`）。
   窗口变窄时「场景下拉 / 开始 / 停止 / EN」换到第二行，不许被挤出屏幕——
   `html, body` 是 `overflow: hidden`，挤出去的元素**没有滚动条、也没有报错**。
2. **只缩留白与字号，不隐藏任何可点控件**。`@media` 分 1680 / 1500 / 1280 /
   1024 / 900 五档收间距，`max-height: 760px` 再收页脚；`brand-sub` 与
   `gainbar__note` 是纯文案，可以隐，按钮/输入框/读数一律保留。
3. **≤900px 侧栏改横向标签条**（`body` 换成单列网格），把整行宽度让给页面。

自检里对应检查项是 `layout_shrink`：`Layout.probe()`（`web/js/app.js`）用
`getBoundingClientRect()` 量顶栏 12 个控件（串口 / 增益条 / 三个模式按钮 /
加减档 / 假固件 / 场景 / 开始 / 停止 / EN / 状态灯 / 侧栏 / 页脚）四边是否越界，
`run_selftest.layout_test()` 依次把窗口缩到 **1280×800 → 1024×680 → 900×620 →
760×560**，任何一档有元素越界就把它（带越界像素数）写进 `report["layout_bad"]`，
该项即假。实测四档全部 `bad: []`，顶栏在 1280 宽时自动变两行（`topH 73`）。

> 只量「在不在可视区」，不判断好不好看——截图仍只存盘不判读。

## 增益调控（顶栏三段 + 自动闭环，`host/gain.py`）

顶栏 `AGC | 手动 | 自动` 三段按钮 + `− 档号 +` 步进 + `目标` 输入框，作用的是
**真固件命令**，不是界面滤镜：`GAIN HARDWARE` / `GAIN MANUAL <index>`。
index 是 **PHY 表项**，既不等于 dB 也不线性。

三种模式各有各的用途，别混：

| 段 | 固件命令 | 干什么用 | 什么时候用 |
|---|---|---|---|
| AGC | `GAIN HARDWARE` | 固件自己挑档 | 只想知道「这段有没有信号」 |
| 手动 | `GAIN MANUAL n` | 钉死一档 | **横向比较必须固定档位**（占用率会随增益走） |
| 自动 | 主机闭环 | 每轮把底噪压到 `target` | 环境噪声漂了、想省掉人工试档 |

### 实板实测：档位 → 底噪（2440 MHz，每档一轮 35~40 帧取 p10 中位数）

```
  0~30 档 : 底噪 0.0 码                      → 动态范围被压死，什么都看不见
 35~45 档 : 6.0 / 6.0 / 8.5 码
 50~65 档 : 25.0 / 22.0 / 31.0 / 24.5 码      → 局部不单调，别指望一档一码
 70~81 档 : 30.3 / 47.0 / 51.5 / 63.0 码
```

跨度 63 码。所以：**同一张表里横向比数字，先看档位；档位不同，数字不可比。**

档位上限不是写死的：开机问一次固件 `GAIN?`（实板回 `GAIN MANUAL 60 0 81 1`），
拿它报的 `[min, max]` 覆盖兜底范围。写死 90 会给出「界面调了、固件不认」的档位。

### 自动闭环怎么算（`GainControl`）

- 控制量 = **一轮里所有帧 `bins_db` 的 p10 中位数**，再对轮之间做指数平滑
  （`AUTO_SMOOTH=0.4`）。**不是单帧**：实测同档同频点连扫 14 轮，单帧 p10 在
  **0 ~ 73 码**之间跳 —— 拿单帧当控制量就是在拿噪声追噪声。
- 一轮只决策一次（`GainAwareSource.scan()` 的 `finally` 里 `end_scan()`）。
- 死区 `AUTO_TOLERANCE=1.5` 码，步进 `AUTO_STEP=2` 档，热身 `AUTO_WARMUP=2` 轮。
- **节奏只由 `AUTO_CONFIRM=2`（连续两轮同方向越界）决定，不用墙上时间做冷却**：
  第一版写了 `AUTO_COOLDOWN_S=1.2`，而实板一轮约 1.1 s，于是每一轮都被冷却卡住，
  真板上 14 轮只动了一档 —— 闭环看着在跑，其实停摆。
- 单帧 p10 = 0（没扫到 / 全零）的帧不进控制量（`DEGENERATE_FLOOR`）：实板这种帧很常见，
  拿它当控制量会把档位往错误方向推。
- `--mock` 下不跑闭环：假固件的 `GAIN` 是空转，调档只会给用户看假动作。

真板实测（起始 60 档、目标 8 码、14 轮）：`60 → 58 → 56 → 54 → 52 → 50 → 48`，
平滑底噪 `39.0 → 14.4`，每两轮动一档，方向一直朝目标。

### 线程纪律（踩过）

`GAIN` 命令只能在**两次扫描之间**发。扫描中途插一条命令，串口读缓冲会被上一轮
残留的二进制打乱，下一次 `scan()` 直接抛 `SPEC 启动回复异常：['\x00\x00…']`。
所以 GUI 线程只往 `GainControl` 里塞意图，真正下发在 `GainAwareSource.scan()` 开头。

自检里对应 5 项：`gain_manual`（点「手动」）、`gain_step`（点 `+` 档号 +1）、
`gain_auto`（点「自动」→ `mode == "AUTO"`）、`gain_on_button`（按钮高亮集合 == `["AUTO"]`）、
`gain_readback`（`#gain-read` 里出现「底噪」字样）。

## BLE 风险评估页（`pages/ble/`）

数据来自 `modes/ble_risk.py` 的 `ble_report()`（`tables` 信号推送，`kind == "ble_risk"`）。
页面结构：告警条 → 5 张判定卡（判词 / 宽带 / 开关幅度 / 广播活动 / 数据信道）→
6 行证据链 → 9 列广播信道表 → 3D 信道柱 → 2.4G 在用信道 → `hints` / `notes` 原文，
外加 `#ble-sample` 存一份完整 payload 供排查。

- **门槛全部从 `payload.thresholds` 取**（`presence_db`、`narrow_ratio`、`contrast_db`、
  `light_ratio`、`min_adv_frames`、`headroom_codes`、`wideband_frac`、`wifi_busy_fill`…），
  前端**一个判据数字都不许写死**：改判据只改模块，网页跟着走。
- **判词不翻**：`risk_type` / `hints` / `notes` 一律显示模块给的中文原文，
  翻了就跟 `python -m app.live` 的 CLI 输出对不上。
- `2.4G 在用信道` 用 `wifi_channels()` 的 `fill`（0~1 占用比例）判「在用」，
  再拿 `payload.overlap` 里 `fraction >= 0.9` 标「硬冲突」；不用电平判在用。
- 自检 6 项：`ble_rows`（≥3 行）、`ble_risk`、`ble_evidence`（≥6 行）、`ble_hints`、
  `ble_notes`、`ble_bars3d`。

## 周期瀑布图页（`pages/waterfall/`）

数据来自 `modes/waterfall.py` 的 `web_payload()`（经 `tables` 信号 1 Hz 推送，
`payload.kind == "waterfall"`）。三个口弸跟 PNG 完全一致：

- **一行 = 一轮完整轮扫**（不是 1 s 时间桶）；一行里三个频段扫到的时刻不同，
  所以时间刻度取每轮首帧的 `ts`。
- **一格一字节**：`bands[].cells` 是 base64（行优先，长 = `rows × cols`），
  0 码 = 这一格没扫到，渲染成最暗底色，不当成“信号为 0”。
- **同一格多窗覆盖取最大保持**（后端 `fill_row` 已算好）；前端不重新归一化
  功率码，只用 `code_lo/code_hi` 拉伸颜色（只影响显示，不改原值）。

颜色映射用 `U.heatRGB()`，与 2D/3D 瀑布共用同一条色带；表格/曲线上还是一套
"分档 → 颜色"（`U.levelColor()`），两套不许混。

`produce()` 在上位机里**做最小间隔节流**（`PRODUCE_MIN_INTERVAL_S=15`）：
`tables` 每秒都 poll 一次，不节流会每秒重渲一遍 PNG；“生成 PNG + HTML”按钮走
`Bridge.renderWaterfall()` → `produce(force=True)` 绕过节流。产物落在
`.data/waterfall/`（`waterfall-p*.png` / `.html` + `index.html`）。

**环形缓冲的粒度是「窗」不是「帧」**：真板一次 `scan()` 能回 30+ 帧，
一帧一条的话默认 `RING_FRAMES=1200` 连一轮（44 窗 × ~37 帧）都装不下，
页面永远只画得出一行半。`on_snapshot()` 对同中心频率的连帧做最大保持合并，
`frames` 仍数真帧、`ring_frames` 才是缓冲长度。卡片上的「一轮 X s」优先取
`row_period_s`（实测墙钟周期，只用**扫满**的轮次估；凑不齐两整轮则为 0，
退回射频时间）——多模块分时会把一轮明显拉长，这是真实读数，不是显示 bug。

## 边界（和 CLI 输出同一套口径，界面不许美化）

- **周期快照，非连续实时**：帧与帧之间有接收空档，瀑布是"离散时间片堆叠"。
- **功率码是相对 dB 码（0..255），不是 dBm**；纵轴标注写的就是"功率码"。
- **固定增益才有可比性**：跨信道比较前先钉 `--gain-index 60`；界面上 `硬件 AGC`
  和 `固定档 N` 分开显示，不混着比。自动闭环把底噪压到同一目标也算「固定基准」，
  但它每轮可能微调一档，跨会话对比仍然要记下当时档位。
- **分类是启发式**：WiFi 体检只说"相对抬升多少 dB"，不说"这里一定是 WiFi"。
- 页面里的"推荐信道"直接取自 `modes/wifi_check.py` 的 `web_payload()`，前端
  **不做二次判定**，保证界面和 `python -m app.live` 的结论一致。

## 已经踩过的坑（都修了，别再踩）

| 现象 | 根因 | 修法 |
|---|---|---|
| 前端 `Bridge.ready()` 一直 false，`window.backend` 不存在 | `QWebChannel` 只交给了 `setWebChannel()`，Python 侧没留引用被 GC | `HostWindow.channel` 持强引用（`QWebChannel(self)`） |
| 页面白屏、网络面板无报错 | QWebEngine 的 `fetch()` 不支持 `file://` | 一律用 `XMLHttpRequest` 加载本地片段（`LocalContentCanAccessFileUrls=True`） |
| 画布全黑但帧数 > 0 | 页面切走后 `requestAnimationFrame` 停、画布尺寸 0 | `fitCanvas()` 只在 `onEnter` 里取容器尺寸；3D 组件在 `onLeave` 里 `dispose()` |
| 游标一秒刷完 90 多轮、覆盖数一直是 1 | 假固件不吃 `dwell_ms`，一个窗口 3ms 就跑完 | `Runner._mock_pace()` 补节奏（见上节） |
| 扫频进度条上没有游标 / 显示「等待扫描计划」 | `info` 是开流后才到，页面 `onEnter` 时还没有 `plans` | `app.js` 把 `info` 也 `Router.dispatch("Info", …)`，页面 `onInfo` 里补 `applyPlan()` |
| 弹出预览一把静态文案全翻回默认值（`#raw-text` 的 ASCII 被清成「未连接」） | `Popout` 里为了翻按钮标题调了 `I18N.apply(整个面板)`，把 `data-i18n` 占位的**动态内容**一起重写了 | 只 `I18N.apply(工具栏)` + 逐个设按钮 `title`，绝不 apply 面板 |
| 新加的按钮（如「生成 PNG + HTML」）点了没反应，Promise 直接 resolve 成 `[]`，Python 侧连日志都没有 | `@Slot(str, result=str)` 写在了**无入参**的方法上：Qt 按“要一个 str 入参”注册，网页只传回调就被吞掉 | `@Slot` 的参数表必须和方法签名严格一致（无入参就只写 `result=str`）；靠 `--selftest` 里真点一次按钮的 `waterfall_render` 项守着 |
| 关闭浮层后面板还在浮层里 / 打不开第二次 | 浮层元素只是 `hidden`，探针按「元素存在」判断成没关 | 判据改成 `.popout:not([hidden])` 与 `overlay.hidden`；关闭时把面板 `insertBefore` 回原位 |
| 切页后弹出去的实时流面板永远恢复不了（一直在暂停） | `decorate()` 会遍历 `#page-container` 下所有 `.panel`，而浮层就挂在那里 → 把已弹出的 wifi 面板重新盖章成「ble 页的」，切回 wifi 被判成跨页 | `decorate()` 见到 `panel--popped` 直接 `return`，不再改归属 |
| 弹窗一开就报 `NotFoundError: insertBefore`，面板卡在半开状态 | `close()` 复原时用 `home.next` 当锚点，但那一页的 DOM 早被 `innerHTML =` 换掉了，锚点成了野指针 | 复原前校验 `home.next.parentNode === home.parent`，不成立就退回 `appendChild` 语义 |
| 自检跑到某一页整进程消失（无回溯，`exit=127`） | 在 `runJavaScript` 的**结果回调里**接着发起下一次 `runJavaScript`，QWebEngine 在同一 C++ 事件循环里重入 → 线程栈打爆 | 每次 `runJavaScript` 之间一律用 `QTimer.singleShot()` 排程；改用 `tests/probe_popout.py`（20 s）替代整套自检（3.5 min）来迭代弹窗逻辑 |
| 弹出/放大后画布高度一点没变，下面一大片空白 | 页面 resize 钩子用 `root.querySelector("#spec-canvas")`，面板被搬到 `body` 下的浮层后就不在 `root` 里了 → 拿到 `null` 抛 `TypeError`，整段重绘中断 | 浮层挂到 `#page-container` 内（`position: fixed`，视觉不变），`fitCanvas(null)` 也返回空对象不再抛错 |
| 点「全屏」没反应，布局跟普通窗口一模一样 | 合成点击没有用户手势，Chromium 拒 `requestFullscreen()`（`QtWebEngine` 的 `fullScreenRequested` 压根没触发） | 全屏走 `Bridge.setFullscreen` → Python `win.showFullScreen()`；DOM API 只作纯浏览器兜底 |
| 独立窗口里画面空白/不刷新 | 新窗口晚连上，`QWebChannel` 之前的信号全丢了 | `runtime()` 带回最后 `spectrum`/`tables`，`syncRuntime()` 直接回放；`HostWindow` 缓存两份 JSON |
| 窗口态 / 最大化切换后「开始 / 停止 / EN」看不见，最大化才出来 | 外壳 `html, body { overflow: hidden }` + 顶栏固定 `52px` 高、`flex:none` 不换行：内容总宽 > 窗口宽时，最右边那几个按钮直接被裁掉，**既没有滚动条也不报错**；页面列又写成 `1fr`（= `minmax(auto,1fr)`），宽表格能把整页撑宽 | 顶栏 `flex-wrap: wrap` + 行高 `auto` + `max-height: 42vh`；页面列 `minmax(0, 1fr)`；`@media` 五档收间距（只收留白/字号，不隐藏控件）；≤900px 侧栏改横向条。自检新增 `layout_shrink`：4 档窗口宽逐个量越界像素（`report["layout_bad"]`） |
| 点自检必崩：`Windows fatal exception: stack overflow`，exit=127，**没有 Python 回溯**，主线程停在 `app.exec()` | `finish()` 是模块级函数 `run_selftest(...)` 里的闭包，里面误用了 `self.session` / `self.runner`：`self` 在该作用域根本不存在 → `NameError` 在 Qt 回调里抛出，PySide6 直接进程级栈溢出。包在 `try/except` 里的那半段更阴——异常被吞掉，`share` 一直是 `{}` | 闭包里一律 `window.xxx`；**别在主线程读正在跑的 `QThread` 实例属性**，用窗口持有的普通 dict（`stats_box`）中转收集线程的数据 |
| 瀑布图页「没数据」但后端在读射频：一轮 4.4 s 实际走了 5 分钟 | `slice_weight` 悬殊（`wifi_check=40.0` vs `waterfall=0.8`），平滑加权轮询把射频几乎全给了 wifi，share 实测 `{waterfall: 0.016}` | 单射频共享要**公平配比**：两边都 `slice_weight = 1.0`（各半），selftest 的 `share`/`waterfall_share` 项守着（实测 0.5 / 0.5） |
| 扫频进度条上 5G 整段消失、`visited` 只到 5 | 进度条跟了「字典序第一个」计划，`ble_risk` 只扫 2.4G 8 窗 | 取**跨度最宽**的计划（`widestPlan()`，并列按 id 排序），不是第一个 |
| 界面点「自动」后档位纹丝不动 | `AUTO_COOLDOWN_S=1.2` 比一轮扫描（约 1.1 s）还长，每轮都被冷却拦掉 | 节奏改用「连续 `AUTO_CONFIRM` 轮同方向越界」，与扫描耗时解耦 |
| 自动闭环偶尔把档位往错方向推 | 单帧 p10 = 0（没扫到）被当成「底噪太低」 | `DEGENERATE_FLOOR` 丢弃这类帧；控制量取一轮中位数 + 指数平滑 |
| `GAIN` 之后下一次 `scan()` 抛 `SPEC 启动回复异常` | 命令插在扫描中途，或上一轮帧没读完就 break | 命令只在 `GainAwareSource.scan()` 开头落地；读循环不许半路 break |
| 多模块同时跑时，扫频进度条游标乱跳、`visited` 一直是 1 | 页面每收到一帧就按 **该帧所属模块** 重设计划，计划在 `waterfall`/`wifi_check` 之间来回换，`visited` 每帧被清空 | 进度条**只跟一个模块**的计划（`planModule` 固定首个计划），只喂匹配模块的帧 |
| 20 MHz 窗 / 15 MHz 步进下，进度条覆盖数涨得极慢 | 窗是**重叠**的，相邻格最近匹配会前后抖；「索引变小 = 换轮」被抖动误判成换轮，反复清空 `visited` | 换轮判据加幅度门槛：只有「跳回大半张图」（`index - found > plan.length / 2`）才算换轮 |
| 网页报错查不到 | 默认不打印前端 console | `ConsolePage.javaScriptConsoleMessage` 转发到 stdout（`[js:err] 文件:行 消息`） |