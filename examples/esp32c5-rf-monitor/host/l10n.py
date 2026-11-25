"""下行本地化：把 Python 侧（模块判词 / 表格 / 状态）的中文在上行到界面前翻成英文。

为什么不在前端字典里翻（``host/web/js/i18n.js`` 已经有完整 UI 词典）：

* 模块（``modes/``）产出的判词、表格列名、建议文本**运行时才拼出来**，前端只能收到
  成品字符串；前端拿不到「这是哪条模板」的语义，硬翻就得在前端再写一遍正则。
* 模块的 ASCII/终端输出必须保持中文（``app.live`` / ``docs/`` 里的实测记录），所以在
  ``modes/`` 里改成 key-based i18n 会把命令行输出也带偏；本地化只发生在「发给界面」
  这一层，不影响任何模块自身产出。

规则：

* 只翻 ``str`` **值**，不碰 key、不碰数字/布尔/None（表格数据原样）。
* 数字先抽出来（``{}`` 占位），翻完再按顺序填回 —— 判词模板里全是实测数字，
  逐条枚举会爆炸。
* 查不到就原样返回（**绝不抛**，界面不能因为一条文案挂掉）；覆盖率由
  ``tests/test_l10n.py`` 盯着：英文模式下整份下行 payload 必须零 CJK。
"""
from __future__ import annotations

import re
from typing import Any

#: CJK 统一表意文字（够用：项目里不会出现假名/韩文）
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

#: 非 ASCII 段（异常文案兜底时整段剔掉，剩下的数字/设备回显才是有效信息）
def _ascii_only(text: str) -> str:
    """剔掉非 ASCII 字符（异常文案兜底用：剩下的数字/设备回显才算信息）。"""
    return "".join(ch if ch.isascii() else " " for ch in text)

#: 数字段（**带符号**）：``-2.0`` 整段算一个 ``{}``，回填时符号跟着回来。
#: 少了这个 ``-?``，像「开关幅度 -2.0 码」这种负数的骨架就与模板对不上 → 英文界面漏中文。
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def has_cjk(text: str) -> bool:
    """是不是含中文（表格里 ``ch1`` / ``2.6/帧`` 只看后半段）。"""
    return bool(CJK_RE.search(text))


def skeleton(text: str) -> str:
    """把数字段换成 ``{}``，得到查表用的骨架。"""
    return NUM_RE.sub("{}", text)


