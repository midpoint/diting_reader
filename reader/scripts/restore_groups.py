"""分组数据恢复：从 `core/groupbackup.py` 留下的快照回到过去的状态。

用法：
    python scripts/restore_groups.py                  # 列出所有快照
    python scripts/restore_groups.py --diff <文件>     # 只看差异，不写库
    python scripts/restore_groups.py <文件>            # 按快照**合并**恢复

★ 恢复语义是**合并**（upsert），不是清空重建：
  快照里的组与归属会被补回来 / 覆盖成快照的值，**快照之后新建的东西不动**。
  （事故场景要的正是"把被删的补回来"，而不是"把这段时间的新东西也抹掉"。）

★ 恢复前会先给当前状态存一份快照，万一恢复方向不对还能再回去。
"""
import json
import os
import sys
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from core import groupbackup  # noqa: E402
from core.models import FeedGroup, FeedGroupMember  # noqa: E402

files = groupbackup.list_snapshots()

if len(sys.argv) < 2:
    print(f"快照目录：{groupbackup.backup_dir()}")
    if not files:
        print("  （还没有任何快照）")
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            print(f"  {f.name}  {d.get('ts','')}  "
                  f"{len(d.get('groups',[]))} 组 / {len(d.get('members',[]))} 归属  "
                  f"原因={d.get('reason','-')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {f.name}  (无法解析：{exc})")
    print("\n恢复：python scripts/restore_groups.py <文件名或完整路径>")
    sys.exit(0)

arg = sys.argv[1]
dry = False
if arg == "--diff":
    dry = True
    arg = sys.argv[2]

path = Path(arg)
if not path.exists():
    cand = [f for f in files if f.name == arg]
    if not cand:
        print(f"找不到快照：{arg}")
        sys.exit(1)
    path = cand[0]

snap = json.loads(path.read_text(encoding="utf-8"))
plan = groupbackup.plan_restore(path)

print(f"快照：{path.name}  {plan['ts']}  原因={plan['reason']}")
print(f"  快照：{plan['n_snap_groups']} 组 / {plan['n_snap_members']} 归属")
print(f"  当前：{plan['n_cur_groups']} 组 / {plan['n_cur_members']} 归属")

add_m, chg_m, add_g = plan["add_members"], plan["change_members"], plan["add_groups"]
print(f"\n将要：补回 {len(add_m)} 条归属 / 改回 {len(chg_m)} 条归属 / 补建 {len(add_g)} 个组")
if add_m:
    print(f"  补回的 feed_id：{add_m[:40]}{' …' if len(add_m) > 40 else ''}")
if chg_m:
    print(f"  改回的 feed_id：{chg_m[:40]}{' …' if len(chg_m) > 40 else ''}")
if add_g:
    print(f"  补建的组 id：{add_g}")

if dry:
    print("\n（--diff 模式：没有写库）")
    sys.exit(0)

done = groupbackup.restore(path)
print(f"\n恢复前已存当前状态快照："
      f"{done.get('before_restore_snapshot') and Path(done['before_restore_snapshot']).name}")
print(f"完成：{done['after_groups']} 组 / {done['after_members']} 归属")
print("★ 回到阅读页刷新即可看到分组。")
