"""达人评分：规则分先行，AI 只在可用时叠加微调与产出推荐文案。"""
from __future__ import annotations

import json
import re

from src.ai.llm_client import LLMClient, LLMUnavailable
from src.models.schemas import CreatorRecord
from src.utils.config_loader import brand_profile, scoring_rules
from src.utils.logger import get_logger

logger = get_logger()


def _score_content_match(rec: CreatorRecord, dim: dict) -> float:
    text = (rec.raw_text + " " + (rec.content_type or "")).lower()
    high = [k.lower() for k in dim.get("high_value_keywords", [])]
    low = [k.lower() for k in dim.get("low_value_keywords", [])]
    max_score = float(dim.get("max", 30))

    if rec.content_type in (
        "女穿男装", "男装测评", "短袖测评",
        "微胖/大码男装", "通勤户外穿搭", "机能/战术/户外",
    ):
        return max_score
    if rec.content_type == "女生测男装":
        return max_score * 0.85
    if rec.content_type == "男友穿搭改造":
        return max_score * 0.70
    if rec.content_type == "泛穿搭":
        base = max_score * 0.70
    elif rec.content_type == "泛生活方式":
        base = max_score * 0.3
    elif rec.content_type == "不相关":
        base = 0.0
    else:
        base = max_score * 0.4

    hit_high = sum(1 for k in high if k in text)
    hit_low = sum(1 for k in low if k in text)
    base += min(hit_high, 3) * (max_score * 0.05)
    base -= min(hit_low, 3) * (max_score * 0.1)
    return max(0.0, min(max_score, base))


def _score_data_performance(rec: CreatorRecord, dim: dict) -> float:
    max_score = float(dim.get("max", 20))
    th = dim.get("like_thresholds", {}) or {}
    bonus_th = int(th.get("bonus", 2000))
    excellent_th = int(th.get("excellent", 10000))

    like = rec.like_count
    comment = rec.comment_count
    if like is None and comment is None:
        return max_score * 0.5

    score = 0.0
    if like is not None:
        if like >= excellent_th:
            score += max_score * 0.7
        elif like >= bonus_th:
            score += max_score * 0.5
        elif like >= 500:
            score += max_score * 0.3
        else:
            score += max_score * 0.15
    if comment is not None and comment >= 100:
        score += max_score * 0.2
    if rec.follower_count and like and rec.follower_count > 0:
        engagement = like / rec.follower_count
        if engagement >= 0.05:
            score += max_score * 0.1
    return min(max_score, score)


def _score_creator_scale(rec: CreatorRecord, dim: dict) -> float:
    max_score = float(dim.get("max", 15))
    tiers = dim.get("tiers", []) or []
    burst_bonus = float(dim.get("burst_bonus", 3))

    f = rec.follower_count
    if f is None:
        return max_score * 0.5

    score = max_score * 0.4
    for t in tiers:
        if f <= int(t.get("max_followers", 0)):
            score = float(t.get("score", max_score * 0.5))
            break

    if f <= 50000 and (rec.like_count or 0) >= 10000:
        score = min(max_score, score + burst_bonus)
    return score


def _score_cooperation(rec: CreatorRecord, dim: dict) -> float:
    max_score = float(dim.get("max", 15))
    has_profile = bool(rec.creator_profile_url)
    has_video = bool(rec.video_url)

    # 缺少关键链接：合作可行性大幅降分
    if not has_profile and not has_video:
        return 3.0  # 满分 15，给 3 分兜底

    score = 0.0
    if has_profile:
        score += float(dim.get("has_profile_url", 5))
    if has_video:
        score += float(dim.get("has_video_url", 3))
    if rec.contact_visible == "是":
        score += float(dim.get("has_contact", 5))
    if "星图" in (rec.tags or "") or "xingtu" in (rec.tags or "").lower():
        score += float(dim.get("has_xingtu", 2))

    # 只有其中一个链接时，上限 10（满分 15）
    if not has_profile or not has_video:
        score = min(10.0, score)
    return min(max_score, score)


