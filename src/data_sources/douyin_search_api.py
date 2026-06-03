"""抖音搜索 API 数据源。

原理：msToken（107 位随机字符串）+ Chrome cookies 即可调用抖音搜索 API，
无需 X-Bogus 签名。API 返回结构化视频数据（达人昵称、粉丝数、点赞/评论/分享等）。

不绕过登录、不破解验证码、不采集非公开信息。
"""
from __future__ import annotations

import random
import string
import time
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.data_sources.base import BaseDataSource
from src.utils.config_loader import load_env, seed_keywords_config, DATA_DIR
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()

_SEARCH_API = "https://www.douyin.com/aweme/v1/web/search/item/"
_USER_PROFILE_API = "https://www.douyin.com/aweme/v1/web/user/profile/other/"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 缓存 sec_uid → 用户详情，避免重复请求
_profile_cache: dict[str, dict] = {}


def _enrich_profile(session: requests.Session, sec_uid: str) -> dict:
    """调用户主页 API 补全 signature（简介）、联系方式等字段。结果缓存。"""
    if sec_uid in _profile_cache:
        return _profile_cache[sec_uid]

    try:
        r = session.get(_USER_PROFILE_API, params={
            "sec_user_id": sec_uid,
            "aid": "6383",
            "msToken": _generate_ms_token(),
        }, timeout=10)
        data = r.json()
        user = (data.get("user") or {}) if isinstance(data, dict) else {}
        result = {
            "signature": user.get("signature", ""),
            "nickname": user.get("nickname", ""),
            "follower_count": user.get("follower_count", 0),
            "total_favorited": user.get("total_favorited", 0),
            "aweme_count": user.get("aweme_count", 0),
            "enterprise_verify_reason": user.get("enterprise_verify_reason", ""),
            "custom_verify": user.get("custom_verify", ""),
        }
        _profile_cache[sec_uid] = result
        return result
    except Exception:
        return {}

def _generate_ms_token() -> str:
    """生成 107 位随机 msToken。"""
    chars = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(chars) for _ in range(107))


def _load_all_profile_cookies() -> list[tuple[str, dict[str, str]]]:
    """从本机 Chrome 所有 Profile 提取抖音 cookies，返回 [(profile_name, cookies), ...]"""
    import browser_cookie3
    from src.utils.config_loader import chrome_cookie_dirs

    results: list[tuple[str, dict[str, str]]] = []

    for db_path in chrome_cookie_dirs():
        try:
            raw = list(browser_cookie3.chrome(cookie_file=str(db_path)))
            profile_cookies = {c.name: c.value for c in raw if "douyin" in c.domain and c.value}
            if any("sessionid" in k for k in profile_cookies):
                results.append((db_path.parent.name, profile_cookies))
        except Exception:
            continue

    return results

    if not cookies:
        try:
            raw = list(browser_cookie3.chrome(domain_name="douyin.com"))
            cookies = {c.name: c.value for c in raw if c.value}
        except Exception:
            pass

    return cookies


