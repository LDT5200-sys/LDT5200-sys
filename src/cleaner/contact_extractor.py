"""公开联系方式提取器。

只从公开文本（bio/title/desc/raw_text/搜索摘要）里识别联系方式线索。
不做隐私挖掘、不调用账号绑定接口、不去拿手机号短信验证。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.models.schemas import CreatorRecord


_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 微信号常见格式：vx/wx/微信 后接 ID（字母+数字+下划线/-，6-20 位）
_RE_WX = re.compile(
    r"(?:微信|wechat|vx|wx|加我|加微|薇|v信)\s*[:：]?\s*([A-Za-z][A-Za-z0-9_\-]{4,19})",
    re.IGNORECASE,
)

_KEYWORDS_BIZ = ["商务合作", "商务", "合作", "私信合作", "合作请", "合作私信", "广告合作"]
_KEYWORDS_XINGTU = ["星图", "巨量星图", "xingtu"]
_KEYWORDS_SHOP = ["橱窗", "小店", "购物车"]
_KEYWORDS_HINT = ["留言", "评论区", "主页"]


@dataclass
class ContactResult:
    contact_visible: str = "未知"   # 是 / 否 / 未知
    contact_text: str = ""           # 原文
    contact_type: str = "未知"       # 邮箱/微信/手机号/星图/商务合作入口/橱窗/未知
    contact_location: str = "未知"


def _pick_location(field_hits: dict[str, str]) -> str:
    """根据命中的字段判断联系方式的"位置"。"""
    priority = ["bio", "video_desc", "video_title", "raw_text", "snippet"]
    for k in priority:
        if field_hits.get(k):
            return {
                "bio": "主页简介",
                "video_desc": "视频文案",
                "video_title": "视频标题",
                "raw_text": "原始文本",
                "snippet": "搜索摘要",
            }[k]
    return "未知"


def extract_contact(rec: CreatorRecord, snippet: str = "") -> ContactResult:
    """对单条 CreatorRecord 提取公开联系方式。"""
    fields = {
        "bio": rec.creator_bio or "",
        "video_desc": rec.video_desc or "",
        "video_title": rec.video_title or "",
        "raw_text": rec.raw_text or "",
        "snippet": snippet or "",
    }
    combined = " | ".join(v for v in fields.values() if v)

    if not combined.strip():
        return ContactResult(
            contact_visible="否",
            contact_text="未发现公开联系方式",
            contact_type="未知",
            contact_location="未知",
        )

    hits: list[tuple[str, str, str]] = []  # (type, text, location_field)

    def _scan(rule_type: str, regex: re.Pattern):
        for fname, text in fields.items():
            if not text:
                continue
            for m in regex.finditer(text):
                hits.append((rule_type, m.group(0), fname))

    _scan("邮箱", _RE_EMAIL)
    _scan("手机号", _RE_PHONE)
    _scan("微信", _RE_WX)

    # 关键词命中（弱信号，用 contains）
    def _keyword_scan(rule_type: str, words: list[str]):
        for fname, text in fields.items():
            for w in words:
                if w.lower() in text.lower():
                    hits.append((rule_type, w, fname))
                    break  # 同字段同类型只记一次

    _keyword_scan("商务合作入口", _KEYWORDS_BIZ)
    _keyword_scan("星图", _KEYWORDS_XINGTU)
    _keyword_scan("橱窗", _KEYWORDS_SHOP)

    if not hits:
        # 公开文本里啥都没命中
        return ContactResult(
            contact_visible="否",
            contact_text="未发现公开联系方式",
            contact_type="未知",
            contact_location="未知",
        )

    # 选优先级最高的命中：邮箱 > 微信 > 手机号 > 星图 > 商务合作入口 > 橱窗
    rank = {"邮箱": 6, "微信": 5, "手机号": 4, "星图": 3, "商务合作入口": 2, "橱窗": 1}
    hits.sort(key=lambda h: rank.get(h[0], 0), reverse=True)
    best_type, best_text, best_field = hits[0]

    # 同时收集所有命中作为 contact_text，方便人工复核
    seen = set()
    parts: list[str] = []
    for t, txt, _ in hits:
        key = (t, txt)
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{t}:{txt}")
    text_blob = "公开文本疑似联系方式 -> " + " ; ".join(parts[:8])

    field_hits = {fname: True for _, _, fname in hits if _ == best_type}

    return ContactResult(
        contact_visible="是",
        contact_text=text_blob,
        contact_type=best_type,
        contact_location=_pick_location(field_hits),
    )


def apply_contact(records: list[CreatorRecord], snippets: dict[str, str] | None = None) -> list[CreatorRecord]:
    """批量给 records 填上 contact_* 字段。snippets 是 creator_key→搜索摘要 的映射，可选。"""
    snippets = snippets or {}
    for rec in records:
        ck = rec.creator_key()
        snippet = snippets.get(ck, "")
        out = extract_contact(rec, snippet=snippet)

        # 如果上游已显式标注「是」，保留 visible="是"，但允许提取器补充原文
        if rec.contact_visible == "是":
            if out.contact_visible == "是":
                rec.contact_text = out.contact_text or rec.contact_text
                rec.contact_type = out.contact_type if out.contact_type != "未知" else rec.contact_type
                rec.contact_location = out.contact_location if out.contact_location != "未知" else rec.contact_location
            else:
                # 上游标"是"但公开文本里没有原文 → 保留标注，原文写明
                if not rec.contact_text:
                    rec.contact_text = "上游来源标注：有公开联系方式（原文未提供）"
            continue

        # 上游显式标了「否」也尊重（不强行翻成"是"）
        if rec.contact_visible == "否":
            if not rec.contact_text:
                rec.contact_text = "上游来源标注：无公开联系方式"
            continue

        # 上游为"未知"时，按提取结果填入
        rec.contact_visible = out.contact_visible
        rec.contact_text = out.contact_text
        rec.contact_type = out.contact_type
        rec.contact_location = out.contact_location
    return records