def _score_reuse(rec: CreatorRecord, dim: dict) -> float:
    max_score = float(dim.get("max", 10))
    bonus = [k for k in dim.get("bonus_keywords", []) if k]
    text = rec.raw_text or ""
    hits = sum(1 for k in bonus if k in text)
    return min(max_score, hits * (max_score / max(len(bonus), 1)) + max_score * 0.2)


def _score_risk_penalty(rec: CreatorRecord, dim: dict) -> float:
    max_pen = float(dim.get("max_penalty", 10))
    risks = [k for k in dim.get("risk_keywords", []) if k]
    text = rec.raw_text or ""
    hits = sum(1 for k in risks if k in text)
    return min(max_pen, hits * (max_pen / max(len(risks), 1)))


def _priority_from_score(score: float, thresholds: dict) -> str:
    if score >= float(thresholds.get("S", 85)):
        return "S"
    if score >= float(thresholds.get("A", 75)):
        return "A"
    if score >= float(thresholds.get("B", 60)):
        return "B"
    if score >= float(thresholds.get("reject_below", 45)):
        return "C"
    return "淘汰"


def _recommend_product(rec: CreatorRecord) -> str:
    text = rec.raw_text or ""
    ct = rec.content_type or ""
    if "短袖" in ct or "短袖" in text or "速干" in text or "吸湿" in text:
        return "秘纤短袖"
    if "通勤" in ct or "通勤" in text:
        return "通勤机能"
    if "户外" in ct or "户外" in text or "防晒" in text:
        return "户外防晒"
    if "战术" in ct or "战术" in text or "机能" in ct or "机能" in text:
        return "战术裤"
    if "外套" in text:
        return "功能外套"
    return "待定"


def _next_action(level: str) -> str:
    return {
        "S": "优先查看视频并进入联系池",
        "A": "进入联系池",
        "B": "放入观察池",
        "C": "暂不处理",
        "淘汰": "淘汰",
    }.get(level, "暂不处理")


_AI_PROMPT = """你是男装直播投放策略助手，正在为「{brand}」给候选达人补充评分理由。

候选信息（已经规则评分 {rule_score:.1f}）：
昵称：{name}
平台：{platform}  粉丝：{followers}
内容类型：{content_type}  适合龙牙：{fit}  果子模式：{guozi}
视频标题：{title}
视频文案：{desc}
账号简介：{bio}

输出 JSON：
{{"ai_score_adjust": -10~+10 之间的整数（基于品牌契合度对规则分的微调）,
  "recommend_reason": "100 字内中文推荐理由",
  "risk_reason": "100 字内中文风险点，没有就写 无",
  "recommended_product": "{products} 之一或 待定"}}
只输出 JSON，不要任何额外内容。
"""


def _ai_refine(rec: CreatorRecord, llm: LLMClient) -> dict | None:
    bp = brand_profile()
    prompt = _AI_PROMPT.format(
        brand=bp.get("brand_name", "龙牙"),
        rule_score=rec.rule_score,
        name=rec.creator_name or "未知",
        platform=rec.platform or "未知",
        followers=rec.follower_count if rec.follower_count is not None else "未知",
        content_type=rec.content_type or "未知",
        fit=rec.is_fit_longya or "不确定",
        guozi=rec.is_guozi_like or "不确定",
        title=rec.video_title or "无",
        desc=rec.video_desc or "无",
        bio=rec.creator_bio or "无",
        products="、".join(bp.get("main_products", [])),
    )
    try:
        content = llm.chat(prompt, system="你只输出 JSON")
    except LLMUnavailable as e:
        logger.warning(f"AI 评分细化失败：{e}")
        return None
    s = content.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1:
        return None
    try:
        return json.loads(s[a: b + 1])
    except json.JSONDecodeError:
        return None


