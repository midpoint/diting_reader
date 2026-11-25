"""手动重跑某天日报：默认走缓存（只有输入变了的层才会真的调模型）。

    python scripts/rerun_daily.py 2026-11-10            # 增量重跑，缓存全用上
    python scripts/rerun_daily.py 2026-11-10 --no-wait  # 不等 feed 新鲜度，立刻开跑
    python scripts/rerun_daily.py 2026-11-10 --no-cache # 忽略缓存重跑（会花钱）

★ `--no-wait` 用于「有个源一直拉不下来，但它不该挡住重跑」的场合：默认会按
  `SUMMARY_FRESH_DEADLINE_MIN` 轮询最多 30 分钟（2026-11-10 实测卡在这里）。
"""
import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from core import summary  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="手动重跑某天日报")
    ap.add_argument("day", nargs="?", default="", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--no-wait", dest="no_wait", action="store_true",
                    help="不等 feed 新鲜度（deadline_min=0）")
    ap.add_argument("--no-cache", dest="no_cache", action="store_true",
                    help="忽略缓存重跑（S3/S2/S1 全部重新调用）")
    a = ap.parse_args()
    res = summary.run(date.fromisoformat(a.day) if a.day else None, force=True,
                      no_cache=a.no_cache,
                      fresh_deadline_min=0 if a.no_wait else None)
    print(json.dumps({k: v for k, v in res.items() if k != "items"},
                     ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
