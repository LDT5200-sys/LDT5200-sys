"""生成 sample_input.xlsx 到 data/input/，方便测试。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.utils.config_loader import DATA_DIR

SAMPLES = [
    {
        "采集日期": "2026-05-29", "数据来源": "manual_export", "平台": "douyin",
        "搜索关键词": "女生测男装", "达人昵称": "测男装的小鹿", "达人ID": "u_001",
        "主页链接": "https://www.douyin.com/user/sample_001",
        "视频链接": "https://www.douyin.com/video/sample_001",
        "视频标题": "女朋友帮男友测了 5 件男装短袖，真实上身反差太大",
        "视频文案": "真实测评 试穿对比 微胖男友 速干短袖 通勤", "发布时间": "2026-05-25",
        "点赞数": 12800, "评论数": 320, "分享数": 95, "收藏数": 410,
        "粉丝数": 86000, "账号简介": "情侣搭子，男装真实测评",
        "标签": "男装测评 真实体验 改造", "是否有联系方式": "是", "联系方式位置": "主页",
    },
    {
        "采集日期": "2026-05-29", "数据来源": "manual_export", "平台": "douyin",
        "搜索关键词": "微胖男生穿搭", "达人昵称": "胖哥的衣橱", "达人ID": "u_002",
        "主页链接": "https://www.douyin.com/user/sample_002",
        "视频链接": "https://www.douyin.com/video/sample_002",
        "视频标题": "180cm 微胖男生夏季短袖怎么挑，避雷三件大码男装",
        "视频文案": "微胖 大码男装 短袖避雷 通勤", "发布时间": "2026-05-26",
        "点赞数": 4500, "评论数": 188, "分享数": 60, "收藏数": 220,
        "粉丝数": 32000, "账号简介": "微胖男生穿搭",
        "标签": "大码 微胖 男装", "是否有联系方式": "否", "联系方式位置": "未知",
    },
    {
        "采集日期": "2026-05-29", "数据来源": "manual_export", "平台": "douyin",
        "搜索关键词": "通勤穿搭", "达人昵称": "机能通勤老张", "达人ID": "u_003",
        "主页链接": "https://www.douyin.com/user/sample_003",
        "视频链接": "https://www.douyin.com/video/sample_003",
        "视频标题": "通勤机能短袖一周穿搭，速干吸湿真的香",
        "视频文案": "通勤 机能 速干 吸湿 户外", "发布时间": "2026-05-24",
        "点赞数": 2100, "评论数": 86, "分享数": 30, "收藏数": 150,
        "粉丝数": 158000, "账号简介": "机能通勤每日穿搭",
        "标签": "机能 通勤 户外", "是否有联系方式": "是", "联系方式位置": "星图",
    },
    {
        "采集日期": "2026-05-29", "数据来源": "manual_export", "平台": "xiaohongshu",
        "搜索关键词": "男装避雷", "达人昵称": "穿搭小芒果", "达人ID": "u_004",
        "主页链接": "https://www.xiaohongshu.com/user/profile/sample_004",
        "视频链接": "https://www.xiaohongshu.com/discovery/item/sample_004",
        "视频标题": "女生视角男装避雷｜男友别再买这种短袖了",
        "视频文案": "男友改造 直男避雷 短袖", "发布时间": "2026-05-22",
        "点赞数": 8800, "评论数": 410, "分享数": 70, "收藏数": 980,
        "粉丝数": 49000, "账号简介": "女生视角看男装",
        "标签": "男友改造 男装避雷", "是否有联系方式": "否", "联系方式位置": "未知",
    },
    {
        "采集日期": "2026-05-29", "数据来源": "manual_export", "平台": "douyin",
        "搜索关键词": "户外穿搭", "达人昵称": "山系老周", "达人ID": "u_005",
        "主页链接": "https://www.douyin.com/user/sample_005",
        "视频链接": "https://www.douyin.com/video/sample_005",
        "视频标题": "夏天户外速干衣到底要不要花钱？三款对比",
        "视频文案": "户外 速干 防晒 短袖 真实体验", "发布时间": "2026-05-20",
        "点赞数": 23000, "评论数": 560, "分享数": 200, "收藏数": 1500,
        "粉丝数": 420000, "账号简介": "户外穿搭与装备测评",
        "标签": "户外 速干 装备测评", "是否有联系方式": "是", "联系方式位置": "主页",
    },
    {
        "采集日期": "2026-05-29", "数据来源": "manual_export", "平台": "douyin",
        "搜索关键词": "美妆", "达人昵称": "美妆小蛋糕", "达人ID": "u_006",
        "主页链接": "https://www.douyin.com/user/sample_006",
        "视频链接": "https://www.douyin.com/video/sample_006",
        "视频标题": "夏季韩系妆容教程，氛围感拉满",
        "视频文案": "美妆 韩系 妆容", "发布时间": "2026-05-18",
        "点赞数": 9000, "评论数": 200, "分享数": 50, "收藏数": 600,
        "粉丝数": 220000, "账号简介": "每日美妆教程",
        "标签": "美妆 教程", "是否有联系方式": "是", "联系方式位置": "主页",
    },
]


def main() -> Path:
    out_dir = DATA_DIR / "input"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "sample_input.xlsx"
    pd.DataFrame(SAMPLES).to_excel(path, index=False)
    print(f"sample_input.xlsx 已生成：{path}")
    return path


if __name__ == "__main__":
    main()