#: 骨架 → 英文模板（``{}`` 数量必须与中文骨架一致）
TABLE: dict[str, str] = {
    # ---- 边界 / 口径文案 -------------------------------------------------------
    # 原 modes/ble_risk.py 的判据判词已随模块删除；下面留下的是**活模块仍在产出**的
    # 共用串（扫描范围、相对 dB 码边界、风险分档名等，见 modes/waterfall.py）。


    "前端底噪参考码 {}（≥{}）⇒ 增益偏高，宽带占用率 {}% 会随增益变化（实板同环境实测：增益 {}/{}/{} → "
    "宽带占用 {}%/{}%/{}%，判词从「复杂噪音」一路滑到「WiFi 压制型」）。拿宽带占用下结论前，先降两档增益复测："
    "占用率不随增益走才是环境，随增益走就是前端动态范围。":
        "Front-end noise-floor reference {} (≥{}) ⇒ the gain is high and the {}% wideband occupancy moves with "
        "gain (measured on a real board in one room: gain {}/{}/{} → occupancy {}%/{}%/{}%, and the verdict "
        "slides from \"complex noise\" all the way to \"WiFi dominated\"). Before drawing any conclusion from "
        "wideband occupancy, drop two gain steps and re-measure: occupancy that does not follow gain is the "
        "environment; occupancy that follows gain is front-end dynamic range.",









    "扫描：{} 个广播信道各对准一窗 + 数据区 {} 窗重叠滑扫（{}MHz 窗 / {}MHz 步进 / {}ms 驻留）；"
    "样本 {} 帧、丢弃整窗 {} 码 {} 帧。":
        "Scan: {} advertising channels each get one aligned window plus a {} window overlapping sweep of the "
        "data region ({}MHz window / {}MHz step / {}ms dwell); {} frames sampled, {} frames whose whole "
        "window was {} codes dropped.",











    "扫描：{}MHz 窗 / {}MHz 步进重叠滑扫，整窗 {} 码的帧丢弃（{} 帧）；信道覆盖 <{}% 记「无覆盖」。":
        "Scan: {}MHz window / {}MHz step overlapping sweep; frames whose whole window is {} codes are dropped "
        "({} frames); channel coverage <{}% is recorded as \"no coverage\".",

    # 单独出现的风险类型名（抓档表单元格、卡片大标题、前端 U.riskClass 的键）
    "轻微干扰": "slight interference",



    # RISK_FLAT 专有 hints：宽带占用高、但时间上几乎不开关 → 只下「形状疑似」，不下 WiFi 结论




    "周期快照，非连续实时 —— 单射频分时扫描，帧与帧之间存在接收空档；图上的行是「扫到过」的功率码，"
    "不是连续采样。":
        "Periodic snapshots, not continuous real time — the one radio scans in time slices, so there are "
        "reception gaps between frames; the rows on the chart are power codes that were \"scanned at some "
        "point\", not continuous samples.",





    "功率码是相对 dB 码（{}..{}），不是 dBm；「抬升 {} 码」不等于绝对功率 {} dB。":
        "Power codes are relative dB codes ({}..{}), not dBm; \"a rise of {} codes\" is not an absolute "
        "power of {} dB.",



    "带宽仅 {} kHz 的窄带，占空 {}% → 不像已知的 WiFi/BLE/微波炉/图传":
        "Only {} kHz wide, duty {}% → does not match known WiFi/BLE/microwave/video-link signatures",



    "缓冲 {} 帧 / 累计 {} 帧，{} 个窗 × {} ms ≈ {} s 一轮":
        "Buffer {} frames / {} in total, {} windows × {} ms ≈ {} s per round",







    "功率码是相对 dB 码（{}..{}），不是 dBm；色标只表示相对强弱。":
        "Power codes are relative dB codes ({}..{}), not dBm; the colour scale only shows relative strength.",


    "带宽 {} MHz、占空 {}% → 不在任何已知类型的特征区间":
        "{} MHz wide, duty {}% → outside the feature range of any known type",
    "带宽 {} MHz、占空 {}% → 间歇出现，不像已知类型":
        "{} MHz wide, duty {}% → intermittent, unlike any known type",




    "可疑信号抓拍（{} 次）": "Suspicious-signal capture ({} times)",



    "数据区 {}–{}": "Data region {}–{}",

    # ---- 表格单元格通用枚举 -----------------------------------------------------
    # 原 app/audit.py 的体检文案已删：它只做命令行输出，不经过 tr() 这道边界。
    "该窗无覆盖": "no coverage in this window",
    "覆盖 {}%": "Coverage {}%",
    "{}G 守卫": "{}G guard",
    "可疑抓拍": "Suspicious captures",
    "WiFi 信道": "WiFi channels",
    "关注": "Watch",
    "可疑": "Suspicious",
    "需复核": "Needs review",
    "低": "Low",
    "未知": "Unknown",

    # ---- 表格列名 / 单元格枚举 ------------------------------------------------
    "{}/帧": "{}/frame",
    "BLE 广播信道风险评估": "BLE advertising-channel risk assessment",
    "5.8G 频段守卫": "5.8G band guard",
    "{}G 频段守卫": "{}G band guard",
    "周期性频谱瀑布图（时频热力图）": "Periodic spectrum waterfall (time-frequency heatmap)",
    "周期性频谱瀑布图": "Periodic spectrum waterfall",
    "可疑信号抓拍": "Suspicious-signal capture",
    "蓝牙设备密度型": "Bluetooth density",
    "无明显风险": "no obvious risk",
    "混合型": "mixed",
    "时间均值频谱": "Time-averaged spectrum",
    "窄带栅格占用": "Narrowband grid occupancy",
    "频点 MHz": "Frequency MHz",
    "带宽 MHz": "Bandwidth MHz",
    "活动占空": "Activity duty",
    "窄带突出": "Narrowband prominence",
    "峰值抬升": "Peak rise",
    "持续 s": "Duration s",
    "首次出现": "First seen",
    "时长 s": "Length s",
    "持续窄带": "Persistent narrow",
    "持续宽带": "Persistent wide",
    "窄带持续": "Narrowband persistent",
    "持续凸起": "Persistent bump",
    "分不清的宽带": "Ambiguous wideband",
    "间歇出现": "Intermittent",
    "持续单频点": "Persistent single tone",
    "间歇单频点": "Intermittent single tone",
    "规律跳频": "Regular frequency hopping",
    "占空条": "Duty bar",
    "占用条": "Occupancy bar",
    "比底噪": "vs floor",
    "比最静": "vs quietest",
    "峰值码": "Peak code",
    "占空比": "Duty cycle",
    "色标码": "Colour code",
    "采样率": "Sample rate",
    "通道": "Channel",
    "信道": "Channel",
    "中心": "Centre",
    "覆盖": "Coverage",
    "判定": "Verdict",
    "空闲": "Idle",
    "无覆盖": "No coverage",
    "有覆盖": "Covered",
    "未采样": "Not sampled",
    "编号": "ID",
    "状态": "State",
    "类型": "Type",
    "帧数": "Frames",
    "排名": "Rank",
    "频段": "Band",
    "占空": "Duty",
    "分档": "Bucket",
    "重度": "Heavy",
    "拥挤": "Crowded",
    "轻度": "Mild",
    "在发": "Active",
    "频点": "Frequency",
    "样本": "Samples",
    "位宽": "Bit depth",
    "分类": "Class",
    "置信": "Confidence",
    "时间": "Time",
    "文件": "File",
    "页": "Page",

    # ---- host/gain.py / host/app.py：状态行 ----------------------------------
    "未知增益模式：{}": "Unknown gain mode: {}",
    "未知操作：{}": "Unknown action: {}",
    "（空）": "(empty)",
    "固件硬件 AGC：由固件自己挑档（测「有没有信号」用它）":
        "Firmware hardware AGC: the firmware picks the step itself (use it to check \"is there a signal\")",
    "自动闭环：目标底噪 {} 码，步进 {}": "Auto loop: target noise floor {} codes, step {}",
    "手动固定档位 {}": "Manual fixed step {}",
    "手动固定档位 {}（已退出手动之外的模式）": "Manual fixed step {} (left the other modes)",
    "已经到档位边界": "Already at the gain limit",
    "设增益失败：{}": "Failed to set gain: {}",
    "自动闭环已到位：底噪 {} 码 ≈ 目标 {}": "Auto loop settled: floor {} codes ≈ target {}",
    "已到档位边界（{}），底噪 {} 码 ≠ 目标 {}": "At the gain limit ({}), floor {} codes ≠ target {}",
    "自动闭环：底噪 {} → 目标 {}，档位调到 {}": "Auto loop: floor {} → target {}, step moved to {}",
    "未开始采集（点了「开始」后这里会显示实时档位与底噪）":
        "Not collecting yet (press Start and the live gain step and noise floor appear here)",
}


