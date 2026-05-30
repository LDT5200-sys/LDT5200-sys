"""自动采集抖音搜索结果：复用你本机 Chrome 的登录态（cookie），无需重复登录。

用法：
    cd /Users/tiexue/Desktop/claude-code-test/creator_finder
    source .venv/bin/activate

    # 搜索单个关键词
    python scripts/douyin_auto_collect.py "女生测男装"

    # 自动搜索全部种子关键词
    python scripts/douyin_auto_collect.py --all

    # 控制数量
    python scripts/douyin_auto_collect.py "男装测评" --max 30

原理：
    读取你 Mac 上 ~/Library/Application Support/Google/Chrome 的 Cookies 文件，
    复制到临时目录后启动无头 Chrome，复用抖音登录态。
    Chrome 不能同时在运行（文件锁），脚本会自动提示。

输出：
    data/input/douyin/douyin_auto_YYYYMMDD_HHMMSS.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "data" / "input" / "douyin"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CHROME_BASE = Path.home() / "Library/Application Support/Google/Chrome"


def _find_cookie_dbs() -> list[Path]:
    """搜索 Chrome 所有 Profile 目录下的 Cookies 文件。"""
    if not CHROME_BASE.exists():
        return []
    # 包括 Default, Profile 1, Profile 2, ... 等
    paths = sorted(CHROME_BASE.glob("*/Cookies"), key=lambda p: p.stat().st_size, reverse=True)
    return paths


def _get_douyin_cookies() -> list[dict]:
    """从本机 Chrome Cookies SQLite 数据库提取抖音域名 cookies。"""
    db_paths = _find_cookie_dbs()
    if not db_paths:
        print(f"[cookie] Chrome Cookies 文件不存在: {CHROME_BASE}")
        print("[cookie] 请确认 Chrome 已安装且登录过抖音")
        return []

    # 优先选最大的 Cookies 文件（更可能是主力 Profile）
    cookies_db = db_paths[0]
    print(f"[cookie] 从 {cookies_db.relative_to(CHROME_BASE)} 读取 cookies ({cookies_db.stat().st_size} bytes)")

    # Chrome 运行时会锁 Cookies，复制到临时文件
    tmp_db = tempfile.mktemp(suffix=".sqlite")
    try:
        shutil.copy2(cookies_db, tmp_db)
    except PermissionError:
        print("[cookie] ⚠️  Chrome 正在运行，请先关闭 Chrome，然后重试")
        print("[cookie] 或者运行: python scripts/douyin_auto_collect.py --login")
        return []

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT host_key, name, value, encrypted_value FROM cookies "
        "WHERE host_key LIKE '%douyin%'"
    ).fetchall()
    conn.close()
    os.unlink(tmp_db)

    cookies = []
    for r in rows:
        c = {"name": r["name"], "value": r["value"], "domain": r["host_key"]}
        # 如果是加密的 (macOS Keychain)，value 为空但 encrypted_value 非空
        if not c["value"] and r["encrypted_value"]:
            c["value"] = "__ENCRYPTED__"
        cookies.append(c)

    print(f"[cookie] 从 Chrome 提取了 {len(cookies)} 个抖音 cookies")
    if any(c["value"] == "__ENCRYPTED__" for c in cookies):
        print("[cookie] ⚠️  部分 cookie 被 macOS Keychain 加密，Playwright 可能无法直接使用")
        print("[cookie] 备选方案：用非 headless 模式打开浏览器登录一次 → 自动保存状态")
    return cookies


def _save_cookies_json(cookies: list[dict]) -> Path:
    """保存 cookies 为 Playwright 兼容格式。"""
    pw_cookies = []
    for c in cookies:
        if c["value"] == "__ENCRYPTED__":
            continue
        pw_cookies.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        })
    path = OUTPUT_DIR / "douyin_cookies.json"
    path.write_text(json.dumps(pw_cookies, ensure_ascii=False, indent=2))
    print(f"[cookie] 保存 {len(pw_cookies)} 条可用 cookie 到 {path}")
    return path


def search_keyword(keyword: str, max_results: int = 20, cookies_path: Path | None = None) -> list[dict]:
    """用 Playwright 无头 Chrome 搜索抖音，复用已有 cookie。"""

    from playwright.sync_api import sync_playwright

    search_url = f"https://www.douyin.com/search/{keyword}?type=general"
    results = []

    print(f"\n[auto] === 搜索: {keyword} ===")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context_kwargs: dict = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "zh-CN",
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        # 如果有 cookies 文件，加载
        if cookies_path and cookies_path.exists():
            saved = json.loads(cookies_path.read_text())
            cookie_list = saved.get("cookies", saved) if isinstance(saved, dict) else saved
            if cookie_list:
                # 修复 secure 字段类型
                for c in (cookie_list if isinstance(cookie_list, list) else []):
                    if isinstance(c.get("secure"), int):
                        c["secure"] = bool(c["secure"])
                context_kwargs["storage_state"] = str(cookies_path)
                print(f"[auto] 加载 {len(cookie_list) if isinstance(cookie_list, list) else '?'} 条 cookie")

        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)

            # 检测登录状态
            html = page.content()
            logged_in = "请登录" not in html and "验证" not in html[:3000]

            if not logged_in:
                print("[auto] ⚠️  Cookie 已过期或无效，需要重新登录")
                print("[auto] 请运行: python scripts/douyin_auto_collect.py --login")
                page.screenshot(path=str(OUTPUT_DIR / f"need_login_{int(time.time())}.png"))
                browser.close()
                return results

            print("[auto] ✅ 已登录，开始滚动加载...")

            # 滚动加载
            prev_count = 0
            for i in range(min(max_results // 3, 15)):
                page.evaluate("window.scrollBy(0, 1000)")
                time.sleep(2)
                cards = page.query_selector_all(
                    'a[href*="/video/"], [data-e2e="search-video-item"], '
                    '.search-result-card, [class*="search-result"]'
                )
                cur = len(cards)
                if cur == prev_count and i > 3:
                    break
                prev_count = cur
            print(f"[auto] 检测到 ~{prev_count} 个卡片")

            # ---- 提取数据 ----
            # 从页面 DOM 直接提取视频卡片
            card_selector = 'a[href*="/video/"]'
            video_links = page.query_selector_all(card_selector)

            seen = set()
            for a_tag in video_links[:max_results]:
                href = a_tag.get_attribute("href") or ""
                m = re.search(r'/video/(\d+)', href)
                if not m:
                    continue
                vid = m.group(1)
                if vid in seen:
                    continue
                seen.add(vid)

                video_url = f"https://www.douyin.com/video/{vid}"
                text = ""
                try:
                    # 往上找父容器，拿到完整卡片文本
                    parent = a_tag.evaluate("el => el.closest('[class*=\"search\"]') || el.closest('li') || el.parentElement")
                    if parent:
                        text = page.evaluate("el => el ? el.innerText : ''", parent) or ""
                    else:
                        text = a_tag.inner_text()
                except Exception:
                    text = a_tag.inner_text() if hasattr(a_tag, 'inner_text') else ""

                lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 1]
                title = lines[0] if lines else ""
                creator = ""
                likes = ""

                # 找含 "粉丝" / "点赞" 的行
                for line in lines:
                    if "粉丝" in line or "获赞" in line:
                        creator = line
                    if re.search(r'[\d.]+[万w]?\s*(赞|点赞)', line):
                        likes = line

                # creator 可能藏在前几行
                if not creator and len(lines) > 1:
                    for line in lines[1:4]:
                        if len(line) < 30 and not re.match(r'^[\d.]+[万w]?$', line):
                            creator = line
                            break

                results.append({
                    "搜索关键词": keyword,
                    "达人昵称": creator,
                    "视频链接": video_url,
                    "视频标题": title,
                    "点赞数": likes,
                    "数据来源": "douyin_auto",
                    "平台": "douyin",
                    "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "提取状态": "部分成功",
                    "缺失原因": "自动采集仅能获取搜索卡片摘要，达人主页/粉丝数等完整字段需通过达人链接二次采集",
                })

            # 如果卡片提取不到，回退到 HTML 正则
            if not results:
                html = page.content()
                vid_pattern = re.findall(r'/video/(\d+)', html)
                for vid in list(set(vid_pattern))[:max_results]:
                    results.append({
                        "搜索关键词": keyword,
                        "达人昵称": "",
                        "视频链接": f"https://www.douyin.com/video/{vid}",
                        "视频标题": "",
                        "点赞数": "",
                        "数据来源": "douyin_auto",
                        "平台": "douyin",
                        "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "提取状态": "部分成功",
                        "缺失原因": "仅从 HTML 提取视频ID，未获取详情",
                    })

        except Exception as e:
            print(f"[auto] 异常: {e}")
            try:
                page.screenshot(path=str(OUTPUT_DIR / f"error_{int(time.time())}.png"))
            except Exception:
                pass
        finally:
            browser.close()

    print(f"[auto] → {len(results)} 条")
    return results


# ----- login 模式：打开可视化浏览器让用户登录一次 -----
def do_login():
    """打开非 headless 的 Chrome，导航到抖音首页，用户手动登录后自动保存 cookie。"""
    from playwright.sync_api import sync_playwright

    print("[login] 正在打开 Chrome 浏览器...")
    print("[login] 请在浏览器中登录抖音，登录完成后回来按 Enter")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto("https://www.douyin.com")
        print("[login] 浏览器已打开 https://www.douyin.com")
        print("[login] 请在浏览器窗口中完成登录（扫码/手机验证等）")
        print("[login] 登录成功后，回到这里按 Enter 继续...")
        input()

        # 保存 storage_state
        state_path = OUTPUT_DIR / "douyin_cookies.json"
        context.storage_state(path=str(state_path))
        print(f"[login] ✅ Cookie 已保存到 {state_path}")
        browser.close()


# ----- 主入口 -----
def main():
    p = argparse.ArgumentParser(description="抖音自动采集（复用 Chrome Cookie）")
    p.add_argument("keyword", nargs="?", help="搜索关键词")
    p.add_argument("--max", type=int, default=20, help="最大结果数")
    p.add_argument("--all", action="store_true", help="搜索种子关键词列表")
    p.add_argument("--login", action="store_true", help="打开浏览器登录抖音并保存 cookie")
    args = p.parse_args()

    if args.login:
        do_login()
        return

    # 准备 cookies
    cookies_path = OUTPUT_DIR / "douyin_cookies.json"
    if not cookies_path.exists():
        print("[auto] 未找到已保存的 cookie，尝试从 Chrome 提取...")
        cookies = _get_douyin_cookies()
        if cookies:
            cookies_path = _save_cookies_json(cookies)
        else:
            print("[auto] ❌ 无法获取抖音登录态")
            print("[auto] 请先运行: python scripts/douyin_auto_collect.py --login")
            return

    keywords = []
    if args.all:
        from src.utils.config_loader import seed_keywords_config
        seeds = seed_keywords_config().get("seed_keywords", [])[:10]
        keywords = seeds
    elif args.keyword:
        keywords = [args.keyword]
    else:
        p.print_help()
        return

    all_results = []
    for kw in keywords:
        try:
            res = search_keyword(kw, max_results=args.max, cookies_path=cookies_path)
            all_results.extend(res)
            time.sleep(1.5)
        except Exception as e:
            print(f"[auto] {kw} 失败: {e}")

    # 去重 + 存 CSV
    seen = set()
    unique = []
    for r in all_results:
        link = r.get("视频链接", "")
        if link and link not in seen:
            seen.add(link)
            unique.append(r)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"douyin_auto_{ts}.csv"
    if unique:
        fieldnames = list(unique[0].keys())
    else:
        fieldnames = ["搜索关键词", "达人昵称", "视频链接", "视频标题", "点赞数", "数据来源", "平台", "采集时间"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(unique)
    print(f"\n[auto] ✅ 总计 {len(unique)} 条 → {path}")


if __name__ == "__main__":
    main()
