"""标准字段定义：用 pydantic 校验并提供默认值，避免下游 NaN 报错。"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def _to_int(v: Any) -> Optional[int]:
    if v is None or v == "" or (isinstance(v, float) and v != v):  # NaN
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "未知", "未公开"):
        return None
    try:
        if s.endswith("w") or s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        return int(float(s))
    except ValueError:
        return None


class CreatorRecord(BaseModel):
    """单条候选记录的标准结构。"""

    collect_date: str = ""
    source_name: str = ""
    platform: str = "other"
    search_keyword: str = ""

    creator_name: str = ""
    creator_id: str = ""           # 内部ID（sec_uid）
    douyin_id: str = ""            # 抖音号（用户可见的 @账号名）
    creator_profile_url: str = ""
    video_url: str = ""
    video_title: str = ""
    video_desc: str = ""
    publish_time: str = ""

    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None
    collect_count: Optional[int] = None
    follower_count: Optional[int] = None

    creator_bio: str = ""
    tags: str = ""

    contact_visible: str = "未知"   # 是 / 否 / 未知
    contact_location: str = "未知"
    contact_text: str = ""           # 公开联系方式原文
    contact_type: str = "未知"       # 邮箱/微信/手机号/星图/商务合作入口/未知

    # 商业化标识
    has_product_link: str = "未知"    # 是否挂车：是/否/未知

    # 来源 & 链接元信息
    source_url: str = ""             # 原始来源链接（搜索结果页/星图卡片等）
    url_type: str = "未知"           # 主页/视频/搜索/未知

    # 抓取/解析状态
    extraction_status: str = "未处理"   # 成功/部分成功/失败/未处理
    missing_reason: str = ""           # 缺失字段的原因说明

    raw_text: str = ""
    raw_data: str = ""              # JSON 字符串，方便写库

    # AI / 评分输出
    content_type: str = ""
    douyin_type: str = ""           # 抖音达人类型（测评博主/穿搭博主等）
    is_fit_longya: str = ""
    is_guozi_like: str = ""
    rule_score: float = 0.0
    ai_score: float = 0.0
    priority_level: str = ""
    cooperation_suggestion: str = ""
    recommended_product: str = "待定"
    recommend_reason: str = ""
    risk_reason: str = ""
    next_action: str = ""

    @field_validator(
        "like_count", "comment_count", "share_count", "collect_count", "follower_count",
        mode="before",
    )
    @classmethod
    def _coerce_int(cls, v: Any) -> Optional[int]:
        return _to_int(v)

    @field_validator(
        "creator_name", "creator_id", "douyin_id", "creator_profile_url", "video_url",
        "video_title", "video_desc", "creator_bio", "tags", "raw_text", "raw_data",
        "platform", "search_keyword", "source_name", "collect_date", "publish_time",
        "contact_visible", "contact_location", "contact_text", "contact_type",
        "has_product_link",
        "source_url", "url_type", "extraction_status", "missing_reason",
        mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, float) and v != v:  # NaN
            return ""
        return str(v).strip()

    def creator_key(self) -> str:
        """达人唯一标识：优先 profile_url，其次 platform+id，再次 platform+name。"""
        if self.creator_profile_url:
            base = self.creator_profile_url
        elif self.creator_id:
            base = f"{self.platform}::{self.creator_id}"
        else:
            base = f"{self.platform}::{self.creator_name}"
        return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

    def video_key(self) -> str:
        if self.video_url:
            return hashlib.md5(self.video_url.encode("utf-8")).hexdigest()[:16]
        return ""


# 标准字段顺序，供 Excel / DataFrame 输出统一使用
STANDARD_FIELDS: list[str] = [
    "collect_date", "source_name", "source_url", "platform", "search_keyword",
    "creator_name", "creator_id", "douyin_id", "creator_profile_url",
    "video_url", "url_type", "video_title", "video_desc", "publish_time",
    "like_count", "comment_count", "share_count", "collect_count", "follower_count",
    "creator_bio", "tags",
    "contact_visible", "contact_text", "contact_type", "contact_location",
    "has_product_link",
    "extraction_status", "missing_reason",
    "raw_text", "raw_data",
    "content_type", "douyin_type", "is_fit_longya", "is_guozi_like",
    "rule_score", "ai_score", "priority_level",
    "cooperation_suggestion", "recommended_product",
    "recommend_reason", "risk_reason", "next_action",
]

# 状态机：人工标注用
CREATOR_STATUS = ["未查看", "合适", "不合适", "待联系", "已联系", "已报价", "已合作", "淘汰"]
