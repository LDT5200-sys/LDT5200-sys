"""反向评估：输入达人主页/视频链接或 ID，AI 深度分析是否适合龙牙。

支持三种模式：
1. 单个达人：贴链接 → 自动拉取数据 → AI 评分 + 合作建议
2. 批量评估：贴多个链接/ID → 逐个分析 → 排名输出
3. 竞品分析：分析达人为什么好、我们能不能挖
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests
from src.utils.config_loader import brand_profile, load_env, scoring_rules
from src.utils.logger import get_logger

logger = get_logger()

_USER_PROFILE_API = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
_USER_VIDEO_API = "https://www.douyin.com/aweme/v1/web/aweme/post/"

_ANALYSIS_PROMPT = """你是抖音男装投放策略助手，正在为「{brand}」评估外部达人。

品牌风格：{style}
主推产品：{products}
评分标准：{scoring}

请分析以下达人，输出 JSON：

{{
  "overall_score": 0-100 的综合评分,
  "content_fit_score": 内容匹配度 0-30,
  "content_fit_reason": "内容匹配度评分理由，50字",
  "data_performance_score": 数据表现 0-20,
  "data_performance_reason": "数据表现评分理由，50字",
  "creator_scale_score": 达人量级 0-15,
  "creator_scale_reason": "达人量级评分理由，50字",
  "cooperation_score": 合作可行性 0-15,
  "cooperation_reason": "合作可行性评分理由，50字",
  "reuse_score": 素材复用潜力 0-10,
  "reuse_reason": "素材复用潜力评分理由，50字",
  "risk_score": 风险扣分 0-10（0=无风险）,
  "risk_reason": "风险点，无则写无",
  "priority_level": "S/A/B/C/淘汰",
  "cooperation_suggestion": "报价评估/低价测试/置换/暂不建议",
  "recommended_product": "秘纤短袖/战术裤/功能外套/通勤机能服饰/户外防晒/待定",
  "recommend_reason": "综合推荐理由，150字以内",
  "competitor_analysis": "如果是竞品合作达人，分析其优势、我们能否挖、挖的成本评估，100字以内",
  "next_action": "下一步动作建议"
}}

只输出 JSON，不要额外文字。

【达人信息】
昵称：{nickname}
抖音号：{short_id}
粉丝数：{follower_count}
获赞总数：{total_favorited}
作品数：{aweme_count}
认证：{verify_info}
简介：{signature}

