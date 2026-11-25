"""分组数据的轻量快照备份（FeedGroup / FeedGroupMember 两张表）。

★★ 为什么需要它（2026-10-16 事故的直接产物）
  虚拟分组**完全活在本地的两张表里** —— Miniflux 那边没有"分组"这个概念，
  所以这份数据**没有第二份真相层，丢了就是真丢了，谁也造不回来**。
  事故当天 `scripts/verify_groups.py` 里一句
  `cleanup_orphans(tree=fake_tree())`（假树里没有真实源）把用户的全部归属
  判成孤儿删光；事后翻库文件只捞出 3 条残页，其余被反复覆盖，无法恢复。

  ⇒ 凡是会**减少**归属关系的写操作（删组 / 移出分组 / 清理孤儿），
    动手之前先把两张表整体快照成 JSON。出事了用
    `python scripts/restore_groups.py <文件>` 一键回到任意一份快照。

设计取舍：
  - 快照是**全量**的（两张表都很小，几十行级别），不做增量 —— 恢复时不需要重放。
  - 轮转保留最近 `KEEP` 份，避免无限增长。
  - 只在**真的要删东西时**才写快照（空操作不产生文件）。
  - 写快照失败**不阻断**业务（备份是保险，不是前置条件），但要记 WARNING。
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from django.conf import settings

from core.models import FeedGroup, FeedGroupMember
from typing import Optional

log = logging.getLogger(__name__)

KEEP = 30          # 保留最近多少份快照
PREFIX = "groups-"


def backup_dir() -> Path:
    d = Path(settings.DATA_DIR) / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune(d: Path):
    """只保留最近 KEEP 份（按文件名里的时间戳排序）。"""
    files = sorted(d.glob(f"{PREFIX}*.json"))
    for f in files[:-KEEP] if len(files) > KEEP else []:
        try:
            f.unlink()
        except OSError:
            pass


def snapshot(reason: str = "") -> Optional[str]:
    """把两张表写成一份 JSON 快照，返回文件路径（失败返回 None）。

    ★ 调用方约定：在任何"会减少归属关系"的删除动作**之前**调用。
    """
    try:
        d = backup_dir()
        now = datetime.now()
        data = {
            "ts": now.isoformat(timespec="seconds"),
            "reason": reason,
            "groups": [
                {"id": g.id, "category_id": g.category_id, "title": g.title,
                 "sort": g.sort, "parent_id": g.parent_id}
                for g in FeedGroup.objects.all().order_by("id")
            ],
            "members": [
                {"feed_id": m.feed_id, "group_id": m.group_id, "sort": m.sort}
                for m in FeedGroupMember.objects.all().order_by("id")
            ],
        }
        # 同一秒内可能被调用两次（如 cleanup 顺带 move），用微秒保证不覆盖
        name = f"{PREFIX}{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond:06d}.json"
        path = d / name
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)          # 原子落盘，避免读到半截文件
        _prune(d)
        log.info("分组快照已存：%s（%d 组 / %d 归属；原因=%s）",
                 name, len(data["groups"]), len(data["members"]), reason or "-")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("分组快照写入失败（不影响本次操作）：%s", exc)
        return None


def list_snapshots() -> list:
    """可用的快照文件（最新在前）。"""
    d = backup_dir()
    return sorted(d.glob(f"{PREFIX}*.json"), reverse=True)


def plan_restore(path) -> dict:
    """**只读**：算出把快照恢复回去会做哪些事（给 --diff / 确认用）。"""
    path = Path(path)
    snap = json.loads(path.read_text(encoding="utf-8"))
    snap_groups = {g["id"]: g for g in snap.get("groups", [])}
    snap_members = {m["feed_id"]: m for m in snap.get("members", [])}
    cur_groups = {g.id: g for g in FeedGroup.objects.all()}
    cur_members = {m.feed_id: m for m in FeedGroupMember.objects.all()}
    return {
        "path": str(path), "ts": snap.get("ts"), "reason": snap.get("reason"),
        "n_snap_groups": len(snap_groups), "n_snap_members": len(snap_members),
        "n_cur_groups": len(cur_groups), "n_cur_members": len(cur_members),
        "add_members": sorted(f for f in snap_members if f not in cur_members),
        "change_members": sorted(f for f in snap_members if f in cur_members
                                 and cur_members[f].group_id != snap_members[f]["group_id"]),
        "add_groups": sorted(i for i in snap_groups if i not in cur_groups),
        "_snap": snap,
    }


def restore(path, *, dry_run: bool = False) -> dict:
    """按快照**合并**恢复（upsert）。

    ★ 语义是"补回被删的"，不是"清空重建"：快照里的组与归属补回/改回快照的值，
      **快照之后新建的东西不动**。
    ★ 非 dry_run 时会先给当前状态存一份快照（恢复方向错了还能再回去）。
    """
    plan = plan_restore(path)
    if dry_run:
        plan.pop("_snap", None)
        return plan

    before = snapshot("before_restore")
    snap = plan.pop("_snap")
    for gid, g in {x["id"]: x for x in snap.get("groups", [])}.items():
        FeedGroup.objects.update_or_create(
            id=gid, defaults={"category_id": g["category_id"], "title": g["title"],
                              "sort": g.get("sort", 0), "parent_id": g.get("parent_id")})
    for fid, m in {x["feed_id"]: x for x in snap.get("members", [])}.items():
        FeedGroupMember.objects.update_or_create(
            feed_id=fid, defaults={"group_id": m["group_id"], "sort": m.get("sort", 0)})
    plan["before_restore_snapshot"] = before
    plan["after_groups"] = FeedGroup.objects.count()
    plan["after_members"] = FeedGroupMember.objects.count()
    log.info("分组已从快照恢复：%s（补回 %d / 改回 %d 条归属）",
             Path(path).name, len(plan["add_members"]), len(plan["change_members"]))
    return plan
