"""AI 分类：内容类型 / 是否适合龙牙 / 是否接近果子模式。

LLM 不可用时回退到关键词启发式，保证流程不中断。
"""
from __future__ import annotations

import json

from src.ai.llm_client import LLMClient, LLMUnavailable
from src.models.schemas import CreatorRecord
from src.utils.config_loader import brand_profile
from src.utils.logger import get_logger

logger = get_logger()

CONTENT_TYPES = [
    "女生测男装", "女穿男装", "男装测评", "短袖测评", "男友穿搭改造",
    "微胖/大码男装", "通勤户外穿搭", "机能/战术/户外", "泛穿搭", "泛生活方式", "不相关",
]

_PROMPT = """你是男装直播投放策略助手，正在为「{brand}」筛选外部达人。

品牌风格：{style}
主推产品：{products}
果子模式定义：{guozi}

请基于下面的达人/视频信息，输出一个 JSON 对象，字段：
1. content_type：必须从下列中选一个：{types}
2. is_fit_longya：是 / 否 / 不确定
3. is_guozi_like：是 / 否 / 不确定
4. classify_reason：50 字内中文说明

只输出 JSON，不要 Markdown，不要多余文字。

【达人昵称】{name}
【平台】{platform}
【粉丝数】{followers}
【账号简介】{bio}
【标签】{tags}
【视频标题】{title}
【视频文案】{desc}
【点赞】{like}  评论 {comment}
"""


def _heuristic_classify(rec: CreatorRecord) -> dict:
    text = (rec.raw_text or "") + " " + (rec.creator_name or "")
    text_l = text.lower()
    rules = [
        ("女生测男装", ["女生测男装", "女测男装", "女生评男装"]),
        ("女穿男装", ["女穿男装", "女生穿男装", "女友穿男装"]),
        ("短袖测评", ["短袖测评", "短袖推荐", "速干短袖", "夏季短袖"]),
        ("男装测评", ["男装测评", "男装评测", "男装避雷", "男装避坑"]),
        ("男友穿搭改造", ["男友改造", "男朋友改造", "直男改造", "男友穿搭改造"]),
        ("微胖/大码男装", ["微胖", "大码男装", "胖男生", "胖哥"]),
        ("通勤户外穿搭", ["通勤", "户外穿搭"]),
        ("机能/战术/户外", ["机能", "战术", "户外"]),
        ("泛穿搭", ["穿搭", "outfit"]),
    ]
    matched = "不相关"
    for label, kws in rules:
        if any(k.lower() in text_l for k in kws):
            matched = label
            break

    fit = "是" if matched not in ("泛生活方式", "不相关") else ("不确定" if matched == "泛穿搭" else "否")
    guozi = "不确定"
    if any(k in text for k in ["真实", "上身", "试穿", "测评", "改造", "对比"]):
        guozi = "是" if fit == "是" else "不确定"
    elif fit == "否":
        guozi = "否"
    return {
        "content_type": matched,
        "is_fit_longya": fit,
        "is_guozi_like": guozi,
        "classify_reason": "本地启发式匹配（AI 不可用或失败）",
    }


def _parse_json_obj(s: str) -> dict | None:
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        obj = json.loads(s[start: end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _build_prompt(rec: CreatorRecord) -> str:
    bp = brand_profile()
    return _PROMPT.format(
        brand=bp.get("brand_name", "龙牙"),
        style="、".join(bp.get("brand_style", [])),
        products="、".join(bp.get("main_products", [])),
        guozi=(bp.get("guozi_mode_definition") or "").strip(),
        types="、".join(CONTENT_TYPES),
        name=rec.creator_name or "未知",
        platform=rec.platform or "未知",
        followers=rec.follower_count if rec.follower_count is not None else "未知",
        bio=rec.creator_bio or "无",
        tags=rec.tags or "无",
        title=rec.video_title or "无",
        desc=rec.video_desc or "无",
        like=rec.like_count if rec.like_count is not None else "未知",
        comment=rec.comment_count if rec.comment_count is not None else "未知",
    )


def classify_records(records: list[CreatorRecord]) -> list[CreatorRecord]:
    llm: LLMClient | None = None
    try:
        llm = LLMClient(role="classify")
        if not llm.usable:
            llm = None
    except Exception as e:
        logger.warning(f"分类器初始化 LLM 失败：{e}")
        llm = None

    for rec in records:
        result: dict | None = None
        if llm is not None:
            try:
                content = llm.chat(_build_prompt(rec), system="你只输出 JSON")
                result = _parse_json_obj(content)
            except LLMUnavailable as e:
                logger.warning(f"LLM 分类失败将回退：{e}")
                result = None
            except Exception as e:
                logger.warning(f"LLM 分类未知异常：{e}")
                result = None

        if not result or "content_type" not in result:
            result = _heuristic_classify(rec)

        ct = result.get("content_type", "不相关")
        if ct not in CONTENT_TYPES:
            ct = "不相关"
        rec.content_type = ct
        rec.is_fit_longya = result.get("is_fit_longya") or "不确定"
        rec.is_guozi_like = result.get("is_guozi_like") or "不确定"
        reason = (result.get("classify_reason") or "").strip()
        if reason and not rec.recommend_reason:
            rec.recommend_reason = reason[:100]
    return records
