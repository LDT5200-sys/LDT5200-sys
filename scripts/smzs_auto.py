"""社媒助手全自动采集：pyautogui 模拟键鼠 → Chrome + 社媒助手 → 导出 Excel。

快捷键：Alt+C 打开社媒助手侧边栏，采集当前页面视频数据。

用法：python scripts/smzs_auto.py "女生测男装"
      python scripts/smzs_auto.py --all
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "data" / "input" / "douyin"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR = Path.home() / "Downloads"


def chrome_open(url: str):
    subprocess.run(["osascript", "-e",
        f'tell application "Google Chrome" to activate\n'
        f'tell application "Google Chrome" to open location "{url}"'
    ], timeout=10)


def get_latest_download() -> Path | None:
    xlsx = sorted(DOWNLOAD_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return xlsx[0] if xlsx else None


def search_one(keyword: str) -> bool:
    import pyautogui
    pyautogui.FAILSAFE = True

    url = f"https://www.douyin.com/search/{quote(keyword)}?type=general"
    print(f"\n[auto] === {keyword} ===")

    # 记录已有文件，避免把旧的当新的
    before = set(DOWNLOAD_DIR.glob("*.xlsx"))

    # 1. 打开搜索页
    print("[auto] 打开抖音搜索...")
    chrome_open(url)
    time.sleep(8)

    # 2. 滚动加载
    print("[auto] 滚动加载视频...")
    for _ in range(8):
        pyautogui.press("pagedown")
        time.sleep(1.2)

    # 3. Option+C 打开社媒助手侧边栏（出现在屏幕右侧）
    print("[auto] 触发社媒助手 (Option+C)...")
    pyautogui.hotkey("option", "c")
    time.sleep(4)

    # 侧边栏在屏幕右侧，鼠标移到面板区域点击获取焦点
    screen_w = pyautogui.size().width
    screen_h = pyautogui.size().height
    # 点击面板中间区域 (右侧 20% 处)
    pyautogui.click(screen_w - 200, screen_h // 3)
    time.sleep(1)

    # 尝试 Tab + Space 找按钮
    print("[auto] 尝试点击面板内按钮...")
    for attempt in range(12):
        pyautogui.press("tab")
        time.sleep(0.5)
        pyautogui.press("space")
        time.sleep(2)

    # 关闭面板
    pyautogui.hotkey("option", "c")
    time.sleep(1)

    # 7. 检查下载
    after = set(DOWNLOAD_DIR.glob("*.xlsx"))
    new_files = after - before
    if new_files:
        f = list(new_files)[0]
        dest = OUTPUT_DIR / f"smzs_{keyword}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        # 等文件完全写入
        time.sleep(2)
        shutil.move(str(f), str(dest))
        print(f"[auto] ✅ {dest.name}")
        return True
    else:
        # 兜底：拿最近的文件
        latest = get_latest_download()
        if latest and latest.stat().st_mtime > time.time() - 30:
            dest = OUTPUT_DIR / f"smzs_{keyword}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
            shutil.move(str(latest), str(dest))
            print(f"[auto] ✅ {dest.name} (兜底)")
            return True
        print("[auto] ⚠️ 未检测到导出文件")
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("keyword", nargs="?", help="搜索关键词")
    p.add_argument("--all", action="store_true", help="全部种子关键词")
    args = p.parse_args()

    if args.all:
        from src.utils.config_loader import seed_keywords_config
        keywords = seed_keywords_config().get("seed_keywords", [])[:5]
    elif args.keyword:
        keywords = [args.keyword]
    else:
        p.print_help()
        return

    print("[auto] 开始全自动采集，请勿触碰鼠标键盘...")
    time.sleep(2)

    ok = 0
    for kw in keywords:
        try:
            if search_one(kw):
                ok += 1
            time.sleep(2)
        except Exception as e:
            print(f"[auto] {kw} 失败: {e}")

    print(f"\n[auto] ✅ {ok}/{len(keywords)} 成功 → {OUTPUT_DIR}")

    # 自动跑后续流水线
    if ok > 0:
        print("\n[auto] 开始清洗+评分...")
        subprocess.run([
            sys.executable, str(ROOT / "src" / "main.py"),
            "--skip-ai", "--skip-keyword-expand", "--douyin-import"
        ])


if __name__ == "__main__":
    main()
