/*
 * i18n.js —— 中英文文案表 + 切换（不联网、不引第三方库）。
 *
 * 用法：
 *   · 静态文本：HTML 写 <span data-i18n="hdr.start">开始</span>，切换时自动改；
 *   · 动态文本：JS 里 T("wifi.card.rec24") 取文案，T("key", {n: 3}) 带占位符；
 *   · 页面注册表：registry.json 里给 title_en/hint_en，路由按当前语言取；
 *   · 语言变了会触发 window 上的 "langchange" 事件，页面自己重画动态部分。
 *
 * 约定：模块（modes/）自己产出的表格/ASCII 沿用模块语言（和 CLI 一致），不在这里翻——
 * 界面只翻「外壳 + 结构化数据」。英文界面下模块那份中文**在 Python 侧就被换成英文了**
 * （见 host/l10n.py：所有下行 payload 统一过 localize()），所以这里不需要（也不应该）
 * 去翻模块正文。
 *
 * 英文模式**绝不退回中文**：缺 key 时返回 key 本身并记进 missing()，自检会把
 * 「英文界面里还剩中文」和「en 词典缺 key」都当失败。
 */
window.I18N = (function () {
  const STORAGE_KEY = "espsdr.lang";
  const SUPPORTED = ["zh", "en"];

  const DICT = {
    zh: {
      "app.title": "esp-sdr 测试上位机",
      "app.subtitle": "ESP32-C5 射频监测 · 周期快照，非连续实时",
      "hdr.port": "串口",
      "hdr.gainTitle": "增益档（PHY 表索引，不是 dB）",
      "hdr.mock": "假固件",
      "hdr.scenario": "场景",
      "hdr.start": "开始",
      "hdr.stop": "停止",
      "hdr.lang": "EN",
      "hdr.langTitle": "切到 English",
      "status.idle": "未连接",
      "status.starting": "连接中…",
      "status.running": "采集中",
      "status.error": "错误",

      "kv.firmware": "固件",
      "kv.transport": "传输",
      "kv.port": "串口",
      "kv.mode": "模式",
      "kv.gain": "增益",
      "kv.modules": "模块",
      "kv.slices": "时间片",
      "kv.snapshots": "快照",
      "kv.frames": "已推帧",
      "kv.rfSeconds": "射频秒",
      "kv.errors": "错误",
      "kv.lastError": "最后错误",
      "kv.gainAgc": "硬件 AGC",
      "kv.gainFixed": "固定档 {n}",
      "kv.mockMode": "假固件/{scenario}",
      "kv.realMode": "真机",

      "foot.next": "下一步：真机跑一遍同名场景（python host/app.py --port COM56 --gain-index 60）",
      "foot.stampTitle": "这个窗口正在跑的代码版本；比磁盘上的文件旧就说明是上一个进程，重启才有新代码",
      "foot.nextMock": "现在看的是假固件数据；接板上真机见 host/README.md",

      "log.ready": "桥已就绪（window.backend 可用）",
      "log.noChannel": "QWebChannel 未连上：页面是不是被别的浏览器打开了？",
      "log.portsFailed": "读串口失败：{error}",
      "log.noPorts": "没读到任何串口，插好板子再点开始",
      "log.startFailed": "启动失败：{error}",
      "log.bridgeFailed": "桥调用失败：{error}",
      "log.stopFailed": "停止失败：{error}",
      "log.stopped": "已停止",
      "router.loadFailed": "页面片段加载失败",
      "log.langSwitched": "界面语言已切换，历史日志已清空（旧日志是切换前语言的）。",
      "log.noBackend": "桥未就绪（页面还没连上 Python）",
      "log.noSlot": "Python 侧没有这个方法：{name}",

      "popout.hint": "双击任意面板可弹出预览；Esc 关闭",
      "popout.dblHint": "双击放大预览",
      "popout.zoom": "缩放",
      "popout.reset": "重置",
      "popout.full": "全屏",
      "popout.window": "独立窗口",
      "popout.exitFull": "退出全屏",
      "popout.close": "关闭",
      "popout.paused": "模式已切换：本面板已暂停刷新（切回「{page}」页自动继续；汇总型面板不会停留）",
      "scanbar.title": "扫频进度（频段地图）",
      "scanbar.hint": "游标 = 正在读的窗；一段 = 一个频段，一小格 = 一个 20MHz 窗；扫到最右会回到最左继续下一轮",
      "scanbar.now": "当前 {center} MHz · 第 {at}/{total} 窗 · 第 {pass} 轮",
      "scanbar.noplan": "该模块没有声明扫描计划（scan_requests）— 画不出频段条",
      "scanbar.heatLow": "冷（空）",
      "scanbar.heatHigh": "热（挤）",
      "scanbar.heatHint": "格子越亮 = 该窗历史占用越强（跨轮累积，相对码）",
      "scanbar.wait": "等待扫描计划…",
      "spectrum.title": "实时频谱（当前窗口）",
      "spectrum.hint": "横轴 = 频率（本窗口），纵轴 = 功率码（相对 dB 码，不是 dBm）；实线 = 当前帧，紫线 = 峰值保持",
      "spectrum.wf3d.title": "3D 瀑布",
      "spectrum.wf3d.hint": "越靠前越新；高度 = 功率码；鼠标左键旋转 / 滚轮缩放",
      "spectrum.wf2d.title": "2D 瀑布（最近 {rows} 帧）",
      "spectrum.wf2d.hint": "每帧一行，自上而下变旧；底部频率轴描述最新一行（扫频时旧行的窗可能不同）；对比度按本帧动态范围自动拉伸",
      "spectrum.latest": "最新",
      "spectrum.mhz": "MHz",
      "spectrum.range": "码 {lo}~{hi}",
      "spectrum.tag": "{module} · 中心 {center} · 带宽 {bw} · {raw} bin → {out} 点",
      "spectrum.noThree": "three.js 没加载（检查 web/assets/vendor/three.min.js）",
      "spectrum.peak": "{freq} / 码 {code}",

      "wifi.card.wait": "等待样本",
      "wf.banner.title": "周期快照，非连续实时",
      "wf.banner.hint": "单射频分时扫描：帧与帧之间有接收空档；图上每一行是「真的扫到过」的一轮，不是连续采样",
      "wf.banner.row": "一行 = 一轮完整轮扫（顺序扫完三个频段的所有窗），不是 1 秒时间桶",
      "wf.banner.code": "颜色 = 功率码（相对 dB 码，不是 dBm）；0 码 = 这一格没扫到，画最暗底色",
      "wf.banner.sweep": "同一格被多个重叠窗扫到时取最大保持；轮扫周期 = 窗数 × 驻留时间",
      "wf.card.sweep": "轮扫周期",
      "wf.card.sweepNote": "射频 {rf} s：{windows} 窗 × {dwell} ms",
      "wf.card.sweepWall": "实测墙钟 {wall} s/轮（多模块分时会拉长）",
      "wf.card.sweeps": "已扫轮数",
      "wf.card.sweepsNote": "一行 = 一轮完整轮扫",
      "wf.card.sweepsPending": "当前轮已扫 {done}/{total} 窗（扫满才画成一行）",
      "wf.card.windows": "覆盖窗口",
      "wf.card.windowsNote": "20 MHz 窗 / {step} MHz 步进（{bw} MHz 宽，重叠滑扫）",
      "wf.card.buffer": "环形缓冲",
      "wf.card.bufferNote": "收到帧 {frames} 个；满了滚掉最旧的",
      "wf.card.wait": "等待数据",
      "wf.wait": "还没有数据：点顶部「开始」，或先跑 app/waterfall.py",
      "wf.map.title": "全频段时频瀑布",
      "wf.heat.title": "记忆热力",
      "wf.heat.hint": "每格一个字节：这次扫到信号 +1（封顶 255），这次扫不到 -1（触底 0）；不存历史帧",
      "wf.heat.clear": "一键清除记忆",
      "wf.heat.idle": "等待数据",
      "wf.heat.info": "{n} 格 · 最高 {max} · 过半 {hot} 格",
      "wf.heat.cleared": "已清除 {n} 格记忆",
      "wf.heat.fail": "清除失败：{error}",
      "wf.map.hint": "横轴 = 频率（三个频段各自等宽显示，刻度按各自跨度）；纵轴 = 时间（自上而下变旧）；颜色 = 历史占用强度，每段按自己的 p5-p95 拉伸（段标题旁标出码值范围）",
      "env.banner.title": "哪些信道异常，谁占着它们",
      "env.banner.hint": "本页数据来自本机无线网卡的管理帧解析，不是板子测的；板子只提供能量",
      "env.card.ssid": "可见热点",
      "env.card.ssidNote": "含隐藏 SSID",
      "env.card.bss": "射频条目",
      "env.card.bssNote": "一个 SSID 可跨多个频点",
      "env.card.hidden": "隐藏网络",
      "env.card.hiddenNote": "有能量但读不到名字",
      "env.card.known": "已灌入固件",
      "env.card.knownNote": "当前自动下发到固件的频点数",
      "env.act.title": "扫描与下发",
      "env.act.hint": "名单是快照：路由器会自动换信道，建议每 60~120 秒重扫重灌",
      "env.watch.title": "自动监测模式",
      "env.watch.hint": "连上设备即自动启动；两种模式在射频侧用同一份常驻采集，差别是多久重扫一次 WiFi",
      "env.watch.off": "关闭",
      "env.watch.daily": "日常",
      "env.watch.burst": "狂暴",
      "env.watch.now": "立刻检查一次",
      "env.watch.interval": "日常模式间隔（秒）",
      "env.watch.idle": "未启动",
      "env.watch.stateOff": "已关闭",
      "env.watch.stateDaily": "日常监测中",
      "env.watch.stateBurst": "狂暴中",
      "env.watch.next": "下一轮",
      "env.watch.rounds": "已检查",
      "env.watch.lastFreqs": "上轮下发频点",
      "env.watch.lastErr": "上轮失败",
      "env.act.scan": "扫描 WiFi 环境",
      "env.act.push": "下发已知热点到固件",
      "env.act.clear": "清空固件名单",
      "env.act.idle": "还没扫描",
      "env.ch.title": "信道占用表",
      "env.ch.hint": "利用率 = AP 在 BSS Load 元素里自报的信道占用率（÷255），不是我们测的能量；— = AP 没上报",
      "env.th.mhz": "频率",
      "env.th.ch": "信道",
      "env.th.ssid": "SSID",
      "env.th.bssid": "BSSID",
      "env.th.sig": "信号",
      "env.th.util": "利用率",
      "env.th.sta": "客户端",
      "env.notes.title": "判读说明与边界",
      "env.empty": "没有扫描到任何 WiFi（本机无线网卡可能被禁用）",
      "env.hiddenSsid": "（隐藏 SSID）",
      "env.scanning": "正在扫描…",
      "env.scanFail": "扫描失败：",
      "env.scanOk": "扫描完成：",
      "env.scanAuto": "已自动扫描：",
      "env.hotspots": " 个热点",
      "env.channels": " 个频点",
      "env.noBridge": "桥未就绪（QWebChannel 还没连上）",
      "env.pushing": "正在下发…",
      "env.pushFail": "下发失败：",
      "env.pushOk": "已下发 ",
      "env.pushOk2": " 个频点",
      "env.pushFull": "已达固件上限 48，多余频点被截断",
      "env.clearOk": "固件名单已清空（板子随后会把所有有能量的频点都算成「未知」）",
      "env.note.1": "本表来自本机无线网卡：SSID / BSSID / 信道是解析 802.11 管理帧得到的，不是板子测的。",
      "env.note.2": "「利用率」是 AP 在 BSS Load 元素里自报的信道占用率，反映这个 AP 有多忙；板子测到的能量反映这个频段有多挤。两者不是一回事。",
      "env.note.3": "板子没有 WiFi 协议栈，拿不到 SSID。它只能回答「这个频点有能量，而且不在你灌进来的名单里」——那份名单就是板子判「未知」的差分基准。",
      "env.note.4": "路由器会自动换信道（ACS）。名单是快照，过期后会把换过信道的老 AP 误报成「未知」；单帧结论不可信，持续多轮无主才可信。",
      "env.note.5": "单天线、无相位信息，只能知道「在这个频点」不能知道「在哪个方向」。要定位只能拿手机走近实测。",
      "wf.cursor.idle": "鼠标移到图上可读频率 / 功率码",
      "wf.legend.title": "亮 = 高出本段底噪",
      "wf.band.meta": "底噪 {ref} · 峰 {max}",
      "wf.band.empty": "本段暂无可视数据",
      "wf.band.emptyBox": "这一段还没扫到",
      "wf.cursor": "{band} · {freq} · 功率码 {code} · 第 {row}/{rows} 行（+{t}）",
      "wf.report.title": "出图（PNG + HTML 翻页）",
      "wf.report.hint": "按缓冲里现有的帧渲染；要小时级历史请用 app/waterfall.py 读 SQLite",
      "wf.report.btn": "生成 PNG + HTML",
      "wf.report.btnBusy": "生成中…",
      "wf.report.open": "在浏览器打开",
      "wf.report.idle": "还没有出图",
      "wf.report.done": "已生成 {pages} 页：{png}（PNG）/ {html}（HTML 索引）",
      "wf.report.fail": "生成失败：{error}",
      "wf.notes.title": "判读说明与边界",
      "wf.notes.rows": "当前画了 {rows} 行（共扫完 {sweeps} 轮）",
      "wf.notes.cover": "覆盖：{cover}",
      "wf.notes.window": "窗",
      "wf.notes.code": "颜色以每段底噪为 0 点、高出 {span} 码即满亮（全图码域 {lo}..{hi}，只影响颜色，不改原始值）",
      "wf.notes.pending": "当前轮已扫 {done}/{total} 窗：没扫满不画成一行，所以 5G HIGH 现在看起来「还没到」是正常的",
      "wf.notes.partial": "最新一轮还没扫满（缺 {missing} 窗）：缺的格子按「没扫到」画最暗色，不当成信号为 0",
      "wf.notes.png": "PNG/HTML 由 output/png.py 手写字节流 + 纯标准库渲染，中文一律落到 HTML",


      "level.重度": "重度",
      "level.拥挤": "拥挤",
      "level.轻度": "轻度",
      "level.空闲": "空闲",
      "level.无覆盖": "无覆盖",

      // ---- 增益控件（gain.js）----
      "gain.label": "增益",
      "gain.hw": "AGC",
      "gain.hwTitle": "固件硬件 AGC：由固件自己挑档，测「这里有没有信号」用它；不能横向比占用率",
      "gain.manual": "手动",
      "gain.manualTitle": "固定档位（PHY 表索引，不是 dB）。横向比较不同时间/场景必须固定档",
      "gain.auto": "自动",
      "gain.autoTitle": "主机闭环：每帧看底噪参考码，把档位调到目标附近（降增益能压掉前端固有起伏）",
      "gain.down": "降一档",
      "gain.up": "升一档",
      "gain.target": "目标",
      "gain.targetTitle": "自动模式的目标底噪参考码（功率码，不是 dBm）",
      "gain.read": "档 {index} · 实测 {eff} · 底噪 {floor}",
      "gain.codes": "{n} 码",
      "gain.adjusts": "已调 {n} 次",
      "gain.failed": "增益命令失败：{error}",
      "kv.gainFloor": "底噪 {code}",
      "kv.gainAuto": "自动调 {n} 次",

      // ---- BLE 风险页 ----
      // ---- 抓拍页（capture.js）----
      // ---- 信道占用页（occ.js） ----
      "occ.banner.title": "信道占用统计（板子实测）",
      "occ.banner.hint": "占空比 = 该信道「有传输」的时间比例，不是信号强度",
      "occ.banner.ema": "「当前」约 1 秒时间常数；板端是逐窗 EMA（τ ≈ 24 秒），两者别逐位比。",
      "occ.banner.unmatched": "有 {n} 帧没落到任何已知信道上（宽窗中心之外的频率）：这些帧不计入任何一行。",
      "occ.card.swept": "已有样本的信道",
      "occ.card.sweptNote": "没扫到的信道不显示成 0%，那是两回事",
      "occ.card.busiest": "当前最忙",
      "occ.card.busiestNote": "按「当前占空比」取最高",
      "occ.card.idle": "全都很安静",
      "occ.card.frames": "统计样本",
      "occ.card.framesNote": "对上信道的帧数 / 进入统计的帧数",
      "occ.card.framesNote2": "对上信道的帧 / 进入统计的帧 · 已统计 {s} 秒",
      "occ.tbl.title": "逐信道占用",
      "occ.tbl.hint": "板子一侧统计（上位机累加），能测别人家的协议：BLE 广播、图传、遥控、无绳电话",
      "occ.th.mhz": "频率",
      "occ.th.ch": "信道",
      "occ.th.band": "频段",
      "occ.th.now": "当前",
      "occ.th.max": "峰值",
      "occ.th.avg": "平均",
      "occ.th.round": "占空比",
      "occ.th.unswept": "未扫到",
      "occ.act.sort": "按占用排序",
      "occ.act.sortFreq": "按频率排序",
      "occ.act.csv": "导出 CSV",
      "occ.act.reset": "清零",
      "occ.st.csv": "已导出 {name}",
      "occ.st.reset": "统计已清零",
      "occ.st.error": "失败：{error}",
      "occ.empty": "还没有数据：先在上方点「开始」让板子扫起来",
      "occ.noBridge": "桥未就绪（QWebChannel 还没连上）",
      "occ.notes.title": "怎么读这张表",
      "occ.notes.hint": "口径写在明处，免得把 0% 当成「这里没人」",
      "occ.notes.judge": "判据与圆屏第二页同源：一帧取中间一半 bin，p50 − p25 > 15 码判「有传输」。功率码是相对值，不是 dBm。",
      "occ.notes.now": "「当前」是逐帧 EMA（α = 1/8，约 1 秒时间常数），比板端（逐窗 EMA，τ ≈ 24 秒）跳得快，两者只比高低不比绝对值。",
      "occ.notes.max": "「峰值」要盯：BLE 广播、遥控这类协议很尖，平均值会被摊薄，峰值远高于当前值就说明是突发发射。",
      "occ.notes.avg": "「平均」= 累计命中帧 / 总帧，看的是「这一整段时间里一直在发射吗」；图传、视频流是这种形态。",
      "occ.notes.unseen": "「—」= 这一轮扫描没落在这个信道上，不是 0%。宽窗扫频时很多信道压根没被单独测过。",
      "occ.notes.gain": "信道之间横比必须固定增益（顶栏 GAIN 固定档）；硬件 AGC 会把每个信道都拉到相近电平，差异被吃掉。",

      // ---- 圆屏轮播页（round.js） ----
      "round.title": "圆屏轮播（真板自动循环翻页）",
      "round.hint": "控制板子圆屏三页（频谱热力 / 信道占用 / 时频瀑布）自动循环切换；秒数 = 每页停留时间，全 0 = 关闭",
      "round.p1": "页 1 秒数",
      "round.p2": "页 2 秒数",
      "round.p3": "页 3 秒数",
      "round.start": "开始轮播",
      "round.stop": "停止轮播",
      "round.query": "读回板子状态",
      "round.on": "轮播运行中",
      "round.off": "轮播已关",
      "round.noBridge": "桥未就绪（QWebChannel 还没连上）",
      "round.hintNeedRun": "提醒：轮播命令随采集间隙发上板子——若点了没生效，先点顶栏「开始采集」再试，或点「读回板子状态」确认。",
      "round.stateFmt": "板子秒数 {s}（{on}）· 已下发 {n} 次 / 读回确认 {a} 次",
      "round.faqTitle": "使用说明与边界",
      "round.faq1": "轮播是板子固件里的显示层循环（RD VIEWSEC），上位机只负责写入秒数；写入后即使关掉上位机，板子也会一直转，直到再写全 0 或断电。",
      "round.faq2": "命令在两次扫描之间发（同增益通道纪律），点按钮后可能要等下一个扫描间隙才落地；「读回板子状态」能看到固件确认的秒数。",
      "round.faq3": "页 2 秒数给长一点（默认 25 s）：信道占用表 8 页翻完需要时间，太短会来不及看。",

      "level.无数据": "无数据",
    },
    en: {
      "app.title": "esp-sdr Test Host",
      "app.subtitle": "ESP32-C5 RF monitor · snapshot mode, not continuous",
      "hdr.port": "Port",
      "hdr.gainTitle": "gain index (PHY table entry, not dB)",
      "hdr.mock": "Fake FW",
      "hdr.scenario": "Scenario",
      "hdr.start": "Start",
      "hdr.stop": "Stop",
      "hdr.lang": "中文(CN)",
      "hdr.langTitle": "Switch to Chinese",
      "status.idle": "Idle",
      "status.starting": "Connecting…",
      "status.running": "Running",
      "status.error": "Error",

      "kv.firmware": "Firmware",
      "kv.transport": "Link",
      "kv.port": "Port",
      "kv.mode": "Mode",
      "kv.gain": "Gain",
      "kv.modules": "Modules",
      "kv.slices": "Slices",
      "kv.snapshots": "Snapshots",
      "kv.frames": "Frames out",
      "kv.rfSeconds": "RF seconds",
      "kv.errors": "Errors",
      "kv.lastError": "Last error",
      "kv.gainAgc": "HW AGC",
      "kv.gainFixed": "fixed index {n}",
      "kv.mockMode": "fake firmware/{scenario}",
      "kv.realMode": "real board",

      "foot.next": "Next: run the same scenario on the real board (python host/app.py --port COM56 --gain-index 60)",
      "foot.stampTitle": "Code version this window is running; older than the files on disk means a stale process - restart to pick up new code",
      "foot.nextMock": "Showing fake-firmware data; see host/README.md for the real board",

      "log.ready": "Bridge ready (window.backend available)",
      "log.noChannel": "QWebChannel not connected — was this page opened in a normal browser?",
      "log.portsFailed": "Failed to list serial ports: {error}",
      "log.noPorts": "No serial port found — plug the board in and press Start",
      "log.startFailed": "Start failed: {error}",
      "log.bridgeFailed": "Bridge call failed: {error}",
      "log.stopFailed": "Stop failed: {error}",
      "log.stopped": "Stopped",
      "router.loadFailed": "Failed to load page fragment",
      "log.langSwitched": "UI language changed — previous log lines were cleared (they were in the old language).",
      "log.noBackend": "Bridge not ready (the page has not connected to Python yet)",
      "log.noSlot": "Python has no such slot: {name}",

      "popout.hint": "Double-click any panel to pop it out; Esc closes",
      "popout.dblHint": "Double-click to pop out",
      "popout.zoom": "Zoom",
      "popout.reset": "Reset",
      "popout.full": "Fullscreen",
      "popout.window": "Separate window",
      "popout.exitFull": "Exit fullscreen",
      "popout.close": "Close",
      "popout.paused": "Mode switched: this panel is paused (switch back to \"{page}\" to resume; summary panels are not kept)",
      "scanbar.title": "Scan progress (band map)",
      "scanbar.hint": "Cursor = window being read; one block = one band, one cell = one 20MHz window; at the right edge it wraps back to the left for the next pass",
      "scanbar.now": "now {center} MHz · window {at}/{total} · pass {pass}",
      "scanbar.noplan": "This module declares no scan plan (scan_requests) — cannot draw a band bar",
      "scanbar.heatLow": "cold (idle)",
      "scanbar.heatHigh": "hot (busy)",
      "scanbar.heatHint": "brighter cell = that window has been busier over time (accumulated across passes, relative codes)",
      "scanbar.wait": "waiting for scan plan…",
      "spectrum.title": "Live spectrum (current window)",
      "spectrum.hint": "X = frequency (this window), Y = power code (relative dB code, not dBm); solid = current frame, purple = peak hold",
      "spectrum.wf3d.title": "3D waterfall",
      "spectrum.wf3d.hint": "Nearest row is newest; height = power code; drag to rotate, wheel to zoom",
      "spectrum.wf2d.title": "2D waterfall (last {rows} frames)",
      "spectrum.wf2d.hint": "One row per frame, older downwards; the bottom frequency axis describes the newest row only (older rows may be other windows); contrast auto-stretched to this frame's range",
      "spectrum.latest": "new",
      "spectrum.mhz": "MHz",
      "spectrum.range": "code {lo}~{hi}",
      "spectrum.tag": "{module} · center {center} · bw {bw} · {raw} bin → {out} pts",
      "spectrum.noThree": "three.js not loaded (check web/assets/vendor/three.min.js)",
      "spectrum.peak": "{freq} / code {code}",

      "wifi.card.wait": "waiting for samples",
      "wf.banner.title": "Periodic snapshots, not continuous",
      "wf.banner.hint": "One radio, time-shared: there are receive gaps between frames; each row is a sweep that really happened",
      "wf.banner.row": "One row = one full sweep (all windows of all three bands, in order) — not a 1 s time bucket",
      "wf.banner.code": "Colour = power code (relative dB code, not dBm); code 0 = that cell was never swept, drawn as darkest",
      "wf.banner.sweep": "Cells covered by several overlapping windows keep the maximum; sweep period = windows × dwell",
      "wf.card.sweep": "Sweep period",
      "wf.card.sweepNote": "RF {rf} s: {windows} windows × {dwell} ms",
      "wf.card.sweepWall": "measured {wall} s/sweep wall-clock (shared radio stretches it)",
      "wf.card.sweeps": "Sweeps done",
      "wf.card.sweepsNote": "one row = one full sweep",
      "wf.card.sweepsPending": "current sweep: {done}/{total} windows (a row is drawn only when full)",
      "wf.card.windows": "Windows",
      "wf.card.windowsNote": "20 MHz window / {step} MHz step ({bw} MHz wide, overlapping)",
      "wf.card.buffer": "Ring buffer",
      "wf.card.bufferNote": "{frames} frames received; oldest dropped when full",
      "wf.card.wait": "waiting for data",
      "wf.wait": "No data yet — press Start, or run app/waterfall.py first",
      "wf.map.title": "All-band time/frequency waterfall",
      "wf.heat.title": "Memory heat",
      "wf.heat.hint": "One byte per band cell: +1 when this sweep sees a signal (caps at 255), -1 when it does not (floors at 0); no history frames kept",
      "wf.heat.clear": "Clear memory",
      "wf.heat.idle": "waiting for data",
      "wf.heat.info": "{n} cells · peak {max} · {hot} above half",
      "wf.heat.cleared": "cleared {n} cells",
      "wf.heat.fail": "clear failed: {error}",
      "wf.map.hint": "X = frequency (three bands, equal width, own ticks); Y = time (older downwards); colour = how busy that cell has been, each band stretched by its own p5-p95 (code range printed next to the band label)",
      "env.banner.title": "Which channels are crowded, and who is on them",
      "env.banner.hint": "This page reads your PC wireless adapter's management-frame parsing, not the board; the board only supplies energy",
      "env.card.ssid": "Visible APs",
      "env.card.ssidNote": "includes hidden SSIDs",
      "env.card.bss": "Radio entries",
      "env.card.bssNote": "one SSID may span several frequencies",
      "env.card.hidden": "Hidden networks",
      "env.card.hiddenNote": "energy present, no readable name",
      "env.card.known": "Pushed to board",
      "env.card.knownNote": "frequencies auto-pushed to the board",
      "env.act.title": "Scan and push",
      "env.act.hint": "The list is a snapshot: routers re-select channels automatically, so rescan and repush every 60-120 s",
      "env.watch.title": "Auto monitoring",
      "env.watch.hint": "Starts on connect. Both modes use the same always-on RF capture; they differ in how often WiFi is rescanned",
      "env.watch.off": "Off",
      "env.watch.daily": "Daily",
      "env.watch.burst": "Burst",
      "env.watch.now": "Check now",
      "env.watch.interval": "Daily interval (seconds)",
      "env.watch.idle": "Not started",
      "env.watch.stateOff": "Off",
      "env.watch.stateDaily": "Daily monitoring",
      "env.watch.stateBurst": "Burst scanning",
      "env.watch.next": "next in",
      "env.watch.rounds": "checks",
      "env.watch.lastFreqs": "Freqs pushed last round",
      "env.watch.lastErr": "Last round failed",
      "env.act.scan": "Scan WiFi environment",
      "env.act.push": "Push known APs to board",
      "env.act.clear": "Clear board list",
      "env.act.idle": "not scanned yet",
      "env.ch.title": "Channel occupancy",
      "env.ch.hint": "Utilization = channel load the AP reports in its own BSS Load element (/255), not our measured energy; — = AP did not report",
      "env.th.mhz": "MHz",
      "env.th.ch": "Ch",
      "env.th.ssid": "SSID",
      "env.th.bssid": "BSSID",
      "env.th.sig": "Signal",
      "env.th.util": "Util",
      "env.th.sta": "Stations",
      "env.notes.title": "How to read it, and the limits",
      "env.empty": "No WiFi found (the PC wireless adapter may be disabled)",
      "env.hiddenSsid": "(hidden SSID)",
      "env.scanning": "scanning…",
      "env.scanFail": "Scan failed: ",
      "env.scanOk": "Scan done: ",
      "env.scanAuto": "Auto-scanned: ",
      "env.hotspots": " hotspots",
      "env.channels": " channels",
      "env.noBridge": "Bridge not ready (QWebChannel has not connected yet)",
      "env.pushing": "pushing…",
      "env.pushFail": "Push failed: ",
      "env.pushOk": "Pushed ",
      "env.pushOk2": " frequencies",
      "env.pushFull": "Hit the firmware limit of 48; extra frequencies truncated",
      "env.clearOk": "Board list cleared (the board then treats every active frequency as unknown)",
      "env.note.1": "This table comes from the PC wireless adapter: SSID / BSSID / channel are parsed from 802.11 management frames, not measured by the board.",
      "env.note.2": "Utilization is the channel load the AP reports about itself in the BSS Load element: how busy that AP is. The energy the board measures is how crowded that band is. Different things.",
      "env.note.3": "The board has no WiFi stack and cannot read SSIDs. It only answers: this frequency carries energy and is not in the list you pushed. That list is the baseline for the board's unknown detection.",
      "env.note.4": "Routers re-select channels automatically (ACS). The list is a snapshot; once stale, an AP that moved channels is misreported as unknown. A single frame proves nothing: only many consecutive rounds without an owner do.",
      "env.note.5": "Single antenna, no phase information: it can say which frequency, never which direction. To locate anything you have to walk over with a phone.",
      // ---- occupancy page (occ.js) ----
      "occ.banner.title": "Channel occupancy (measured by the board)",
      "occ.banner.hint": "Duty cycle = fraction of time this channel carried a transmission, not signal strength",
      "occ.banner.ema": "\"Now\" has a ~1 s time constant; the board's own EMA is per-window (tau about 24 s). Compare them by rank, never digit by digit.",
      "occ.banner.unmatched": "{n} frames did not land on any known channel (outside a wide-window center); they are counted in no row.",
      "occ.card.swept": "Channels with samples",
      "occ.card.sweptNote": "A channel never swept is not 0% - that is a different claim",
      "occ.card.busiest": "Busiest right now",
      "occ.card.busiestNote": "Highest current duty cycle",
      "occ.card.idle": "all quiet",
      "occ.card.frames": "Samples",
      "occ.card.framesNote": "frames matched to a channel / frames counted",
      "occ.card.framesNote2": "frames matched / counted - {s} s of statistics",
      "occ.tbl.title": "Per-channel occupancy",
      "occ.tbl.hint": "Board-side statistics accumulated by the host; it sees other people's protocols too: BLE adverts, video links, RC, DECT",
      "occ.th.mhz": "Freq",
      "occ.th.ch": "Ch",
      "occ.th.band": "Band",
      "occ.th.now": "Now",
      "occ.th.max": "Peak",
      "occ.th.avg": "Avg",
      "occ.th.round": "Duty",
      "occ.th.unswept": "not swept",
      "occ.act.sort": "Sort by occupancy",
      "occ.act.sortFreq": "Sort by frequency",
      "occ.act.csv": "Export CSV",
      "occ.act.reset": "Reset",
      "occ.st.csv": "Saved {name}",
      "occ.st.reset": "Statistics reset",
      "occ.st.error": "Failed: {error}",
      "occ.empty": "No data yet: hit Start above so the board sweeps",
      "occ.noBridge": "Bridge not ready (QWebChannel not connected)",
      "occ.notes.title": "How to read this table",
      "occ.notes.hint": "The conventions are spelled out, so 0% is never mistaken for \"nobody here\"",
      "occ.notes.judge": "Same criterion as the round screen's second page: middle half of one frame's bins, p50 - p25 > 15 codes means a transmission. Codes are relative, not dBm.",
      "occ.notes.now": "\"Now\" is a per-frame EMA (alpha = 1/8, about 1 s); the board's is per-window (tau about 24 s). It moves much faster, so compare ranks not absolute values.",
      "occ.notes.max": "Watch \"Peak\": BLE adverts and RC are spiky, so the average dilutes them. A peak far above the current value means bursty transmissions.",
      "occ.notes.avg": "\"Avg\" = cumulative hits / frames: is something transmitting the whole time? Video links and streams look like that.",
      "occ.notes.unseen": "\"-\" means this sweep never landed on that channel, not 0%. With wide windows most channels are never measured on their own.",
      "occ.notes.gain": "Comparing channels across the band needs a fixed gain (the GAIN preset in the header); hardware AGC levels every channel out and eats the difference.",

      // ---- round display page (round.js) ----
      "round.title": "Round display carousel",
      "round.hint": "Auto-cycle the three board pages (spectrum heat / channel occupancy / waterfall); seconds = dwell per page, all zero = off",
      "round.p1": "Page 1 seconds",
      "round.p2": "Page 2 seconds",
      "round.p3": "Page 3 seconds",
      "round.start": "Start carousel",
      "round.stop": "Stop carousel",
      "round.query": "Read back from board",
      "round.on": "carousel running",
      "round.off": "carousel off",
      "round.noBridge": "Bridge not ready (QWebChannel not connected yet)",
      "round.hintNeedRun": "Reminder: carousel commands go out between scans -- if a click does nothing, press Start in the header first, then use Read back from board to confirm.",
      "round.stateFmt": "Board seconds {s} ({on}) · sent {n} / acked {a}",
      "round.faqTitle": "How it works, and its limits",
      "round.faq1": "The carousel runs in the board firmware's display layer (RD VIEWSEC); the host only writes the dwell seconds. Once written, the board keeps cycling even if you close the host, until you write all zeros or power it off.",
      "round.faq2": "Commands go out between two scans (same discipline as the gain channel), so a click may land on the next scan gap; \"Read back from board\" shows the seconds the firmware confirmed.",
      "round.faq3": "Give page 2 a longer dwell (25 s by default): the occupancy table spans 8 sub-pages, too short and you won't finish reading it.",

      "wf.cursor.idle": "Move the mouse over the plot to read frequency / code",
      "wf.legend.title": "brighter = above this band's noise floor",
      "wf.band.meta": "floor {ref} · peak {max}",
      "wf.band.empty": "no data in this band yet",
      "wf.band.emptyBox": "this band has not been swept yet",
      "wf.cursor": "{band} · {freq} · code {code} · row {row}/{rows} (+{t})",
      "wf.report.title": "Render report (PNG + paged HTML)",
      "wf.report.hint": "Renders what is in the ring buffer; use app/waterfall.py for hour-scale SQLite history",
      "wf.report.btn": "Render PNG + HTML",
      "wf.report.btnBusy": "rendering…",
      "wf.report.open": "Open in browser",
      "wf.report.idle": "not rendered yet",
      "wf.report.done": "{pages} page(s): {png} (PNG) / {html} (HTML index)",
      "wf.report.fail": "render failed: {error}",
      "wf.notes.title": "How to read it · limits",
      "wf.notes.rows": "showing {rows} rows ({sweeps} sweeps completed)",
      "wf.notes.cover": "coverage: {cover}",
      "wf.notes.window": "windows",
      "wf.notes.code": "colour 0-point is each band's own noise floor; +{span} codes = full brightness (page code range {lo}..{hi}; colour only, raw values unchanged)",
      "wf.notes.pending": "current sweep: {done}/{total} windows - a partial sweep is not drawn as a row, so 5G HIGH looking 'not there yet' is expected",
      "wf.notes.partial": "the newest sweep is unfinished ({missing} windows missing); those cells stay darkest = never swept, not zero signal",
      "wf.notes.png": "PNG/HTML are hand-written byte streams from output/png.py (stdlib only); Chinese text lands in HTML",


      "level.重度": "Heavy",
      "level.拥挤": "Crowded",
      "level.轻度": "Light",
      "level.空闲": "Idle",
      "level.无覆盖": "No cov.",

      // ---- gain control (gain.js) ----
      "gain.label": "Gain",
      "gain.hw": "AGC",
      "gain.hwTitle": "Firmware hardware AGC — picks its own index. Use it to ask 'is anything here', not to compare occupancy",
      "gain.manual": "Fixed",
      "gain.manualTitle": "Fixed index (PHY table entry, not dB). Comparisons across time/scenes need a fixed index",
      "gain.auto": "Auto",
      "gain.autoTitle": "Host loop: read the noise floor each frame and move the index toward the target (lower gain flattens front-end ripple)",
      "gain.down": "One step down",
      "gain.up": "One step up",
      "gain.target": "Target",
      "gain.targetTitle": "Target noise-floor code for auto mode (relative dB code, not dBm)",
      "gain.read": "Index {index} · measured {eff} · floor {floor}",
      "gain.codes": "{n} codes",
      "gain.adjusts": "{n} adjustments",
      "gain.failed": "Gain command failed: {error}",
      "kv.gainFloor": "floor {code}",
      "kv.gainAuto": "auto-tuned {n}×",

      // ---- BLE risk page ----
      // ---- capture page (capture.js) ----
      "level.无数据": "No data",
    },
  };

  let lang = detect();

  function detect() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved && SUPPORTED.indexOf(saved) >= 0) return saved;
    } catch (err) { /* 隐私模式拿不到 localStorage，忽略 */ }
    return "zh";
  }

  const missing = {};

  function missingKeys() {
    return Object.keys(missing);
  }

  function clearMissing() {
    Object.keys(missing).forEach((key) => { delete missing[key]; });
  }

  function t(key, params) {
    const table = DICT[lang] || DICT.zh;
    let text = table[key];
    if (text === undefined) {
      if (lang === "en") {
        // 英文界面退回中文 = 静默漏中文，宁可把 key 亮出来让自检抓
        missing[key] = true;
        return key;
      }
      text = DICT.zh[key];
    }
    if (text === undefined) return key;
    if (!params) return text;
    return text.replace(/\{(\w+)\}/g, (all, name) =>
      params[name] === undefined ? all : String(params[name])
    );
  }

  function level(levelName) {
    const key = "level." + levelName;
    // 英文界面下模块给的 level 已经是英文（host/l10n.py 翻过了），词典不必再收一份
    if (lang === "en" && !(DICT.en && DICT.en[key] !== undefined)) return String(levelName);
    return t(key);
  }

  /** 把 root 子树里所有 [data-i18n] / [data-i18n-title] 填上当前语言 */
  function apply(root) {
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll("[data-i18n]").forEach((node) => {
      // **只写叶子节点**：`#scanbar` 这类容器本身带 data-i18n 当"等待中"占位文案，
      // 里面装的却是 JS 现场画的 <canvas>。在这里无脑 textContent = 文本，会把
      // canvas（连同 WebGL 上下文、已累积的热力/瀑布）整个删掉 —— 表现就是
      // 「切一下语言，扫频进度图就空着了」。有元素子节点的一律跳过。
      if (node.firstElementChild) return;
      const text = t(node.dataset.i18n);
      if (text !== undefined) node.textContent = text;
    });
    scope.querySelectorAll("[data-i18n-title]").forEach((node) => {
      node.title = t(node.dataset.i18nTitle);
    });
    scope.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
      node.placeholder = t(node.dataset.i18nPlaceholder);
    });
  }

  function setLang(next, opts) {
    if (SUPPORTED.indexOf(next) < 0) return lang;
    lang = next;
    try { localStorage.setItem(STORAGE_KEY, lang); } catch (err) { /* 忽略 */ }
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    const btn = document.querySelector("[data-lang-toggle]");
    if (btn) btn.title = t("hdr.langTitle");
    apply(document);
    if (!opts || opts.notify !== false) {
      window.dispatchEvent(new CustomEvent("langchange", { detail: { lang: lang } }));
    }
    return lang;
  }

  /** 页面注册表条目按当前语言取名（英文模式下缺 *_en 也不退回中文，只报缺 key） */
  function entry(field, page) {
    if (lang === "en") {
      const key = field + "_en";
      if (page[key]) return page[key];
      missing["registry." + (page.id || "?") + "." + key] = true;
      return page.id || "";
    }
    return page[field] || "";
  }

  return {
    t, apply, setLang, level, entry, missingKeys, clearMissing,
    lang: () => lang,
    supported: SUPPORTED.slice(),
    dict: DICT,
  };
})();

// 短别名：页面里写 T("wifi.col.rank") 比 I18N.t(...) 省事
window.T = window.I18N.t;