def _is_store_account(rec: CreatorRecord) -> bool:
    """检测是否为店铺/品牌号而非个人达人"""
    name = (rec.creator_name or "")
    name_l = name.lower()
    bio = (rec.creator_bio or "").lower()
    tags = (rec.tags or "").lower()
    douyin_id = (rec.douyin_id or "")
    combined = f"{name_l} {bio} {tags}"

    # 抖音号含"店" → 直接判定为店铺号
    if "店" in douyin_id:
        return True

    # 名称含强店铺词 → 直接判定（旗舰店/官方店/企业店/专卖店等，不管简介写什么）
    strong_store_name = ["旗舰店", "官方店", "品牌店", "专卖店", "直营店", "企业店", "工厂店"]
    if any(kw in name for kw in strong_store_name):
        return True

    # 品牌/店铺信号词
    store_signals = [
        "旗舰店", "官方店", "品牌店", "专卖店", "直营店",
        "服饰店", "男装店", "商城", "店铺",
        "正品", "工厂", "源头", "批发", "一件代发",
        "限时折扣", "秒杀", "包邮", "下单", "点击购买",
        "shop", "store", "品牌直销",
    ]
    # 品牌名信号（昵称中含明显品牌词）
    brand_names = [
        "劲霸", "海澜之家", "七匹狼", "太平鸟", "gxg", "gxg",
        "森马", "美特斯邦威", "优衣库", "无印良品",
        "恒源祥", "罗蒙", "杉杉", "雅戈尔", "九牧王",
        "龙牙", "秘纤",
    ]
    # 达人信号（排除项，有这些说明是真人）
    creator_signals = [
        "测评", "穿搭", "改造", "避雷", "推荐", "试穿", "分享",
        "ootd", "日常", "生活", "vlog", "博主",
        "男生", "女生", "微胖", "大码", "小个子", "梨形",
        "身高", "体重", "kg", "cm",
    ]

    has_store = any(kw in combined for kw in store_signals)
    has_brand = any(kw in name_l for kw in brand_names)
    has_creator = any(kw in combined for kw in creator_signals)

    # 品牌名在昵称中且无个人特征 → 品牌号
    if has_brand and not has_creator:
        return True

    # 店铺信号 + 无达人信号 → 店铺号
    if has_store and not has_creator:
        return True

    # 昵称纯品牌名（2-6 个汉字，无数字无 emoji）且无达人信号
    pure_cn = re.sub(r'[^一-龥]', '', name)
    if 2 <= len(pure_cn) <= 6 and len(name) <= 8 and has_store:
        return True

    return False


def _is_waste_account(rec: CreatorRecord) -> tuple:
    """检测废号：图文号、AI生成号、空壳号。返回 (是否为废号, 原因)。"""
    bio = (rec.creator_bio or "").lower()
    tags = (rec.tags or "").lower()
    combined = f"{bio} {tags}"

    # 图文号信号（只发图片/文案，不出镜）
    image_text_signals = [
        "图文作品", "图文创作", "图片分享", "每日壁纸", "壁纸分享",
        "头像", "表情包", "日签", "早安语录", "晚安语录",
        "写真", "摄影作品", "插画", "每日一图", "图文号",
        "图文带货", "图文直发", "好物图文", "图文种草",
        "不出镜", "不露脸", "非真人",
        "素材号", "切片", "搬运",
        "文案号", "语录号", "摘抄",
    ]
    # AI生成信号（数字人/虚拟人，非真人出镜）
    ai_signals = [
        "ai生成", "aigc", "ai绘画", "ai创作", "ai数字人",
        "数字人", "虚拟人", "虚拟主播", "ai虚拟",
    ]

    image_hits = [kw for kw in image_text_signals if kw in combined]
    if image_hits:
        return (True, f"疑似图文号（命中: {', '.join(image_hits[:3])}）")

    ai_hits = [kw for kw in ai_signals if kw in combined]
    if ai_hits:
        return (True, f"疑似AI生成号（命中: {', '.join(ai_hits[:3])}）")

    return (False, "")


