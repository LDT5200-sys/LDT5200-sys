"""通过 CDP (Chrome DevTools Protocol) 在真实浏览器中调用抖音搜索 API。

原理：浏览器加载 douyin.com 后，页面 JS 会自动生成 msToken/X-Bogus 等签名参数。
我们在页面上下文里直接 fetch 抖音搜索 API，浏览器自动附带签名。

用法：
  python scripts/douyin_api_collect.py "女生测男装"
  python scripts/douyin_api_collect.py --all
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import websocket

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "data" / "input" / "douyin"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CDP_URL = "http://127.0.0.1:31010"


def cdp_call(ws, method: str, params: dict | None = None) -> dict:
    """Send a CDP command and return the result."""
    msg_id = int(time.time() * 1000) % 1000000
    payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})
    ws.send(payload)
    # Simple sync receive - read until we get our response
    timeout = time.time() + 15
    while time.time() < timeout:
        try:
            resp = json.loads(ws.recv())
            if resp.get("id") == msg_id:
                return resp.get("result", {})
            # For events (no id), we ignore them
        except Exception:
            time.sleep(0.1)
    return {}


def search_keyword_cdp(keyword: str, max_results: int = 15) -> list[dict]:
    """Use CDP to navigate to Douyin then call the search API from the page context."""

    # Get WebSocket URL from CDP HTTP endpoint
    import requests
    try:
        r = requests.get(f"{CDP_URL}/json/version", timeout=5)
        ws_url = r.json().get("webSocketDebuggerUrl", "")
    except Exception:
        # Fallback
        r = requests.get(f"{CDP_URL}/json", timeout=5)
        pages = r.json()
        ws_url = pages[0].get("webSocketDebuggerUrl", "") if pages else ""

    if not ws_url:
        print("[cdp] 找不到 websocket URL，请确认 OpenClaw browser 已启动")
        return []

    print(f"[cdp] 连接 CDP websocket...")
    ws = websocket.create_connection(ws_url, timeout=15)

    # Navigate to douyin.com to initialize msToken etc.
    print(f"[cdp] 导航到 douyin.com...")
    cdp_call(ws, "Page.enable")
    cdp_call(ws, "Runtime.enable")

    # Navigate
    nav_result = cdp_call(ws, "Page.navigate", {"url": "https://www.douyin.com/"})
    print(f"[cdp] 导航结果: {nav_result.get('frameId','?')[:20]}...")

    # Wait for page load
    time.sleep(5)

    # Call the search API from within the page context
    print(f"[cdp] 搜索: {keyword}")
    js_code = f"""
    (async () => {{
        try {{
            const url = '/aweme/v1/web/search/item/?keyword={keyword}&count={max_results}&aid=6383';
            const resp = await fetch(url, {{ credentials: 'include' }});
            const data = await resp.json();
            const results = [];
            const items = data.data || [];
            for (const item of items) {{
                const a = item.aweme_info || {{}};
                const au = a.author || {{}};
                results.push({{
                    nickname: au.nickname || '',
                    sec_uid: au.sec_uid || '',
                    uid: au.uid || '',
                    follower_count: au.follower_count || 0,
                    signature: (au.signature || '').substring(0, 200),
                    desc: (a.desc || '').substring(0, 300),
                    aweme_id: a.aweme_id || '',
                    create_time: a.create_time || 0,
                    digg_count: (a.statistics || {{}}).digg_count || 0,
                    comment_count: (a.statistics || {{}}).comment_count || 0,
                    share_count: (a.statistics || {{}}).share_count || 0,
                    collect_count: (a.statistics || {{}}).collect_count || 0,
                    video_play_count: (a.statistics || {{}}).play_count || 0,
                }});
            }}
            return JSON.stringify({{ count: results.length, results: results }});
        }} catch(e) {{
            return JSON.stringify({{ error: e.message }});
        }}
    }})()
    """

    eval_result = cdp_call(ws, "Runtime.evaluate", {
        "expression": js_code,
        "returnByValue": True,
        "awaitPromise": True,
        "timeout": 15000,
    })

    ws.close()

    # Parse the result
    result_value = eval_result.get("result", {}).get("value", "{}")
    if isinstance(result_value, str):
        try:
            parsed = json.loads(result_value)
        except json.JSONDecodeError:
            print(f"[cdp] JSON 解析失败: {result_value[:200]}")
            return []
    else:
        parsed = result_value

    if "error" in parsed:
        print(f"[cdp] JS 错误: {parsed['error']}")
        return []

    items = parsed.get("results", [])
    print(f"[cdp] 返回 {len(items)} 条")

    records = []
    for item in items:
        profile_url = f"https://www.douyin.com/user/{item['sec_uid']}" if item.get("sec_uid") else ""
        video_url = f"https://www.douyin.com/video/{item['aweme_id']}" if item.get("aweme_id") else ""
        records.append({
            "搜索关键词": keyword,
            "达人昵称": item.get("nickname", ""),
            "达人ID": item.get("sec_uid", "") or item.get("uid", ""),
            "达人主页链接": profile_url,
            "视频链接": video_url,
            "视频标题": item.get("desc", ""),
            "视频描述": item.get("desc", ""),
            "点赞数": item.get("digg_count", 0),
            "评论数": item.get("comment_count", 0),
            "分享数": item.get("share_count", 0),
            "收藏数": item.get("collect_count", 0),
            "粉丝数": item.get("follower_count", 0),
            "达人简介": item.get("signature", ""),
            "数据来源": "douyin_api_cdp",
            "平台": "douyin",
            "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "提取状态": "成功" if item.get("nickname") else "部分成功",
            "缺失原因": "" if item.get("nickname") else "API 返回数据中缺少达人昵称",
        })

    return records


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("keyword", nargs="?", help="搜索关键词")
    p.add_argument("--all", action="store_true")
    p.add_argument("--max", type=int, default=15)
    args = p.parse_args()

    if args.all:
        from src.utils.config_loader import seed_keywords_config
        keywords = seed_keywords_config().get("seed_keywords", [])[:8]
    elif args.keyword:
        keywords = [args.keyword]
    else:
        p.print_help()
        return

    all_records = []
    for kw in keywords:
        recs = search_keyword_cdp(kw, max_results=args.max)
        all_records.extend(recs)
        time.sleep(1)
        if recs:
            print(f"  {kw}: {len(recs)} 条, 示例: {recs[0].get('达人昵称','?')}")

    # Save CSV
    import csv
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"douyin_api_{ts}.csv"
    if all_records:
        fieldnames = list(all_records[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_records)
    else:
        path.touch()
    print(f"\n[cdp] ✅ {len(all_records)} 条 → {path}")


if __name__ == "__main__":
    main()