【最近视频（共 {video_count} 条）】
{video_list}
"""


def extract_sec_uid(input_str: str) -> str | None:
    """从各种输入格式中提取 sec_uid"""
    s = input_str.strip()
    # douyin.com/user/xxx
    m = re.search(r'douyin\.com/user/([A-Za-z0-9_\-]+)', s)
    if m: return m.group(1)
    # 纯 sec_uid 格式
    if re.match(r'^MS4wLjAB[A-Za-z0-9_\-]{20,}$', s):
        return s
    return None


def fetch_creator_profile(sec_uid: str) -> dict:
    """通过 API 获取达人主页信息"""
    cookies = _load_cookies()
    if not cookies:
        return {"error": "无法获取Chrome登录态，请先登录抖音网页版"}

    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.douyin.com/",
    })

    # 生成 msToken
    import random, string
    ms_token = ''.join(random.choice(string.ascii_letters + string.digits + "-_") for _ in range(107))

    r = session.get(_USER_PROFILE_API, params={
        "sec_user_id": sec_uid, "aid": "6383", "msToken": ms_token,
    }, timeout=15)
    if r.status_code != 200:
        return {"error": f"API返回 {r.status_code}"}

    data = r.json()
    user = data.get("user", {})
    if not user:
        return {"error": "未找到该达人，请检查链接/ID是否正确"}

    return {
        "nickname": user.get("nickname", ""),
        "sec_uid": user.get("sec_uid", sec_uid),
        "short_id": user.get("short_id", ""),
        "signature": user.get("signature", ""),
        "follower_count": user.get("follower_count", 0),
        "total_favorited": user.get("total_favorited", 0),
        "aweme_count": user.get("aweme_count", 0),
        "avatar": user.get("avatar_thumb", {}).get("url_list", [""])[0],
        "verify_info": user.get("custom_verify", "") or user.get("enterprise_verify_reason", ""),
        "profile_url": f"https://www.douyin.com/user/{sec_uid}",
    }


def fetch_recent_videos(sec_uid: str, count: int = 5) -> list[dict]:
    """获取达人最近视频"""
    cookies = _load_cookies()
    if not cookies:
        return []

    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": f"https://www.douyin.com/user/{sec_uid}",
    })

    import random, string
    ms_token = ''.join(random.choice(string.ascii_letters + string.digits + "-_") for _ in range(107))

    try:
        r = session.get(_USER_VIDEO_API, params={
            "sec_user_id": sec_uid, "count": count, "max_cursor": 0, "aid": "6383",
            "msToken": ms_token,
        }, timeout=15)
        data = r.json()
        aweme_list = data.get("aweme_list", [])
        videos = []
        for a in aweme_list[:count]:
            stats = a.get("statistics", {})
            videos.append({
                "desc": (a.get("desc", "") or "")[:100],
                "likes": stats.get("digg_count", 0),
                "comments": stats.get("comment_count", 0),
                "shares": stats.get("share_count", 0),
                "video_url": f"https://www.douyin.com/video/{a.get('aweme_id', '')}",
                "create_time": a.get("create_time", 0),
            })
        return videos
    except Exception as e:
        logger.warning(f"获取视频失败: {e}")
        return []


def analyze_creator(input_str: str) -> dict[str, Any]:
    """综合分析单个达人"""
    sec_uid = extract_sec_uid(input_str)
    if not sec_uid:
        return {"error": "无法识别达人链接/ID，请提供 douyin.com/user/xxx 或 sec_uid"}

    profile = fetch_creator_profile(sec_uid)
    if "error" in profile:
        return profile

    videos = fetch_recent_videos(sec_uid, count=8)
    profile["recent_videos"] = videos

    # 构建 AI 分析 prompt
    bp = brand_profile()
    rules = scoring_rules()

    # 构建评分标准摘要
    scoring_summary = json.dumps({
        "内容匹配(30分)": "女生测男装/女穿男装/男装测评/短袖测评/通勤户外/机能战术优先",
        "数据表现(20分)": "单条点赞2000+加分，缺数据给中性分",
        "达人量级(15分)": "5万以下置换测试/5-50万中腰部/50万以上评估成本",
        "合作可行性(15分)": "有主页/视频/公开联系方式/星图加分",
        "素材复用(10分)": "测评/试穿/反差/改造类加分",
        "风险扣分(最多10分)": "低俗/擦边/争议/刷量/营销号扣分",
    }, ensure_ascii=False)

    video_lines = []
    for i, v in enumerate(videos, 1):
        video_lines.append(
            f"{i}. {v['desc'][:80]} | 赞{v['likes']} 评{v['comments']} 分{v['shares']}"
        )
    video_text = "\n".join(video_lines) if video_lines else "（无法获取视频列表）"

    prompt = _ANALYSIS_PROMPT.format(
        brand=bp.get("brand_name", "龙牙战术服装"),
        style="、".join(bp.get("brand_style", [])),
        products="、".join(bp.get("main_products", [])),
        scoring=scoring_summary,
        nickname=profile.get("nickname", "未知"),
        short_id=profile.get("short_id", "未知"),
        follower_count=profile.get("follower_count", 0),
        total_favorited=profile.get("total_favorited", 0),
        aweme_count=profile.get("aweme_count", 0),
        verify_info=profile.get("verify_info", "无"),
        signature=profile.get("signature", "无"),
        video_count=len(videos),
        video_list=video_text,
    )

    # 调 AI
    try:
        from src.ai.llm_client import LLMClient
        llm = LLMClient(role="evaluate")
        if llm.usable:
            resp = llm.chat(prompt, system="你只输出 JSON 对象，不要额外文字")
            ai_result = _parse_json(resp)
            if ai_result:
                profile["ai_analysis"] = ai_result
                profile["ai_analysis"]["profile_url"] = profile["profile_url"]
                return profile
    except Exception as e:
        logger.warning(f"AI 分析失败: {e}")

    # AI 不可用时给规则评分
    profile["ai_analysis"] = _rule_based_analysis(profile, videos)
    profile["ai_analysis"]["profile_url"] = profile["profile_url"]
    return profile


def batch_analyze(inputs: list[str]) -> list[dict]:
    """批量分析多个达人，按评分排序"""
    results = []
    for inp in inputs:
        inp = inp.strip()
        if not inp:
            continue
        logger.info(f"分析: {inp[:60]}...")
        result = analyze_creator(inp)
        results.append(result)
        time.sleep(1)  # 避免请求过快
    # 按评分排序
    results.sort(
        key=lambda r: r.get("ai_analysis", {}).get("overall_score", 0),
        reverse=True,
    )
    return results


def _rule_based_analysis(profile: dict, videos: list[dict]) -> dict:
    """规则兜底评分"""
    sig = (profile.get("signature", "") or "").lower()
    followers = profile.get("follower_count", 0)

    # 内容匹配
    high_kw = ["测男装", "穿男装", "男装测评", "短袖测评", "通勤", "户外", "机能", "战术", "微胖", "穿搭"]
    low_kw = ["美妆", "母婴", "萌宠", "美食", "游戏"]
    hit_high = sum(1 for k in high_kw if k in sig)
    hit_low = sum(1 for k in low_kw if k in sig)
    content_fit = min(30, 10 + hit_high * 6 - hit_low * 8)

    # 数据表现
    max_likes = max((v.get("likes", 0) for v in videos), default=0)
    data_score = 10 if max_likes > 10000 else (8 if max_likes > 2000 else (6 if max_likes > 500 else 4))

    # 达人量级
    if followers >= 500000: scale = 10
    elif followers >= 50000: scale = 15
    elif followers >= 10000: scale = 12
    else: scale = 8

    # 合作可行性
    has_contact = any(k in sig for k in ["微信", "vx", "合作", "商务", "@", "邮箱", "星图"])
    coop = min(15, 5 + (3 if has_contact else 0))

    total = content_fit + data_score + scale + coop
    level = "S" if total >= 85 else ("A" if total >= 75 else ("B" if total >= 60 else "C"))

    return {
        "overall_score": total,
        "content_fit_score": content_fit,
        "data_performance_score": data_score,
        "creator_scale_score": scale,
        "cooperation_score": coop,
        "reuse_score": 5,
        "risk_score": 0,
        "priority_level": level,
        "cooperation_suggestion": "报价评估" if level == "S" else ("低价测试" if level == "A" else "置换"),
        "recommended_product": "待定",
        "recommend_reason": f"规则评分：内容{content_fit}/数据{data_score}/量级{scale}/合作{coop}",
        "competitor_analysis": "（AI 不可用，无竞品分析）",
        "next_action": "优先查看视频" if level in ("S","A") else "放入观察池",
    }


def _parse_json(s: str) -> dict | None:
    s = s.strip()
    if s.startswith("```"): s = s.strip("`")
    if s.lower().startswith("json"): s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1: return None
    try: return json.loads(s[start:end+1])
    except json.JSONDecodeError: return None


def _load_cookies() -> dict[str, str]:
    try:
        import browser_cookie3
        from src.utils.config_loader import chrome_cookie_dirs
        for db in chrome_cookie_dirs():
            try:
                cookies = list(browser_cookie3.chrome(cookie_file=str(db)))
                douyin = {c.name: c.value for c in cookies if "douyin" in c.domain and c.value}
                if any("sessionid" in k for k in douyin):
                    return douyin
            except Exception:
                continue
        return {}
    except Exception:
        return {}