def _is_stale_account(rec: CreatorRecord) -> tuple:
    """检测停更号。返回 (停更等级: 0=正常 1=>3月 2=>半年, 原因)。"""
    publish_time = (rec.publish_time or "").strip()
    if not publish_time:
        return (0, "")

    try:
        from datetime import datetime
        pub_dt = datetime.strptime(publish_time[:10], "%Y-%m-%d")
        days = (datetime.now() - pub_dt).days
    except (ValueError, TypeError):
        return (0, "")

    if days >= 180:
        return (2, f"最近更新{days}天前（超过半年），疑似停更")
    elif days >= 90:
        return (1, f"最近更新{days}天前（超过3个月），可能停更")

    return (0, "")


def _fmt_followers(v) -> str:
    """数字转万单位"""
    if v is None:
        return "0"
    try:
        n = int(float(v))
    except (ValueError, TypeError):
        return str(v)
    if n >= 10000:
        w = n / 10000
        return f"{w:.1f}w" if w < 100 else f"{int(w)}w"
    return str(n)


def _build_score_reason(rec: CreatorRecord, scores: dict) -> str:
    """根据各维度得分生成评分理由"""
    parts = []
    dim_names = {
        "content_match": "内容匹配", "data_performance": "数据表现",
        "creator_scale": "达人量级", "cooperation_feasibility": "合作可行性",
        "reuse_potential": "素材复用", "risk_penalty": "风险扣分",
    }
    for key, name in dim_names.items():
        s = scores.get(key, 0)
        if key == "risk_penalty" and s > 0:
            parts.append(f"{name}-{s:.0f}")
        elif key != "risk_penalty":
            parts.append(f"{name}{s:.0f}")
    return " | ".join(parts)


_DOUYIN_TYPE_MAP = {
    "女生测男装": "女测男装博主",
    "女穿男装": "女穿男装博主",
    "男装测评": "男装测评博主",
    "短袖测评": "短袖测评/推荐博主",
    "男友穿搭改造": "穿搭改造博主",
    "微胖/大码男装": "微胖/大码男装博主",
    "通勤户外穿搭": "通勤户外穿搭博主",
    "机能/战术/户外": "机能战术户外博主",
    "泛穿搭": "泛穿搭博主",
    "泛生活方式": "泛生活方式博主",
    "不相关": "不相关",
}


def _douyin_type(rec: CreatorRecord) -> str:
    """根据内容类型映射为抖音达人类型"""
    return _DOUYIN_TYPE_MAP.get(rec.content_type, "其他")


