"""把 WeeklyRun 合并进 SummaryRun（同一 id 池，按 kind 区分类型）。

为什么合并（2026-10-29 定）
--------------------------
原设计把周报放在独立的 `WeeklyRun` 表，理由是「`SummaryRun.date` 唯一，
周报的 week_end 与当天日报同一天会撞唯一键」。但这个理由在 `NotionPush` 上
已经用 `unique_together(kind, date)` 解决过一次了，周报这里没必要绕开。

合并后直接消掉两个补丁：
  · `LlmUsage.summary_run_id` 对周报也成立 —— 不用再加 `weekly_run_id`
    字段，也不用按 ts 区间猜归属；
  · 「失败的周报在页面上看不见、无法手动重跑」自动消失 —— DB 变成事实源，
    日报与周报的可见性天然一致。

字段映射
--------
    WeeklyRun.week_end      → SummaryRun.date          (kind="weekly")
    WeeklyRun.daily_count   → SummaryRun.source_count  （中性名，月报也用）
    WeeklyRun.state.running → "summarizing"            （并入 IN_FLIGHT）
    其余同名字段直接搬。

反向迁移
--------
反向必须**先**把周报行搬回 WeeklyRun、**再**删掉 SummaryRun 里的周报行，
否则 `AlterField(date, unique=True)` 会因为同日两行而失败。
"""
from django.db import migrations, models


def _iso(v):
    return v


def merge_weekly_into_summary(apps, schema_editor):
    SummaryRun = apps.get_model("core", "SummaryRun")
    WeeklyRun = apps.get_model("core", "WeeklyRun")
    LlmUsage = apps.get_model("core", "LlmUsage")

    for w in WeeklyRun.objects.all():
        # ★ 用构造器 + save()，不依赖 manager（历史模型的 manager 未必带 kind 过滤）
        row = SummaryRun.objects.filter(kind="weekly", date=w.week_end).first()
        if row is None:
            row = SummaryRun(kind="weekly", date=w.week_end)
        row.state = "summarizing" if w.state == "running" else w.state
        row.attempt = int(w.attempt or 0)
        row.source_count = int(w.daily_count or 0)
        row.missing_json = list(w.missing_json or [])
        row.notified = bool(w.notified)
        row.report_path = w.report_path or ""
        row.error = w.error or ""
        row.started_at = w.started_at
        row.finished_at = w.finished_at
        row.window_start = w.window_start
        row.window_end = w.window_end
        row.save()

        # 回填：周报的历史调用靠 ts 落在该次运行的 [started, finished] 区间认领
        if w.started_at and w.finished_at:
            LlmUsage.objects.filter(
                kind="summary_weekly", summary_run_id__isnull=True,
                ts__gte=w.started_at, ts__lte=w.finished_at,
            ).update(summary_run_id=row.id)


def unmerge_weekly_from_summary(apps, schema_editor):
    SummaryRun = apps.get_model("core", "SummaryRun")
    WeeklyRun = apps.get_model("core", "WeeklyRun")
    LlmUsage = apps.get_model("core", "LlmUsage")

    rows = list(SummaryRun.objects.filter(kind="weekly"))
    for row in rows:
        w, _ = WeeklyRun.objects.get_or_create(week_end=row.date)
        w.state = "running" if row.state == "summarizing" else row.state
        w.attempt = int(row.attempt or 0)
        w.daily_count = int(row.source_count or 0)
        w.missing_json = list(row.missing_json or [])
        w.notified = bool(row.notified)
        w.report_path = row.report_path or ""
        w.error = row.error or ""
        w.started_at = row.started_at
        w.finished_at = row.finished_at
        w.window_start = row.window_start
        w.window_end = row.window_end
        w.save()
        LlmUsage.objects.filter(kind="summary_weekly",
                                summary_run_id=row.id).update(summary_run_id=None)

    # ★ 必须删掉：否则 date 恢复 unique 时会与同日日报冲突
    SummaryRun.objects.filter(kind="weekly").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_weekly_kind_star"),
    ]

    operations = [
        # ---- 1. 加字段（先加，后面搬数据才有地方放）----
        migrations.AddField(
            model_name="summaryrun",
            name="kind",
            field=models.CharField(
                choices=[("daily", "日报"), ("weekly", "周报")],
                db_index=True, default="daily", max_length=12),
        ),
        migrations.AddField(
            model_name="summaryrun",
            name="source_count",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="summaryrun",
            name="missing_json",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="summaryrun",
            name="anomalies_total",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="summaryrun",
            name="notified",
            field=models.BooleanField(blank=True, default=False),
        ),
        # ---- 2. 唯一键 date → (kind, date)。★ 必须在搬数据**之前**，
        #         否则插入周报行时还会被旧的 date 唯一约束挡住 ----
        migrations.AlterField(
            model_name="summaryrun",
            name="date",
            field=models.DateField(),
        ),
        migrations.AlterUniqueTogether(
            name="summaryrun",
            unique_together={("kind", "date")},
        ),
        migrations.AddIndex(
            model_name="summaryrun",
            index=models.Index(fields=["state", "date"], name="sr_state_date_idx"),
        ),
        migrations.AddIndex(
            model_name="summaryrun",
            index=models.Index(fields=["kind", "date"], name="sr_kind_date_idx"),
        ),
        # ---- 3. 搬数据 + 回填记账归属 ----
        migrations.RunPython(merge_weekly_into_summary, unmerge_weekly_from_summary),
        # ---- 4. 删表 ----
        migrations.DeleteModel(name="weeklyrun"),
    ]
