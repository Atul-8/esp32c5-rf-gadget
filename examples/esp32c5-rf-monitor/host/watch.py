"""监测调度：什么时候扫一次 WiFi、什么时候把已知热点灌进板子、射频流开多久。

为什么要单独一层
----------------
「扫一次 netsh + 下发十几条 RD KNOWN + 开一会儿射频流」这三件事各自都有代码，
但**谁在什么时候触发它们**是策略，不是驱动。策略要能单测（不碰串口、不碰
Qt 事件循环），所以放在这里做纯函数，``app.py`` 只负责按 ``status()`` 的
``due`` 去执行。

两种模式（用户定义）
--------------------
* ``burst`` 狂暴：高强度检索收发，用来排查周围环境。
  每 ``BURST_INTERVAL_S`` 秒重扫一次 WiFi（路由器 ACS 换信道后立刻修正名单），
  射频流全程常开 —— 屏幕和上位机都在持续出新的频谱/瀑布。
* ``daily`` 日常：放在那里每隔 ``interval_s``（默认 120 秒 = 2 分钟）才检查一次，
  用来发现「有人临时装了个东西」或「有人临时唤醒收发」。
  每轮检查时射频流只开 ``DAILY_STREAM_S`` 秒，剩下时间安静。

边界（必须诚实）
----------------
* 这里调不动射频硬件参数：狂暴模式的「高强度」= **检查频率是日常的 60 倍
  + 射频流常开**，不是把接收机调到更灵敏。真正的灵敏度由增益档决定。
* 日常模式的间隔是「检查周期」而不是「检测延迟」：一次临时收发只持续 3 秒、
  又恰好落在两次检查之间，就会漏掉。要抓这种短事件只能用狂暴模式。
"""

from __future__ import annotations

import time

#: 狂暴模式的重扫周期（秒）。取 15 秒是因为路由器 ACS 换信道通常发生在
#: 关联变化后几秒内，15 秒足够跟上，又不至于把 netsh 扫到卡死。
BURST_INTERVAL_S = 15.0

#: 日常模式默认检查周期（秒）= 2 分钟。
#:
#: 名单是**快照**不是订阅：路由器 ACS 换信道后，上一次灌进固件的频点就过期，
#: 真实 AP 落在新信道上而名单里没有 -> 固件判成「认不出的强信号」-> B 虚高。
#:
#: 以前不敢设短，是因为下发本身要 70.5 秒（15 条 RD KNOWN 条条卡满 5 秒串口超时）；
#: rd_reply 补上行尾后降到 0.014 秒，120 秒周期才不至于大部分时间在堵串口。
#: 取 120 而不是 60：WiFi 扫描本身会短暂占射频，越短越吵；要更实时就在界面改 60。
DEFAULT_DAILY_S = 120.0

#: 每轮额外开射频流的时长（秒）。**保持 0**，理由见 ``stream_seconds()``。
DAILY_STREAM_S = 0.0

#: 日常模式允许的周期范围（秒）。下限 60 秒是给「想盯一小时」的人用的，
#: 再短就不叫日常了；上限 24 小时。
DAILY_MIN_S = 60.0
DAILY_MAX_S = 86400.0

OFF = "off"
DAILY = "daily"
BURST = "burst"
MODES = (OFF, DAILY, BURST)


def clamp_interval(value: object, default: float = DEFAULT_DAILY_S) -> float:
    """把用户填的间隔夹到合法区间。非法值一律回默认值，不抛异常。"""
    try:
        sec = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if sec != sec or sec in (float("inf"), float("-inf")):  # NaN / inf
        return default
    if sec < DAILY_MIN_S:
        return DAILY_MIN_S
    if sec > DAILY_MAX_S:
        return DAILY_MAX_S
    return sec