def score_records(records: list[CreatorRecord]) -> list[CreatorRecord]:
    rules = scoring_rules()
    dims = rules.get("dimensions", {}) or {}
    thresholds = rules.get("priority_thresholds", {}) or {}
    coop_map = rules.get("cooperation_suggestion_map", {}) or {}

    llm: LLMClient | None = None
    try:
        llm = LLMClient(role="score")
        if not llm.usable:
            llm = None
    except Exception as e:
        logger.warning(f"评分器初始化 LLM 失败：{e}")
        llm = None

    for rec in records:
        s_match = _score_content_match(rec, dims.get("content_match", {}))
        s_data = _score_data_performance(rec, dims.get("data_performance", {}))
        s_scale = _score_creator_scale(rec, dims.get("creator_scale", {}))
        s_coop = _score_cooperation(rec, dims.get("cooperation_feasibility", {}))
        s_reuse = _score_reuse(rec, dims.get("reuse_potential", {}))
        s_risk = _score_risk_penalty(rec, dims.get("risk_penalty", {}))

        rule_total = s_match + s_data + s_scale + s_coop + s_reuse - s_risk
        rule_total = max(0.0, min(100.0, rule_total))
        rec.rule_score = round(rule_total, 1)
        rec.ai_score = rec.rule_score

        # 评分明细（简短数字）
        rec.risk_reason = _build_score_reason(rec, {
            "content_match": s_match, "data_performance": s_data,
            "creator_scale": s_scale, "cooperation_feasibility": s_coop,
            "reuse_potential": s_reuse, "risk_penalty": s_risk,
        })

        # 如果 AI 没有生成推荐理由，用规则生成一个简洁的
        if not rec.recommend_reason:
            parts = []
            if rec.content_type and rec.content_type != "不相关":
                parts.append(f"内容匹配{rec.content_type}")
            if rec.follower_count:
                parts.append(f"{_fmt_followers(rec.follower_count)}粉")
            if rec.like_count:
                parts.append(f"最高赞{_fmt_followers(rec.like_count)}")
            if rec.is_guozi_like == "是":
                parts.append("接近果子模式")
            if parts:
                rec.recommend_reason = "，".join(parts) + "。"

        rec.douyin_type = _douyin_type(rec)
        rec.recommended_product = _recommend_product(rec)

        if llm is not None:
            ai_out = _ai_refine(rec, llm)
            if ai_out:
                try:
                    adjust = float(ai_out.get("ai_score_adjust", 0))
                except (TypeError, ValueError):
                    adjust = 0.0
                adjust = max(-10.0, min(10.0, adjust))
                rec.ai_score = round(max(0.0, min(100.0, rule_total + adjust)), 1)
                rr = (ai_out.get("recommend_reason") or "").strip()
                if rr:
                    rec.recommend_reason = rr[:100]
                risk = (ai_out.get("risk_reason") or "").strip()
                if risk and risk != "无":
                    rec.risk_reason = risk[:100]
                rp = (ai_out.get("recommended_product") or "").strip()
                if rp:
                    rec.recommended_product = rp

        rec.priority_level = _priority_from_score(rec.ai_score, thresholds)

        # S 级硬约束：缺少主页链接或代表视频链接，最高只能到 A
        if rec.priority_level == "S":
            if not rec.creator_profile_url or not rec.video_url:
                rec.priority_level = "A"

        # 店铺/品牌号检测：直接淘汰
        if _is_store_account(rec):
            rec.priority_level = "淘汰"
            rec.cooperation_suggestion = "暂不建议（店铺/品牌号，非达人）"
            rec.risk_reason = (rec.risk_reason or "") + "；疑似店铺/品牌账号，非个人达人"

        # 内容类型弱匹配约束：泛生活方式最高 C
        if rec.content_type == "泛生活方式" and rec.priority_level in ("S", "A", "B"):
            rec.priority_level = "C"

        # 废号检测：图文号 / AI生成号 → 最高 C
        is_waste, waste_reason = _is_waste_account(rec)
        if is_waste and rec.priority_level not in ("淘汰", "C"):
            rec.priority_level = "C"
            rec.risk_reason = (rec.risk_reason or "") + f"；{waste_reason}"

        # 停更检测：代表视频发布时间过旧 → 降级
        stale_level, stale_reason = _is_stale_account(rec)
        if stale_level >= 2 and rec.priority_level not in ("淘汰", "C"):
            rec.priority_level = "C"
        elif stale_level >= 1 and rec.priority_level in ("S", "A"):
            rec.priority_level = "B"
        if stale_reason:
            rec.risk_reason = (rec.risk_reason or "") + f"；{stale_reason}"

        rec.cooperation_suggestion = coop_map.get(rec.priority_level, "暂不建议")
        rec.next_action = _next_action(rec.priority_level)

        if not rec.recommend_reason:
            rec.recommend_reason = (
                f"内容类型 {rec.content_type or '未知'}，规则评分 {rec.rule_score:.0f}，"
                f"匹配度 {s_match:.0f}/数据 {s_data:.0f}/量级 {s_scale:.0f}"
            )[:100]
        if not rec.risk_reason:
            rec.risk_reason = "无明显风险" if s_risk == 0 else f"风险扣分 {s_risk:.0f}"
    return records
