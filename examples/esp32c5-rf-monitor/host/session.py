"""上位机逻辑层（**不依赖 Qt**）：连链路、跑调度、把快照/产出转成前端要的 JSON。

分层（对齐参考工程 ``MagicRobot_V0.1/host`` 的做法）：

    host/app.py       Qt 外壳：QWebEngineView + QWebChannel，只做窗口与桥
    host/session.py   ← 本文件：无界面逻辑，可用 unittest 直接测
    host/web/         前端（HTML/CSS/JS + three.js 3D 引擎）

``app.py`` 里的 ``Runner`` 线程只做两件事：反复调 :meth:`HostSession.step` 跑时间片、
把 :meth:`HostSession.spectrum` / :meth:`HostSession.tables` 的 JSON 丢给前端。
所有能出错、能算错的东西都在本文件，方便不开窗口也能验证。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import l10n  # 本地模块：下行本地化（英文界面下模块判词/表格也必须是英文）
import paths  # 本地模块：定位分析层（core/ modes/ output/ tests/）
from gain import MODES as GAIN_MODES, GainAwareSource, GainControl, parse_gain_line  # noqa: E402
from round_ctl import DEFAULT_SECS as DEFAULT_VIEWSECS, RoundControl, parse_payload  # noqa: E402 - 圆屏轮播

#: 分析层目录（<proj>/repo/examples/esp32c5-rf-monitor 或 $ESPSDR_SRC）
SRC_DIR = str(paths.ensure_on_path())

from core.registry import Output, Registry, discover  # noqa: E402 - 必须先 ensure_on_path
from core.scheduler import Scheduler, SchedulerStats  # noqa: E402
from core.serial_link import EspSdrLink  # noqa: E402
from link_guard import ResilientLink  # noqa: E402
import port_lock  # 本地模块：同一串口只允许一个上位机实例采集（见 host/port_lock.py）
from core.snapshot import Snapshot  # noqa: E402
from core.sweep_source import SweepLinkFacade, SweepSource, sweep_key  # noqa: E402
from tests.fake_firmware import FakeFirmware  # noqa: E402
from tests.mock_modes import ProbePlugin  # noqa: E402
from tests.mock_source import SCENARIOS  # noqa: E402
from app import bind_capture  # noqa: E402
from app.capture_link import (  # noqa: E402
    CaptureAwareSource,
    iter_capture_plugins,
    profile_for,
    synthetic_capture,
)

#: 采集引擎：host = 主机逐窗驱动；device = 设备自扫（方案 A，调度搬进固件）
ENGINES = ("host", "device")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(SRC_DIR, ".data")
WEB_DIR = os.path.join(HERE, "web")

# ---------------------------------------------------------------------------
# 界面文案（中/英）。前端自己有一份完整的 UI 词典；这里只翻 Python 侧会发给
# 界面的那几条（链路说明、启停消息、错误），模块自带的表格/ASCII 沿用模块语言。
# ---------------------------------------------------------------------------
MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "probe_only": "modes/ 还没有功能模块 → 只用「链路探针」验证真机链路",
        "read_link_failed": "读链路信息失败：{error}",
        "already_running": "已经在跑了，先停一下",
        "no_run": "当前没有在跑",
        "started": "已开始：{what}",
        "stopped": "已停止",
        "not_connected": "（未连接）",
        "no_output": "（没有模块产出）",
        "rf_error": "[射频错误] {message}",
        "auto_stop": "连续 {n} 个时间片失败，已自动停机。最后一次：{message}",
        "pack_failed": "打包频谱失败：{error}",
        "thread_error": "[线程异常] {error}",
        "already_running_log": "[打开失败] {message}",
        "gain_no_session": "还没开始采集：点「开始」后再调增益",
        "relink_ok": "[串口] 已重新同步（第 {n} 次），继续采集",
        "relink_failed": "[串口] 重同步失败：{error}",
        "relink_closed": "[串口] 链路已关闭，无法重同步",
    },
    "en": {
        "probe_only": "No functional module in modes/ yet — using the link probe to verify the board",
        "read_link_failed": "Failed to read link info: {error}",
        "already_running": "Already running — stop it first",
        "no_run": "Nothing is running",
        "started": "Started: {what}",
        "stopped": "Stopped",
        "not_connected": "(not connected)",
        "no_output": "(no module output)",
        "rf_error": "[RF error] {message}",
        "auto_stop": "{n} consecutive slices failed — stopped automatically. Last: {message}",
        "pack_failed": "Failed to pack spectrum: {error}",
        "thread_error": "[Thread error] {error}",
        "already_running_log": "[Open failed] {message}",
        "gain_no_session": "Not collecting yet — press Start before changing gain",
        "relink_ok": "[Serial] Re-synchronised (attempt {n}) - collecting continues",
        "relink_failed": "[Serial] Re-sync failed: {error}",
        "relink_closed": "[Serial] Link already closed - cannot re-sync",
    },
}

#: 支持的语言（前端下拉/按钮也只给这两个）
LANGS = ("zh", "en")


def tr(lang: str | None, key: str, **kwargs: Any) -> str:
    """取一条 UI 文案；缺语言/缺 key 时退回中文（绝不抛，界面不能因为文案挂掉）。"""
    table = MESSAGES.get((lang or "zh").lower()) or MESSAGES["zh"]
    template = table.get(key) or MESSAGES["zh"].get(key) or key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template

def error_text(lang: str | None, error: object) -> str:
    """异常 / 错误串 → 界面文案。

    英文模式**保证不残留中文**：异常正文常带固件回显与数字，骨架匹配基本对不上，
    所以走 :func:`l10n.translate_error`（片段替换 + 剔掉非 ASCII）。中文模式原样，
    终端日志里始终是原文，所以剔掉的部分不会真的丢。
    """
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    return l10n.translate_error(text) if lang == "en" else text


#: 默认下行点数：前端画图够用，又不至于把 QWebChannel 灌满（1024 → 256 取桶内最大值）
DEFAULT_BINS_OUT = 256


def build_registry(lang: str = "zh") -> tuple[Registry, list[str]]:
    """扫描 ``modes/`` 包，拿到所有功能模块；一个都没有时退化成一个链路探针。"""
    registry = discover("modes")
    notes = list(registry.errors)
    if len(registry) == 0:
        registry.register(ProbePlugin(name="链路探针" if lang == "zh" else "Link probe", slice_weight=1.0))
        notes.append(tr(lang, "probe_only"))
    return registry, notes


def list_ports() -> list[dict[str, str]]:
    """本机串口清单（给前端下拉框用）。打不开的口也会列出来，但不主动去开。"""
    try:
        from serial.tools import list_ports as lp
    except Exception:  # pragma: no cover - pyserial 必装
        return []
    out = []
    for info in lp.comports():
        out.append(
            {
                "device": info.device,
                "description": info.description or "",
                "hwid": info.hwid or "",
            }
        )
    return out


def downsample_bins(bins: Iterable[float], out_n: int = DEFAULT_BINS_OUT) -> list[float]:
    """把 N 个 bin 压成 ``out_n`` 个桶，**取桶内最大值**。

    取最大值而不是平均：窄带信号（BLE / CW）只有几个 bin 宽，平均会被噪声底稀释，
    峰值保留才能在图上看出来。代价是噪声底看起来比实际高一点——图注里写清楚了。
    """
    values = list(bins)
    if not values:
        return []
    out_n = max(1, min(int(out_n), len(values)))
    per = len(values) / out_n
    out: list[float] = []
    for i in range(out_n):
        lo = int(i * per)
        hi = max(lo + 1, int((i + 1) * per))
        chunk = values[lo:hi]
        out.append(max(chunk) if chunk else 0.0)
    return out


#: 允许前端下发的配置字段 → (类型, 是否可空)。漏了字段直接报错，避免"传了但没生效"。
CONFIG_FIELDS: dict[str, tuple[type, bool]] = {
    "port": (str, False),
    "baudrate": (int, False),
    "gain_index": (int, True),
    "gain_mode": (str, True),
    "gain_target": (float, True),
    "mock": (bool, False),
    "scenario": (str, False),
    "bins_out": (int, False),
    "store_path": (str, False),
    "lang": (str, False),
    #: 采集引擎：host = 主机逐窗驱动（默认）；device = 设备自扫（方案 A）
    "engine": (str, False),
    #: 设备自扫时计划表里每个窗的驻留上限（ms）—— device 引擎的节流阀
    "plan_dwell_ms": (int, False),
}


@dataclass
class HostConfig:
    """前端「开始」按钮传下来的一整包参数。"""

    port: str = "COM56"
    baudrate: int = 2_000_000
    gain_index: int | None = None
    #: 增益模式：HARDWARE(固件 AGC) / MANUAL(固定档) / AUTO(主机闭环)；空 = 有档位就 MANUAL
    gain_mode: str | None = None
    #: AUTO 模式的目标底噪参考码（功率码，不是 dBm）
    gain_target: float | None = None
    mock: bool = False
    scenario: str = "mixed"
    bins_out: int = DEFAULT_BINS_OUT
    store_path: str = ""  # 空 = 不入库
    lang: str = "zh"  # 界面语言（zh/en），只影响 Python 侧发给界面的那几条文案
    #: 采集引擎。``host`` = 主机逐窗发 FREQ/SPEC；``device`` = 设备自扫（方案 A）。
    #: 设备不会自扫时自动退回 host，并在 ``describe()`` 里如实回报。
    engine: str = "host"
    #: 设备自扫的每窗驻留上限（ms）。真板实测：固件一次 SPEC 是 ~1 ms 突发，
    #: 模块声明的 100 ms 在逐窗驱动下从没花过；设备自扫是真 paced 的，
    #: 照抄 100 ms 会把自扫压到 10 窗/秒（标定见 docs/07-fsm.md）。
    plan_dwell_ms: int = 20

    @classmethod
    def from_json(cls, text: str) -> "HostConfig":
        raw = json.loads(text) if text else {}
        cfg = cls()
        for key, value in raw.items():
            if key not in CONFIG_FIELDS:
                raise ValueError(f"未知参数：{key}")
            kind, nullable = CONFIG_FIELDS[key]
            if value is None or value == "":
                if nullable or kind is str:
                    setattr(cfg, key, None if nullable else "")
                    continue
                raise ValueError(f"参数 {key} 不能为空")
            if kind is bool:
                setattr(cfg, key, bool(value))
            elif kind is int:
                setattr(cfg, key, int(value))
            elif kind is float:
                setattr(cfg, key, float(value))
            else:
                setattr(cfg, key, str(value))
        cfg.lang = (cfg.lang or "zh").lower()
        if cfg.lang not in LANGS:
            raise ValueError(f"未知语言：{cfg.lang}（可选 {'/'.join(LANGS)}）")
        if cfg.gain_mode and cfg.gain_mode.upper() not in GAIN_MODES:
            raise ValueError(f"未知增益模式：{cfg.gain_mode}（可选 {'/'.join(GAIN_MODES)}）")
        if cfg.gain_mode:
            cfg.gain_mode = cfg.gain_mode.upper()
        if cfg.scenario not in SCENARIOS:
            raise ValueError(f"未知场景：{cfg.scenario}（可选 {', '.join(sorted(SCENARIOS))}）")
        cfg.engine = (cfg.engine or "host").lower()
        if cfg.engine not in ENGINES:
            raise ValueError(f"未知采集引擎：{cfg.engine}（可选 {'/'.join(ENGINES)}）")
        return cfg

    def validate(self) -> None:
        if not self.mock and not self.port:
            raise ValueError("真机模式必须给串口")
        if self.gain_index is not None and self.mock:
            self.gain_index = None
        if self.baudrate <= 0:
            raise ValueError("波特率必须为正")


@dataclass
class HostSession:
    """一次「打开链路 + 跑调度」的会话；由 Qt 线程驱动，也可纯脚本驱动。"""

    bins_out: int = DEFAULT_BINS_OUT
    frames_in: int = 0
    frames_out: int = 0
    started_at: float = 0.0
    registry: Registry = field(init=False)
    notes: list[str] = field(init=False, default_factory=list)
    link: EspSdrLink | None = field(init=False, default=None)
    scheduler: Scheduler | None = field(init=False, default=None)
    store: Any = field(init=False, default=None)
    config: HostConfig | None = field(init=False, default=None)
    last_error: str = field(init=False, default="")
    #: 增益调控（GUI 线程写意图 / 射频线程落地 + 闭环；见 host/gain.py）
    gain: GainControl = field(init=False, default_factory=GainControl)
    #: 圆屏轮播（RD VIEWSEC）：同一条扫描间隙纪律，见 host/round_ctl.py
    round_ctl: RoundControl = field(init=False, default_factory=RoundControl)
    #: 每个快照的回调（GUI 用它推频谱；``step()`` 每次可换）
    on_snapshot_cb: Any = field(init=False, default=None, repr=False)
    #: 支持抓档的模块 + 落盘目录 + 最近抓档日志（任务 5）
    capture_plugins: list[Any] = field(init=False, default_factory=list)
    capture_dir: str = field(init=False, default="")
    capture_log: list[str] = field(init=False, default_factory=list)
    #: 采集引擎与自扫源（方案 A）。永远是普通属性，不靠 ``open()`` 建：
    #: 未打开就 ``close()`` 也不能炸（单测会这么调）。
    sweep: Any = field(init=False, default=None, repr=False)
    engine: str = field(init=False, default="host")
    engine_note: str = field(init=False, default="")
    #: 本进程占着的串口（``host/port_lock.py`` 的坑）；空 = 没占
    _lock_port: str = field(init=False, default="", repr=False)

    def __post_init__(self) -> None:
        self.registry, self.notes = build_registry()

    @property
    def lang(self) -> str:
        return (self.config.lang if self.config else "zh") or "zh"

    def set_lang(self, lang: str) -> str:
        """切界面语言（只影响下行文案，不改判据）。

        界面里的语言开关会通过 ``Bridge.setLang`` 调到这里：日志/状态/表格之后都按新语言出。
        """
        lang = (lang or "zh").lower()
        if lang not in LANGS:
            lang = "zh"
        if self.config is not None:
            self.config.lang = lang
        return lang

    # ---- 启停 ------------------------------------------------------------
    def open(self, config: HostConfig) -> dict[str, Any]:
        """打开链路（真机 or 假固件）并建好调度器。返回信息块给前端顶部显示。"""
        config.validate()
        self.config = config
        # 语言可能在 open() 时才确定（前端点「开始」才下发），这里重建一次说明文案
        if (config.lang or "zh") != "zh" or not self.notes:
            self.registry, self.notes = build_registry(config.lang)
        factory = None
        if config.mock:
            scenario = config.scenario
            factory = lambda port, baud, timeout=5.0: FakeFirmware(  # noqa: E731
                port, baud, timeout, scenario=scenario
            )
        else:
            factory = None
        # ResilientLink = EspSdrLink + 命令前排空残留帧 + 失效后自动重同步
        # （见 host/link_guard.py；core/ 的协议解析一个字不动）
        # 同一串口只允许一个实例采集：两个窗口同时开会互相抢口，被顶掉的那个表面上
        # "还在跑"（扫频地图是累积状态），但瀑布图再也拼不出一行（真板踩过，见
        # host/port_lock.py 抬头）。mock 不碰硬件，不用占坑。
        if not config.mock:
            port_lock.acquire(config.port)
            self._lock_port = config.port
        self.link = ResilientLink(config.port, config.baudrate, factory=factory)
        try:
            self.link.open()
        except Exception:
            # 打不开端口就把坑放掉，否则界面报错后锁还留着，用户重试会被自己挡住
            if self._lock_port:
                port_lock.release(self._lock_port)
                self._lock_port = ""
            raise
        mode = config.gain_mode or ("MANUAL" if config.gain_index is not None else "HARDWARE")
        self.gain = GainControl(
            mode=mode,
            index=config.gain_index if config.gain_index is not None else None,
            target=config.gain_target if config.gain_target is not None else 4.0,
            mock=config.mock,
        )
        # 给每个模块下行的帧打上模块 id（前端靠它把帧对回自己那条扫描计划）
        for plugin in self.registry.plugins:
            plugin.on_snapshot = tag_module(plugin)
        # 初始档位**建控制器时**就落地：真链路等第一次 scan 才设的话，前几帧都是 AGC 值
        if not config.mock:
            # 先问固件要真实的档位范围（实板 0..81，兜底写死 90 会给出无效档）
            try:
                self.gain.set_limits(parse_gain_line(self.link.command("GAIN?", strict=False)))
            except Exception:  # noqa: BLE001 - 老固件不认 GAIN? 就用兜底范围
                pass
            self.gain.apply(self.link)
        # 留档库包一层节流：一条一提交会把采集时间片从 134 ms 拖到 526 ms
        # （真板实测，见 host/store_throttle.py 抬头注释），瀑布图就只剩两行可画
        self.store = None
        if config.store_path:
            from core.store import Store

            from host.store_throttle import WindowStore

            self.store = WindowStore(Store(config.store_path))
        self.last_run_stats = None
        # 采集引擎：默认主机逐窗；engine=device 且设备会自扫时换成订阅模式（见 core/sweep_source.py）
        self.sweep: Any = None
        self.engine = "host"
        self.engine_note = ""
        ops_link: Any = self.link
        if (config.engine or "host") == "device":
            sweep = self._device_sweep()
            if sweep is None:
                self.engine_note = "设备不支持自扫（CAPS 里没有 FSM），已退回主机逐窗"
            else:
                self.sweep = sweep
                ops_link = SweepLinkFacade(self.link, sweep)
                self.engine = "device"
                self.engine_note = "设备自扫：计划与换频在设备里，判据仍在主机"
        # 抓档（任务 5）：把「真抓」函数交给有 capture_fn 槽的模块，再包一层 Source
        # 让抓档只在两次扫描之间落地（跟增益同一条纪律，理由见 app/capture_link.py）。
        source: Any = GainAwareSource(ops_link, self.gain, self.round_ctl)
        # 抓档也走同一个门面：自扫期间要先停下来把射频让出来
        self._capture_link = ops_link
        self.capture_plugins = iter_capture_plugins(self.registry)
        self.capture_dir = ""
        if self.capture_plugins:
            capture_fn: Any
            if config.mock:
                # mock 下按场景挑假抓档长相：CW 场景给纯单音、图传/微波炉给平谱、
                # 间歇场景给半段载波 —— 这样存档的 IQ 文件本身带着场景特征，能回放验证。
                profile = profile_for(config.scenario)
                capture_fn = lambda f, n, r, b: synthetic_capture(f, n, r, b, profile=profile)  # noqa: E731
            else:
                capture_fn = self._link_capture
            base_dir = os.path.dirname(config.store_path) if config.store_path else DEFAULT_DIR
            self.capture_dir = os.path.join(base_dir or DEFAULT_DIR, "capture")
            bind_capture(self.registry, capture_fn, out_dir=self.capture_dir)
            source = CaptureAwareSource(source, self.capture_plugins, capture_fn, logger=self._log_capture)
        self.scheduler = Scheduler(
            # 包一层：增益变更只在两次扫描之间落地（扫描中途插 GAIN 会把串口读缓冲打乱）
            source,
            self.registry,
            store=self.store,
            on_snapshot=self._dispatch_snapshot,
        )
        self.frames_in = 0
        self.frames_out = 0
        self.started_at = time.time()
        self.last_error = ""
        return self.describe()

    def relink(self) -> dict[str, Any]:
        """串口被判失效后重同步：重开链路 → 重设增益 →（自扫模式下）重建自扫。

        为什么要这一步：``core/serial_link.py`` 一旦置 ``failed = True`` 就**永不复位**
        （``open()`` 也不复位），之后每个 ``scan()`` 都抛「连接已标记失效」，
        上层只能自动停机 —— 现场表现就是「跑一会儿提示断开串口，只能重启上位机」。
        这里不改 core 的判据，只在外围重开串口：``ResilientLink.reopen()`` 会
        重新 SYNC、重读 CAPS、并把失效标记清掉。

        返回 ``{"ok": bool, "message": str, "count": int}``；失败不抛，让 RF 线程
        照常走「连续失败自动停机」那条路。
        """
        link = self.link
        if link is None:
            return {"ok": False, "message": tr(self.lang, "relink_closed"), "count": 0}
        sweeping = self.sweep is not None
        if sweeping:
            # 先让设备停扫：重开串口时设备还在吐帧的话，SYNC 又会读歪
            try:
                self.sweep.stop()
            except Exception:  # noqa: BLE001 - 停不下来也要重开
                pass
        try:
            ok = link.reopen()
        except Exception as exc:  # noqa: BLE001 - 重开失败如实回报
            return {"ok": False, "message": error_text(self.lang, exc), "count": 0}
        if not ok:
            # 冷却中（串口可能真被拔了）：不算失败，让 RF 线程停一会儿再试
            return {"ok": False, "message": "", "count": 0}
        try:
            if self.config is not None and not self.config.mock and self.gain is not None:
                self.gain.apply(link)
        except Exception:  # noqa: BLE001 - 增益设不上不该挡住恢复
            pass
        if sweeping:
            try:
                self.sweep.start()
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "message": error_text(self.lang, exc),
                    "count": getattr(link, "reopen_count", 0),
                }
        count = int(getattr(link, "reopen_count", 0) or 0)
        return {"ok": True, "message": tr(self.lang, "relink_ok", n=count), "count": count}

    def close(self) -> None:
        # 自扫先停：别让设备在链路关掉之后还在往串口吐帧
        if self.sweep is not None:
            try:
                self.sweep.stop()
            except Exception:  # noqa: BLE001
                pass
            self.sweep = None
        if self.link is not None:
            try:
                if not (self.config and self.config.mock):
                    self.link.set_gain("HARDWARE")
            except Exception:  # noqa: BLE001 - 关链路时不掩盖原错误
                pass
            try:
                self.link.close()
            except Exception:  # noqa: BLE001
                pass
        if self.store is not None:
            # 收尾先把节流里攒着的那一条写进库，再关连接（否则最后一个窗白采）
            flush = getattr(self.store, "flush", None)
            if callable(flush):
                try:
                    flush()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.store.close()
            except Exception:  # noqa: BLE001
                pass
        self.link = None
        self.scheduler = None
        self.store = None
        # 坑最后放：链路真关了再让别的实例进来，否则对方 open 到一半又会抢
        if self._lock_port:
            port_lock.release(self._lock_port)
            self._lock_port = ""

    # ---- 已知热点名单（固件判「未知」的差分基准）--------------------------
    def push_known(self, freqs: list[int]) -> dict[str, Any]:
        """把本机扫到的已知热点中心频率灌进固件，清空旧名单后整批重灌。

        为什么必须整批重灌而不是增量追加
        --------------------------------
        路由器会自动换信道（ACS）。如果我只是一直 ADD，上一轮在 6 信道的
        老记录会永久留在固件里，把换到 11 信道的那台 AP 误判成「未知」。
        先 ``RD KNOWN CLR`` 再全量下发，名单才是「这一刻的事实」。

        为什么固件还需要这份名单
        ------------------------
        板子只有 FFT 功率谱、没有 802.11 协议栈，拿不到 SSID；
        它只能回答「这个频点有能量，而且不在你给的名单里」。
        名单是差分基准，不是识别结果。

        返回 ``{"sent": n, "full": bool, "replies": [...]}``。
        """
        # 走 self.link 而不是 self._capture_link
        # ------------------------------------
        # device 引擎下 _capture_link 是 SweepLinkFacade，它的 command() 是
        # 「pause_around」：每条命令都 停扫 → 执行 → **恢复推流**。连发十几条
        # RD KNOWN 时，每发一条就把固件的 USB CDC 发送缓冲重新灌满一次，
        # 主机侧写必然部分失败（pyserial 在 Windows 上报 Write timeout）。
        # 所以这里手工做一次「停 → 连发 → 恢复」，而不是逐条 pause_around。
        link = self.link
        if link is None:
            raise RuntimeError("链路已关闭")
        from wifi_env import KNOWN_MAX  # 延迟导入：session 不该硬依赖 Windows 扫描

        sweep = self.sweep
        was_running = bool(sweep is not None and getattr(sweep, "running", False))
        if was_running:
            try:
                sweep.stop()
            except Exception:  # noqa: BLE001 - 停不下来也要继续试下发
                was_running = False

        replies: list[str] = []
        try:
            replies.append(self._rd_quiet(link, "FSM STREAM OFF"))
            replies.append(self._rd_quiet(link, "RD KNOWN CLR"))
            sent = 0
            full = False
            seen: set[int] = set()
            for mhz in freqs:
                mhz = int(mhz)
                # 只收 WiFi 实际会用到的频段；越界值固件也不认
                if not (2400 <= mhz <= 7200) or mhz in seen:
                    continue
                if sent >= KNOWN_MAX:
                    full = True
                    break
                seen.add(mhz)
                r = self._rd_quiet(link, f"RD KNOWN {mhz}")
                replies.append(r)
                sent += 1
                if "full" in (r or "").lower():
                    full = True
                    break
            return {"sent": sent, "full": full, "replies": replies[-3:]}
        finally:
            if was_running:
                try:
                    sweep.start()
                except Exception:  # noqa: BLE001 - 恢复失败不该盖住主流程的结果
                    pass

    @staticmethod
    def _rd_quiet(link: Any, line: str, tries: int = 3) -> str:
        """发一条圆屏 RD 命令，容忍 TX 缓冲满导致的部分写。

        ``Write timeout`` 是「写得不够多」而不是「写失败」，短命令重试一次
        基本都能出去；每次重试前先把读缓冲里堆着的帧丢掉，否则 readline
        会拿到上一帧的尾巴当成应答。
        """
        last = ""
        for i in range(tries):
            try:
                if i:
                    port = getattr(link, "_port", None)
                    if port is not None:
                        try:
                            port.reset_input_buffer()
                        except Exception:  # noqa: BLE001
                            pass
                return link.command(line, strict=False)
            except Exception as exc:  # noqa: BLE001 - 兜一次再抛
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(0.15 * (i + 1))
        raise RuntimeError(last or "命令失败")

    # ---- 抓档 ------------------------------------------------------------
    def _link_capture(self, freq_hz: int, samples: int, rate_code: int, bits: int) -> Any:
        """真机抓档：把模块给的 ``(freq, samples, rate, bits)`` 转成 ``link.capture_iq`` 的参数顺序。"""
        link = getattr(self, "_capture_link", None) or self.link
        if link is None:
            raise RuntimeError("链路已关闭")
        return link.capture_iq(samples=samples, rate_code=rate_code, bits=bits, freq_hz=freq_hz)

    def _log_capture(self, message: str) -> None:
        self.capture_log.append(message)
        del self.capture_log[: max(0, len(self.capture_log) - 40)]

    def _out(self, payload: Any) -> Any:
        """下行出口：``lang == "en"`` 时把 payload 里的中文串翻成英文。

        只翻发往界面的那份（模块自己的 ASCII/终端输出保持中文）；中文界面原样返回，
        零额外开销。翻不动的串原样留着 —— ``tests/test_l10n.py`` 会盯住「英文模式
        下行 payload 零 CJK」，所以漏翻在测试里就会红，不会溜到界面上。
        """
        if self.lang == "zh":
            return payload
        return l10n.localize(payload)

    # ---- 增益 ------------------------------------------------------------
    def gain_state(self) -> dict[str, Any]:
        return self._out(self.gain.state())

    def gain_control(self, payload: str) -> dict[str, Any]:
        """顶栏增益控件 → 控制器。**只写意图**：真正的串口命令在两次扫描之间发。"""
        try:
            req = json.loads(payload) if payload else {}
        except Exception as exc:  # noqa: BLE001 - 前端传坏了不能把窗口带崩
            return {"kind": "gain", "ok": False, "note": f"参数不是 JSON：{exc}"}
        if not isinstance(req, dict):
            return {"kind": "gain", "ok": False, "note": "参数必须是对象"}
        action = str(req.get("action") or "").lower()
        try:
            if action == "mode":
                return self._out(self.gain.request_mode(str(req.get("mode") or "")))
            if action == "step":
                return self._out(self.gain.request_step(int(req.get("delta") or 0)))
            if action == "index":
                return self._out(self.gain.request_index(int(req.get("index") or 0)))
            if action == "target":
                return self._out(self.gain.request_target(float(req.get("target") or 0)))
        except (TypeError, ValueError) as exc:
            return {"kind": "gain", "ok": False, "note": f"参数不合法：{exc}"}
        return self._out(self.gain.state(message=f"未知操作：{action or '（空）'}"))

    def round_control(self, payload: str) -> dict[str, Any]:
        """圆屏轮播控件 → 控制器。**只写意图**：真正的 ``RD VIEWSEC`` 在两次扫描之间发。

        action：``start`` = 按前端给的秒数开轮播（缺省 8/25/8）；
        ``stop`` = 全 0 关轮播；``status`` = 读最近一次确认的秒数。
        """
        req = parse_payload(payload)
        if not isinstance(req, dict):
            return self.round_ctl.state(message="参数必须是 JSON 对象")
        action = str(req.get("action") or "").lower()
        try:
            if action == "start":
                secs = req.get("secs") or DEFAULT_VIEWSECS
                if not isinstance(secs, (list, tuple)) or len(secs) != 3:
                    st = self.round_ctl.state()
                    st["ok"] = False
                    st["message"] = "secs 需要 [a, b, c] 三个秒数"
                    return st
                return self._out(self.round_ctl.request_carousel([int(s) for s in secs]))
            if action == "stop":
                return self._out(self.round_ctl.request_carousel([0, 0, 0]))
            if action == "status":
                # 不是回缓存：排队一次真正的串口读回，下个扫描间隙生效
                return self._out(self.round_ctl.request_query())
        except (TypeError, ValueError) as exc:  # noqa: BLE001 - 前端传坏了不能把窗口带崩
            return self.round_ctl.state(message=f"参数不合法：{exc}")
        return self.round_ctl.state(message=f"未知操作：{action or '（空）'}")

    # ---- 驱动 ------------------------------------------------------------
    def _dispatch_snapshot(self, snap: Snapshot) -> None:
        callback = self.on_snapshot_cb
        if callback is not None:
            callback(snap)

    def step(self, seconds: float = 0.35, on_snapshot=None) -> SchedulerStats | None:
        """跑一小段时间片。GUI 反复调用它，任意时刻都能在 ``seconds`` 内停下来。"""
        if self.scheduler is None:
            return None
        self.on_snapshot_cb = on_snapshot
        stats = self.scheduler.run(duration_s=seconds)
        self.last_run_stats = stats  # 收尾统计（停线程后 stats() 还能拿到 share）
        return stats

    # ---- 数据打包 --------------------------------------------------------
    def spectrum(self, snap: Snapshot, module_id: str = "") -> dict[str, Any]:
        """一帧频谱 → 前端 JSON（降采样后的点数由 ``bins_out`` 决定）。"""
        self.frames_out += 1
        bins = downsample_bins(snap.bins_db, self.bins_out)
        half = snap.bw_hz / 2
        meta = dict(snap.meta or {})
        return {
            "kind": "spectrum",
            "module": module_id or str(meta.get("module", "")),
            "seq": int(meta.get("seq", self.frames_out)),
            "ts": round(snap.ts, 3),
            "center_hz": int(snap.center_hz),
            "bw_hz": int(snap.bw_hz),
            "freq_lo_hz": int(round(snap.center_hz - half)),
            "freq_hi_hz": int(round(snap.center_hz + half)),
            "bin_hz": round(snap.bw_hz / len(bins), 1) if bins else 0.0,
            "raw_bins": snap.bin_count,
            "max_detector": bool(meta.get("max_detector", False)),
            "gain_code": meta.get("gain_code"),
            "bins": [round(v, 1) for v in bins],
        }

    def tables(self, modules: Iterable[str] | None = None) -> dict[str, Any]:
        """模块产出物 → 前端 JSON；模块自带 ``web_payload()`` 时一并带上。

        ``modules`` 给定时**只算这些模块**（页面门控：只算当前页面要显示的模块）。
        给 ``None`` 表示全算。现在 ``modes/`` 只剩 ``waterfall``，两种写法等价；
        保留门控是因为 ``produce()`` + ``web_payload()`` 是每秒真算一遍的，
        将来模块变多时全算就是纯白工，和「前端不显示就不该算」相悖。
        """
        wanted = None if modules is None else set(modules)
        plugins = []
        for plugin in self.registry.plugins:
            if wanted is not None and plugin.id not in wanted:
                continue
            item: dict[str, Any] = {
                "id": plugin.id,
                "name": plugin.name,
                "weight": plugin.slice_weight,
                "table": None,
                "payload": None,
                "error": "",
            }
            try:
                output = plugin.produce()
                item["table"] = self.table_json(output)
                extra = getattr(plugin, "web_payload", None)
                if callable(extra):
                    item["payload"] = extra()
            except Exception as exc:  # noqa: BLE001 - 单个模块炸了不能拖垮整个界面
                item["error"] = error_text(self.lang, exc)
            plugins.append(item)
        return self._out({
            "kind": "tables",
            "ts": round(time.time(), 3),
            # 只算了哪几个模块（None → 全量）：自检据此验门控真的生效，不是「发了但没算」
            "modules": None if modules is None else [p.id for p in self.registry.plugins if p.id in wanted],
            "plugins": plugins,
        })

    def _store_state(self) -> dict[str, Any]:
        """留档库计数（没开库就返回空，界面就不会多显示一块）。"""
        state = getattr(self.store, "state", None)
        if not callable(state):
            return {}
        try:
            return dict(state())
        except Exception:  # noqa: BLE001 - 统计不上报不该影响采集
            return {}

    @staticmethod
    def table_json(output: Output | None) -> dict[str, Any] | None:
        if output is None:
            return None
        return {
            "kind": output.kind,
            "title": output.title,
            "columns": [str(c) for c in output.columns],
            "rows": [[_cell(c) for c in row] for row in output.rows],
            "notes": [str(n) for n in output.notes],
            "files": [str(f) for f in output.files],
        }

    def stats(self, run_stats: SchedulerStats | None = None) -> dict[str, Any]:
        stats = run_stats if run_stats is not None else (self.scheduler.stats if self.scheduler else None)
        if stats is None:
            # 线程停了 scheduler 就拆掉了；留档让"停之后"的查询（自检收尾、late window）还有数
            stats = self.last_run_stats
        if stats is None:
            return {"kind": "stats", "plugins": [], "slices": 0, "snapshots": 0, "elapsed_s": 0.0}
        self.last_run_stats = stats
        return self._out({
            "kind": "stats",
            "uptime_s": round(time.time() - self.started_at, 1),
            # 增益读数跟着每秒的统计一起推：前端不用额外轮询，也不会算出「改了但没生效」
            "gain": self.gain.state(),
            # 留档库的「收帧数 / 落库条数」：一窗多帧合并成一条，两个数对不上是正常的
            **({"store": state} if (state := self._store_state()) else {}),
            **stats.as_dict(),
        })

    # ---- 设备自扫（方案 A） -----------------------------------------------
    def _sweep_windows(
        self, modules: Iterable[str] | None = None
    ) -> tuple[list[tuple[int, int, int]], int, int]:
        """把各模块的扫描计划合成一张设备计划表。

        返回 ``(windows, bins, bw_hz)``：``windows`` 是 ``[(freq_mhz, dwell_ms, module_index)]``，
        同一频点只留一个窗口（多个模块共用一个窗时设备只扫一次，帧被各自拿去用），
        ``bins`` 取所有请求里最大的那个（设备一次只能用一档 FFT 点数），
        ``bw_hz`` 取最大带宽（用于选采样率剖面）。
        """
        wanted = {m for m in modules or () if m}
        windows: list[tuple[int, int, int]] = []
        seen: set[int] = set()
        bins = 256
        bw_hz = 0
        for index, plugin in enumerate(self.registry.plugins):
            if wanted and plugin.id not in wanted:
                continue
            try:
                requests = list(plugin.scan_requests())
            except Exception:  # noqa: BLE001 - 单个模块计划失败不该拖死计划表
                continue
            for request in requests:
                mhz = sweep_key(request.freq_hz)
                if mhz in seen:
                    continue
                seen.add(mhz)
                windows.append((mhz, max(1, int(request.dwell_ms)), index))
                bins = max(bins, int(request.bins))
                bw_hz = max(bw_hz, int(request.bw_hz))
        return windows, bins, bw_hz

    def _device_sweep(self, modules: Iterable[str] | None = None) -> SweepSource | None:
        """建自扫源；设备不支持自扫或计划为空时返回 None（调用方退回主机逐窗）。"""
        link = self.link
        if link is None or not getattr(link, "sweep_supported", False):
            return None
        windows, bins, bw_hz = self._sweep_windows(modules)
        if not windows:
            return None
        rate_code = 2
        try:
            _fs, rate_code, bins, _stride, _units, _cont = link.choose_profile(bw_hz or 20_000_000, bins)
        except Exception:  # noqa: BLE001 - 剖面选不出来就用默认值，设备会自己校验
            pass
        return SweepSource(
            link,
            windows,
            bins=bins,
            rate_code=rate_code,
            plan_dwell_ms=int(getattr(self.config, "plan_dwell_ms", 20) or 20),
            # 真板要把"射频时间"补回来（权重按占空算）；假固件没有射频，补了只会让
            # 自检跑得比 host 引擎还慢，所以 mock 下不补。
            pace=not bool(self.config and self.config.mock),
            logger=lambda message: self.notes.append(message),
        )

    def restrict_modules(self, modules: Iterable[str] | None) -> None:
        """把设备计划收窄到指定模块（只扫这些模块的窗）。

        **不要接到"换页"上**：真板实测（``--selftest --engine device``）一换页就要
        ``FSM STOP`` + 重建计划，一次停表 + 一轮重扫，表格整段是"等待样本"；页切得
        越勤饿得越狠。默认计划就取全体模块的并集（同频点合并），换页只门控"算不算
        表格"，不动物理计划。这个方法留给"用户本来就只用一两个页"的场景手动调。
        """
        sweep = self.sweep
        if sweep is None or self.link is None:
            return
        windows, bins, bw_hz = self._sweep_windows(modules)
        if not windows:
            return
        rate_code = 2
        try:
            _fs, rate_code, bins, _stride, _units, _cont = self.link.choose_profile(bw_hz or 20_000_000, bins)
        except Exception:  # noqa: BLE001
            pass
        if windows == sweep.windows and bins == sweep.bins:
            return
        was_running = sweep.running
        sweep.stop()
        sweep.windows = windows
        sweep._keys = {key for key, _dwell, _mod in windows}
        sweep.bins = bins
        sweep.rate_code = rate_code
        if was_running:
            sweep.start()

    def sweep_state(self) -> dict[str, Any]:
        state = self.sweep.state() if self.sweep is not None else {}
        state.update({"engine": self.engine, "note": self.engine_note})
        return self._out(state)

    def _plugin_requests(self) -> list[tuple[Any, list[Any]]]:
        """(插件, 扫描请求列表)；没实现 scan_requests 的插件给空表。"""
        pairs: list[tuple[Any, list[Any]]] = []
        for plugin in self.registry.plugins:
            try:
                requests = list(plugin.scan_requests()) if hasattr(plugin, "scan_requests") else []
            except Exception:  # noqa: BLE001 - 计划拿不到就画不出频段条，但不该拖垮界面
                requests = []
            pairs.append((plugin, requests))
        return pairs

    def describe(self) -> dict[str, Any]:
        """链路自报 + 模块清单 + 扫描计划数目（前端顶部那一排）。"""
        link = self.link
        info: dict[str, Any] = {
            "kind": "info",
            "port": self.config.port if self.config else "",
            "baudrate": self.config.baudrate if self.config else 0,
            "mock": bool(self.config and self.config.mock),
            "scenario": self.config.scenario if self.config else "",
            "gain_index": self.config.gain_index if self.config else None,
            "gain": self.gain.state(),
            "lang": self.lang,
            "identity": "",
            "transport": "",
            "caps": [],
            "profiles": [],
            "notes": list(self.notes),
            "engine": self.engine,
            "engine_note": self.engine_note,
            "sweep_supported": bool(self.link is not None and getattr(self.link, "sweep_supported", False)),
            "plugins": [
                {
                    "id": p.id,
                    "name": p.name,
                    "weight": p.slice_weight,
                    "requests": len(requests),
                }
                for p, requests in self._plugin_requests()
            ],
            # 扫描计划（每窗的中心/带宽，按扫描顺序）—— 前端据此画频段条与游标，
            # 不在这里写死 2.4G/5G 的边界，换实验时前端零改动。
            "plans": {
                p.id: [[int(req.freq_hz), int(req.bw_hz)] for req in requests]
                for p, requests in self._plugin_requests()
            },
        }
        if link is not None:
            try:
                info["identity"] = link.identity
                info["transport"] = link.transport or ""
                info["caps"] = list(link.caps)
                info["profiles"] = [
                    {"sample_rate_hz": fs, "bins": bins, "dwell_ms": dwell}
                    for fs, _code, bins, _start, dwell, _cont in link.profiles()
                ]
            except Exception as exc:  # noqa: BLE001
                info["notes"].append(
                    tr(self.lang, "read_link_failed", error=error_text(self.lang, exc))
                )
        return self._out(info)

    def render_text(self) -> str:
        """把当前产出渲染成 CLI 同款 ASCII（「原始输出」页用它做对照）。

        英文界面下**不返回**模块自带的中文 ASCII（那些表头/注释是给人对着命令行看的，
        逐行硬翻会把定宽对齐全打碎），而是把同一批数据渲成英文纯文本 —— 「原始输出」
        页仍然能看到全部内容，界面上又不会漏出一个中文字。
        """
        if self.lang != "zh":
            return self._render_text_en()
        blocks = []
        for plugin in self.registry.plugins:
            try:
                blocks.append(plugin.produce().render())
            except Exception as exc:  # noqa: BLE001
                blocks.append(f"[error] {plugin.id}: {error_text(self.lang, exc)}")
        return "\n\n".join(blocks) if blocks else tr(self.lang, "no_output")

    def _render_text_en(self) -> str:
        """英文版纯文本：直接用已经本地化好的 :meth:`tables` 拼（零额外判据）。"""
        payload = self.tables()
        blocks: list[str] = []
        for item in payload.get("plugins") or []:
            table = item.get("table") or {}
            title = table.get("title") or item.get("name") or item.get("id") or ""
            lines = [f"[{item.get('id', '')}] {title}"]
            columns = table.get("columns") or []
            if columns:
                lines.append(" | ".join(str(c) for c in columns))
                lines.append("-" * min(72, max(8, sum(len(str(c)) + 3 for c in columns))))
            for row in table.get("rows") or []:
                lines.append(" | ".join("" if cell is None else str(cell) for cell in row))
            for note in table.get("notes") or []:
                lines.append(f"- {note}")
            for name in table.get("files") or []:
                lines.append(f"  file: {name}")
            if item.get("error"):
                lines.append(f"[error] {item['error']}")
            blocks.append("\n".join(lines).rstrip())
        return "\n\n".join(blocks) if blocks else tr(self.lang, "no_output")


def _cell(value: Any) -> Any:
    """表格单元格转成 JSON 安全值（numpy/Decimal 之类不会进到这里，但防一手）。"""
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def tag_module(plugin: Any) -> Any:
    """把插件包一层：喂给它的每一帧都在 ``meta["module"]`` 上打出模块 id。

    为什么需要：单射频是**分时**的，同一串帧流里可能混着多个模块的窗。前端要区分
    「这一帧是哪个模块的窗」（扫频进度条只能跟自己那条计划对齐，否则游标会被别家
    的窗带着乱跳、「第 N 轮」被假回卷刷高）——而 ``core/`` 的帧解析里没有这个字段。

    只包 host 层，``core/`` 一个字不动；打标时机由 ``core.scheduler._serve`` 保证：
    它先调 ``plugin.on_snapshot(snap)`` 再调 ``on_snapshot`` 回调，所以这里打完标
    之后的下行（``HostSession.spectrum``）一定读得到。
    """
    inner = plugin.on_snapshot
    module_id = str(getattr(plugin, "id", "") or "")

    def tagged(snap: Any) -> Any:
        try:
            meta = getattr(snap, "meta", None)
            if isinstance(meta, dict) and module_id:
                meta["module"] = module_id
        except Exception:  # noqa: BLE001 - 打标失败不该影响分析
            pass
        return inner(snap)

    tagged.__name__ = "tagged_on_snapshot"
    return tagged


def to_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))