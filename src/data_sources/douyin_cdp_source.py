"""抖音 CDP 数据源：通过 Chrome DevTools Protocol 在浏览器页面内调搜索 API。

优势：浏览器自动生成 msToken/X-Bogus 签名，不会被 verify_check 拦截。
前提：Chrome 需以 --remote-debugging-port=9222 --remote-allow-origins=* 启动。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src.data_sources.base import BaseDataSource
from src.utils.config_loader import seed_keywords_config, DATA_DIR
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()

CDP_URL = "http://127.0.0.1:9222"


def _cdp_available() -> bool:
    import requests
    try:
        r = requests.get(f"{CDP_URL}/json/version", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


class DouyinCDPSource(BaseDataSource):
    """通过 CDP 连接真实 Chrome，在页面内调用抖音搜索 API。"""

    def __init__(self, name: str, config: dict[str, Any], keywords: list[str] | None = None):
        super().__init__(name, config)
        self._keywords = keywords or []

    def fetch(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        if not self._keywords:
            self._keywords = list(seed_keywords_config().get("seed_keywords", []) or [])

        if not _cdp_available():
            logger.warning(
                f"[{self.name}] CDP 不可用。请用以下命令重启 Chrome:\n"
                f'  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
                f'--remote-debugging-port=9222 "--remote-allow-origins=*" '
                f'--user-data-dir="/tmp/cdp-chrome-profile" &'
            )
            return []

        from playwright.sync_api import sync_playwright

        rows: list[dict[str, Any]] = []
        seen_vids: set[str] = set()

        try:
            # 直接连 CDP websocket，不走 Playwright 的 browser context 管理
            import requests as req
            import websocket as wsc
            tabs = req.get(f"{CDP_URL}/json").json()
            # 没有页面就创建一个
            if not tabs:
                req.put(f"{CDP_URL}/json/new?https://www.douyin.com/", timeout=5)
                time.sleep(3)
                tabs = req.get(f"{CDP_URL}/json").json()
            # 找 douyin 页面
            ws_url = None
            for t in tabs:
                if 'douyin.com' in t.get('url', '') and t.get('type') == 'page':
                    ws_url = t['webSocketDebuggerUrl']
                    break
            if not ws_url and tabs:
                ws_url = tabs[0]['webSocketDebuggerUrl']

            ws = wsc.create_connection(ws_url, timeout=10)
            _cdp_send(ws, "Runtime.enable")

            # 如果不在 douyin 页面，先导航
            cur_url = ""
            for t in tabs:
                if t.get('webSocketDebuggerUrl') == ws_url:
                    cur_url = t.get('url', '')
                    break
            if 'douyin.com' not in cur_url:
                _cdp_send(ws, "Page.enable")
                _cdp_send(ws, "Page.navigate", {"url": "https://www.douyin.com/"})
                time.sleep(3)

            for kw in self._keywords:
                try:
                    batch = _search_via_ws(ws, kw)
                except Exception as e:
                    logger.warning(f"[{self.name}] kw={kw} CDP异常: {e}")
                    continue

                for r in batch:
                    vid = r.get("视频链接", "")
                    if vid and vid not in seen_vids:
                        seen_vids.add(vid)
                        rows.append(r)
                logger.info(f"[{self.name}] kw={kw} → {len(batch)} 条")
                time.sleep(0.3)

            ws.close()
        except Exception as e:
            logger.error(f"[{self.name}] CDP 连接失败: {e}")

        logger.info(f"[{self.name}] 完成，去重后 {len(rows)} 条")
        return rows


def _cdp_send(ws, method: str, params: dict | None = None, timeout: float = 10) -> dict:
    """通过 websocket 发送 CDP 命令并等待结果"""
    import json as _json
    mid = int(time.time() * 1000) % 1000000
    ws.send(_json.dumps({"id": mid, "method": method, "params": params or {}}))
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            resp = _json.loads(ws.recv())
            if resp.get("id") == mid:
                return resp.get("result", {})
        except Exception:
            time.sleep(0.1)
    return {}


def _search_via_ws(ws, keyword: str) -> list[dict[str, Any]]:
    """通过 CDP websocket 在页面内调搜索 API"""
    result = _cdp_send(ws, "Runtime.evaluate", {
        "expression": f"""
        (async () => {{
            const r = await fetch('/aweme/v1/web/search/item/?keyword={keyword}&count=15&aid=6383',
                {{credentials:'include'}});
            const d = await r.json();
            const items = d.data || [];
            return JSON.stringify(items.map(i => {{
                const a = i.aweme_info || {{}};
                const au = a.author || {{}};
                const st = a.statistics || {{}};
                return {{
                    kw: '{keyword}',
                    nickname: au.nickname||'',
                    sec_uid: au.sec_uid||'',
                    uid: au.uid||'',
                    signature: au.signature||'',
                    follower_count: au.follower_count||0,
                    aweme_id: a.aweme_id||'',
                    aweme_desc: (a.desc||'').substring(0,500),
                    create_time: a.create_time||0,
                    digg_count: st.digg_count||0,
                    comment_count: st.comment_count||0,
                    share_count: st.share_count||0,
                    collect_count: st.collect_count||0,
                }};
            }}));
        }})()
        """,
        "returnByValue": True,
        "awaitPromise": True,
    })

    val = result.get("result", {}).get("value", "{}")
    items = json.loads(val) if isinstance(val, str) else (val or [])

    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sec_uid = item.get("sec_uid", "")
        aweme_id = str(item.get("aweme_id", ""))
        profile_url = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else ""
        video_url = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""
        signature = item.get("signature", "")
        follower_count = int(item.get("follower_count", 0))

        if sec_uid and not signature and len(records) < 3:
            # 只对每关键词前3条补全简介，节省时间
            profile = _enrich_profile_ws(ws, sec_uid)
            signature = profile.get("signature", "")
            if profile.get("follower_count"):
                follower_count = int(profile["follower_count"])

        desc = item.get("aweme_desc", "")

        records.append({
            "采集日期": today_str("%Y-%m-%d"),
            "数据来源": "douyin_cdp",
            "平台": "douyin",
            "搜索关键词": item.get("kw", keyword),
            "达人昵称": item.get("nickname", ""),
            "达人ID": sec_uid or item.get("uid", ""),
            "达人主页链接": profile_url,
            "视频链接": video_url,
            "视频标题": desc,
            "视频描述": desc,
            "发布时间": str(item.get("create_time", "")),
            "点赞数": int(item.get("digg_count", 0)),
            "评论数": int(item.get("comment_count", 0)),
            "分享数": int(item.get("share_count", 0)),
            "收藏数": int(item.get("collect_count", 0)),
            "粉丝数": follower_count,
            "达人简介": signature,
            "原始文本": f"{desc} | {signature}",
            "链接类型": "视频",
            "提取状态": "成功" if signature else "部分成功",
            "缺失原因": "" if signature else "简介需调用户主页API补全",
        })

    return records


def _enrich_profile_ws(ws, sec_uid: str) -> dict:
    if sec_uid in _profile_cache:
        return _profile_cache[sec_uid]
    try:
        result = _cdp_send(ws, "Runtime.evaluate", {
            "expression": f"""
            (async () => {{
                const r = await fetch('/aweme/v1/web/user/profile/other/?sec_user_id={sec_uid}&aid=6383',
                    {{credentials:'include'}});
                const d = await r.json();
                const u = d.user || {{}};
                return JSON.stringify({{
                    signature: u.signature||'',
                    follower_count: u.follower_count||0,
                    nickname: u.nickname||'',
                }});
            }})()
            """,
            "returnByValue": True,
            "awaitPromise": True,
        })
        val = result.get("result", {}).get("value", "{}")
        data = json.loads(val) if isinstance(val, str) else val
        _profile_cache[sec_uid] = data
        return data
    except Exception:
        return {}


def _search_via_page(page, keyword: str) -> list[dict[str, Any]]:
    result = page.evaluate(f"""
    async () => {{
        const r = await fetch('/aweme/v1/web/search/item/?keyword={keyword}&count=15&aid=6383',
            {{credentials:'include'}});
        const d = await r.json();
        const items = d.data || [];
        return JSON.stringify(items.map(i => {{
            const a = i.aweme_info || {{}};
            const au = a.author || {{}};
            const st = a.statistics || {{}};
            return {{
                kw: '{keyword}',
                nickname: au.nickname||'',
                sec_uid: au.sec_uid||'',
                uid: au.uid||'',
                signature: au.signature||'',
                follower_count: au.follower_count||0,
                aweme_id: a.aweme_id||'',
                aweme_desc: (a.desc||'').substring(0,500),
                create_time: a.create_time||0,
                digg_count: st.digg_count||0,
                comment_count: st.comment_count||0,
                share_count: st.share_count||0,
                collect_count: st.collect_count||0,
            }};
        }}));
    }}
    """)

    items = json.loads(result) if isinstance(result, str) else result
    records = []

    for item in items:
        if not isinstance(item, dict):
            continue
        sec_uid = item.get("sec_uid", "")
        aweme_id = str(item.get("aweme_id", ""))
        profile_url = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else ""
        video_url = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""
        signature = item.get("signature", "")
        follower_count = int(item.get("follower_count", 0))

        # 补全用户主页详情（搜索 API 不返 signature，单独调）
        if sec_uid and not signature:
            profile = _enrich_profile_cdp(page, sec_uid)
            signature = profile.get("signature", "")
            if profile.get("follower_count"):
                follower_count = int(profile["follower_count"])

        desc = item.get("aweme_desc", "")

        records.append({
            "采集日期": today_str("%Y-%m-%d"),
            "数据来源": "douyin_cdp",
            "平台": "douyin",
            "搜索关键词": item.get("kw", keyword),
            "达人昵称": item.get("nickname", ""),
            "达人ID": sec_uid or item.get("uid", ""),
            "达人主页链接": profile_url,
            "视频链接": video_url,
            "视频标题": desc,
            "视频描述": desc,
            "发布时间": str(item.get("create_time", "")),
            "点赞数": int(item.get("digg_count", 0)),
            "评论数": int(item.get("comment_count", 0)),
            "分享数": int(item.get("share_count", 0)),
            "收藏数": int(item.get("collect_count", 0)),
            "粉丝数": follower_count,
            "达人简介": signature,
            "原始文本": f"{desc} | {signature}",
            "链接类型": "视频",
            "提取状态": "成功" if signature else "部分成功",
            "缺失原因": "" if signature else "简介需调用户主页API补全",
        })

    return records


_profile_cache: dict[str, dict] = {}

def _enrich_profile_cdp(page, sec_uid: str) -> dict:
    if sec_uid in _profile_cache:
        return _profile_cache[sec_uid]
    try:
        result = page.evaluate(f"""
        async () => {{
            const r = await fetch('/aweme/v1/web/user/profile/other/?sec_user_id={sec_uid}&aid=6383',
                {{credentials:'include'}});
            const d = await r.json();
            const u = d.user || {{}};
            return JSON.stringify({{
                signature: u.signature||'',
                follower_count: u.follower_count||0,
                nickname: u.nickname||'',
            }});
        }}
        """)
        data = json.loads(result) if isinstance(result, str) else result
        _profile_cache[sec_uid] = data
        return data
    except Exception:
        return {}
