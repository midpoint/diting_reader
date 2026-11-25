#!/usr/bin/env python
"""周报手动运维命令。

    python scripts/run_weekly.py                       # 生成本周（默认今天为 week_end）
    python scripts/run_weekly.py --week-end 2026-11-01 # 指定那期（= 那周的周日）
    python scripts/run_weekly.py --force               # 已有文件也重跑（约 ¥0.13）
    python scripts/run_weekly.py --dry                 # 只检查素材齐不齐，不调模型

★ 周报 = 过去 7 天日报的合集，**不重采、不重判**，所以缺哪天的日报会直接说：
    · 缺的是**历史**某天 ⇒ 立即失败（历史日报不会自己出现，得先补那份日报）；
    · 缺的是**当天**日报 ⇒ 提示"等日报"，不判死（guard 会自动重试）。
"""
import argparse
import os
import sys
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from core import weekly as wk  # noqa: E402


def _parse(value):
    try:
        return _date.fromisoformat((value or "").strip())
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description="谛听周报手动命令")
    ap.add_argument("--week-end", dest="week_end", default="",
                    help="那期周报的结束日（周日），默认今天")
    ap.add_argument("--force", action="store_true", help="已有文件也重跑")
    ap.add_argument("--dry", action="store_true",
                    help="只检查这 7 份日报齐不齐，不调模型、不落盘")
    args = ap.parse_args()

    week_end = _parse(args.week_end) or wk._due_week_end()

    if args.dry:
        col = wk.collect(week_end)
        print(f"week_end   : {week_end}")
        print(f"窗口       : {wk.window_label(week_end)}")
        print(f"日报       : {col['covered']}/{col['need']} 份")
        if col["missing"]:
            print("缺日报     : " + "、".join(d.isoformat() for d in col["missing"]))
        if col["degraded"]:
            print("规则版     : " + "、".join(d.isoformat() for d in col["degraded"]))
        mat = wk.build_material(col["parts"])
        print(f"素材字符   : {len(mat)}")
        print(f"已有文件   : {wk.weekly_path(week_end)}"
              f"{'（已存在）' if wk.weekly_path(week_end).is_file() else '（无）'}")
        return 0

    res = wk.run(week_end, force=args.force)
    if not res.get("ok"):
        print("未生成：", res.get("reason") or res)
        if res.get("wait"):
            print("（当天日报还没出来，等它跑完再来一次即可）")
        else:
            print("（先补上面缺的那几份日报：python scripts/run_summary.py --date <日>）")
        return 1
    if res.get("skipped"):
        print("跳过：", res.get("reason"))
    else:
        print(f"已生成：{res.get('path')}（{res.get('days')} 份日报，"
              f"{res.get('tokens')} tokens，star {res.get('stars')} 篇，"
              f"异常 {res.get('anomalies')} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
