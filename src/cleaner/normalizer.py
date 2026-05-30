"""字段归一化：把不同来源的 dict 列表整理成 CreatorRecord 列表。"""
from __future__ import annotations

import json
from typing import Any

from src.cleaner.contact_extractor import apply_contact
from src.data_sources.douyin_url_enricher import classify_url, enrich_records
from src.models.schemas import CreatorRecord
from src.utils.config_loader import field_mapping
from src.utils.logger import get_logger
from src.utils.time_utils import parse_publish_time, today_str

logger = get_logger()


def _build_lookup(mapping: dict) -> dict[str, str]:
    """{原始列名 lower → 标准字段}"""
    out: dict[str, str] = {}
    for std, aliases in (mapping.get("standard_fields") or {}).items():
        for a in aliases or []:
            out[str(a).strip().lower()] = std
        out[std.lower()] = std
    return out


def _detect_platform(url: str, fallback: str) -> str:
    if not url:
        return fallback or "other"
    u = url.lower()
    if "douyin" in u or "iesdouyin" in u or "tiktok" in u:
        return "douyin"
    if "xiaohongshu" in u or "xhs" in u or "redbook" in u:
        return "xiaohongshu"
    if "bilibili" in u:
        return "bilibili"
    if "kuaishou" in u:
        return "kuaishou"
    if "weibo" in u:
        return "weibo"
    return fallback or "other"


def _clean_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in ("nan", "none", "null"):
        return ""
    return s


def normalize_records(
    rows: list[dict[str, Any]],
    *,
    enrich_remote: bool = False,
) -> list[CreatorRecord]:
    """把多源 dict 行归一化为 CreatorRecord 列表。

    enrich_remote=True 时，对识别为 douyin 主页/视频的链接发起一次 og:meta 抓取。
    若遇到登录/验证码/风控/失败，会写入 missing_reason，不会抛异常、不会绕过限制。
    """
    mapping = field_mapping()
    lookup = _build_lookup(mapping)
    today = today_str("%Y-%m-%d")

    # 第一步：尽可能在 dict 阶段就补字段（包括可选的远程 enrich）
    try:
        rows = enrich_records(rows, fetch_remote=enrich_remote)
    except Exception as e:
        logger.warning(f"URL enrich 阶段异常，已忽略：{e}")

    records: list[CreatorRecord] = []
    for raw in rows:
        std: dict[str, Any] = {}
        leftover: dict[str, Any] = {}

        for k, v in raw.items():
            if k in ("__source_file__", "__source_name__"):
                continue
            key = str(k).strip().lower()
            std_key = lookup.get(key)
            if std_key:
                std.setdefault(std_key, v)
            else:
                leftover[str(k)] = v

        std.setdefault("collect_date", today)
        if not std.get("source_name"):
            std["source_name"] = raw.get("__source_name__", "unknown")

        std["platform"] = _detect_platform(
            _clean_str(std.get("creator_profile_url") or std.get("video_url")),
            _clean_str(std.get("platform")),
        )

        std["publish_time"] = parse_publish_time(std.get("publish_time")) or ""

        # 联系方式可见性归一化（保留上游已显式标注的「是/否」）
        cv = _clean_str(std.get("contact_visible")).lower()
        if cv in ("yes", "true", "1", "是"):
            std["contact_visible"] = "是"
        elif cv in ("no", "false", "0", "否"):
            std["contact_visible"] = "否"
        else:
            std["contact_visible"] = "未知"
        if not _clean_str(std.get("contact_location")):
            std["contact_location"] = "未知"
        if not _clean_str(std.get("contact_type")):
            std["contact_type"] = "未知"

        # url_type：优先用 enricher 已经写入的；缺失就用 URL 重新判一次
        if not _clean_str(std.get("url_type")):
            link = _clean_str(std.get("creator_profile_url") or std.get("video_url"))
            std["url_type"] = classify_url(link) if link else "未知"

        # 默认提取状态：上游没填就视为「未处理」
        if not _clean_str(std.get("extraction_status")):
            std["extraction_status"] = "未处理"

        # raw_text：拼接已有 raw_text + 标题、文案、简介，供 AI/联系方式提取使用
        existing_raw = _clean_str(std.get("raw_text"))
        parts = [existing_raw] if existing_raw else []
        parts += [
            _clean_str(std.get("video_title")),
            _clean_str(std.get("video_desc")),
            _clean_str(std.get("creator_bio")),
            _clean_str(std.get("tags")),
        ]
        std["raw_text"] = " | ".join([p for p in parts if p])

        # 保留未映射字段到 raw_data
        if leftover:
            try:
                std["raw_data"] = json.dumps(leftover, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                std["raw_data"] = str(leftover)

        try:
            rec = CreatorRecord(**std)
        except Exception as e:
            logger.warning(f"记录归一化失败，已跳过：{e} 原始={raw}")
            continue

        if not rec.creator_name and not rec.creator_profile_url and not rec.video_url:
            # 完全无名无链，直接丢弃
            continue
        records.append(rec)

    # 第二步：联系方式提取（用 raw_text + bio + 视频文案）
    records = apply_contact(records)

    # 第三步：缺失字段写明 missing_reason（即便 enricher 没跑，也能给出原因）
    for rec in records:
        _fill_missing_reason(rec)

    logger.info(f"归一化完成：输入 {len(rows)} 行，有效 {len(records)} 条")
    return records


def _fill_missing_reason(rec: CreatorRecord) -> None:
    reasons: list[str] = []
    if rec.missing_reason:
        reasons.append(rec.missing_reason)

    has_profile = bool(rec.creator_profile_url)
    has_video = bool(rec.video_url)

    if not has_profile and not has_video:
        # 两者都没有 → 最高严重级别
        reasons.append(
            "缺少关键链接（达人主页链接和代表视频链接均缺失，可能仅来自搜索摘要或弱信息源，无法直接进行合作对接）"
        )
    elif not has_profile:
        reasons.append("未发现达人主页链接（来源未提供或链接为视频页）")
    elif not has_video:
        reasons.append("未发现代表视频链接（来源仅给出主页或搜索摘要）")
    if rec.follower_count is None:
        reasons.append("公开页面/搜索摘要未提供粉丝数")
    if rec.contact_visible == "否" and "上游来源标注" not in (rec.contact_text or ""):
        reasons.append("公开文本未发现联系方式")
    if rec.url_type == "未知":
        reasons.append("链接类型未识别为达人主页/视频")

    # 去重保序
    seen = set()
    out: list[str] = []
    for r in reasons:
        if not r or r in seen:
            continue
        seen.add(r)
        out.append(r)
    if out:
        rec.missing_reason = "；".join(out)
    if not rec.extraction_status or rec.extraction_status == "未处理":
        rec.extraction_status = "成功" if not out else "部分成功"
