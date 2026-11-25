"""esp-sdr 测试上位机（PySide6 + QWebEngine + QWebChannel）。

参照 ``MagicRobot_V0.1/host`` 的做法，只做三件事：

1. ``QWebEngineView`` 加载 ``host/web/index.html``（UI 全在网页里）
2. ``QWebChannel`` 把 :class:`Bridge` 暴露成 JS 的 ``window.backend``
3. ``Runner`` 线程反复调 :meth:`session.HostSession.step` 跑射频时间片

**换实验 = 换网页**：网页按 ``web/pages/registry.json`` 注册页面，加一个实验只要
新增 ``web/pages/<id>/page.html`` + 一行注册，不用改 Python。

约定（踩过的坑，别改回去）：

* 信号一律传 **JSON 字符串**：``QVariantList`` 经 QWebChannel 会退化成 JS
  ``object``，前端拿不到数组（参考工程 app.py 注释里的同一条）。
* 前端发来的参数也用 **JSON 字符串**，Python 侧统一在
  :meth:`session.HostConfig.from_json` 里解析，避免 Qt 类型转换的玄学。
* 帧率上限 ~20 fps：实板 300 帧/s，按帧推给 QWebChannel 会灌满 IPC 卡死界面。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QObject, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

import occupancy  # noqa: E402 - 逐信道占用统计（判据与固件同源）
import paths  # noqa: E402 - 本地模块，负责定位分析层
import watch  # noqa: E402 - 监测策略（何时扫 WiFi / 下发已知名单）
from session import (  # noqa: E402
    ENGINES,
    LANGS,
    WEB_DIR,
    HostConfig,
    HostSession,
    error_text,
    list_ports,
    to_json,
    tr,
)

#: 分析层目录（core/ modes/ 所在）。找不到会在这里就抛明确的错误。
SRC_DIR = paths.ensure_on_path()

#: 界面/控制台一律显示「现在跑的是哪一版代码」。
#:
#: 为什么需要它：改完代码但窗口是**上一个进程**（QWebEngine 在 load 时就把 JS 装进内存），
#: 表现是「明明改了却没生效」，两边都看不到异常。这一行时间戳是唯一能当场对齐的证据。
def code_stamp() -> str:
    """最近一次改动 host/ 下界面或代码的时间 + git 短哈希（拿不到就只给时间）。"""
    host_dir = os.path.dirname(os.path.abspath(WEB_DIR))
    newest, newest_name = 0.0, ""
    for base, _dirs, names in os.walk(host_dir):
        for name in names:
            if not name.endswith((".py", ".js", ".html", ".css", ".json")):
                continue
            path = os.path.join(base, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > newest:
                newest, newest_name = mtime, os.path.relpath(path, host_dir)
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(newest)) if newest else "?"
    sha = ""
    try:
        out = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.dirname(host_dir)), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        sha = (out.stdout or "").strip()
        if sha:
            # 工作区有未提交改动就标 +local：一眼看出「磁盘上这份比 git 里的新」
            dirty = subprocess.run(
                ["git", "-C", os.path.dirname(os.path.dirname(host_dir)), "status", "--porcelain", "--", host_dir],
                capture_output=True, text=True, timeout=3,
            )
            if (dirty.stdout or "").strip():
                sha += "+local"
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"{when} · {sha or 'nogit'} · {newest_name or '-'}"

class ConsolePage(QWebEnginePage):
    """把前端 console 输出打到 Python 的 stdout：网页出错时不用开 DevTools 也能查。

    同时接住 ``window.open``：前端「独立窗口」按钮就是一次 ``window.open``，
    这里把它变成一个**真正的独立窗口**（可以拖到别的显示器），而不是被 Qt 丢掉。
    """

    #: 反向引用，由 :meth:`HostWindow.__init__` 填上
    owner: "HostWindow | None" = None

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 - Qt 覆写
        name = getattr(level, "name", str(level))
        tag = {"InfoMessageLevel": "log", "WarningMessageLevel": "warn", "ErrorMessageLevel": "err"}.get(name, name)
        print(f"[js:{tag}] {source}:{line} {message}", flush=True)

    def createWindow(self, _window_type):  # noqa: N802 - Qt 覆写
        if self.owner is None:
            return None
        return self.owner.open_popout()


def _loads(payload: str) -> object:
    """把缓存的 JSON 字符串还原成对象（缓存为空时给 None）。"""
    if not payload:
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return None


def configure_settings(view: QWebEngineView) -> None:
    """主窗口和每个独立窗口用同一套设置（本地文件可读、不联网、开 WebGL）。"""
    settings = view.settings()
    settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
    settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
    settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
    settings.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
    settings.setAttribute(QWebEngineSettings.WebGLEnabled, True)
    settings.setAttribute(QWebEngineSettings.Accelerated2dCanvasEnabled, True)


#: 频谱推送间隔（秒）。实板 300 帧/s，全推会灌满 QWebChannel IPC。
SPECTRUM_MIN_INTERVAL = 0.05
#: 表格/统计推送间隔（秒）
TABLE_INTERVAL = 1.0
#: 单个时间片长度（秒）：GUI 里 stop 的响应时间上限
STEP_SECONDS = 0.35

#: 全功能并行（nginx 式「一份数据多个消费者」）。
#:
#: 以前的实现按「当前停在哪个页面」裁剪模块：切页就只算那一页的模块。
#: 实测这是错的：切页会顺带改变射频线程的调度与 tables 节拍，吞吐反而掉下去，
#: 而且切回来之前那一页的数据是断的。现在所有模块一直在线，每次 tables() 全量产出，
#: 前端各页按需取用；页面切换只影响显示，不再影响采集与计算。
#: 连续失败多少个时间片就自动停机（core 目前没有退避，应用层兜底）
MAX_CONSECUTIVE_ERRORS = 5
#: 串口重同步冷却中（多半是真被拔了）就歇这么久再试，别原地空转
REOPEN_RETRY_SLEEP_S = 3.0
#: 一次采集里最多重同步几次（防止真坏了的链路无限重连刷日志）
MAX_RELINKS = 20
MOCK_FRAMES_PER_WINDOW = 2  # 假固件每个窗口吐 2 帧（tests/fake_firmware.py 的约定）


def wanted_pages(view: dict[str, object]) -> set[str]:
    """当前所有窗口正在看的页面；没上报过就按「全都要」处理（宁可多算，不能黑屏）。"""
    views = view.get("views") or {}
    pages = {p for p in (views.values() if isinstance(views, dict) else []) if p}
    return pages or {"raw"}


def needed_modules(view: dict[str, object], all_ids: Iterable[str] = ()) -> list[str]:
    """要算的模块 id —— **永远是全部**（全功能并行，不按页面裁剪）。"""
    return list(all_ids)


def wants_spectrum(view: dict[str, object]) -> bool:
    """是否该推实时频谱帧 —— **永远是**（频谱帧同时喂「实时频谱」页与所有浮层）。"""
    return True


class Bridge(QObject):
    """暴露给前端 JS 的 ``window.backend``。"""

    # Python → JS（全部是 JSON 字符串）
    info = Signal(str)
    spectrum = Signal(str)
    tables = Signal(str)
    stats = Signal(str)
    gain = Signal(str)
    state = Signal(str)
    log = Signal(str)
    wifiEnv = Signal(str)   # 开机自扫的 WiFi 环境（ENV 页首屏数据）

    def __init__(self, window: "HostWindow", view: dict[str, object] | None = None) -> None:
        super().__init__()
        self._window = window
        #: 页面门控状态放**普通 dict**（由 HostWindow 持有）：射频线程要读它，
        #: 而跨线程读 QObject 的实例属性是 PySide6 的已知死法（栈溢出、无回溯）。
        self._view: dict[str, object] = view if view is not None else {"views": {}, "epoch": 0}

    # ---- 页面门控 ----------------------------------------------------------
    @Slot(str, result=str)
    def setView(self, payload: str) -> str:  # noqa: N802 - JS 侧用驼峰
        """前端报告「我这个窗口现在停在哪个页面」。

        JS 在 ``Router.navigate()`` 里调一次；独立小窗口带自己的窗口标签，互不覆盖。
        多窗口取**并集**：主窗口停在 BLE 页、独立窗口弹着瀑布图，两边都得有数据。
        返回并集后的模块清单，前端/自检据此直接看到门控结果。
        """
        try:
            data = json.loads(payload) if payload else {}
        except Exception:  # noqa: BLE001 - 前端传坏不该崩
            data = {}
        page = str((data or {}).get("page") or "")
        win = str((data or {}).get("win") or "main")
        views = self._view.setdefault("views", {})
        if page and isinstance(views, dict) and views.get(win) != page:
            views[win] = page
            self._view["epoch"] = int(self._view.get("epoch", 0) or 0) + 1
        modules = needed_modules(self._view, self._module_ids())
        return to_json({"page": page, "win": win,
                        "epoch": int(self._view.get("epoch", 0) or 0), "modules": modules})

    def _module_ids(self) -> list[str]:
        """当前会话的模块 id 清单（拿不到就空表 → 调用方退回「全量」）。"""
        session = getattr(self._window, "session", None)
        registry = getattr(session, "registry", None)
        try:
            return [p.id for p in getattr(registry, "plugins", []) or []]
        except Exception:  # noqa: BLE001 - 取不到清单不是错误，逐页门控会退回全量
            return []

    # ---- JS 主动查询 ------------------------------------------------------
    @Slot(result=str)
    def listPorts(self) -> str:  # noqa: N802 - JS 侧用驼峰
        return to_json(list_ports())

    @Slot(result=str)
    def uiDefaults(self) -> str:  # noqa: N802 - JS 侧用驼峰
        """界面初始值（语言/串口/增益）：让 ``--lang en`` 这类命令行参数也作用到网页。"""
        return to_json(self._window.ui_defaults())

    @Slot(result=str)
    def runtime(self) -> str:
        """当前运行态快照：给**晚连上**的前端补一次状态。

        QWebChannel 连上之前的信号会丢；``--mock`` / ``--autostart`` 早在页面
        加载完之前就把采集跑起来了，前端不补这一下会一直显示「未连接」。
        """
        return to_json(self._window.runtime())

    @Slot(result=str)
    def scenarios(self) -> str:
        from tests.mock_source import SCENARIOS

        return to_json(sorted(SCENARIOS))

    @Slot(result=str)
    def renderWaterfall(self) -> str:  # noqa: N802 - JS 侧用驼峰
        """瀑布图页的「生成 PNG + HTML」按钮：强制重渲一次（绕过节流）。

        注意 ``@Slot`` 的参数表必须和方法签名一致：写成 ``@Slot(str, result=str)``
        时 Qt 会按「一个 str 入参」注册，网页只传回调就会被吞掉，前端拿到的是空值
        （自检里实测过：Promise 直接 resolve 成 ``[]``，Python 侧根本没被调用）。
        """
        return to_json(self._window.render_waterfall())

    @Slot(result=str)
    def clearHeatMemory(self) -> str:  # noqa: N802 - JS 侧用驼峰
        """一键清除「记忆热力」：把每个频段格的积分计数归零。

        只清这一个字节数组，**不**动环形缓冲和已存的瀑布帧 —— 用户要的是
        「忘掉之前累积的热度」，不是「把历史数据删掉」。
        """
        session = self._window.session
        if session is None:
            return to_json({"ok": False, "error": "not_connected"})
        cleared = 0
        for plugin in session.registry.plugins:
            fn = getattr(plugin, "clear_heat", None)
            if fn is None:
                continue
            try:
                cleared += int(fn())
            except Exception:  # noqa: BLE001 - 单个插件失败不影响其它
                continue
        return to_json({"ok": True, "cleared": cleared})

    @Slot(str, result=str)
    def openFile(self, path: str) -> str:  # noqa: N802 - JS 侧用驼峰
        """用系统默认程序打开产物（只允许 ``.data/`` 下的东西，防越权）。"""
        return to_json(self._window.open_report(path))

    @Slot(str, result=str)
    def gainControl(self, payload: str) -> str:  # noqa: N802 - JS 侧用驼峰
        """顶栏增益控件：``{action:"mode"|"step"|"index"|"target", ...}``。

        只写「意图」，真正的 `GAIN` 命令由射频线程在**两次扫描之间**发出去
        （扫描中途插命令会把串口读缓冲打乱，实测 ``SPEC 启动回复异常``）。
        """
        return to_json(self._window.gain_control(payload))

    @Slot(str, result=str)
    def roundControl(self, payload: str) -> str:  # noqa: N802 - JS 侧用驼峰
        """圆屏轮播控件：``{action:"start"|"stop"|"status", secs?:[a,b,c]}``。

        只写「意图」，真正的 ``RD VIEWSEC`` 由射频线程在**两次扫描之间**发出去
        （同 GAIN 纪律：扫描中途插命令会把串口读缓冲打乱）。
        """
        return to_json(self._window.round_control(payload))

    # ---- 信道占用（上位机侧统计，判据与固件同源）----------------------
    @Slot(result=str)
    def occNow(self) -> str:  # noqa: N802 - JS 侧用驼峰
        """逐信道占用快照（38 个信道各一行：当前 / 峰值 / 平均 / 帧数）。

        统计在射频线程里跟着扫描**一直**在跑（见 ``Runner.on_snapshot``），
        这里只是取一份加过锁的拷贝；前端只在「信道占用」页可见时每秒来读一次。
        所以“没打开页面的时候不统计”是错的，反过来才是真的。
        """
        occ = getattr(self._window, "occ", None)
        if occ is None:
            return to_json({"ok": False, "error": "not_ready"})
        return to_json(occ.snapshot())

    @Slot(result=str)
    def occReset(self) -> str:  # noqa: N802 - JS 侧用驼峰
        """把占用统计清零（只清统计，不动扫描、不动热力记忆、不动瀑布）。"""
        occ = getattr(self._window, "occ", None)
        if occ is None:
            return to_json({"ok": False, "error": "not_ready"})
        occ.reset()
        return to_json({"ok": True})

    @Slot(result=str)
    def occCsv(self) -> str:  # noqa: N802 - JS 侧用驼峰
        """把当前快照写成 CSV（放 ``.data/``），返回路径 —— 前端提示 + 可直接打开。"""
        occ = getattr(self._window, "occ", None)
        if occ is None:
            return to_json({"ok": False, "error": "not_ready"})
        try:
            root = os.path.realpath(os.path.join(WEB_DIR, os.pardir, ".data"))
            os.makedirs(root, exist_ok=True)
            name = time.strftime("occ_%Y%m%d_%H%M%S.csv")
            target = os.path.join(root, name)
            snap = occ.snapshot()
            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write("# 相对功率码判据，不是 dBm；occ_pct=逐帧 EMA 占空比\n")
                fh.write("# 会话时长 %.1f s，共 %d 帧，对上信道 %d 帧\n"
                         % (snap.get("elapsed_s", 0.0), snap.get("fed", 0),
                            snap.get("matched", 0)))
                fh.write(occ.csv_header() + "\n")
                for row in occ.csv_rows():
                    fh.write(row + "\n")
        except OSError as exc:
            return to_json({"ok": False, "error": str(exc)})
        return to_json({"ok": True, "path": target, "name": name})

    # ---- WiFi 环境 / 未知热点 ---------------------------------------------
    @Slot(result=str)
    def scanWifi(self) -> str:  # noqa: N802 - JS 侧用驼峰
        """扫本机可见的 WiFi，返回 SSID/BSSID/信道/**BSS Load 利用率**。

        为什么这件事必须在上位机做
        --------------------------
        板子只有 FFT 功率谱，没有 802.11 协议栈，拿不到 SSID。
        电脑的网卡本来就在解析管理帧，直接问系统就行。
        拿到名单后 ``pushKnown`` 灌进固件，固件才做得了差分。

        ``频道利用率`` 是 AP 在 BSS Load 元素里**自己报**的，
        反映「这个 AP 有多忙」；不是我们测到的「这个频段有多挤」。
        两者对照才有诊断价值（见 ``wifi_env`` 模块注释）。
        """
        try:
            import wifi_env
        except Exception as exc:  # noqa: BLE001 - 缺依赖不该崩界面
            return to_json({"ok": False, "error": str(exc), "networks": []})
        # refresh=True：先让网卡做一次**全信道**扫描。不带它时 netsh 只读缓存，
        # 已连接状态下常驻 1 个 AP，名单缺一半 ⇒ 固件那边 B 白白虚高。
        nets = wifi_env.scan(refresh=True)
        rows = []
        for n in nets:
            for b in n.bss:
                rows.append({
                    "ssid": n.ssid,
                    "hidden": bool(n.hidden),
                    "auth": n.auth,
                    "bssid": b.bssid,
                    "channel": b.channel,
                    "mhz": b.mhz,
                    "band": b.band,
                    "signal": b.signal,
                    "util": b.utilization,
                    "stations": b.stations,
                })
        rows.sort(key=lambda r: (r["mhz"], -r["signal"]))
        return to_json({
            "ok": True,
            "ssid_count": len(nets),
            "bss_count": len(rows),
            "hidden_count": sum(1 for n in nets if n.hidden),
            "freqs": wifi_env.known_frequencies(nets),
            "rows": rows,
        })

    @Slot(str, result=str)
    def pushKnown(self, payload: str) -> str:  # noqa: N802
        """把已知热点中心频率灌进固件（``RD KNOWN CLR`` + 逐条 ``RD KNOWN``）。

        ``payload`` 缺省时先扫一次再灌。整批重灌而不是增量追加：
        路由器会自动换信道，旧条目留着会把换过信道的老 AP 误判成「未知」。
        """
        session = self._window.session
        if session is None:
            return to_json({"ok": False, "error": "not_connected"})
        try:
            data = json.loads(payload) if payload else {}
        except Exception:  # noqa: BLE001
            data = {}
        freqs = data.get("freqs") if isinstance(data, dict) else None
        if not freqs:
            try:
                import wifi_env
                freqs = wifi_env.known_frequencies(wifi_env.scan(refresh=True))
            except Exception as exc:  # noqa: BLE001
                return to_json({"ok": False, "error": str(exc)})
        freqs = list(freqs)

        # 下发必须交给 Runner 线程
        # ------------------------
        # 这个按钮最初直接从主线程调 ``session.push_known()``，而 Runner 线程
        # 同时在读写同一个 ``Serial``。两线程并发的结果：pyserial 抛
        # ``Write timeout``，紧接着 Runner 线程整个崩掉，它的
        # ``finally: session.close()`` 把 ``link`` 置 None —— 之后所有下发
        # 都报「链路已关闭」，界面看起来像永久坏了。
        # 所以和 ``on_watch_changed`` 走同一条队列：主线程只投递，Runner 消费。
        box = getattr(self._window, "watch_box", None)
        runner = getattr(self._window, "runner", None)
        if box is not None and runner is not None and runner.isRunning():
            box["pending"] = freqs
            return to_json({"ok": True, "queued": len(freqs), "freqs": freqs})

        # Runner 没在跑（只连了串口、还没点开始采集）→ 此时没有并发的读者，
        # 主线程直接下发是安全的。
        try:
            res = session.push_known(freqs)
        except Exception as exc:  # noqa: BLE001
            return to_json({"ok": False, "error": str(exc)})
        res = dict(res or {})
        res["ok"] = True
        res["freqs"] = freqs
        return to_json(res)

    # ---- 监测策略（自动扫描 / 狂暴 / 日常）------------------------------
    @Slot(result=str)
    def watchStatus(self) -> str:  # noqa: N802
        """当前模式、周期、下一轮倒计时、上轮结果。前端每秒拉一次做倒计时。"""
        return to_json(self._window.watch.status())

    @Slot(str, result=str)
    def setWatch(self, payload: str) -> str:  # noqa: N802
        """``{"mode": "off|daily|burst", "interval_s": 900}`` → 切模式。

        切过去会立刻跑一轮（``Watch.set_mode`` 把 due 归零），所以前端点完
        按钮在 3~5 秒内就能看到日志里出现新的扫描结果。
        """
        data = _loads(payload)
        if not isinstance(data, dict):
            data = {}
        return to_json(self._window.watch_mode(
            str(data.get("mode") or "off"),
            data.get("interval_s"),
        ))

    @Slot(result=str)
    def watchNow(self) -> str:  # noqa: N802
        """立刻插一轮，不等下一个周期。"""
        return to_json(self._window.watch_now())

    @Slot(str, result=str)
    def setLang(self, lang: str) -> str:  # noqa: N802 - JS 侧用驼峰
        """界面切语言：``en`` 之后所有下行文案（日志/状态/表格）改出英文。"""
        return to_json(self._window.set_lang(lang))

    # ---- 控制 -------------------------------------------------------------
    @Slot(str, result=str)
    def start(self, config_json: str) -> str:  # noqa: N802
        return to_json(self._window.start_run(config_json))

    @Slot(str, result=str)
    def setFullscreen(self, payload: str) -> str:  # noqa: N802 - JS 侧用驼峰
        """网页里的「全屏」按钮。

        ``element.click()`` 是合成事件、没有用户手势，``requestFullscreen()`` 会被
        Chromium 直接拒（现场量的就是「点了没反应/跟普通布局一模一样」）。所以真正
        的全屏由 Python 侧调窗口 API 完成：手上有窗口，不需要手势。
        """
        return to_json(self._window.set_fullscreen(payload))

    @Slot(result=str)
    def stop(self) -> str:
        return to_json(self._window.stop_run())



class WatchWorker(QThread):
    """后台**只做 WiFi 扫描**（netsh），扫完把频点列表交给主线程去下发。

    分工的边界（踩过坑，别越界）
    ----------------------------
    一开始让这个线程自己调 ``session.push_known()``，结果上位机直接整窗消失、
    没有任何 Python 回溯 —— 那是 pyserial 同一个 ``Serial`` 对象被采集线程
    （Runner）和本线程同时动，在原生层炸掉。

    所以这里**只碰 netsh**（纯子进程，不碰串口），串口操作一律交回主线程。
    主线程每轮被 ``push_known`` 占 1~2 秒，但那是原本「点一次下发按钮」就
    有的开销，用户也只在 15 秒一次的节奏上遇到，界面感受远比崩掉好。
    """

    due = Signal(str)    # Watch.status() 的 JSON，主线程据此执行下发

    def __init__(self, box: dict[str, object]) -> None:
        super().__init__()
        #: **普通 dict，由 HostWindow 持有**。绝不存 HostWindow（QObject）再点它的
        #: 属性：PySide6 下跨线程读 QObject 实例属性会走原生查找，直接栈溢出。
        #: ``{"watch": Watch}``。
        self.box = box
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def _sleep(self, seconds: float) -> bool:
        """可中断的睡眠：``request_stop()`` 之后最多 250 ms 就能退出。"""
        left = float(seconds)
        while left > 0 and not self._stop:
            step = min(left, 0.25)
            self.msleep(int(step * 1000))
            left -= step
        return not self._stop

    def run(self) -> None:  # noqa: D401 - QThread 入口
        w = self.box["watch"]
        try:
            import wifi_env
        except Exception as exc:  # noqa: BLE001 - 缺依赖不该让线程崩掉
            self.due.emit(to_json({"error": str(exc), "freqs": []}))
            return

        while not self._stop:
            if not w.due():
                if not self._sleep(0.25):
                    break
                continue
            w.tick()                     # 先排下一轮：失败也不会每拍重撞
            freqs: list[int] = []
            err = ""
            try:
                # 周期监测也要 refresh：这就是「多久重扫一次 WiFi」的那个扫。
                nets = wifi_env.scan(refresh=True)
                freqs = wifi_env.known_frequencies(nets)
            except Exception as exc:  # noqa: BLE001 - 一轮失败只记录，不停循环
                err = f"{type(exc).__name__}: {exc}"
            if self._stop:
                break
            import sys as _s
            print(f"[wdiag] tick -> emit freqs={len(freqs)} err={err!r}", file=_s.stderr, flush=True)
            # 下发交给主线程（串口只能一个线程动）
            self.due.emit(to_json({"freqs": freqs, "error": err}))


class Runner(QThread):
    """射频线程：反复跑时间片，把快照/表格节流后推给前端。"""

    def __init__(
        self,
        session: HostSession,
        bridge: Bridge,
        lang: str = "zh",
        box: dict[str, object] | None = None,
        view: dict[str, object] | None = None,
        module_ids: Iterable[str] = (),
        watch_box: dict[str, object] | None = None,
        occ: occupancy.ChannelOccupancy | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        #: 逐信道占用统计（普通 Python 对象 + 内部锁，不是 QObject —— 主线程
        #: 会随时来读快照，跨线程碰 QObject 属性在 PySide6 里会直接崩进程）。
        self.occ = occ if occ is not None else occupancy.ChannelOccupancy()
        self.bridge = bridge
        self.lang = lang
        #: 最近一次 stats 快照的落脚点（**普通 dict，由窗口持有**）。
        #: 不要把它存在 Runner 自己身上：主线程去读一个还在跑的 QThread 实例属性，
        #: PySide6 会走原生属性查找 → 直接栈溢出崩进程（实测无任何 Python 回溯）。
        self.box: dict[str, object] = box if box is not None else {}
        #: 页面门控状态（普通 dict，主线程写 / 本线程读，绝不碰 QObject 属性）
        self.view: dict[str, object] = view if view is not None else {"views": {}, "epoch": 0}
        #: 会话里有哪些模块 id（构造时在主线程取一份，线程里不再回读会话）
        self.module_ids: tuple[str, ...] = tuple(module_ids or ())
        #: 监测下发的中转站（普通 dict，由 HostWindow 持有）：主线程放
        #: ``{"pending": [freqs]}``，本线程取出后自己执行 ``push_known``。
        #: **串口只由本线程碰** —— 主线程直接下发会和这里的读并发，
        #: 原生层会直接把进程干掉（无回溯）。
        self.watch_box: dict[str, object] = watch_box if watch_box is not None else {}
        self._stop = False
        self.last_error = ""

    def request_stop(self) -> None:
        self._stop = True

    def _mock_pace(self) -> float:
        """假固件不模拟驻留时间（一个窗口 3ms 就跑完），整条扫描计划会瞬间刷完、
        游标闪成一片。这里按「真机每窗 100ms / 每窗 2 帧」给帧之间补一个步长，
        界面节奏就和真机一致（约 10 窗/秒）。真机返回 0（节奏由串口自然决定）。"""
        config = self.session.config
        if not config or not config.mock:
            return 0.0
        dwell_ms = 100
        for plugin in self.session.registry.plugins:
            try:
                requests = list(plugin.scan_requests()) if hasattr(plugin, "scan_requests") else []
            except Exception:  # noqa: BLE001 - 折算用的常量，拿不到就用默认值
                requests = []
            if requests:
                dwell_ms = max(10, int(requests[0].dwell_ms))
                break
        return (dwell_ms / 1000.0) / MOCK_FRAMES_PER_WINDOW

    def run(self) -> None:  # noqa: C901 - 单线程主循环，读下来比拆散更清楚
        session, bridge = self.session, self.bridge
        last_spectrum = 0.0
        last_slow = 0.0
        last_epoch = int(self.view.get("epoch", 0) or 0)
        consecutive_errors = 0
        seen_errors = 0
        relinks = 0
        #: 故障注入（只给自检/回归用）：ESPSDR_FAULT_FAILED_AFTER_S=6 → 跑满 6 s
        #: 后人为把链路标成失效，用来证明「自动重同步」这条恢复路径真的接上了。
        fault_after_s = float(os.environ.get("ESPSDR_FAULT_FAILED_AFTER_S", "0") or 0)
        fault_done = False

        pace = self._mock_pace()

        def on_snapshot(snap) -> None:
            nonlocal last_spectrum
            # 逐信道占用统计：**放在门控与节流之前**。它是慢变量积累，一帧都不能少；
            # 前端只在「信道占用」页可见时才来读快照，统计本身一直跟着扫描在跑。
            try:
                self.occ.feed(snap)
            except Exception:  # noqa: BLE001 - 统计失败绝不该停掉采集
                pass
            # 页面门控：没人停在「实时频谱」页就整帧跳过（前端也没地方画）。
            # 注意这里的 return **必须**在补节拍之前打住并按假固件的节拍睡一下，
            # 否则假固件会把整条扫描瞬间跑完（真机由串口自然限速，不会）。
            if not wants_spectrum(self.view):
                if pace:
                    time.sleep(pace)
                return
            now = time.time()
            if now - last_spectrum < SPECTRUM_MIN_INTERVAL:
                return
            last_spectrum = now
            try:
                bridge.spectrum.emit(to_json(session.spectrum(snap)))
                self.box["spectrum_emits"] = int(self.box.get("spectrum_emits", 0) or 0) + 1
            except Exception as exc:  # noqa: BLE001 - 一帧打包失败不该停掉整个会话
                bridge.log.emit(tr(self.lang, "pack_failed", error=error_text(self.lang, exc)))
            if pace:
                time.sleep(pace)

        try:
            while not self._stop:
                # 监测线程扫完 WiFi 会往 watch_box 放一批频点，由**本线程**下发。
                # 放在循环头、step() 之前：这一刻本线程不在串口里，push_known
                # 内部的 sweep.stop()/start() 才不会和自己的读打架。
                pend = self.watch_box.get("pending")
                if pend is not None:
                    import sys as _s2
                    print(f"[wdiag] runner got pending={len(pend)}", file=_s2.stderr, flush=True)
                    self.watch_box["pending"] = None
                    w_obj = self.watch_box.get("watch")
                    try:
                        session.push_known(list(pend))  # type: ignore[arg-type]
                        st = w_obj.note_round(list(pend)) if w_obj else {}
                    except Exception as exc:  # noqa: BLE001 - 一轮失败不停循环
                        st = (
                            w_obj.note_round(list(pend), f"{type(exc).__name__}: {exc}")
                            if w_obj else {}
                        )
                if fault_after_s and not fault_done:
                    if time.time() - getattr(session, "started_at", time.time()) >= fault_after_s:
                        fault_done = True
                        link = getattr(session, "link", None)
                        if link is not None and not getattr(link, "failed", False):
                            link.failed = True
                            self.box["fault_injected"] = True
                            bridge.log.emit("[test] fault injection: link marked failed")
                stats = session.step(STEP_SECONDS, on_snapshot=on_snapshot)
                now = time.time()
                # view_epoch 变了（有人切页）就立刻补推一次，新页面不用干等一个周期
                epoch = int(self.view.get("epoch", 0) or 0)
                if stats is not None and (now - last_slow >= TABLE_INTERVAL or epoch != last_epoch):
                    last_slow = now
                    last_epoch = epoch
                    modules = needed_modules(self.view, self.module_ids)
                    snapshot = session.stats(stats)
                    self.box["stats"] = snapshot  # 收尾时 scheduler 已拆，自检靠这份留档
                    bridge.stats.emit(to_json(snapshot))
                    bridge.tables.emit(to_json(session.tables(modules or None)))
                    # 留档：自检/浸泡靠它核对「全量并行」真的在跑（modules 恒为全部）
                    views = self.box.setdefault("table_views", [])
                    if isinstance(views, list):
                        views.append(
                            {"pages": sorted(wanted_pages(self.view)),
                             "modules": None if not modules else list(modules),
                             "total": len(self.module_ids)}
                        )
                        del views[:-80]
                if stats is not None:
                    if stats.source_errors > seen_errors:
                        seen_errors = stats.source_errors
                        consecutive_errors += 1
                        message = error_text(self.lang, stats.last_source_error) if stats.last_source_error else "scan failed"
                        if message != self.last_error:
                            self.last_error = message
                            bridge.log.emit(tr(self.lang, "rf_error", message=message))
                        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                            # 先把串口重同步一次再判死：core 的 scan() 一旦置 failed
                            # 就永不复位，不重开的话现场只能重启上位机。
                            healed = session.relink() if relinks < MAX_RELINKS else {}
                            if healed.get("ok"):
                                consecutive_errors = 0
                                relinks += 1
                                bridge.log.emit(str(healed.get("message") or ""))
                                continue
                            reason = str(healed.get("message") or "")
                            if not reason and relinks < MAX_RELINKS:
                                # 冷却中：串口可能真被拔了，歇一下再来，不算停机
                                time.sleep(REOPEN_RETRY_SLEEP_S)
                                consecutive_errors = MAX_CONSECUTIVE_ERRORS - 1
                                continue
                            reason = reason or message
                            bridge.log.emit(tr(self.lang, "relink_failed", error=reason))
                            bridge.log.emit(
                                tr(self.lang, "auto_stop", n=consecutive_errors, message=reason)
                            )
                            break
                    else:
                        consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001
            bridge.log.emit(tr(self.lang, "thread_error", error=error_text(self.lang, exc)))
        finally:
            try:
                session.close()
            finally:
                bridge.state.emit(to_json({"state": "stopped", "ts": round(time.time(), 3)}))


class HostWindow(QObject):
    """窗口 + 会话 + 线程的生命周期管理（也负责 ``--selftest`` 的自检流程）。"""

    def __init__(self, view: QWebEngineView) -> None:
        super().__init__()
        #: 当前界面语言（由最近一次 start_run / 命令行决定）
        self.lang = "zh"
        self.default_port = "COM56"
        self.default_gain: int | None = None
        #: 增益模式默认值（HARDWARE/MANUAL/AUTO）；界面里随时可切
        self.default_gain_mode = "MANUAL"
        #: AUTO 模式目标底噪参考码
        self.default_gain_target = 4.0
        self.view = view
        #: 页面门控状态：``{"views": {窗口标签: 页面 id}, "epoch": 序号}``。
        #: 前端切页 → ``Bridge.setView`` 写这里；射频线程读它决定「这一秒算哪些模块」。
        #: **必须早于 Bridge 构造**：Bridge 要拿它当共享状态（普通 dict，不是 QObject 属性）。
        self.view_box: dict[str, object] = {"views": {}, "epoch": 0}
        self.bridge = Bridge(self, view=self.view_box)
        self.session: HostSession | None = None
        self.runner: Runner | None = None
        #: 监测策略（纯逻辑层，见 host/watch.py）。连上设备后自动进 daily。
        self.watch = watch.Watch()
        self.watcher: WatchWorker | None = None
        #: 监测线程要用的两个对象放**普通 dict**：后台线程只准读这个 dict，
        #: 不准点 HostWindow 的属性（理由见 WatchWorker.__init__）。
        self.watch_box: dict[str, object] = {
            "watch": self.watch,
            "session": None,
            "pending": None,
        }
        #: 采集线程写、主线程读的普通 dict（放 stats 留档；见 Runner.box 的注释）
        self.stats_box: dict[str, object] = {}
        #: 逐信道占用统计（射频线程写、界面线程读；跨会话保留，除非用户点「清零」）
        self.occ = occupancy.ChannelOccupancy()
        #: 最近一次 stats 快照（采集线程写；收尾自检要用 share 判定射频时间配比）
        self.last_stats: dict[str, object] = {}
        self.page_loaded = False
        #: 最后一帧频谱 / 最后一次表格：独立窗口是「晚连上」的，靠这个立刻有内容
        self.last_spectrum = ""
        self.last_tables = ""
        #: 独立窗口（window.open 来的）：(view, channel) —— channel 必须留引用，否则被 GC
        self.popouts: list[tuple[QWebEngineView, QWebChannel]] = []
        self.bridge.spectrum.connect(self._cache_spectrum)
        self.bridge.tables.connect(self._cache_tables)
        #: **必须持有 QWebChannel 的 Python 引用**：只交给 setWebChannel 时对象可能被 GC，
        #: 表现是前端 new QWebChannel 回调永不触发（Bridge 一直 ready=false）。
        self.channel: QWebChannel | None = None

    def _cache_spectrum(self, payload: str) -> None:
        self.last_spectrum = payload

    def _cache_tables(self, payload: str) -> None:
        self.last_tables = payload

    def gain_state(self) -> dict[str, object]:
        session = self.session
        if session is None:
            # 还没开始采集：给一份「将要用的」状态，顶栏不会空着
            return {
                "kind": "gain",
                "mode": self.default_gain_mode,
                "index": self.default_gain if self.default_gain is not None else 60,
                "effective": None,
                "floor_code": None,
                "target": self.default_gain_target,
                "tolerance": 1.5,
                "step": 2,
                "min_index": 0,
                "max_index": 90,
                "samples": 0,
                "adjusts": 0,
                "history": [],
                "note": "未开始采集（点了「开始」后这里会显示实时档位与底噪）",
                "mock": False,
                "ok": True,
            }
        return session.gain_state()

    def gain_control(self, payload: str) -> dict[str, object]:
        session = self.session
        if session is None:
            return {"kind": "gain", "ok": False, "note": tr(self.lang, "gain_no_session")}
        state = session.gain_control(payload)
        # 立刻回推一次：界面按下去就有反馈，不用等下一个 stats 周期
        self.bridge.gain.emit(to_json(state))
        return state

    def round_control(self, payload: str) -> dict[str, object]:
        session = self.session
        if session is None:
            return {"kind": "round", "ok": False, "note": tr(self.lang, "gain_no_session")}
        return dict(session.round_control(payload))

    def runtime(self) -> dict[str, object]:
        session = self.session
        runner = self.runner
        return {
            "gain": self.gain_state(),
            "running": bool(runner is not None and runner.isRunning()),
            "info": session.describe() if session is not None else None,
            "stats": session.stats() if session is not None else None,
            # 晚连上的窗口（独立小窗口）要靠这两个才有画面
            "spectrum": _loads(self.last_spectrum),
            "tables": _loads(self.last_tables),
        }

    def ui_defaults(self) -> dict[str, object]:
        return {
            "lang": self.lang,
            "port": self.default_port,
            "gain_index": self.default_gain,
            "gain_mode": self.default_gain_mode,
            "gain_target": self.default_gain_target,
        }

    # ---- 瀑布图出图（网页按钮 → Python） ---------------------------------
    def render_waterfall(self) -> dict[str, object]:
        """强制渲染最新时频热力图，返回产物路径给网页。

        为什么放在 GUI 线程里跑：一次渲染量的是环形缓冲（最多 1200 帧），
        实测 27 轮扫描约 0.5 s，冷启动秒级；为它单开线程不值得，
        代价是点按钮瞬间界面卡一下（按钮文案已换「生成中…」）。
        """
        session = self.session
        if session is None:
            return {"ok": False, "error": tr(self.lang, "not_connected")}
        plugin = None
        for item in session.registry.plugins:
            if getattr(item, "id", "") == "waterfall":
                plugin = item
                break
        if plugin is None:
            return {"ok": False, "error": "modes/ 里没有 waterfall 模块"}
        try:
            output = plugin.produce(force=True)
        except Exception as exc:  # noqa: BLE001 - 界面上要看到原因
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        payload = output.payload or {}
        files = [str(item) for item in (output.files or [])]
        index = str(payload.get("index") or "")
        png = next((f for f in files if f.lower().endswith(".png")), "")
        html = index if index else next((f for f in files if f.lower().endswith(".html")), "")
        return {
            "ok": True,
            "pages": int(payload.get("pages") or 0),
            "index": index,
            "png": os.path.basename(png) if png else "",
            "html": os.path.basename(html) if html else "",
        }

    def open_report(self, path: str) -> dict[str, object]:
        """用系统浏览器打开产物（只接受 ``.data/`` 之内的路径）。"""
        target = str(path or "").strip()
        if not target:
            return {"ok": False, "error": "空路径"}
        root = os.path.realpath(os.path.join(WEB_DIR, os.pardir, ".data"))
        real = os.path.realpath(target)
        if os.path.commonpath([root, real]) != root:
            return {"ok": False, "error": f"只允许打开 .data/ 下的产物：{target}"}
        if not os.path.isfile(real):
            return {"ok": False, "error": f"文件不存在：{target}"}
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(real)):
            return {"ok": False, "error": "系统没有可打开该文件的程序"}
        return {"ok": True, "path": real}

    # ---- 页面 -------------------------------------------------------------
    def _wire_page(self, page: QWebEnginePage) -> None:
        """页面接线：接住 window.open，以及网页里的 requestFullscreen()。

        不接 ``fullScreenRequested`` 的话 Qt 会**拒绝**全屏请求，前端就只能退化成
        「铺满窗口」—— 用户看到的现象就是「全屏跟普通布局一模一样」。
        """
        if isinstance(page, ConsolePage):
            page.owner = self
        page.fullScreenRequested.connect(self._on_fullscreen)

    def _on_fullscreen(self, request) -> None:
        view = request.page().view() if hasattr(request, "page") else None
        win = (view.window() if view is not None else self.view.window())
        request.accept()
        if request.toggleOn():
            win.showFullScreen()
        else:
            win.showNormal()
        print(f"[ui] 全屏 {'开' if request.toggleOn() else '关'}", flush=True)

    def load(self) -> None:
        page = self.view.page()
        self._wire_page(page)
        self.channel = QWebChannel(self)
        self.channel.registerObject("backend", self.bridge)
        self.view.page().setWebChannel(self.channel)
        self.view.loadFinished.connect(self._on_load_finished)
        self.view.load(QUrl.fromLocalFile(os.path.join(WEB_DIR, "index.html")))

    def _auto_scan_wifi(self) -> None:
        """开机后台扫一次 WiFi，把 ENV 页首屏数据推给前端。

        为什么单起线程
        --------------
        ``scan(refresh=True)`` 要 4 秒左右：调 wlanapi 的 WlanScan 下发全信道
        扫描 -> 等 2.5 秒 -> 再 netsh 解析。放在 GUI 线程里就是启动卡 4 秒。
        扫完通过 ``Bridge.wifiEnv``（Qt 信号，跨线程自动排队）回主线程，
        不直接碰任何 UI 对象。

        失败必须静默
        ------------
        非 Windows / 没有无线网卡 / API 缺失时 ``force_scan`` 返回 False，
        ``scan`` 仍然会退回读缓存。真出异常也只 log，绝不能让开机崩界面 ——
        这条功能的定位是「顺手把数据备好」，不是启动的必要条件。
        """
        def work() -> None:
            try:
                import wifi_env
                nets = wifi_env.scan(refresh=True)
            except Exception as exc:  # noqa: BLE001 - 自扫失败不许影响界面
                print("[wifi] 开机自扫失败：%s: %s" % (type(exc).__name__, exc), flush=True)
                return
            rows = []
            for n in nets:
                for b in n.bss:
                    rows.append({
                        "ssid": n.ssid,
                        "hidden": bool(n.hidden),
                        "auth": n.auth,
                        "bssid": b.bssid,
                        "channel": b.channel,
                        "mhz": b.mhz,
                        "band": b.band,
                        "signal": b.signal,
                        "util": b.utilization,
                        "stations": b.stations,
                    })
            rows.sort(key=lambda r: (r["mhz"], -r["signal"]))
            payload = {
                "ok": True,
                "auto": True,
                "ssid_count": len(nets),
                "bss_count": len(rows),
                "hidden_count": sum(1 for n in nets if n.hidden),
                "freqs": wifi_env.known_frequencies(nets),
                "rows": rows,
            }
            print("[wifi] 开机自扫完成：%d 个热点 / %d 个 BSSID / %d 个频点"
                  % (len(nets), len(rows), len(payload["freqs"])), flush=True)
            self.bridge.wifiEnv.emit(to_json(payload))

        threading.Thread(target=work, name="wifi-autoscan", daemon=True).start()

    def _on_load_finished(self, ok: bool) -> None:
        self.page_loaded = bool(ok)
        self.bridge.state.emit(to_json({"state": "ui-loaded" if ok else "ui-load-failed"}))
        if ok:
            # 勘误：这一段绝不能影响界面启动——戳只用于自证「跑的是哪一版」
            try:
                stamp = code_stamp()
                self.stamp = stamp
                self.view.page().runJavaScript(
                    "(function(){var e=document.getElementById('code-stamp');"
                    "if(e)e.textContent=" + json.dumps("code " + stamp) + ";})()",
                    0,
                )
                print("[code] " + stamp + "  <- 页脚应显示同一时间戳；不一致就是旧进程", flush=True)
                # 开机自扫：WiFi 环境是 PC 网卡的事，跟有没有连板无关。
                # 晚 1.5 秒是因为 WebChannel 的 JS 侧刚注入完，太早 emit 会没人接。
                QTimer.singleShot(1500, self._auto_scan_wifi)
            except Exception as exc:  # noqa: BLE001 - 戳失败不许拖垮界面
                self.stamp = ""
                print(f"[code] 版本戳计算失败：{exc}", flush=True)
        if not ok:
            print("[ui] 页面加载失败", flush=True)

    def set_fullscreen(self, payload: str) -> dict[str, object]:
        """payload = {"on": bool, "target": "main" | "popout"}（老式 true/false 也认）"""
        try:
            data = json.loads(payload) if payload else {}
        except ValueError:
            data = {}
        if isinstance(data, bool):
            wanted, target = data, "main"
        else:
            wanted = bool((data or {}).get("on"))
            target = str((data or {}).get("target") or "main")
        win = self.view.window()
        if target == "popout":
            live = self.live_popouts()
            if live:
                win = live[-1].window()
        if wanted:
            win.showFullScreen()
        else:
            win.showNormal()
        return {"fullscreen": bool(win.isFullScreen()), "target": target, "on": wanted}

    # ---- 独立窗口（窗口外弹窗）--------------------------------------------
    def open_popout(self) -> QWebEnginePage:
        """接住前端的 ``window.open``：开一个真正的窗口，共用同一个 Bridge。

        新窗口加载的还是 ``web/index.html``，只是带 ``?solo=<页面:序号>``，
        由网页自己把它变成「只显示那一个面板」。数据不用另外接：QWebChannel
        把同一个 :class:`Bridge` 暴露给每个窗口，信号天然广播。
        """
        view = QWebEngineView()
        page = ConsolePage(view)
        view.setPage(page)
        self._wire_page(page)
        channel = QWebChannel(page)
        channel.registerObject("backend", self.bridge)
        page.setWebChannel(channel)
        configure_settings(view)
        view.setWindowTitle("esp-sdr · 面板窗口")
        view.resize(1024, 680)
        view.show()
        self.popouts.append((view, channel))
        view.destroyed.connect(lambda *_: self._forget_popout(view))
        print("[ui] 独立窗口已打开", flush=True)
        return page

    def _forget_popout(self, view: QWebEngineView) -> None:
        self.popouts = [(v, c) for (v, c) in self.popouts if v is not view]

    def live_popouts(self) -> list[QWebEngineView]:
        return [v for (v, _c) in self.popouts if v.isVisible()]

    # ---- 会话 -------------------------------------------------------------
    def start_run(self, config_json: str) -> dict[str, object]:
        if self.runner is not None and self.runner.isRunning():
            return {"ok": False, "message": tr(self.lang, "already_running")}
        session = None
        try:
            config = HostConfig.from_json(config_json)
            self.lang = config.lang
            session = HostSession(bins_out=config.bins_out)
            info = session.open(config)
        except Exception as exc:  # noqa: BLE001 - 打不开端口是最常见的用户错误
            if session is not None:
                # 半开的会话要收干净：串口占用锁、已打开的链路都别留着
                try:
                    session.close()
                except Exception:  # noqa: BLE001 - 收尾失败不该盖住原始错误
                    pass
            message = error_text(self.lang, exc)
            self.bridge.log.emit(tr(self.lang, "already_running_log", message=message))
            self.bridge.state.emit(to_json({"state": "error", "message": message}))
            return {"ok": False, "message": message}
        self.session = session
        self.watch_box["session"] = session
        self.bridge.info.emit(to_json(info))
        self.bridge.state.emit(to_json({"state": "running", "ts": round(time.time(), 3)}))
        self.runner = Runner(
            session,
            self.bridge,
            config.lang,
            box=self.stats_box,
            view=self.view_box,
            module_ids=[p.id for p in session.registry.plugins],
            watch_box=self.watch_box,
            occ=self.occ,
        )
        self.runner.start()
        self.start_watch()
        message = tr(config.lang, "started", what=info.get("identity") or config.port)
        return {"ok": True, "message": message, "info": info}

    # ---- 监测策略（自动扫描 + 定时下发）------------------------------
    def start_watch(self) -> dict[str, object]:
        """起监测线程。默认进 ``daily``（15 分钟一轮），且**立刻跑第一轮**。

        「开启就自动启动」是用户明确要求：连上就该看到 WiFi 环境已经在被
        摸清、名单已经在往板子里灌，而不是让人先去点一个按钮。
        """
        if self.watcher is not None and self.watcher.isRunning():
            return {"ok": True, "status": self.watch.status()}
        self.watch.set_mode(watch.DAILY)
        self.watcher = WatchWorker(self.watch_box)
        self.watcher.due.connect(self.on_watch_changed)
        self.watcher.start()
        return {"ok": True, "status": self.watch.status()}

    def stop_watch(self) -> dict[str, object]:
        if self.watcher is not None and self.watcher.isRunning():
            self.watcher.request_stop()
            self.watcher.wait(3000)
        self.watcher = None
        return {"ok": True, "status": self.watch.status()}

    def watch_mode(self, mode: str, interval_s: object = None) -> dict[str, object]:
        """切模式：``off`` / ``daily`` / ``burst``，可顺带改日常间隔。

        切过去时 ``Watch.set_mode`` 会把 ``_next_due`` 归零 → 下一拍立刻跑
        一轮，界面上马上能看到效果（不用等 15 分钟）。
        """
        sec: float | None = None
        if interval_s not in (None, ""):
            sec = watch.clamp_interval(interval_s)
        st = self.watch.set_mode(mode, sec)
        if st["mode"] == watch.OFF:
            self.stop_watch()
        elif self.watcher is None or not self.watcher.isRunning():
            self.watcher = WatchWorker(self.watch_box)
            self.watcher.due.connect(self.on_watch_changed)
            self.watcher.start()
        return {"ok": True, "status": self.watch.status()}

    def watch_now(self) -> dict[str, object]:
        """立刻插一轮（不改周期）。排查现场时不想等下一个 15 分钟。"""
        self.watch._next_due = 0.0  # noqa: SLF001 - 同类内部协作，策略层不再开 public 口
        return {"ok": True, "status": self.watch.status()}

    def on_watch_changed(self, payload: str) -> None:
        """监测线程报「到点了，这批频点请下发」→ **在主线程**真的下发。

        为什么下发必须留在主线程
        ------------------------
        串口对象只能一个线程碰。采集线程（Runner）一直在读写它，如果监测线程
        也去发 ``RD KNOWN``，两边同时动同一个 ``Serial`` 会让进程在原生层
        直接消失（无 Python 回溯）。所以线程只管 netsh，串口留在主线程。

        这样每轮会让界面停顿 1~2 秒（就是原来「点一次下发按钮」的开销），
        但在 15 秒／15 分钟的节奏下完全可接受。
        """
        box = _loads(payload)
        if not isinstance(box, dict):
            box = {}
        # 判**值**不判键：worker 每次都带一个 error 字段（失败时才有内容），
        # 写成 `if "error" in box` 会永远为真 —— 每一轮都当失败处理，
        # 频点一个都记不上账（症状：last_freqs 恒为 0）。
        if box.get("error"):
            # 线程里就失败了（缺 wifi_env / netsh 异常），直接记账上报
            self.watch.note_round(error=str(box.get("error") or ""))
            return
        freqs = [int(x) for x in (box.get("freqs") or [])]
        if self.session is None or self.runner is None or not self.runner.isRunning():
            self.watch.note_round(freqs, "not_connected")
            return
        # 只放中转站，真正下发由 Runner 线程做（串口只归它一个线程）。
        self.watch_box["pending"] = freqs

    def set_lang(self, lang: str) -> dict[str, object]:
        """切界面语言：窗口 / 会话 / 射频线程三处一起换（只影响文案，不动判据）。

        ``Runner.lang`` 是普通字符串，射频线程只在自己线程里读它；
        主线程做的是整体替换引用（CPython 下原子），不会读到半个字符串。
        """
        lang = (lang or "zh").lower()
        if lang not in LANGS:
            return {"ok": False, "lang": self.lang, "error": "unknown language"}
        self.lang = lang
        if self.session is not None:
            self.session.set_lang(lang)
        if self.runner is not None:
            self.runner.lang = lang
        return {"ok": True, "lang": lang}

    def stop_run(self) -> dict[str, object]:
        if self.runner is None or not self.runner.isRunning():
            return {"ok": False, "message": tr(self.lang, "no_run")}
        self.runner.request_stop()
        self.runner.wait(4000)
        self.bridge.tables.emit(to_json(self.session.tables()) if self.session else "{}")
        return {"ok": True, "message": tr(self.lang, "stopped")}

    def shutdown(self) -> None:
        if self.watcher is not None and self.watcher.isRunning():
            self.watcher.request_stop()
            self.watcher.wait(3000)
        if self.runner is not None and self.runner.isRunning():
            self.runner.request_stop()
            self.runner.wait(4000)
        for view in list(self.live_popouts()):
            view.close()


def build_app(argv: list[str]) -> tuple[QApplication, QWebEngineView, HostWindow, argparse.Namespace]:
    parser = argparse.ArgumentParser(description="esp-sdr 测试上位机（WebView 版）")
    parser.add_argument("--port", default="COM56", help="默认串口（界面里还能改）")
    parser.add_argument("--gain-index", type=int, default=60, help="默认固定增益档位（真机）")
    parser.add_argument(
        "--gain-mode",
        default="MANUAL",
        choices=["HARDWARE", "MANUAL", "AUTO"],
        help="默认增益模式：HARDWARE=固件 AGC / MANUAL=固定档位 / AUTO=主机闭环（把底噪压到 --gain-target）",
    )
    parser.add_argument("--gain-target", type=float, default=4.0, help="--gain-mode AUTO 的目标底噪参考码")
    parser.add_argument("--mock", action="store_true", help="启动即用假固件自跑")
    parser.add_argument("--scenario", default="wifi_busy", help="--mock 时的场景")
    parser.add_argument(
        "--autostart",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="页面加载完成后自动连接并开始（默认开；--no-autostart 关掉）",
    )
    parser.add_argument("--selftest", action="store_true", help="自检：自动跑几秒 → 抓图 → 打印界面自检报告 → 退出")
    parser.add_argument("--real", action="store_true", help="--selftest 时对着真板跑（默认假固件）")
    parser.add_argument("--seconds", type=float, default=6.0, help="--selftest 跑多久")
    parser.add_argument("--shot", default="", help="--selftest 截图保存路径")
    parser.add_argument("--soak-page", default="", help="--soak 时自动切到这一页（spectrum/waterfall/env）")
    parser.add_argument(
        "--soak",
        type=float,
        default=0.0,
        help="浸泡测试：自动开始采集，每 2s 打印一次实时频谱/瀑布页探针，跑满 N 秒后退出（查「跑一会儿卡住」用）",
    )
    parser.add_argument("--window-size", default="1440x900", help="窗口尺寸 WxH")
    parser.add_argument("--lang", default="zh", choices=list(LANGS), help="界面默认语言（网页里还能切，切换后记在 localStorage）")
    parser.add_argument(
        "--engine",
        default="host",
        choices=list(ENGINES),
        help="采集引擎：host = 主机逐窗驱动（默认）；device = 设备自扫（调度在固件里，判据仍在主机）",
    )
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("esp-sdr 测试上位机")
    width, height = (int(v) for v in args.window_size.lower().split("x", 1))
    view = QWebEngineView()
    view.setPage(ConsolePage(view))
    view.resize(width, height)
    configure_settings(view)
    view.setWindowTitle("esp-sdr 测试上位机 · ESP32-C5 射频监测")
    view.show()

    window = HostWindow(view)
    window.lang = args.lang
    window.default_port = args.port
    window.default_gain = args.gain_index
    window.default_gain_mode = args.gain_mode
    window.default_gain_target = args.gain_target
    window.load()
    return app, view, window, args


def device_engine_probe(seconds: float = 1.2, bins_out: int = 256) -> dict[str, object]:
    """自检里顺手验一遍「设备自扫」引擎（假固件，不开窗口、不占真串口）。

    验的是方案 A 真正动到的那条链路：下发计划 → 设备自己吐帧 → 主机解帧 → 进调度。
    判据只有一条：**真拿到帧**。帧数为 0 说明订阅模式没接上，必须红。
    """
    report: dict[str, object] = {"ok": False, "engine": "", "note": ""}
    session = HostSession(bins_out=bins_out)
    try:
        info = session.open(
            HostConfig.from_json(
                to_json(
                    {
                        "mock": True,
                        "scenario": "mixed",
                        "engine": "device",
                        "gain_mode": "MANUAL",
                        "bins_out": bins_out,
                    }
                )
            )
        )
        report["engine"] = str(info.get("engine", ""))
        report["note"] = str(info.get("engine_note", ""))
        report["supported"] = bool(info.get("sweep_supported"))
        seen: list[int] = []
        session.step(seconds, on_snapshot=lambda snap: seen.append(int(snap.center_hz)))
        state = session.sweep_state()
        report.update(
            {
                "frames": int(state.get("frames", 0) or 0),
                "windows": int(state.get("windows", 0) or 0),
                "misses": int(state.get("misses", 0) or 0),
                "snapshots": len(seen),
                "distinct": len(set(seen)),
            }
        )
        report["ok"] = report["engine"] == "device" and int(report["frames"]) > 0 and len(seen) > 0
    except Exception as exc:  # noqa: BLE001 - 自检要报错不要抛
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    return report


#: 英文界面「零中文」扫描：把 body 下所有文本节点 + title/placeholder/option 扫一遍。
#: 只豁免语言切换按钮本身（它写着「中文(CN)」，那是给中文用户切回去的入口）。
#: 自检必须可重复：先把上次跑存下的语言偏好清掉、强制回中文再开始，
#: 否则上一次切到 en 存在 localStorage 里，这次一上来就是英文，
#: 「底噪」这类中文断言会莫名其妙地挂（实际是状态串了）。
LANG_RESET_JS = """
(function () {
  try { localStorage.removeItem("espsdr.lang"); } catch (e) {}
  if (window.I18N) I18N.setLang("zh");
  return "zh";
})()
"""

CJK_SCAN_JS = """
(function () {
  var CJK = /[\\u3400-\\u4dbf\\u4e00-\\u9fff]/;
  // 豁免两类节点：
  //   [data-lang-toggle] 语言切换按钮（它的文案就是「中文/EN」本身）
  //   [data-raw]         扫描回来的用户数据（SSID 等），中文是隔壁热点的真名，不是漏翻
  var exempt = [];
  var exemptSel = "[data-lang-toggle],[data-raw]";
  document.querySelectorAll(exemptSel).forEach(function (n) { exempt.push(n); });
  var hits = [];
  function path(el) {
    var out = [];
    var n = el;
    for (var i = 0; i < 4 && n && n.tagName; i++) {
      out.push(n.tagName.toLowerCase() + (n.className ? "." + String(n.className).split(" ").join(".") : ""));
      n = n.parentElement;
    }
    return out.join(" < ");
  }
  function add(where, text, el) {
    var t = String(text === null || text === undefined ? "" : text).trim();
    if (!t) return;
    // 只截前 140 字符是不够的：ASCII 面板一开头几百字全是英文，中文在后面就看不到现场，
    // 报出来只有「有这么一条」却没法定位。改成以第一处中文为中心开窗。
    var m = CJK.exec(t);
    if (!m) return;
    var lo = Math.max(0, m.index - 60);
    var frag = (lo > 0 ? "..." : "") + t.slice(lo, lo + 200);
    hits.push(where + "@" + (el ? path(el) : "?") + ": " + frag);
  }
  var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
  while (walker.nextNode()) {
    var node = walker.currentNode;
    var skip = false;
    var p = node.parentElement;
    while (p) { if (exempt.indexOf(p) >= 0) { skip = true; break; } p = p.parentElement; }
    if (!skip) add("text", node.nodeValue, node.parentElement);
  }
  document.querySelectorAll("[title],[placeholder]").forEach(function (n) {
    add("title", n.getAttribute("title"), n);
    add("placeholder", n.getAttribute("placeholder"), n);
  });
  document.querySelectorAll("option").forEach(function (n) { add("option", n.textContent, n); });
  return JSON.stringify({
    lang: window.I18N ? I18N.lang() : null,
    page: (window.Router && Router.current) ? Router.current.id : null,
    hits: hits.slice(0, 60),
    hitCount: hits.length,
    missing: window.I18N ? I18N.missingKeys() : null,
    popouts: [].map.call(document.querySelectorAll(".popout"), function (o) {
      var t = o.querySelector(".popout__title");
      var p = o.querySelector(".popout__stage .panel .panel__title");
      return {
        connected: o.isConnected,
        hidden: !!o.hidden,
        title: t ? t.textContent : null,
        panelTitle: p ? p.textContent : null,
        hasPanel: !!o.querySelector(".popout__stage .panel")
      };
    })
  });
})()
"""


def run_selftest(app: QApplication, view: QWebEngineView, window: HostWindow, args: argparse.Namespace) -> int:
    """自动跑一段：证明链路 + 桥 + 三个页面都真的活着，打印证据并退出。

    验收点（``report["checks"]`` 全 true 才算 OK，逐条都有实测字段）：
      1. 频谱页：收到帧 > 0、2D/3D 画布非纯黑、扫频进度条分段/回卷/热力正常；
      2. 瀑布页：行数/着色行/卡片/横幅/说明都在、出图（PNG + HTML）能生成；
      3. ENV 页：本机 WiFi 扫描有结果、信道占用表有行；
      另加四组横切检查：弹出预览（浮层/缩放/全屏/独立窗口/切页暂停）、
      增益控件（JS→Bridge→Session→状态回推）、窗口缩放四档不越界、
      英文模式零中文（逐页扫 DOM + en 词典缺 key）。
    """
    report: dict[str, object] = {"ok": False, "pages": {}}
    steps: list[str] = []
    started: dict[str, object] = {}

    def fail(message: str) -> None:
        report["ok"] = False
        report["error"] = message
        steps.append(f"FAIL {message}")
        print("[selftest] " + json.dumps(report, ensure_ascii=False), flush=True)
        app.quit()

    def shot(tag: str) -> dict[str, object]:
        base = args.shot or os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest.png")
        root, ext = os.path.splitext(base)
        path = base if tag == "spectrum" else f"{root}-{tag}{ext or '.png'}"
        image = view.grab()
        saved = image.save(path)
        return {"path": path, "saved": bool(saved), "w": image.width(), "h": image.height()}

    # 每个页面的 DOM 探针：只读，不改状态
    PAGE_JS = {
        "waterfall": """JSON.stringify({
            probe: (window.Pages.waterfall && Pages.waterfall.probe ? Pages.waterfall.probe() : null),
            cards: document.querySelectorAll('#page-container .card').length,
            banner: (((document.querySelector('#wf-banner')||{}).textContent) || '').length,
            files: (((document.querySelector('#wf-files')||{}).textContent) || '').length,
            sweep: (((document.querySelector('#wf-sweep-s')||{}).textContent) || ''),
            sweepNote: (((document.querySelector('#wf-sweep-note')||{}).textContent) || '')
        })""",
        # ENV 页（本机 WiFi 扫描）：这张表是唯一不在板子上的数据，必须验它**真的填进来了**
        "env": """JSON.stringify({
            rows: document.querySelectorAll('#env-tbody tr').length,
            ssid: (((document.querySelector('#env-ssid')||{}).textContent) || ''),
            bss: (((document.querySelector('#env-bss')||{}).textContent) || ''),
            hidden: (((document.querySelector('#env-hidden')||{}).textContent) || ''),
            known: (((document.querySelector('#env-known')||{}).textContent) || ''),
            status: (((document.querySelector('#env-status')||{}).textContent) || ''),
            interval: (((document.querySelector('#env-interval')||{}).value) || ''),
            watchState: (((document.querySelector('#env-watch-state')||{}).textContent) || ''),
            notes: document.querySelectorAll('#env-notes li').length
        })""",
    }

    def finish() -> None:
        pages = report["pages"]
        spectrum = pages.get("spectrum", {}) if isinstance(pages, dict) else {}
        wf_page = pages.get("waterfall", {}) if isinstance(pages, dict) else {}
        wf_page_probe = (wf_page.get("probe") or {}) if isinstance(wf_page, dict) else {}
        env_page = pages.get("env", {}) if isinstance(pages, dict) else {}
        env_probe = env_page if isinstance(env_page, dict) else {}
        pp = report.get("popout_pause") or {}
        if isinstance(pp, dict):
            report["table_views"] = window.stats_box.get("table_views") or []
        canvases = spectrum.get("canvases", []) if isinstance(spectrum, dict) else []
        drawn = [c for c in canvases if float(c.get("nonBlack", 0)) > 0.01]
        frames = int(spectrum.get("framesSeen", 0) or 0)
        wf = next((c for c in canvases if c.get("id") == "wf-canvas"), {})
        i18n = report.get("i18n") or {}
        nav = i18n.get("navLabels") or []
        wf_probe = i18n.get("wf2d") or {}
        bar = ((spectrum.get("pageProbe") or {}).get("scanbar") or {}) if isinstance(spectrum, dict) else {}
        bar_groups = bar.get("groups") or []
        bar_wrap = spectrum.get("scanbarSelfTest") or {} if isinstance(spectrum, dict) else {}
        page_probe = (spectrum.get("pageProbe") or {}) if isinstance(spectrum, dict) else {}
        axis = page_probe.get("wf2dAxis") or {}
        popout = report.get("popout") or {}
        pp_open = popout.get("open") or {}
        pp_zoom = popout.get("zoom") or {}
        pp_close = popout.get("close") or {}
        pp_win = report.get("popout_window") or {}
        share: dict[str, object] = {}
        try:
            # 注意：finish() 是模块级函数里的闭包，只能拿 window.*，用 self.* 会 NameError
            # （在 Qt 回调里抛异常是 C 级栈溢出、连 Python 回溯都没有，排查成本极高）
            cached = window.stats_box.get("stats") or {}
            share = dict(cached.get("share") or {})
            if not share and window.session is not None:
                share = dict((window.session.stats() or {}).get("share") or {})
        except Exception:  # noqa: BLE001 - 自检收尾不该因统计失败而崩
            share = {}
        report["share"] = share
        # 故障注入留档（只有 ESPSDR_FAULT_FAILED_AFTER_S 设了才会有），自检/回归靠它
        report["fault_injected"] = bool(window.stats_box.get("fault_injected"))


        report["canvases_drawn"] = len(drawn)
        report["frames_seen"] = frames
        checks = {
            "spectrum_frames": frames > 0,
            "canvas_drawn": len(drawn) > 0,
            # 2D 瀑布（坏过的那块）：画布要有内容 + 缓冲里「一帧一行」真的在累积，
            # 且最新几行比未填充的老行亮（= 旧行确实在下移，不是原地覆盖/拉伸）
            # 2D 瀑布在动：画过的行几乎都互不相同（冻住的话 distinctRows 会只有个位数），
            # 且最新一行真的有亮度。
            # 注意**不能**用「最上一行比最下一行亮 100 码」当判据：缓冲区扫满之后最下面
            # 一行也是真数据（老行只是滚出去，不会被压暗），这个差值会自己失效 —— 实测
            # mock 跑久一点 bottomMean 从 33 涨到 189，检查就假红了。
            "waterfall_2d": (
                float(wf.get("nonBlack", 0)) > 0.05
                and int(wf_probe.get("frames", 0) or 0) > 10
                and int(wf_probe.get("paintedRows", 0) or 0) >= 8
                and int(wf_probe.get("distinctRows", 0) or 0)
                >= max(8, int(wf_probe.get("paintedRows", 0) or 0) * 0.8)
                and int(wf_probe.get("topMean", 0) or 0) > 20
                and not wf_probe.get("imagesTooMany")
            ),
            # 扫频进度条：至少切出两个频段（2.4G / 5G），游标在轨道内、已走过多个窗
            "scanbar_bands": len(bar_groups) >= 2 and int(bar.get("windows", 0) or 0) > 10,
            "scanbar_progress": (
                # 与 waterfall 平分射频后 8s 内只走过 2 个窗（独占时 >3）；
                # 这里守的是"游标在轨道内真的在动"，不是"动得快"。
                # 注意 device 引擎下窗是"按设备吐帧顺序"消费的，采样点可能刚好落在
                # 一趟的**第一窗**（trail 里 visited=1）——所以"走完过一整趟(passes>=1)"
                # 也算动过，别只看 visited（真板实测会误判）。
                (
                    int(bar.get("visited", 0) or 0) >= 2
                    or int(bar.get("passes", 0) or 0) >= 1
                )
                and int(bar.get("index", -1)) >= 0
                and len(bar.get("trail") or []) >= 2
                and 0 <= int(bar.get("x", -1)) <= int(bar.get("width", 0) or 0)
            ),
            # 到终点回卷：喂到最后一格再喂第一格，轮次 +1、本轮覆盖清零
            "scanbar_wrap": int(bar_wrap.get("passesAfterWrap", 0) or 0) == 2
            and int(bar_wrap.get("visitedAfterWrap", 0) or 0) == 1
            and int(bar_wrap.get("indexAtEnd", -1)) == 3,
            # 全频段并集：地图要把**所有模块**的计划合起来画（2.4G + 5G 低 + 5G 高），
            # 早期只跟单个模块 → 别的模块扫到的窗在图上没有格子，看着像"没扫全频段"
            # 注意 5G 常是**一段**：固件自扫计划把 5180~5825 MHz 用重叠窗铺满
            # （20 MHz 窗 / 15 MHz 步进），中间没有 >3 倍窗宽的空档，分成两段反而是错的。
            # 这里守的是「2.4G 和 5G 都在、且 5G 这一段一直画到 5.7 GHz 以上」
            # （= 5G HIGH 没被地图漏掉）。
            "scanbar_union": (
                len(bar_groups) >= 2
                and int(bar.get("windows", 0) or 0) >= 44
                and any(str((g or {}).get("name", "")) == "2.4G" for g in bar_groups)
                and any(
                    float((g or {}).get("lo", 0) or 0) > 5e9 and float((g or {}).get("hi", 0) or 0) >= 5.7e9
                    for g in bar_groups
                )
            ),
            # 历史热力：格子底色跨轮累积（第二轮把同窗抬 20 码，热力必须上升而不是重置）
            "scanbar_heat": (
                int(bar.get("hot", 0) or 0) >= 3
                and bool(bar_wrap.get("heatRose"))
                and sum(int((b or {}).get("hot", 0) or 0) for b in (bar.get("perBand") or []))
                == int(bar.get("hot", 0) or 0)
            ),
            # 语言切换：中文 → English，导航与状态栏都要跟着变
            "i18n_switch": i18n.get("lang") == "en" and any("Live spectrum" in str(x) for x in nav),
            # 外壳文案（含扫频进度条）真的翻了英文，level 数据值仍走显示层映射
            "i18n_shell": (
                len(i18n.get("shell") or []) == 4
                and "Scan progress" in str((i18n.get("shell") or [""])[0])
                and "window 6/49" in str((i18n.get("shell") or ["", ""])[1])
                and str((i18n.get("shell") or ["", "", "", ""])[3]) == "Heavy"
            ),
            # 2D 瀑布频率轴：5 个刻度、升序、中点是本窗中心频点
            "wf2d_axis": (
                len(axis.get("ticks") or []) == 5
                and list(axis.get("ticks") or []) == sorted(axis.get("ticks") or [])
                # 中点 = 本窗中心频点，且落在两端之间
                and int(axis.get("lo", 0)) < int(axis.get("center", 0)) < int(axis.get("hi", 0))
            ),
            # 双击弹出：浮层出现、面板搬进浮层；放大到 150% 后画布真的更宽；关闭后回到页面
            "popout_open": bool(pp_open.get("inOverlay")) and float(pp_open.get("width", 0) or 0) > 0,
            "popout_zoom": (
                float(pp_zoom.get("zoom", 0) or 0) == 1.5
                and float(pp_zoom.get("width", 0) or 0) > float(pp_open.get("width", 0) or 0) * 1.35
            ),
            "popout_restore": bool(pp_close.get("backHome")) and pp_close.get("overlay") is False,
            # 全屏/放大后高度自适应：面板高度贴着浮层高度，画布比原来高（不是留一大片空白）
            "popout_fill_height": (
                bool(pp_zoom.get("fitGrow"))
                and float(pp_zoom.get("height", 0) or 0) > float(pp_open.get("height", 0) or 0) * 1.3
                and float(pp_zoom.get("panelH", 0) or 0) >= float(pp_zoom.get("stageH", 0) or 0) * 0.9
            ),
            # 全屏：窗口真进了全屏，且面板把整屏高度吃满（不是缩在中间）
            "popout_fullscreen": bool((report.get("popout_fullscreen") or {}).get("check")),
            # 窗口外弹窗：新的独立窗口里只有那个面板，且画布真的按窗口尺寸渲染
            "popout_window": (
                bool(pp_win.get("solo"))
                and bool(pp_win.get("panel"))
                and int((pp_win.get("shot") or {}).get("saved") is True)
                and any(
                    float(c.get("h", 0) or 0) > 200 and float(c.get("w", 0) or 0) > 300
                    for c in (pp_win.get("canvases") or [])
                )
            ),
            # 增益控件：手动→升档→自动 一路走通，且顶栏读数是 Python 回推的
            "gain_manual": str((report.get("gain") or {}).get("index0", "")) != "",
            "gain_step": bool(report.get("gain"))
            and int((report.get("gain") or {}).get("stepped", {}).get("index", -1))
            == int((report.get("gain") or {}).get("index0", -9)) + 1,
            "gain_auto": str(((report.get("gain") or {}).get("auto") or {}).get("mode", "")) == "AUTO",
            "gain_on_button": list((report.get("gain") or {}).get("on") or []) == ["AUTO"],
            # 读数由 JS 按当前语言渲染；自检跑到这一步可能已经切过英文，两种措辞都算过
            "gain_readback": ("底噪" in str((report.get("gain") or {}).get("read", "")))
            or ("floor" in str((report.get("gain") or {}).get("read", ""))),
            # 瀑布图页：页面自报的画布/卡片/声明都到位，且热力图像素真画上了
            "waterfall_rows": int(wf_page_probe.get("rows", 0) or 0) >= 1,
            "waterfall_painted": float(wf_page_probe.get("painted", 0) or 0) > 1.0,
            "waterfall_cards": int(wf_page.get("cards", 0) or 0) >= 4,
            "waterfall_banner": int(wf_page.get("banner", 0) or 0) > 20,
            "waterfall_notes": int(wf_page_probe.get("notes", 0) or 0) >= 3,
            # 三个频段都要有数据且都画得出来（每段非零格占比 >= 50%）：
            # 「5G HIGH 一片黑」既可能是没数据，也可能是共用一条拉伸范围被压暗了
            "waterfall_bands": (
                len([b for b in (wf_page_probe.get("bandStats") or [])
                     if float((b or {}).get("ratio", 0) or 0) >= 0.5]) >= 3
                and all((b or {}).get("range") for b in (wf_page_probe.get("bandStats") or []))
            ),
            "waterfall_sweep": str(wf_page.get("sweep", "")).endswith(" s"),
            # 单射频分时：瀑布图必须真拿到射频时间，不能被别的模块饿死。
            # 门槛按「本仓当前模块数的公平份额」算（5 模块 0.17 → 判 ≥0.17），
            # 写死 0.3 会在模块数变化后假红（历史：3 模块时公平份额 0.333，
            # 2026-11 抓拍上线后 5 模块各 ~20%）。瀑布图和抓档都要真拿到射频时间。
            # 只剩全频段轮扫一个模块时，公平份额就是 100%
            "waterfall_share": float(share.get("waterfall", 0) or 0) >= 0.85,
            # 窗口缩放：4 档尺寸（1280/1024/900/760 宽）下顶栏按钮都完整落在可视区。
            # 见 layout_test：只要有一档有元素越界，bad 里就有它（带越界像素数）。
            "layout_shrink": len(report.get("layout") or {}) >= 4
            and not (report.get("layout_bad") or {})
            and all(bool((v or {}).get("ok")) for v in (report.get("layout") or {}).values()),
            # 弹窗切页规则：实时流暂停（带提示）→ 切回恢复；汇总型直接关。
            # 只认 Popout.state() 的字段，不看「看起来像不像」。
            "popout_pause": bool((pp.get("opened") or {}).get("open"))
            and bool((pp.get("paused") or {}).get("paused"))
            and bool((pp.get("paused") or {}).get("banner"))
            and str((pp.get("paused") or {}).get("owner")) == "spectrum"
            and str((pp.get("paused") or {}).get("page")) == "waterfall"
            and bool((pp.get("paused") or {}).get("connected")),
            "popout_resume": bool((pp.get("resumed") or {}).get("open"))
            and not bool((pp.get("resumed") or {}).get("paused"))
            and not bool((pp.get("resumed") or {}).get("banner"))
            and int((pp.get("resumed") or {}).get("rows", 0) or 0) > 0,
            # 汇总型面板：先确认它**真的弹出来了**（否则「没弹」也能骗过这一条），再确认切页后关了
            "popout_static_closes": bool((pp.get("static_open_raw") or {}).get("open"))
            and not bool((pp.get("static_after_switch") or {}).get("open")),
            # 全功能并行：页面切换**不再**裁剪模块 —— 每条留档的 modules 都是全部模块，
            # 而且确实跨了两个不同页面（否则只是没切过页，证明不了并行）。
            "view_gating": len({tuple(v.get("pages") or []) for v in (report.get("table_views") or [])}) >= 2
            and all(
                len(list(v.get("modules") or [])) == int(v.get("total", -1))
                for v in (report.get("table_views") or [])
            ),
            # 频谱帧同样不再按页门控：停在 BLE 页时仍在推（浮层/频谱页共用一份数据）
            "spectrum_gating": int(pp.get("spectrum_after", 0) or 0)
            > int(pp.get("spectrum_on_ble", 0) or 0),
            "waterfall_render": bool((report.get("waterfall_render") or {}).get("open"))
            and len(str((report.get("waterfall_render") or {}).get("files", ""))) > 10,
            # ENV 页：本机 WiFi 扫描真的落到表格里了（行数 > 0），四张卡片都填了数字。
            # 这一页的数据不在板子上，只能靠上位机；空表 = 扫描/桥/渲染任一环断了。
            "env_scan_rows": int(env_probe.get("rows", 0) or 0) > 0
            and all(str(env_probe.get(k, "—")).strip() not in ("", "—") for k in ("ssid", "bss", "known")),
            # 工单周期真的回填进输入框（这处曾因「只在无焦点时写」的判据踩过坑）
            "env_interval": str(env_probe.get("interval", "")) not in ("", "undefined"),
            # 状态行必须不是 HTML 初始文案（env.act.idle = 还没扫描）。
            # 实测踩过：表格数据进了 state 缓存能重铺，状态行文案没缓存，
            # 切页重新注入 DOM 后被初始文案盖回去——于是「55 行数据 + 还没扫描」。
            "env_status_written": bool(str(env_probe.get("status", "")).strip())
            and str(env_probe.get("status", "")).strip().lower()
            not in ("还没扫描", "not scanned yet", "—"),
            # 英文界面零中文（只放行「中文(CN)」切换按钮）+ en 词典不缺 key
            "en_no_cjk": bool((report.get("en_cjk") or {}).get("pages"))
            and int((report.get("en_cjk") or {}).get("hitCount", 1) or 0) == 0,
            "en_keys_complete": bool((report.get("en_cjk") or {}).get("pages"))
            and int((report.get("en_cjk") or {}).get("missingCount", 1) or 0) == 0,
            # 页脚版本戳：证明「页面拿到的是当前这一版代码」。戳空 = 跑的是旧进程/旧文件，
            # 那后面所有检查的对象都不对——所以这条必须排在报告最前面看。
            "code_stamp": bool(str((report["pages"].get("spectrum") or {}).get("codeStamp", "")).strip()),
        }
        # 设备自扫引擎（方案 A）单独验一遍：跑假固件 1.2 s，必须有帧。
        # 放在这里是因为它不依赖窗口，也不需要用户点任何东西。
        report["device"] = device_engine_probe()
        checks["device_engine"] = bool((report.get("device") or {}).get("ok"))
        report["checks"] = checks
        ok = all(checks.values())
        report["ok"] = ok
        steps.append("OK" if ok else "FAIL " + ",".join(k for k, v in checks.items() if not v))
        print("[selftest] " + json.dumps(report, ensure_ascii=False), flush=True)
        app.quit()

    def stage(page_id: str) -> None:
        """切到某个页面 → 等它画好 → 截图 + DOM 探针。"""
        print(f"[selftest] 页 {page_id}", flush=True)  # 崩了也能知道走到哪一步
        view.page().runJavaScript(f"Router.navigate('{page_id}')")

        def probe() -> None:
            code = PAGE_JS.get(page_id)
            if page_id == "spectrum":
                code = (
                    "JSON.stringify(Object.assign(window.probeReport ? window.probeReport() : {missing:true},"
                    " {scanbarSelfTest: (window.ScanBar ? ScanBar.selftest() : null)}))"
                )
            view.page().runJavaScript(code, 0, lambda result: collect(page_id, result))

        def collect(page_id: str, result: object) -> None:
            try:
                payload = json.loads(str(result)) if result else {}
            except Exception:  # noqa: BLE001
                payload = {"parse_error": str(result)}
            if isinstance(payload, dict):
                payload["shot"] = shot(page_id)
            report["pages"][page_id] = payload
            nxt = ORDER[ORDER.index(page_id) + 1 :]
            if page_id == "spectrum":
                # 频谱页验完顺手验「双击弹出预览」，再验中英切换，然后保持英文继续验后面的页
                QTimer.singleShot(300, lambda: popout_test(lambda: popout_window_test(lambda: popout_fullscreen_test(lambda: switch_lang(nxt[0])))))

            elif page_id == "waterfall":
                # 瀑布页两种面板（实时流画布 + 出图按钮/判读说明）：先点一次
                # 「生成 PNG + HTML」验 JS→Bridge→produce(force) 整条链路，再验切页规则
                waterfall_render_test(lambda: popout_pause_test(lambda: stage(nxt[0])))
            elif page_id == "env":
                # ENV 页探完顺手动一遍顶栏增益控件：JS → Bridge → HostSession → 状态回推。
                # 它现在是**最后一页**，验完直接量窗口缩放（以前最后一页是 raw，走下面 else）。
                QTimer.singleShot(
                    400,
                    lambda: gain_test(
                        lambda: stage(nxt[0]) if nxt else layout_test(finish)
                    ),
                )
            elif nxt:
                QTimer.singleShot(400, lambda: stage(nxt[0]))
            else:
                # 页面全探完，最后量一遍「缩放」：见 layout_test 注释
                layout_test(finish)

        QTimer.singleShot(1800, probe)

    def layout_test(then) -> None:
        """窗口缩放实测：把窗口依次拖小，量顶栏每个按钮是否还完整落在可视区。

        为什么要在自检里做：外壳是 `overflow:hidden` 的定宽网格，窗口一变窄，
        右边的「开始/停止/EN」会被挤到屏幕外且**不报错、不滚动条**——只能靠量。
        判据看的是 `getBoundingClientRect()` 四边是否越界，不是「看不见就算了」。
        每一档都顺手看一眼 `.page` 是否被撑宽（页面里宽表格应内部滚，不许撑外壳）。
        """
        sizes = [(1280, 800), (1024, 680), (900, 620), (760, 560)]
        results: dict[str, object] = {}
        restore = (view.width(), view.height())

        def step(i: int) -> None:
            if i >= len(sizes):
                report["layout"] = results
                view.resize(restore[0], restore[1])
                bad = {
                    k: (v or {}).get("bad")
                    for k, v in results.items()
                    if isinstance(v, dict) and (v.get("bad") or [])
                }
                steps.append(
                    "窗口缩放：" + json.dumps(
                        {k: (v or {}).get("ok") for k, v in results.items()}, ensure_ascii=False
                    )
                )
                report["layout_bad"] = bad
                then()
                return
            w, h = sizes[i]

            def collect(result: object, tag: str = f"{w}x{h}") -> None:
                try:
                    payload = json.loads(str(result)) if result else {}
                except Exception:  # noqa: BLE001
                    payload = {"parse_error": str(result)}
                if isinstance(payload, dict):
                    payload["window"] = [view.width(), view.height()]
                results[tag] = payload
                QTimer.singleShot(220, lambda: step(i + 1))

            def grab() -> None:
                view.page().runJavaScript(
                    "JSON.stringify(window.Layout ? Layout.probe() : {missing:1})", 0, collect
                )

            view.resize(w, h)
            # 等一次窗口 resize 事件把画布/进度条重排完，再量，否则量到的是旧布局
            QTimer.singleShot(420, grab)

        step(0)

    def gain_test(then) -> None:
        """顶栏增益控件实操：切手动 → 升一档 → 切自动，读回状态与显示。

        验的是「按钮真能改到射频参数」这条链，不是好看：
        `#gain-read` 由 Python 的 `stats.gain` 驱动，它有数就说明
        Slot → HostSession.gain_control → 回推 → 顶栏渲染 全都通了。
        """
        view.page().runJavaScript(
            "(async function () {"
            " window.__gain = {};"
            " var call = async function (o) { return Bridge.call('gainControl', JSON.stringify(o)); };"
            " try {"
            "   var before = Gain.probe() || {};"
            "   window.__gain.manual = await call({action: 'mode', mode: 'MANUAL'});"
            "   window.__gain.index0 = window.__gain.manual.index;"
            "   window.__gain.stepped = await call({action: 'step', delta: 1});"
            "   window.__gain.auto = await call({action: 'mode', mode: 'AUTO'});"
            "   window.__gain.target = await call({action: 'target', target: 5});"
            "   window.__gain.before = {mode: before.mode, index: before.index};"
            " } catch (e) { window.__gain.err = String(e); }"
            "})()"
        )

        def read_back() -> None:
            view.page().runJavaScript(
                "JSON.stringify(Object.assign({}, window.__gain || {}, {"
                "  read: ((document.querySelector('#gain-read')||{}).textContent||''),"
                "  note: ((document.querySelector('#gain-note')||{}).textContent||''),"
                "  on: Array.from(document.querySelectorAll('[data-gain-mode]'))"
                "        .filter(function (b) { return b.classList.contains('seg__btn--on'); })"
                "        .map(function (b) { return b.dataset.gainMode; }),"
                "  live: Gain.probe()"
                "}))",
                0,
                collect_gain,
            )

        def collect_gain(result: object) -> None:
            try:
                payload = json.loads(str(result)) if result else {}
            except Exception:  # noqa: BLE001
                payload = {"parse_error": str(result)}
            payload["shot"] = shot("gain")
            report["gain"] = payload
            steps.append(
                "增益控件：" + json.dumps(
                    {k: payload.get(k) for k in ("index0", "read", "on")}, ensure_ascii=False
                )
            )
            then()

        QTimer.singleShot(1000, read_back)

    def waterfall_render_test(then) -> None:
        """点瀑布图页的「生成 PNG + HTML」，等生成完后读回状态行。

        为什么放 4.5 s 后读：渲染是同步跑的（QThread 里没占，直接在主线程），
        一次 buffered 全量渲染约 0.1～1 s；固定等 4.5 s 是为了避开慢机器抖动，
        免得把「还没回包」误判为失败。
        """

        def read_back() -> None:
            view.page().runJavaScript(
                "JSON.stringify({files: ((document.querySelector('#wf-files')||{}).textContent||''),"
                " open: !((document.querySelector('#wf-open')||{}).disabled)})",
                0,
                collect_render,
            )

        def collect_render(result: object) -> None:
            try:
                payload = json.loads(str(result)) if result else {}
            except Exception:  # noqa: BLE001
                payload = {"parse_error": str(result)}
            report["waterfall_render"] = payload
            steps.append(f"瀑布图出图：{json.dumps(payload, ensure_ascii=False)}")
            then()

        view.page().runJavaScript(
            "(function(){const b=document.querySelector('#wf-render'); if(b){b.click(); return true;} return false;})()"
        )
        QTimer.singleShot(4500, read_back)

    def popout_test(then) -> None:
        """双击面板 → 浮层 → 放大 150% 重画 → 关闭复位。结果由 Popout.selftest() 写进 window.__popout。"""

        def read_out() -> None:
            view.page().runJavaScript(
                "JSON.stringify(window.__popout || {})", 0, lambda r: collect_popout(r, then)
            )

        view.page().runJavaScript("Popout.selftest()")
        # selftest 内部是 0 → 700ms(放大) → +500ms(关)，共 1.2s；这里留 2.4s 读结果。
        # 原来只留 1600ms，偏偏界面在真板高频刷新时 setTimeout 会被推迟，读到 close=null
        # 就误报「关闭没复位」——这是读数太早，不是功能坏了。
        QTimer.singleShot(2400, read_out)

    def collect_popout(result: object, then) -> None:
        try:
            payload = json.loads(str(result)) if result else {}
        except Exception:  # noqa: BLE001
            payload = {"parse_error": str(result)}
        payload["shot"] = shot("spectrum-popout")
        report["popout"] = payload
        steps.append(f"弹出预览：{json.dumps({k: payload.get(k) for k in ('open', 'zoom', 'close')}, ensure_ascii=False)}")
        QTimer.singleShot(200, then)

    def popout_pause_test(then) -> None:
        """切页时弹窗怎么办：实时流**留着暂停**（带提示），切回**继续刷新**；汇总型直接关。

        为什么只能实测：暂停是「路由只把事件派给当前页」顺带带来的效果，光读代码
        分不清「真的挂了提示条」还是「只是看起来没画」。判据全取 ``Popout.state()``
        的 paused/banner/owner 字段 + 切页前后频谱帧计数。

        排程约定：**每一步都只靠 QTimer 单独排下一次**，绝不在 runJavaScript 的结果
        回调里接着发起下一次 runJavaScript —— 那会在同一个 C++ 事件循环里重入，
        实测直接把线程栈打爆（见本函数上一版）。
        """
        out: dict[str, object] = {}
        report["popout_pause"] = out
        box = window.stats_box

        def js(code: str, cb=None) -> None:
            if cb is None:
                view.page().runJavaScript(code, 0)
            else:
                view.page().runJavaScript(code, 0, cb)

        def store(tag: str):
            """把一次 JS 结果落进 out[tag]（只存，不触发下一步）。"""

            def take(result: object) -> None:
                try:
                    out[tag] = json.loads(str(result)) if result else {}
                except Exception:  # noqa: BLE001
                    out[tag] = {"parse_error": str(result)}

            return take

        def read_state(tag: str) -> None:
            js("JSON.stringify(Popout.state())", store(tag))

        def emits() -> int:
            return int(box.get("spectrum_emits", 0) or 0)

        schedule: list[tuple[int, object]] = [
            # 归一化到频谱页（实时流）
            (0, lambda: js("Router.navigate('spectrum')")),
            # 弹第一个面板（实时流面板）
            (900, lambda: js(
                "(function(){const p=document.querySelector('#page-container .panel');"
                "if(!p||!window.Popout)return 'no-panel';Popout.open(p);return 'ok';})()",
                store("open_raw"))),
            (350, lambda: read_state("opened")),
            # 切到热力图页：面板该留着、进入暂停
            (250, lambda: (out.__setitem__("spectrum_before", emits()),
                           js("Router.navigate('waterfall')"))),
            (900, lambda: read_state("paused")),
            (250, lambda: js(
                "JSON.stringify({len:(((document.querySelector('.popout__paused-text')||{})"
                ".textContent)||'').length})", store("paused_text"))),
            # 停在热力图页：频谱帧不该再推（门控真的生效）
            (250, lambda: out.__setitem__("spectrum_on_ble", emits())),
            (800, lambda: out.__setitem__("spectrum_after", emits())),
            # 切回频谱页：暂停解除、刷新继续
            (150, lambda: js("Router.navigate('spectrum')")),
            (1000, lambda: js(
                "JSON.stringify(Object.assign(Popout.state(),"
                "{rows: document.querySelectorAll('#spec-canvas').length}))", store("resumed"))),
            # 汇总型面板（热力图页的「出图」面板）弹出来再切页：应当直接关掉。
            # 用 data-panel-static 选中，不按下标——实时流面板弹出去以后仍挂在
            # #page-container 里（浮层就挂在那儿），下标会整体错位（踩过）。
            # 先切到热力图页：static 面板只在那儿，站在频谱页会选不到（no-static）。
            (350, lambda: js("Router.navigate('waterfall')")),
            (700, lambda: js(
                "(function(){const p=document.querySelector('#page-container .panel[data-panel-static=\"1\"]');"
                "if(!p)return 'no-static';"
                "const k=p.dataset.panelKey;Popout.open(p);"
                "return JSON.stringify({key:k,open:Popout.state().open,static:Popout.state().static});})()",
                store("static_open_raw"))),
            (350, lambda: js("Router.navigate('spectrum')")),
            (900, lambda: read_state("static_after_switch")),
            # 收尾：回到频谱页再交棒，别让后面的步骤从热力图页开始
            (200, lambda: js("Router.navigate('spectrum')")),
            (600, then),
        ]

        def step(i: int) -> None:
            if i >= len(schedule):
                return
            delay, fn = schedule[i]

            def run() -> None:
                fn()
                step(i + 1)  # 只是再排一次 QTimer，不是递归执行

            QTimer.singleShot(delay, run)

        step(0)

    def popout_window_test(then) -> None:
        """点「独立窗口」按钮 → Python 侧 createWindow → 新窗口真的渲染出画布。"""
        view.page().runJavaScript("Popout.openDocked()")

        def read_out() -> None:
            live = window.live_popouts()
            if not live:
                report["popout_window"] = {"ok": False, "windows": len(window.popouts)}
                steps.append("独立窗口：没开出来")
                QTimer.singleShot(200, then)
                return
            child = live[-1]

            def collect_child(result: object) -> None:
                try:
                    payload = json.loads(str(result)) if result else {}
                except Exception:  # noqa: BLE001
                    payload = {"parse_error": str(result)}
                image = child.grab()
                path = (args.shot or "selftest.png")
                root, ext = os.path.splitext(path)
                shot_path = f"{root}-window{ext or '.png'}"
                payload["shot"] = {"path": shot_path, "saved": bool(image.save(shot_path))}
                payload["windows"] = len(live)
                payload["title"] = child.windowTitle()
                report["popout_window"] = payload
                steps.append(f"独立窗口：{json.dumps(payload, ensure_ascii=False)}")
                QTimer.singleShot(200, then)

            child.page().runJavaScript(
                "JSON.stringify({"
                " solo: document.body.classList.contains('solo'),"
                " panel: !!Popout.current,"
                " key: Popout.current ? Popout.current.dataset.panelKey : '',"
                " canvases: Array.from(document.querySelectorAll('canvas')).map(c => ({id: c.id || '(3d)', w: c.width, h: c.height})),"
                " frames: (window.probeReport ? probeReport().framesSeen : -1)"
                "})",
                0,
                collect_child,
            )

        QTimer.singleShot(2200, read_out)

    def popout_fullscreen_test(then) -> None:
        """点「全屏」→ 真的进全屏，且面板把整屏高度用满（不是照原样缩在中间）。"""
        view.page().runJavaScript(
            "(function () {"
            " var g = document.querySelector('#page-container [data-fit-grow=\"1\"]');"
            " var p = g && g.closest('.panel');"
            " if (!p) return 'no panel';"
            " Popout.open(p);"
            " return 'ok';"
            "})()"
        )

        def click_full() -> None:
            view.page().runJavaScript("document.querySelector('.popout [data-act=\"full\"]').click()")

        def read_out() -> None:
            win = window.view.window()

            def collect(result: object) -> None:
                try:
                    payload = json.loads(str(result)) if result else {}
                except Exception:  # noqa: BLE001
                    payload = {"parse_error": str(result)}
                payload["full"] = bool(win.isFullScreen())
                payload["screenH"] = int(window.view.screen().geometry().height())
                payload["check"] = bool(win.isFullScreen()) and (
                    float(payload.get("panelH", 0) or 0) >= float(payload.get("stageH", 0) or 0) * 0.85
                ) and float(payload.get("stageH", 0) or 0) >= payload["screenH"] * 0.8
                report["popout_fullscreen"] = payload
                steps.append(f"全屏：{json.dumps(payload, ensure_ascii=False)}")
                # 收尾：再点一次「退出全屏」+ 关浮层，后面还要截图
                view.page().runJavaScript("document.querySelector('.popout [data-act=\"full\"]').click()")
                QTimer.singleShot(900, leave)

            def leave() -> None:
                view.page().runJavaScript("if (window.Popout) Popout.close();")
                QTimer.singleShot(600, then)

            view.page().runJavaScript(
                "(function () {"
                " var st = document.querySelector('.popout__stage');"
                " var pn = document.querySelector('.panel--popped');"
                " var css = pn ? parseFloat(getComputedStyle(pn).getPropertyValue('--fit-grow')) || 0 : 0;"
                " return JSON.stringify({"
                "   stageH: st ? Math.round(st.getBoundingClientRect().height) : 0,"
                "   panelH: pn ? Math.round(pn.getBoundingClientRect().height) : 0,"
                "   fitGrow: Math.round(css),"
                "   innerH: window.innerHeight,"
                "   label: (document.querySelector('.popout [data-act=\"full\"]') || {}).textContent || '',"
                "   bridge: !!(window.Bridge && Bridge.ready())"
                " });"
                "})()",
                0,
                collect,
            )

        QTimer.singleShot(400, click_full)
        QTimer.singleShot(1800, read_out)

    def en_cjk_test(then) -> None:
        """英文界面「零中文」实测：逐页扫 DOM，缺 key 也算失败。

        为什么要逐页扫而不是只扫静态文案：模块 payload（判词/表格）是 Python 现算的，
        哪一条漏翻只有真渲出来才知道；`I18N.missingKeys()` 则专门抓「en 词典少 key」。
        """
        pages = ["spectrum", "waterfall", "env"]
        seen: dict[str, object] = {}
        hits: list[str] = []
        missing: set[str] = set()

        def step(i: int) -> None:
            if i >= len(pages):
                report["en_cjk"] = {
                    "pages": len(seen),
                    "hits": hits[:40],
                    "hitCount": len(hits),
                    "missing": sorted(missing)[:40],
                    "missingCount": len(missing),
                    # 排障用：浮层标题是面板标题的副本，容易忘同步 → 留一份现场
                    # （取最后一页的现场；以前取的是已删的「原始输出」页，恒为 None）
                    "popouts": (seen.get(pages[-1]) or {}).get("popouts"),
                }
                steps.append(
                    f"英文零中文：{len(seen)} 页，残留 {len(hits)} 条，缺 key {len(missing)} 个"
                )
                then()
                return
            page_id = pages[i]
            view.page().runJavaScript(f"Router.navigate('{page_id}')")

            def collect(result: object, page_id: str = page_id) -> None:
                try:
                    payload = json.loads(str(result)) if result else {}
                except Exception:  # noqa: BLE001
                    payload = {"hits": [f"parse_error: {result}"], "missing": []}
                seen[page_id] = payload
                for item in payload.get("hits") or []:
                    hits.append(f"{page_id}/{item}")
                for key in payload.get("missing") or []:
                    missing.add(str(key))
                QTimer.singleShot(260, lambda: step(i + 1))

            def grab(page_id: str = page_id) -> None:
                if page_id == "raw":
                    # 「原始输出」页的文本 3s 才刷一次，多等一会儿再扫
                    QTimer.singleShot(2600, lambda: view.page().runJavaScript(CJK_SCAN_JS, 0, collect))
                    return
                view.page().runJavaScript(CJK_SCAN_JS, 0, collect)

            # 页面进场要现场算一次表格（1 Hz 节流），等它画出来再扫
            QTimer.singleShot(1700, grab)

        step(0)

    def switch_lang(next_id: str) -> None:
        view.page().runJavaScript("I18N.setLang('en')")

        def probe_lang() -> None:
            view.page().runJavaScript(
                "JSON.stringify(Object.assign(window.probeReport ? window.probeReport() : {},"
                ' {shell: [T("scanbar.title"), T("scanbar.now", {center: 5180, at: 6, total: 49, pass: 2}),'
                ' T("spectrum.title"), T("level.重度")]}))',
                0,
                collect_lang,
            )

        def collect_lang(result: object) -> None:
            try:
                payload = json.loads(str(result)) if result else {}
            except Exception:  # noqa: BLE001
                payload = {}
            i18n = {k: payload.get(k) for k in ("lang", "statusText", "navLabels", "shell")}
            i18n["wf2d"] = (payload.get("pageProbe") or {}).get("wf2d")
            i18n["shot"] = shot("en-spectrum")
            report["i18n"] = i18n
            steps.append(f"语言切换：{i18n.get('lang')} / {i18n.get('navLabels')}")
            # 切完英文顺手把「零中文」验一遍（逐页扫 DOM + 查 en 词典缺 key）
            QTimer.singleShot(300, lambda: en_cjk_test(lambda: stage(next_id)))

        QTimer.singleShot(700, probe_lang)

    ORDER = ["spectrum", "waterfall", "env"]

    def start_after_load() -> None:
        if not window.page_loaded:
            if time.time() - started["load"] > 20:
                return fail("页面 20s 未加载完成")
            QTimer.singleShot(200, start_after_load)
            return
        if not started.get("lang_reset"):
            started["lang_reset"] = True
            view.page().runJavaScript(LANG_RESET_JS, 0, lambda _r: start_after_load())
            return
        steps.append("页面已加载")
        result = window.start_run(
            to_json(
                {
                    "port": args.port,
                    # 自检默认用假固件（无板也能验）；加 --real 就对着真板跑
                    "mock": (bool(args.mock) or not args.real) if args.selftest else bool(args.mock),
                    "scenario": args.scenario,
                    "gain_index": args.gain_index,
                    # 自检也走同一套模式选择：AUTO 会被自检顺手验一遍
                    "gain_mode": args.gain_mode,
                    "gain_target": args.gain_target,
                    "bins_out": 256,
                    "lang": args.lang,
                    "engine": args.engine,
                }
            )
        )
        if not result.get("ok"):
            return fail(f"启动失败：{result.get('message')}")
        steps.append(f"启动成功：{result.get('message')}")
        QTimer.singleShot(int(args.seconds * 1000), lambda: stage("spectrum"))

    started["load"] = time.time()
    QTimer.singleShot(200, start_after_load)
    QTimer.singleShot(int(args.seconds * 1000) + 80000, lambda: fail("自检超时"))
    app.exec()
    print("[selftest] steps: " + " | ".join(steps), flush=True)
    window.shutdown()
    return 0 if report.get("ok") else 2


def main(argv: list[str] | None = None) -> int:
    args_holder: dict[str, object] = {}

    app, view, window, args = build_app(list(argv if argv is not None else sys.argv[1:]))
    args_holder["args"] = args

    if args.selftest:
        return run_selftest(app, view, window, args)

    if args.soak:
        def soak_start() -> None:
            if not window.page_loaded:
                QTimer.singleShot(300, soak_start)
                return
            result = window.start_run(
                to_json(
                    {
                        "port": args.port,
                        "mock": bool(args.mock),
                        "scenario": args.scenario,
                        "gain_index": args.gain_index,
                        "gain_mode": args.gain_mode,
                        "gain_target": args.gain_target,
                        "bins_out": 256,
                        "lang": args.lang,
                        "engine": args.engine,
                    }
                )
            )
            print(f"[soak] start: {result.get('message')}", flush=True)
            if args.soak_page:
                view.page().runJavaScript(f"Router.navigate('{args.soak_page}')", 0)
            QTimer.singleShot(2500, soak_tick)

        soak_began = {"t": time.time()}

        def soak_tick() -> None:
            elapsed = round(time.time() - soak_began["t"], 1)
            view.page().runJavaScript(
                "JSON.stringify({"
                "page:(window.Router&&Router.current)?Router.current.id:null,"
                "spec:(window.Pages&&Pages.spectrum&&Pages.spectrum.probe)?Pages.spectrum.probe():null,"
                "wf:(window.Pages&&Pages.waterfall&&Pages.waterfall.probe)?Pages.waterfall.probe():null"
                "})",
                0,
                lambda res: print(f"[soak] t={elapsed}s {res}", flush=True),
            )
            if elapsed < args.soak:
                QTimer.singleShot(2000, soak_tick)
            else:
                print(f"[soak] done after {elapsed}s", flush=True)
                app.quit()

        QTimer.singleShot(300, soak_start)
        app.aboutToQuit.connect(window.shutdown)
        app.exec()
        return 0

    if args.autostart or args.mock:
        def autostart() -> None:
            if not window.page_loaded:
                QTimer.singleShot(300, autostart)
                return
            window.start_run(
                to_json(
                    {
                        "port": args.port,
                        "mock": bool(args.mock),
                        "scenario": args.scenario,
                        "gain_index": args.gain_index,
                        "gain_mode": args.gain_mode,
                        "gain_target": args.gain_target,
                        "bins_out": 256,
                        "lang": args.lang,
                    }
                )
            )

        QTimer.singleShot(300, autostart)

    app.aboutToQuit.connect(window.shutdown)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())