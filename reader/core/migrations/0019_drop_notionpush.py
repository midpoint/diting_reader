"""删除 NotionPush 表：Notion 推送于 2026-11-01 整体下线。

为什么删
--------
推送的**唯一目的**是「方便在 Notion 里划线做笔记」，而谛听自己已经支持划线与
批注（wiki 阶段 1）。此后推送只是额外依赖：多一套配置（token / page / anchor）、
多一个网络失败面、多一个静默漏推的可能。

本表只记「推没推、推到哪个 block、上次发出去的标题」，**不含任何正文**
（正文事实源永远是 `data/reports/*.md`）。所以删表不损失任何内容。

★ 删前已把 28 行完整导出：
  `data/backup/notion_push_20261101.json`（Django serialize 格式，可 loaddata 还原）
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_ingestcandidate_quote_from'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='notionpush',
            unique_together=None,
        ),
        migrations.RemoveIndex(
            model_name='notionpush',
            name='core_notion_state_5cdad8_idx',
        ),
        migrations.RemoveIndex(
            model_name='notionpush',
            name='core_notion_kind_2a380c_idx',
        ),
        migrations.DeleteModel(
            name='NotionPush',
        ),
    ]
