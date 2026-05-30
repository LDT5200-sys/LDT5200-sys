"""用 OpenClaw Browser 采集抖音搜索结果。

前提：
  - OpenClaw browser 已启动（openclaw browser start）
  - 你本机 Chrome 已登录抖音

用法：
  python scripts/douyin_openclaw_collect.py "女生测男装"
  python scripts/douyin_openclaw_collect.py --all

输出：data/input/douyin/douyin_ocl_YYYYMMDD_HHMMSS.csv
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "data" / "input" / "douyin"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OPENCLAW = "/Users/tiexue/.stepclaw/bin/openclaw"


def run_ocl(*args, timeout: int = 20) -> str:
    """Run an openclaw browser command and return stdout."""
    cmd = [OPENCLAW, "browser"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def get_cookies_from_chrome() -> list[dict]:
    """Extract Douyin cookies from local Chrome."""
    import browser_cookie3
    cookies = list(browser_cookie3.chrome(domain_name='douyin.com'))
    return [{"name": c.name, "value": c.value, "domain": c.domain,
             "path": c.path, "secure": bool(c.secure)}
            for c in cookies if c.value]


def search_keyword(keyword: str, cookies: list[dict], max_results: int = 15) -> list[dict]:
    """Search Douyin for a keyword and extract video data."""

    # Step 1: Navigate to douyin.com first, set cookies via evaluate
    print(f"\n[ocl] === {keyword} ===")
    run_ocl("navigate", "https://www.douyin.com", timeout=25)
    time.sleep(3)

    # Step 2: Inject all cookies via JS (one evaluate call for all)
    cookie_js_parts = []
    for c in cookies:
        if c['name'].startswith('__sec') or len(c['value']) > 500:
            continue  # skip security tokens and huge values
        val = c['value'].replace("\\", "\\\\").replace("'", "\\'").replace("\n", "")
        domain = c['domain']
        cookie_js_parts.append(
            f"document.cookie = '{c['name']}={val}; domain={domain}; path=/';"
        )

    inject_js = f"() => {{ try {{ {' '.join(cookie_js_parts[:20])}; return 'ok'; }} catch(e) {{ return e.message; }} }}"
    result = run_ocl("evaluate", "--fn", inject_js, timeout=10)
    print(f"  cookie inject: {'ok' if 'ok' in result else result[:100]}")

    # Step 3: Navigate to search page
    from urllib.parse import quote
    search_url = f"https://www.douyin.com/search/{quote(keyword)}?type=general"
    run_ocl("navigate", search_url, timeout=25)
    time.sleep(6)

    # Step 4: Scroll to load more results
    for i in range(5):
        run_ocl("evaluate", "--fn", "() => window.scrollBy(0, 1200)", timeout=5)
        time.sleep(1.5)

    # Step 5: Extract video data via evaluate
    extract_js = """() => {
        const results = [];
        // Find all links containing /video/
        const links = document.querySelectorAll('a[href*="/video/"]');
        const seen = new Set();
        links.forEach(a => {
            const href = a.getAttribute('href');
            const match = href && href.match(/\\/video\\/(\\d+)/);
            if (!match || seen.has(match[1])) return;
            seen.add(match[1]);

            // Try to find parent container text
            const parent = a.closest('li') || a.closest('[class*="search"]') || a.parentElement;
            const text = parent ? parent.innerText : a.innerText;
            const lines = text.split('\\n').filter(l => l.trim());

            results.push({
                video_id: match[1],
                video_url: 'https://www.douyin.com/video/' + match[1],
                title: lines[0] || '',
                description: lines.slice(1, 4).join(' | '),
                text: text.substring(0, 500)
            });
        });
        return {count: results.length, results: results.slice(0, 20)};
    }"""

    result = run_ocl("evaluate", "--fn", extract_js, timeout=15)
    print(f"  extract raw: {result[:300]}...")

    # Parse the JSON result
    try:
        # Find the JSON object in the output
        match = re.search(r'\{.*"count".*\}', result, re.DOTALL)
        if match:
            data = json.loads(match.group())
            print(f"  found {data.get('count', 0)} video cards")
            items = data.get('results', [])
        else:
            items = []
            print("  no JSON found in output")
    except json.JSONDecodeError:
        items = []
        print("  JSON parse failed")

    # Step 6: Build standard records
    records = []
    for item in items:
        records.append({
            "搜索关键词": keyword,
            "达人昵称": "",
            "视频链接": item.get("video_url", ""),
            "视频标题": item.get("title", ""),
            "视频描述": item.get("description", ""),
            "数据来源": "douyin_ocl",
            "平台": "douyin",
            "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "提取状态": "部分成功",
            "缺失原因": "OpenClaw自动采集，仅获取搜索卡片摘要",
        })

    return records


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("keyword", nargs="?", help="搜索关键词")
    p.add_argument("--all", action="store_true", help="全部种子关键词")
    p.add_argument("--max", type=int, default=15)
    args = p.parse_args()

    # Get cookies
    print("[ocl] 从 Chrome 提取 cookies...")
    try:
        cookies = get_cookies_from_chrome()
        print(f"[ocl] 提取了 {len(cookies)} 个 cookie")
    except Exception as e:
        print(f"[ocl] Cookie 提取失败: {e}")
        print("[ocl] 请确保 Chrome 已安装且登录了抖音")
        return

    # Determine keywords
    if args.all:
        from src.utils.config_loader import seed_keywords_config
        seeds = seed_keywords_config().get("seed_keywords", [])[:8]
        keywords = seeds
    elif args.keyword:
        keywords = [args.keyword]
    else:
        p.print_help()
        return

    # Collect
    all_records = []
    for kw in keywords:
        try:
            recs = search_keyword(kw, cookies, max_results=args.max)
            all_records.extend(recs)
            time.sleep(2)
        except Exception as e:
            print(f"[ocl] {kw} 失败: {e}")

    # Save CSV
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"douyin_ocl_{ts}.csv"

    if all_records:
        import csv
        fieldnames = list(all_records[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_records)
    else:
        path.touch()

    print(f"\n[ocl] ✅ {len(all_records)} 条 → {path}")


if __name__ == "__main__":
    main()
