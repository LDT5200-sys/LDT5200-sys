"""时间相关工具。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

CN_TZ = timezone(timedelta(hours=8))


def today_str(fmt: str = "%Y%m%d") -> str:
    return datetime.now(CN_TZ).strftime(fmt)


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.now(CN_TZ).strftime(fmt)


def parse_publish_time(value) -> str | None:
    """尽力把各种时间格式转换成 'YYYY-MM-DD HH:MM:SS'。失败返回原始字符串。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    s = str(value).strip()
    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y.%m.%d",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    if s.isdigit() and len(s) in (10, 13):
        ts = int(s)
        if len(s) == 13:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return s
    return s
