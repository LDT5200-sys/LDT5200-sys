"""AI 驱动关键词优化：根据品牌需求 + 历史结果反馈，动态生成搜索关键词。

每次运行前调 LLM 生成一组新关键词，避免人工设定局限。
"""
from __future__ import annotations

import json

from src.utils.config_loader import brand_profile
from src.utils.logger import get_logger

logger = get_logger()

_PROMPT = """你是抖音男装直播投放策略助手，正在为「{brand}」寻找最适合合作的外部达人。

品牌风格：{style}
主推产品：{products}
目标达人类型：{targets}
内容规则：{rules}

{feedback_section}

请生成 {n} 个最适合在抖音搜索的精准关键词（短语），用于发现上述类型的达人。
关键词要求：
1. 必须是真人在抖音搜索框会输入的短语
2. 覆盖不同搜索意图：测评、改造、推荐、避雷、场景穿搭、特定身材/码数
3. 不要堆砌品牌词
4. 不要含擦边/违规内容
5. 每个关键词 4-15 字
6. 优先产出强信号关键词（能直接定位目标达人的），少产泛词

输出纯 JSON 数组，不要任何解释：
["关键词1", "关键词2", ...]
"""


def generate_keywords(
    n: int = 25,
    feedback: list[str] | None = None,
    good_examples: list[str] | None = None,
    bad_examples: list[str] | None = None,
) -> list[str]:
    """用 LLM 生成优化后的搜索关键词。AI 不可用时退回种子词。"""

    bp = brand_profile()
    brand = bp.get("brand_name", "龙牙战术服装")
    style = "、".join(bp.get("brand_style", []))
    products = "、".join(bp.get("main_products", []))
    targets = "、".join(bp.get("target_creator_types", []))
    rules = "；".join(bp.get("content_rules", []))

    # 构建反馈段落
    feedback_parts = []
    if good_examples:
        feedback_parts.append(f"✅ 以下类型的达人效果好，优先产相关关键词：{', '.join(good_examples[:8])}")
    if bad_examples:
        feedback_parts.append(f"❌ 以下类型的达人效果差，避免产相关关键词：{', '.join(bad_examples[:8])}")
    if feedback:
        feedback_parts.append(f"📝 最新需求：{'; '.join(feedback[:5])}")

    feedback_section = ""
    if feedback_parts:
        feedback_section = "【重要反馈】\n" + "\n".join(feedback_parts)

    prompt = _PROMPT.format(
        brand=brand, style=style, products=products, targets=targets,
        rules=rules, feedback_section=feedback_section, n=n,
    )

    # 调用 LLM
    try:
        from src.ai.llm_client import LLMClient
        llm = LLMClient(role="keyword")
        if llm.usable:
            resp = llm.chat(prompt, system="你只输出 JSON 数组，不要任何其他内容")
            keywords = _parse_json_array(resp)
            if keywords and len(keywords) >= 5:
                logger.info(f"AI 生成 {len(keywords)} 个优化关键词")
                return keywords[:n]
    except Exception as e:
        logger.warning(f"AI 关键词生成失败，回退种子词: {e}")

    # 兜底：种子词 + 扩展
    from src.keyword.keyword_expander import expand_keywords
    df = expand_keywords(use_ai=False)
    fallback = df["扩展关键词"].dropna().astype(str).tolist()
    if not fallback:
        fallback = bp.get("target_creator_types", [])
    logger.info(f"回退种子词 {len(fallback)} 个")
    return fallback[:n]


def _parse_json_array(s: str) -> list[str] | None:
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b == -1:
        return None
    try:
        arr = json.loads(s[a:b + 1])
        return arr if isinstance(arr, list) else None
    except json.JSONDecodeError:
        return None


def load_feedback_history() -> dict:
    """从本地文件加载历史反馈记录。"""
    from pathlib import Path
    from src.utils.config_loader import DATA_DIR
    fb_path = DATA_DIR / "processed" / "keyword_feedback.json"
    if fb_path.exists():
        try:
            return json.loads(fb_path.read_text())
        except Exception:
            pass
    return {"good": [], "bad": [], "notes": []}


def save_feedback(good: list[str] | None = None, bad: list[str] | None = None, note: str | None = None):
    """保存反馈到本地文件。"""
    from pathlib import Path
    from src.utils.config_loader import DATA_DIR
    fb_path = DATA_DIR / "processed"
    fb_path.mkdir(parents=True, exist_ok=True)
    fb_file = fb_path / "keyword_feedback.json"

    history = load_feedback_history()
    if good:
        history["good"].extend(good)
        history["good"] = list(dict.fromkeys(history["good"]))[-30:]  # 去重，保留最近30条
    if bad:
        history["bad"].extend(bad)
        history["bad"] = list(dict.fromkeys(history["bad"]))[-30:]
    if note:
        history["notes"].append(note)
        history["notes"] = history["notes"][-20:]

    fb_file.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    logger.info(f"反馈已保存: good={len(history['good'])} bad={len(history['bad'])}")
