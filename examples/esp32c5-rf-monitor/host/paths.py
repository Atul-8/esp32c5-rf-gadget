"""定位「Python 分析层」（``core/`` ``modes/`` ``output/`` ``tests/``）的位置。

上位机自己住在项目根 ``<proj>/host``，而被它驱动的分析层仍在 fork 仓库里
``<proj>/repo/examples/esp32c5-rf-monitor``。两者是解耦的：

* 换仓库位置 → 设环境变量 ``ESPSDR_SRC`` 指向含 ``core/registry.py`` 的目录；
* 或者把分析层整个拷到 ``<proj>/host/src``（自包含分发）；
* 都没设时按下面的候选顺序自动找（向上找 4 层）。

找不到就报一条明确的错误，而不是抛一堆 ``ModuleNotFoundError``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent
ENV_VAR = "ESPSDR_SRC"
#: 分析层的判据：有 core/registry.py 且有 modes/ 目录
MARKER = Path("core") / "registry.py"


def _is_source(path: Path) -> bool:
    try:
        return (path / MARKER).is_file() and (path / "modes").is_dir()
    except OSError:  # pragma: no cover - 权限问题当成不是
        return False


def candidates() -> list[Path]:
    """按优先级列出候选目录（纯函数，方便测试）。"""
    found: list[Path] = []
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        found.append(Path(env).expanduser())
    found.append(HOST_DIR / "src")  # 自包含分发：把分析层整包放到 host/src
    anchors = [HOST_DIR, Path.cwd().resolve()]
    for anchor in anchors:
        for parent in [anchor, *anchor.parents][:5]:
            found.append(parent / "repo" / "examples" / "esp32c5-rf-monitor")
            found.append(parent / "examples" / "esp32c5-rf-monitor")
            found.append(parent)
    # 去重但保序
    seen: set[str] = set()
    unique: list[Path] = []
    for path in found:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_source() -> Path:
    """返回分析层目录；找不到抛 ``RuntimeError``（消息里带全部候选）。

    ``ESPSDR_SRC`` 一旦设了就是**权威值**：指向不对立刻报错，不静默回退到自动探测
    （否则用户设错了还以为生效了）。
    """
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        path = Path(env).expanduser()
        if not _is_source(path):
            raise RuntimeError(
                f"{ENV_VAR}={path} 不是分析层目录（需要 core/registry.py + modes/）。"
                "改正或清掉这个环境变量。"
            )
        return path
    tried = candidates()
    for path in tried:
        if _is_source(path):
            return path
    lines = "\n".join(f"  - {p}" for p in tried[:12])
    raise RuntimeError(
        f"找不到 Python 分析层（需要 core/registry.py + modes/）。已尝试：\n{lines}\n"
        f"请设置环境变量 {ENV_VAR}=<含 core/registry.py 的目录>，或把分析层拷到 {HOST_DIR / 'src'}"
    )


def ensure_on_path() -> Path:
    """找到分析层并插到 ``sys.path`` 最前面（幂等）。"""
    path = find_source()
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)
    return path


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    print(ensure_on_path())