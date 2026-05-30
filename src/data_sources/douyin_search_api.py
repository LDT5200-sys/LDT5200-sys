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

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _generate_ms_token() -> str:
    """生成 107 位随机 msToken。"""
    chars = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(chars) for _ in range(107))


def _load_cookies() -> dict[str, str]:
    """从本机 Chrome 提取抖音 cookies。"""
    try:
        import browser_cookie3
        raw = list(browser_cookie3.chrome(domain_name="douyin.com"))
        return {c.name: c.value for c in raw if c.value}
    except Exception as e:
        logger.warning(f"无法提取 Chrome cookies: {e}")
        return {}


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
        if not self.enabled:
            return []

        if not self._keywords:
            self._keywords = list(
                seed_keywords_config().get("seed_keywords", []) or []
            )

        if not self._keywords:
            logger.warning(f"[{self.name}] 无关键词")
            return []

        session = self._ensure_session()
        if not self._cookies:
            logger.warning(
                f"[{self.name}] 无法获取 Chrome cookies，请先登录抖音网页版"
            )
            return []

        rows: list[dict[str, Any]] = []
        seen_videos: set[str] = set()
        consecutive_verify = 0

        for kw in self._keywords:
            # 触发验证次数过多时停止
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

            # 控制频率：每个关键词间隔 1-3 秒
            if len(batch) > 0:
                time.sleep(1 + (len(self._keywords) % 3) * 0.5)

        logger.info(f"[{self.name}] 完成，去重后 {len(rows)} 条")
        return rows

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
                "粉丝数": int(author.get("follower_count", 0)),
                "达人简介": author.get("signature", ""),
                "原始文本": f"{aweme.get('desc','')} | {author.get('signature','')}",
                "链接类型": "视频",
                "提取状态": "成功",
                "缺失原因": "点赞/评论等实时数据可能已变化",
                "raw_data": str(item),
            }
            records.append(rec)

        return records, False