class Watch:
    """监测策略状态机。``tick()`` 返回本轮该做什么，调用方负责真的去做。

    时钟可注入（``clock``），测试里不需要 sleep。
    """

    def __init__(self, interval_s: float = DEFAULT_DAILY_S, clock=time.monotonic) -> None:
        self._clock = clock
        self.mode = OFF
        self.interval_s = clamp_interval(interval_s)
        self.rounds = 0
        self.last_round_at = 0.0
        self.last_error = ""
        self.last_freqs: list[int] = []
        self._next_due = 0.0

    # ---- 配置 ---------------------------------------------------------
    def set_mode(self, mode: str, interval_s: float | None = None) -> dict:
        """切模式。切过去时 ``_next_due = 0`` → 下一拍立刻做一轮。

        「立刻做一轮」是刻意的：用户要求「开启就自动启动扫描下发」。
        如果切模式后还要干等 15 分钟，界面上看不出任何东西在工作。
        """
        mode = (mode or OFF).strip().lower()
        if mode not in MODES:
            mode = OFF
        if interval_s is not None:
            self.interval_s = clamp_interval(interval_s)
        self.mode = mode
        self._next_due = 0.0
        return self.status()

    def period(self) -> float:
        """当前模式下两次检查之间的间隔；``off`` 返回 0。"""
        if self.mode == BURST:
            return BURST_INTERVAL_S
        if self.mode == DAILY:
            return self.interval_s
        return 0.0

    # ---- 调度 ---------------------------------------------------------
    def due(self, now: float | None = None) -> bool:
        """现在到点了吗。``off`` 永远不到点。"""
        if self.mode == OFF:
            return False
        return (self._clock() if now is None else now) >= self._next_due

    def tick(self, now: float | None = None) -> bool:
        """到点就排下一轮并返回 ``True``；没到点返回 ``False``。

        **先排下一轮再返回**：即使调用方这一轮失败了，也不会因为 ``_next_due``
        没动而每一拍都重试 —— 那会把一次 netsh 失败变成每 0.5 秒撞一次。
        """
        if not self.due(now):
            return False
        t = self._clock() if now is None else now
        self._next_due = t + self.period()
        return True

    def note_round(self, freqs: list[int] | None = None, error: str = "") -> dict:
        """调用方做完一轮后回报结果（成功或失败都要报，界面要显示）。"""
        self.rounds += 1
        self.last_round_at = self._clock()
        if freqs is not None:
            self.last_freqs = list(freqs)
        self.last_error = error
        return self.status()

    def stream_seconds(self) -> float:
        """本轮**额外**要开多久射频流；``0`` = 不要碰它。

        恒为 0 是有意的：射频侧常驻的 ``Runner`` 线程本来就在连续跑扫描，
        「强度监测」这件事不需要这里再开关一次推流。反过来做的话，日常模式
        每轮都发 ``FSM STREAM ON`` 会让固件持续往 USB CDC 灌数据，主机侧
        写命令直接撞 ``Write timeout``（这个问题排查过一整轮）。

        所以两种模式在射频侧是**同一份常驻采集**，差别只在「多久重扫一次
        WiFi、多久重下发一次已知名单」——也就是发现新设备的速度。
        """
        return DAILY_STREAM_S

    def reset(self) -> dict:
        self.mode = OFF
        self._next_due = 0.0
        return self.status()

    # ---- 上报 ---------------------------------------------------------
    def status(self, now: float | None = None) -> dict:
        t = self._clock() if now is None else now
        left = 0.0
        if self.mode != OFF:
            left = max(self._next_due - t, 0.0)
        return {
            "mode": self.mode,
            "period_s": self.period(),
            "interval_s": self.interval_s,
            "next_in_s": round(left, 1),
            "rounds": self.rounds,
            "last_freqs": len(self.last_freqs),
            "last_error": self.last_error,
            "due": self.due(t),
            # 界面直接显示这两条，用户不用猜模式到底干什么
            "detail": self.detail(),
        }

    def detail(self) -> str:
        if self.mode == BURST:
            return f"burst: rescan WiFi every {BURST_INTERVAL_S:.0f}s, RF capture always on"
        if self.mode == DAILY:
            return f"daily: check WiFi every {self.interval_s:.0f}s, RF capture always on"
        return "off: no automatic scan"


__all__ = [
    "Watch",
    "OFF",
    "DAILY",
    "BURST",
    "MODES",
    "BURST_INTERVAL_S",
    "DEFAULT_DAILY_S",
    "DAILY_STREAM_S",
    "DAILY_MIN_S",
    "DAILY_MAX_S",
    "clamp_interval",
]