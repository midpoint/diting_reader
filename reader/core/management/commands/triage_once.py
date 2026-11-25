from django.core.management.base import BaseCommand

from core import triage


class Command(BaseCommand):
    help = "跑一轮分级（常驻 TriageWorker 的手动版本；--dry-run 不写库不调 LLM 处置）"

    def add_arguments(self, parser):
        parser.add_argument("--feed", type=int, action="append", help="只处理指定 feed（可多次）")
        parser.add_argument("--dry-run", action="store_true", help="只统计，不写库、不写回 Miniflux")
        parser.add_argument("--max-calls", type=int, default=None)

    def handle(self, *args, **opts):
        stats = triage.run_once(feed_ids=opts["feed"], dry_run=opts["dry_run"],
                                max_calls=opts["max_calls"])
        self.stdout.write(self.style.SUCCESS("分级完成："))
        for k, v in stats.items():
            self.stdout.write(f"  {k:<14} {v}")
