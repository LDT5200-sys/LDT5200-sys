"""douyin URL 解析器。

只做合规事情：
- 通过正则识别 URL 类型（主页 / 视频 / 搜索 / 未知）
- 在已配置允许的前提下，发起一次普通 GET 抓 OpenGraph / meta 信息
- 一旦遇到登录页、验证码、403/451、跳转风控页 → 立刻放弃并把 missing_reason 标清楚

绝不绕登录、绝不绕风控、绝不解 sec_uid 加密、绝不解 webid。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

from src.utils.logger import get_logger

logger = get_logger()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_URL_TYPE_USER = "主页"
_URL_TYPE_VIDEO = "视频"
_URL_TYPE_SEARCH = "搜索"
_URL_TYPE_UNKNOWN = "未知"

# douyin URL 关键路径
_RE_USER = re.compile(r"douyin\.com/user/([A-Za-z0-9_\-]+)")
_RE_VIDEO = re.compile(r"douyin\.com/video/(\d+)")
_RE_SEARCH = re.compile(r"douyin\.com/search/")
_RE_AWEME_VIDEO = re.compile(r"v\.douyin\.com/[A-Za-z0-9]+")

# 其他平台的主页/视频识别（仅做 URL 分类，不抓远程）
_RE_OTHER_USER = re.compile(
    r"(xiaohongshu\.com/user/profile/|space\.bilibili\.com/|bilibili\.com/space/|"
    r"weibo\.com/u/|kuaishou\.com/profile/)"
)
_RE_OTHER_VIDEO = re.compile(
    r"(xiaohongshu\.com/(discovery/item|explore)/|bilibili\.com/video/|"
    r"weibo\.com/tv/show/|kuaishou\.com/short-video/)"
)

# 登录 / 验证码 / 风控 信号
_BLOCK_SIGNALS = [
    "请先登录", "扫码登录", "登录抖音", "verification", "captcha",
    "verify_image", "验证滑块", "访问受限", "请稍后再试", "block",
    "云盾", "禁止访问",
]


@dataclass
class EnrichResult:
    url: str
    url_type: str = _URL_TYPE_UNKNOWN
    creator_id: str = ""
    creator_name: str = ""
    creator_profile_url: str = ""
    video_url: str = ""
    video_title: str = ""
    creator_bio: str = ""
    extraction_status: str = "未处理"   # 成功 / 部分成功 / 失败 / 未处理
    missing_reason: str = ""
    raw_meta: dict = field(default_factory=dict)


def classify_url(url: str) -> str:
    if not url:
        return _URL_TYPE_UNKNOWN
    u = url.strip()
    if _RE_VIDEO.search(u) or _RE_AWEME_VIDEO.search(u):
        return _URL_TYPE_VIDEO
    if _RE_USER.search(u):
        return _URL_TYPE_USER
    if _RE_SEARCH.search(u):
        return _URL_TYPE_SEARCH
    if _RE_OTHER_VIDEO.search(u):
        return _URL_TYPE_VIDEO
    if _RE_OTHER_USER.search(u):
        return _URL_TYPE_USER
    return _URL_TYPE_UNKNOWN


def _extract_meta(html: str) -> dict[str, str]:
    """从 HTML 抓取常见 meta / og 字段。失败返回空 dict。"""
    out: dict[str, str] = {}
    if not html:
        return out
    patterns = {
        "og:title": r'<meta[^>]+property=[\'"]og:title[\'"][^>]+content=[\'"]([^\'"]+)[\'"]',
        "og:description": r'<meta[^>]+property=[\'"]og:description[\'"][^>]+content=[\'"]([^\'"]+)[\'"]',
        "og:url": r'<meta[^>]+property=[\'"]og:url[\'"][^>]+content=[\'"]([^\'"]+)[\'"]',
        "title": r"<title>([^<]+)</title>",
        "description": r'<meta[^>]+name=[\'"]description[\'"][^>]+content=[\'"]([^\'"]+)[\'"]',
        "keywords": r'<meta[^>]+name=[\'"]keywords[\'"][^>]+content=[\'"]([^\'"]+)[\'"]',
    }
    for key, pat in patterns.items():
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            out[key] = m.group(1).strip()
    return out


def _looks_blocked(html: str, status_code: int) -> Optional[str]:
    if status_code in (401, 403, 451):
        return f"页面返回 HTTP {status_code}（受访问控制或法律限制）"
    if status_code >= 500:
        return f"页面返回 HTTP {status_code}（服务端不可用）"
    lower = (html or "")[:6000].lower()
    for sig in _BLOCK_SIGNALS:
        if sig.lower() in lower:
            return f"页面命中风控/登录信号：{sig}"
    return None


def enrich_url(
    url: str,
    *,
    fetch_remote: bool = False,
    timeout: int = 12,
) -> EnrichResult:
    """对单个 URL 做合规识别。

    fetch_remote=False（默认）只做本地正则识别，零网络请求；适合主流程。
    fetch_remote=True 才发一次 GET 抓 og:title / description；遇任何风控/登录信号立刻放弃。
    """
    url = (url or "").strip()
    res = EnrichResult(url=url)

    if not url:
        res.extraction_status = "失败"
        res.missing_reason = "空链接"
        return res

    res.url_type = classify_url(url)
    is_douyin = "douyin.com" in url

    # ---- 本地正则能拿到的字段（仅 douyin）----
    if is_douyin and res.url_type == _URL_TYPE_USER:
        m = _RE_USER.search(url)
        if m:
            res.creator_id = m.group(1)
            res.creator_profile_url = url
    elif is_douyin and res.url_type == _URL_TYPE_VIDEO:
        res.video_url = url

    # ---- 仅本地识别就够时，直接返回 ----
    if not fetch_remote:
        if res.url_type == _URL_TYPE_UNKNOWN:
            res.extraction_status = "失败"
            res.missing_reason = "URL 无法识别为达人主页或视频（不在已知平台路径模式中）"
        else:
            res.extraction_status = "部分成功"
            res.missing_reason = "未发起远程抓取，仅基于 URL 模式识别（合规模式）"
        return res

    # 非 douyin 的远程抓取本版不实现，仅做 URL 分类
    if not is_douyin:
        if res.url_type == _URL_TYPE_UNKNOWN:
            res.extraction_status = "失败"
            res.missing_reason = "URL 无法识别为达人主页或视频"
        else:
            res.extraction_status = "部分成功"
            res.missing_reason = "非抖音平台暂仅做 URL 分类，未发起远程抓取"
        return res

    # ---- 远程抓取 og 元信息 ----
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        text = r.text or ""
        block_reason = _looks_blocked(text, r.status_code)
    except requests.RequestException as e:
        res.extraction_status = "失败"
        res.missing_reason = f"页面不可访问：{type(e).__name__}"
        return res

    if block_reason:
        res.extraction_status = "失败"
        res.missing_reason = f"需要登录/验证码/页面不可访问：{block_reason}"
        return res

    meta = _extract_meta(text)
    res.raw_meta = meta

    title = meta.get("og:title") or meta.get("title") or ""
    desc = meta.get("og:description") or meta.get("description") or ""

    filled = 0
    if res.url_type == _URL_TYPE_USER:
        if title:
            res.creator_name = title.split("-")[0].split("｜")[0].strip()
            filled += 1
        if desc:
            res.creator_bio = desc.strip()
            filled += 1
    elif res.url_type == _URL_TYPE_VIDEO:
        if title:
            res.video_title = title.strip()
            filled += 1
        if desc:
            res.creator_bio = ""  # 视频页 description 通常是文案，不算 bio
            filled += 1

    if filled == 0:
        res.extraction_status = "失败"
        res.missing_reason = "公开页面未发现 og:title / description（可能被前端注水或风控降级）"
    elif res.url_type == _URL_TYPE_UNKNOWN:
        res.extraction_status = "失败"
        res.missing_reason = "URL 类型未知，已抓取但无法归类为达人或视频"
    else:
        res.extraction_status = "部分成功"
        res.missing_reason = (
            "已从公开 og/meta 抓取标题/简介；粉丝数、点赞数、联系方式等字段公开页面未提供"
        )
    return res


def enrich_records(
    records: list[dict],
    *,
    fetch_remote: bool = False,
) -> list[dict]:
    """对一批 dict 记录做 enrich，并把结果合并回原 dict。

    输入 dict 至少要有「主页链接」/「视频链接」/「数据来源链接」 之一。
    """
    for rec in records:
        url = (
            rec.get("主页链接")
            or rec.get("视频链接")
            or rec.get("creator_profile_url")
            or rec.get("video_url")
            or rec.get("source_url")
            or rec.get("数据来源链接")
            or ""
        )
        # 数据来源链接通常是搜索 query，不是真实页面，跳过远程抓取
        if "site:" in url:
            url = rec.get("主页链接") or rec.get("视频链接") or ""

        res = enrich_url(url, fetch_remote=fetch_remote)
        rec.setdefault("链接类型", res.url_type)
        if res.url_type != _URL_TYPE_UNKNOWN:
            rec["链接类型"] = res.url_type
        if res.creator_id and not rec.get("达人ID"):
            rec["达人ID"] = res.creator_id
        if res.creator_profile_url and not rec.get("主页链接"):
            rec["主页链接"] = res.creator_profile_url
        if res.video_url and not rec.get("视频链接"):
            rec["视频链接"] = res.video_url
        if res.creator_name and not rec.get("达人昵称"):
            rec["达人昵称"] = res.creator_name
        if res.video_title and not rec.get("视频标题"):
            rec["视频标题"] = res.video_title
        if res.creator_bio and not rec.get("账号简介"):
            rec["账号简介"] = res.creator_bio

        # 提取状态：优先保留更严重的（失败 > 部分成功 > 成功）
        prev_status = rec.get("提取状态", "")
        new_status = res.extraction_status
        rank = {"失败": 3, "部分成功": 2, "成功": 1, "未处理": 0, "": 0}
        rec["提取状态"] = new_status if rank.get(new_status, 0) >= rank.get(prev_status, 0) else prev_status

        # 缺失原因：拼接，去重
        prev_reason = (rec.get("缺失原因") or "").strip()
        merged = "；".join([s for s in (prev_reason, res.missing_reason) if s])
        if merged:
            rec["缺失原因"] = merged

    return records