class DouyinSearchAPI(BaseDataSource):
    """抖音搜索 API 数据源。"""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        keywords: list[str] | None = None,
    ):
        super().__init__(name, config)
        self._keywords: list[str] = keywords or []
        self._cookies: dict[str, str] | None = None
        self._session: requests.Session | None = None

    def _ensure_session(self) -> requests.Session:
        if self._session is None:
            self._cookies = _load_cookies()
            self._session = requests.Session()
            self._session.cookies.update(self._cookies or {})
            self._session.headers.update({
                "User-Agent": _USER_AGENT,
                "Referer": "https://www.douyin.com/",
            })
            # 先访问首页激活 session
            try:
                self._session.get("https://www.douyin.com/", timeout=10)
            except Exception:
                pass
        return self._session

    def fetch(self) -> list[dict[str, Any]]:
        global _profile_cache
        _profile_cache = {}  # 每次运行清缓存

        if not self.enabled:
            return []

        if not self._keywords:
            self._keywords = list(
                seed_keywords_config().get("seed_keywords", []) or []
            )
        if not self._keywords:
            logger.warning(f"[{self.name}] 无关键词")
            return []

        # 从所有 Chrome Profile 中提取 cookies
        all_profiles = _load_all_profile_cookies()
        if not all_profiles:
            logger.warning(
                f"[{self.name}] 无法获取 Chrome cookies，请先登录抖音网页版"
            )
            return []

        rows: list[dict[str, Any]] = []
        seen_videos: set[str] = set()
        working_profile = None

        # 逐个 profile 试，找到第一个能搜出结果的
        for profile_name, cookies in all_profiles:
            logger.info(f"[{self.name}] 尝试 Profile: {profile_name} ({len(cookies)} cookies)")
            session = self._make_session(cookies)

            test_batch, is_verify = self._search_keyword(session, self._keywords[0])
            if is_verify or len(test_batch) == 0:
                logger.info(f"[{self.name}] Profile {profile_name} 验证失败，尝试下一个...")
                continue

            working_profile = profile_name
            logger.info(f"[{self.name}] ✅ Profile {profile_name} 可用！")
            for r in test_batch:
                vid = r.get("视频链接", "")
                if vid and vid not in seen_videos:
                    seen_videos.add(vid)
                    rows.append(r)
            break

        if not working_profile:
            logger.warning(f"[{self.name}] 所有 Profile 均触发验证，本次无数据")
            return []

        # 用已验证的 session 继续搜剩余关键词
        session = self._make_session(dict(all_profiles))  # hmm, need the right cookies
        # Re-create session with working profile
        for pn, ck in all_profiles:
            if pn == working_profile:
                session = self._make_session(ck)
                break

        consecutive_verify = 0
        for kw in self._keywords[1:]:  # 第一个关键词已经在测试时搜过了
            if consecutive_verify >= 5:
                logger.warning(f"[{self.name}] 连续 {consecutive_verify} 次触发验证，停止后续请求")
                break

            try:
                batch, is_verify = self._search_keyword(session, kw)
            except Exception as e:
                logger.warning(f"[{self.name}] 搜索失败 kw={kw}: {e}")
                continue

            if is_verify:
                consecutive_verify += 1
            else:
                consecutive_verify = 0

            for r in batch:
                vid = r.get("视频链接", "")
                if vid and vid not in seen_videos:
                    seen_videos.add(vid)
                    rows.append(r)
            logger.info(f"[{self.name}] kw={kw} → {len(batch)} 条")

            if len(batch) > 0:
                time.sleep(1.5)

        logger.info(f"[{self.name}] 完成，去重后 {len(rows)} 条 (Profile: {working_profile})")
        return rows

    @staticmethod
    def _make_session(cookies: dict[str, str]) -> requests.Session:
        """用 cookies 创建一个已认证的 requests session。"""
        session = requests.Session()
        session.cookies.update(cookies)
        session.headers.update({
            "User-Agent": _USER_AGENT,
            "Referer": "https://www.douyin.com/",
        })
        try:
            session.get("https://www.douyin.com/", timeout=10)
        except Exception:
            pass
        return session

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _search_keyword(self, session: requests.Session, keyword: str) -> tuple[list[dict[str, Any]], bool]:
        params = {
            "aid": "6383",
            "keyword": keyword,
            "count": 15,
            "offset": 0,
            "device_platform": "webapp",
            "msToken": _generate_ms_token(),
        }
        r = session.get(_SEARCH_API, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()

        nil_type = (
            data.get("search_nil_info", {}).get("search_nil_type", "")
            if isinstance(data.get("search_nil_info"), dict)
            else ""
        )
        if nil_type in ("verify_check", "captcha"):
            logger.warning(f"[{self.name}] 触发验证: {nil_type}")
            return [], True

        items = data.get("data") or []
        records: list[dict[str, Any]] = []
        enriched_sec_uids: set[str] = set()

        for item in items:
            aweme = (item.get("aweme_info") or {}) if isinstance(item, dict) else {}
            if not aweme:
                continue

            author = aweme.get("author") or {}
            stats = aweme.get("statistics") or {}
            aweme_id = str(aweme.get("aweme_id", ""))
            sec_uid = author.get("sec_uid", "")

            profile_url = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else ""
            video_url = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""

            # 补全达人简介：搜索 API 不返回 signature，需单独调用户主页 API
            signature = author.get("signature", "")
            follower_count = int(author.get("follower_count", 0))

            if sec_uid and sec_uid not in enriched_sec_uids and not signature:
                enriched_sec_uids.add(sec_uid)
                profile = _enrich_profile(session, sec_uid)
                if profile.get("signature"):
                    signature = profile["signature"]
                if profile.get("follower_count"):
                    follower_count = int(profile["follower_count"])
                if profile.get("nickname") and not author.get("nickname"):
                    author["nickname"] = profile["nickname"]

            rec = {
                "采集日期": today_str("%Y-%m-%d"),
                "数据来源": "douyin_api",
                "平台": "douyin",
                "搜索关键词": keyword,
                "达人昵称": author.get("nickname", ""),
                "达人ID": sec_uid or author.get("uid", ""),
                "达人主页链接": profile_url,
                "视频链接": video_url,
                "视频标题": aweme.get("desc", ""),
                "视频描述": aweme.get("desc", ""),
                "发布时间": str(aweme.get("create_time", "")),
                "点赞数": int(stats.get("digg_count", 0)),
                "评论数": int(stats.get("comment_count", 0)),
                "分享数": int(stats.get("share_count", 0)),
                "收藏数": int(stats.get("collect_count", 0)),
                "粉丝数": follower_count,
                "达人简介": signature,
                "原始文本": f"{aweme.get('desc','')} | {signature}",
                "链接类型": "视频",
                "提取状态": "成功" if signature else "部分成功",
                "缺失原因": "" if signature else "达人简介未获取（需单独调用用户主页API）",
                "raw_data": str(item),
            }
            records.append(rec)

        return records, False
