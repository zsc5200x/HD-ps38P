"""在系统文件管理器中定位本机数据文件。

设置页只允许打开白名单内的配置路径，避免把任意路径暴露成系统调用。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 设置页可定位的路径：kind -> (config 键, 是否目录)。
OPENABLE_PATHS: Dict[str, Tuple[str, bool]] = {
    "orders_db": ("db_path", False),
    "follow_db": ("follow_db_path", False),
    "merge_db": ("merge_db_path", False),
    "pool_db": ("pool_db_path", False),
    "pool_dir": ("pool_dir", True),
}


def resolve_target(config: Dict[str, Any], kind: str) -> Tuple[Path, bool]:
    """把页面传来的 kind 解析成本机路径；非法取值直接拒绝。"""
    entry = OPENABLE_PATHS.get(str(kind or ""))
    if entry is None:
        raise ValueError("打开目标无效")
    key, is_dir = entry
    raw = config.get(key)
    if not raw:
        raise ValueError("该路径尚未配置")
    return Path(str(raw)).expanduser(), is_dir


def open_in_file_manager(path: Path, *, is_dir: bool, platform: Optional[str] = None) -> None:
    """打开目录，或定位到文件。不等待子进程结束。"""
    path = path.expanduser()
    if not path.exists():
        # 数据库尚未生成时退回到父目录，仍然给出可见反馈。
        path, is_dir = path.parent, True
    if not path.is_dir() and not path.is_file():
        raise ValueError("路径不存在")

    system = platform or sys.platform
    if system == "darwin":
        command = ["open", str(path)] if is_dir else ["open", "-R", str(path)]
    elif system.startswith("win"):
        command = ["explorer", str(path)] if is_dir else ["explorer", f"/select,{path}"]
    else:
        command = ["xdg-open", str(path if is_dir else path.parent)]

    try:
        subprocess.Popen(  # noqa: S603 - 固定命令 + 已校验的本机路径
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:  # pragma: no cover - 取决于本机环境
        raise ValueError("无法调用系统文件管理器") from exc