#: 「列表长度会变」的句子：骨架匹配天生干不了这类活——占位数量随列表长度变，
#: 而模板是固定的。所以单独用正则：**只换句子外壳，列表原样（数字不参与翻译）**。
#: 踩过：真板上「有明显抬升的信道」是 6 个，词典里只备了 1/4/5/9/10/11/12 个，
#: 差这 2 个就整句漏中文——英文界面里一眼看不出是「少了个长度」。
#: 异常文案里常混着固件回显和数字，骨架对不上，改按「已知中文片段」换；
#: 片段之外剩下的 ASCII 尾巴（数字、``SPEC``/``FSM`` 之类）原样保留。
ERROR_PHRASES: list[tuple[str, str]] = [
    ("端口 ", "port "),
    ("已经被另一个上位机实例占用，pid ", "is already held by another host instance, pid "),
    ("：同一个端口同时只能有一个窗口采集，请关掉那个窗口或换成别的端口再点开始",
     ": only one window can capture a port at a time - close the other one (or pick another port) and press start again"),
    ("连接已标记失效，请重新打开", "the link was marked dead, reopen the port"),
    ("串口读取超时（无数据）", "serial read timeout (no data)"),
    ("无法取得串口租约（SYNC 未回显）", "could not take the serial lease (no SYNC echo)"),
    ("FSM STOP 未收到 FSMEND（在途帧没排空）",
     "FSM STOP got no FSMEND (in-flight frames were not drained)"),
    ("固件未通告频谱能力（CAPS 无 SPECCAPS），无法扫描",
     "the firmware announced no spectrum capability (CAPS has no SPECCAPS), cannot scan"),
    ("缺少 pyserial：pip install pyserial", "pyserial is missing: pip install pyserial"),
    ("连接未打开", "the port is not open"),
    ("连接已标记失效", "the link was marked dead"),
    ("帧同步丢失", "frame sync lost"),
    ("频谱帧解析失败", "spectrum frame could not be parsed"),
    ("SPEC 未产出任何帧", "SPEC produced no frame"),
    ("SPECEND 记录异常", "SPECEND record looks wrong"),
    ("SPEC 启动回复异常", "unexpected SPEC start reply"),
    ("SPEC 剖面与请求不符", "SPEC profile does not match the request"),
    ("SPECINFO? 回复异常", "unexpected SPECINFO? reply"),
    ("FSMPLAN 回读不符", "FSMPLAN read-back mismatch"),
    ("FSMPLANEND 缺失", "FSMPLANEND missing"),
    ("FSM START 回复异常", "unexpected FSM START reply"),
    ("频点超出固件范围", "frequency outside the firmware range"),
    ("固件的便携快照 stride 必须为 1，实际", "portable snapshot stride must be 1, got"),
    ("固件剖面点数异常", "firmware profile bin count looks wrong"),
    ("二进制读取超时", "binary read timeout"),
    ("CAP20 载荷不足", "CAP20 payload too short"),
    ("I/Q 载荷 CRC32 不匹配", "I/Q payload CRC32 mismatch"),
    ("扫频计划不能为空", "the sweep plan cannot be empty"),
    ("（需重新连接）", "(reconnect needed)"),
    ("帧长度不符", "frame length mismatch"),
    ("CRC32 不匹配", "CRC32 mismatch"),
    ("帧太短", "frame too short"),
    ("magic 不匹配", "magic mismatch"),
    ("不支持的 FFT 点数", "unsupported FFT size"),
    ("统计记录 CRC 不匹配", "stat record CRC mismatch"),
    ("统计记录格式错误", "stat record layout is wrong"),
    ("不支持的采样位宽", "unsupported sample width"),
    ("dwell_ms 超出固件范围", "dwell_ms outside the firmware range"),
]


LIST_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?P<band>-?[0-9.]+G) 在用的 WiFi 信道：(?P<list>.+)$"),
     "WiFi channels in use on {band}: {list}"),
    (re.compile(r"^(?P<band>-?[0-9.]+G) 建议避开（占用偏高）：(?P<list>.+)。$"),
     "{band}: avoid these (occupancy too high): {list}."),
    (re.compile(r"^有明显抬升（轻度以上）的信道：(?P<list>.+)。$"),
     "Channels with a clear rise (mild or worse): {list}."),
]


def _translate_list(text: str) -> str | None:
    """列表句：只换外壳，列表内容（``ch1, ch6`` 之类）原样带回。

    用**命名组**而不是反向引用：反向引用要写反斜杠，跨工具传递时极易被吃掉
    （本项目踩过两次），命名组一个转义都不用。
    """
    for pattern, template in LIST_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        fields = {name: ("none" if value == "无" else value) for name, value in match.groupdict().items()}
        return template.format(**fields)
    return None



def translate_error(text: str) -> str:
    """英文模式下的异常文案：**保证不残留中文**（数字与设备回显尽量保住）。

    异常信息走的是另一条路：正文里常混着固件回显（``ERR fsm_args``）和数字，
    骨架匹配基本对不上，所以先用 :data:`ERROR_PHRASES` 按片段换，再把剩下来的
    非 ASCII 字符剔掉。剔掉的部分不是丢了——终端日志里还是原文。
    """
    if not text or not has_cjk(text):
        return text
    full = translate(text)
    if not has_cjk(full):
        return full
    out = text
    for zh, en in ERROR_PHRASES:
        if zh in out:
            out = out.replace(zh, " " + en + " ")
    tail = re.sub(r"\s{2,}", " ", _ascii_only(out)).strip(" :;,、")
    return f"error: {tail}" if tail else "error (details in the terminal log)"


def translate(text: str) -> str:
    """中文 → 英文；翻不了就原样返回（调用方用 :func:`has_cjk` 兜底检查）。"""
    if not text or not has_cjk(text):
        return text
    direct = TABLE.get(text)
    if direct is not None:
        return direct
    key = skeleton(text)
    template = TABLE.get(key)
    if template is None:
        listed = _translate_list(text)
        return text if listed is None else listed
    numbers = NUM_RE.findall(text)
    if template.count("{}") != len(numbers):
        return text  # 模板与骨架占位不一致：宁可原样返回，也不给错位数字
    for value in numbers:
        template = template.replace("{}", value, 1)
    return template


def localize(node: Any) -> Any:
    """递归翻一份下行 payload：只翻 ``str`` **值**，key 与其它类型原样返回。"""
    if isinstance(node, str):
        return translate(node)
    if isinstance(node, dict):
        return {key: localize(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [localize(value) for value in node]
    return node