"""阅读器自有数据（SQLite）。Miniflux 仍是正文/已读/星标的真相层，这里只存「我们的数据」。"""
from django.db import models

LEVELS = ((0, "L0"), (1, "L1"), (2, "L2"), (3, "L3"))


class CategoryTriageConfig(models.Model):
    """分类级分级要求（逐级别与 feed 段拼接，见设计 §6.2.2）。

    ★ 分类**没有** default_level —— 默认级别是 feed 自己的事（feed 不配则全局 L2）。
    """

    category_id = models.IntegerField(unique=True)
    l0_text = models.TextField(blank=True, default="")
    l1_text = models.TextField(blank=True, default="")
    l2_text = models.TextField(blank=True, default="")
    l3_text = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"cat:{self.category_id}"


class FeedTriageConfig(models.Model):
    """feed 级分级要求。判据空 = 该段缺席（分类段照常拼接）。"""

    feed_id = models.IntegerField(unique=True)
    l0_text = models.TextField(blank=True, default="")
    l1_text = models.TextField(blank=True, default="")
    l2_text = models.TextField(blank=True, default="")
    l3_text = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="", help_text=(
        "本源备注：这个源的文本结构 / 格式说明（例如「正文是三层复合文本」），"
        "**不是判据**——它只进 prompt 的「本源备注」段，不参与级别判定规则"))
    default_level = models.IntegerField(choices=LEVELS, null=True, blank=True,
                                        help_text="空 = 全局默认 L2")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"feed:{self.feed_id}"


class FeedGroup(models.Model):
    """分类与 feed 之间的**虚拟分组**（纯视图层）。

    ★ 定位：只解决"阅读页树太长、一屏放不下"，**不进分级判据、不进周期总结、
      不进开销统计**。所以它既不是 Miniflux 的分类（分类有语义、有判据继承、
      进总结分节），也不是新的内容域。
    ★ `category_id` 用裸 int 不用 FK：真相层在 Miniflux，级联删除会误伤
      （与 CategoryTriageConfig / FeedTriageConfig 的既有约定一致）。
    ★ `parent_id` 是**为将来多层嵌套预留**的；UI 第一版只做一层 —— 侧栏只有
      290px，每级缩进 16px，再加一级 feed 标题就没宽度了。
    """

    category_id = models.IntegerField(db_index=True)
    title = models.CharField(max_length=64)
    sort = models.IntegerField(default=0)
    parent_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["category_id", "sort"])]

    def __str__(self):
        return f"grp:{self.id} cat:{self.category_id} {self.title}"


class FeedGroupMember(models.Model):
    """feed 归属某个分组。

    ★ `feed_id` 唯一 ⇒ 一个 feed 最多属于一个组，树才还是树。
    ★ 有效性规则（见 `core/readergroups.py`）：`group.category_id == feed 当前分类`。
      不符的成员**不渲染**（视为未分组）—— 这样在 Miniflux 原生 UI 里直接改分类，
      也不会在本地点出"幽灵分组"。
    """

    feed_id = models.IntegerField(unique=True)
    group_id = models.IntegerField(db_index=True)
    sort = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"feed:{self.feed_id} → grp:{self.group_id}"


class TreeOrder(models.Model):
    """侧栏树的**手动顺序**（拖动重排的结果）：分类 / 目录 / 组内与分类根下的 feed。

    ★ 一张表管三层，`kind` + `scope` 定位「同一个兄弟列表」：
      - `kind="cat"`  ⇒ `scope=""`（分类只有全局这一个列表）
      - `kind="grp"`  ⇒ `scope="cat:<category_id>"`（该分类下的目录列表）
      - `kind="feed"` ⇒ `scope="cat:<category_id>"` = 该分类根下的未分组 feed，
                        `scope="grp:<group_id>"`   = 该目录内的 feed
    ★ **只记手动排过的那一项**：没行的项 = 未参与重排，排在有行的项之后，
      并维持默认顺序（分类按名称、目录按 `FeedGroup.sort` 即创建序、
      feed 按未读优先）⇒ 新建 / 外部改归属进来的项自动落到该层末尾，
      不用为新项补 sort，"恢复默认顺序"也只是删掉本层的行。
    ★ `FeedGroup.sort` **不是**手动顺序（它是创建序，拖动写在这里）。
      否则同一个语义两处真相，重置也无从谈起。
    ★ 真相层仍是 Miniflux：这里只排顺序，不改归属。分类 / 源在 Miniflux 侧被删
      或改了归属，行就成孤儿 —— 由 `core/treeorder.py` 的清理钩子回收。
    """

    KIND = (("cat", "分类"), ("grp", "目录"), ("feed", "feed"))

    kind = models.CharField(max_length=8, choices=KIND)
    scope = models.CharField(max_length=32, blank=True, default="")
    ref_id = models.IntegerField()
    sort = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["kind", "scope", "ref_id"], name="uniq_treeorder_item")]
        indexes = [models.Index(fields=["kind", "scope", "sort"])]

    def __str__(self):
        return f"{self.kind} {self.scope or '-'} #{self.ref_id} → {self.sort}"


class TriagePrompt(models.Model):
    """feed 的 prompt 版本快照，全留做审计（不可人工编辑）。"""

    BUILT_BY = (("rule", "规则拼装"), ("llm", "AI 润色"))
    STATUS = (("active", "生效"), ("archived", "归档"))

    feed_id = models.IntegerField()
    version = models.IntegerField()
    spec_json = models.JSONField(default=dict)
    prompt_text = models.TextField(blank=True, default="")
    source_hash = models.CharField(max_length=64, blank=True, default="")
    change_note = models.TextField(blank=True, default="")
    built_by = models.CharField(max_length=8, choices=BUILT_BY, default="rule")
    validated = models.BooleanField(default=False)
    status = models.CharField(max_length=8, choices=STATUS, default="active")
    llm_usage_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("feed_id", "version")
        indexes = [models.Index(fields=["feed_id", "version"])]

    def __str__(self):
        return f"feed:{self.feed_id} v{self.version}"


class EntryRating(models.Model):
    """条目分级结果。一个 entry 只判一次（幂等）。"""

    REASONS = (
        ("llm", "模型判定"),
        ("default", "未配置分级，取 default_level"),
        ("fail_open", "调用失败，掉到 default_level"),
        ("manual", "手工调整"),
    )

    entry_id = models.BigIntegerField(unique=True)
    feed_id = models.IntegerField()
    category_id = models.IntegerField(null=True, blank=True)
    level = models.IntegerField(choices=LEVELS)
    reason = models.CharField(max_length=16, choices=REASONS, default="llm")
    default_level = models.IntegerField(choices=LEVELS, null=True, blank=True,
                                        help_text="判定时该 feed 的 default_level 快照")
    votes = models.JSONField(default=list, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    model = models.CharField(max_length=64, blank=True, default="")
    prompt_version = models.IntegerField(null=True, blank=True)
    manual = models.BooleanField(default=False)
    manual_note = models.CharField(max_length=200, blank=True, default="")
    prev_level = models.IntegerField(choices=LEVELS, null=True, blank=True)
    rated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["feed_id", "level"])]

    def __str__(self):
        return f"entry:{self.entry_id} L{self.level}"


class LlmUsage(models.Model):
    """token 开销总账。feed_id / category_id 皆空 = 公共开销。"""

    KINDS = (
        ("triage", "分级"),
        ("prompt_build", "prompt 构建"),
        ("summary_entry", "总结·单篇压缩"),
        ("summary_feed", "总结·按源"),
        ("summary_category", "总结·按分类"),
        ("summary_global", "总结·全局汇总"),
        ("summary_weekly", "总结·周报"),
        ("translate_title", "标题翻译"),
        # ★ 知识库（第 5 池）开销。命名对应「环节」而不是「功能按钮」：
        #   chat=文章内对话，extract=对话后抽候选，compile=候选编译成原子页，
        #   lint=守门审计。
        #   wiki_summary（一句话总结）**功能已于 2026-11-03 移除**，这里保留
        #   是为了历史账单仍能读出中文标签 —— 删掉 choice 不会删数据，只会让
        #   旧记录在开销页显示成裸 key。
        #   加 kind 必须同时进 costreport.KIND_ORDER 与 app.css
        #   的 `.k-<key>`（漏了柱子会隐身，verify_ui_shell 有断言）。
        ("wiki_chat", "知识库·对话"),
        ("wiki_extract", "知识库·提取"),
        ("wiki_compile", "知识库·编译"),
        ("wiki_lint", "知识库·审计"),
        ("wiki_summary", "知识库·总结"),
    )

    ts = models.DateTimeField(auto_now_add=True, db_index=True)
    kind = models.CharField(max_length=24, choices=KINDS, db_index=True)
    provider = models.CharField(max_length=32, blank=True, default="")
    model = models.CharField(max_length=64, blank=True, default="")
    feed_id = models.IntegerField(null=True, blank=True, db_index=True)
    category_id = models.IntegerField(null=True, blank=True)
    entry_id = models.BigIntegerField(null=True, blank=True)
    summary_run_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    cached_tokens = models.IntegerField(default=0)
    cost_cny = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    retries = models.IntegerField(default=0)
    latency_ms = models.IntegerField(default=0)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True, default="")

    def __str__(self):
        return f"{self.kind} feed={self.feed_id} @{self.ts:%m-%d %H:%M}"


class FeedOverride(models.Model):
    """feed 级开关（快速刷新等）。

    `last_fast_at` 是**节流依据**，不是日志：tick 每 60 秒跑一次，只有
    `now - last_fast_at >= fast_interval_min` 的 feed 才真去 refresh。
    ★ 必须在发起 refresh **之前**写 —— 否则刷新失败会每个 tick 重试一次，
      对有 WAF 的源等于自杀。
    """

    feed_id = models.IntegerField(unique=True)
    fast_refresh = models.BooleanField(default=False)
    fast_interval_min = models.IntegerField(default=10)
    last_fast_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True, default="")
    notes = models.CharField(max_length=200, blank=True, default="")

    def __str__(self):
        return f"feed:{self.feed_id} fast={self.fast_refresh}"


class DailySummaryRunManager(models.Manager):
    """默认 manager：**只放行日报**。

    ★★ 这是个「安全默认」，不是笔误（2026-10-29 定）：
      `SummaryRun` 同时承载日报与周报，两者**日期可以重合**（周报的 week_end
      就是那天）。历史代码里有 15 处 `SummaryRun.objects.filter(date=...)`，
      全都假定"一天只有一行日报"。若默认 manager 返回全部类型，其中至少 7 处
      会静默出错，最狠的两个：

        · `get_or_create(date=day)` 会拿到那天的**周报行** → 判定"日报已生成"，
          当天日报从此再也不会跑；
        · `latest_report()` 可能返回周报行 → 再去读 `daily-*.md`（不存在）→
          页面弹「报告文件不见了」假告警。

      所以这里的取舍是：**默认只给日报，要跨类型必须显式写 `all_objects`**。
      代价是 `objects` 这个名字看起来像"全部"，故此处显式留证。

    ★ 反过来的坑（同样必须记住）：过滤式 manager 不能当 `_base_manager`，
      否则 `refresh_from_db()` / 关联对象反查会找不到周报行而抛 DoesNotExist。
      Meta 里已把 `base_manager_name` 指到 `all_objects`。
    """

    def get_queryset(self):
        return super().get_queryset().filter(kind=SummaryRun.KIND_DAILY)


class SummaryRun(models.Model):
    """周期总结的运行记录（报告正文落盘，db 不存 md），**按 kind 分类型**。

    ★ 为什么日报和周报共用一张表、共用一个 id 池（2026-10-29 定）：
      1. `LlmUsage.summary_run_id` 对周报也成立 —— 否则周报的花费与 token
         无处归属（`LlmUsage` 没有 date 字段，只能靠 ts 猜）；
      2. 「失败的周报在页面上看不见、无法手动重跑」这类问题自动消失 ——
         DB 是事实源时，日报与周报的可见性天然一致；
      3. 以后加月报只需要多一个 kind，不必再开一张表 + 抄一套状态机。

    ★ 唯一键是 `(kind, date)`：同一天允许「日报 + 周报」并存（周报的 date
      = week_end = 那天）。周报原先是独立的 `WeeklyRun` 表，合并进来正是为了
      共用这一套状态机（见 0012 的迁移说明）。

    ★ 字段归属（哪几个字段对哪一类有意义）：

    | 字段 | 日报 | 周报 | 说明 |
    |---|---|---|---|
    | state/attempt/error/report_path/started_at/finished_at/window_* | ✓ | ✓ | 共用 |
    | source_count | — | 7 | 构成素材的报告份数（周报=拼进来的日报份数；月报=30） |
    | missing_json | — | 缺哪几天 | 素材缺口 |
    | anomalies_total | — | 61 | 7 天原始异常之和（与日报同口径，但跨度是 7 天） |
    | notified | — | ✓ | 失败通知只发一次，别每 30 分钟刷 |
    | catch_up / items_count / anomalies_json | ✓ | — | 日报专属 |

    ★ 状态枚举统一：周报原先的 "running" 已并入 "summarizing"，否则
      `summary.reset_stale()` 的 `IN_FLIGHT` 认不出它 —— 进程被杀后周报会
      永远卡在 running。
    """

    KIND_DAILY = "daily"
    KIND_WEEKLY = "weekly"
    KINDS = (
        (KIND_DAILY, "日报"),
        (KIND_WEEKLY, "周报"),
    )

    STATES = (
        ("pending", "待跑"),
        ("prefetch", "补救刷新中"),
        ("waiting", "等待 feed 更新"),
        ("summarizing", "总结中"),
        ("done", "完成"),
        ("failed", "失败"),
    )

    kind = models.CharField(max_length=12, choices=KINDS, default=KIND_DAILY,
                            db_index=True)
    # 日报 = 窗口结束日；周报 = week_end（周日，也等于生成当天）
    date = models.DateField()
    state = models.CharField(max_length=16, choices=STATES, default="pending")
    attempt = models.IntegerField(default=0)
    catch_up = models.BooleanField(default=False)
    window_start = models.DateTimeField(null=True, blank=True)
    window_end = models.DateTimeField(null=True, blank=True)
    # —— 日报专属 ——
    items_count = models.IntegerField(default=0)   # 窗口内进入总结的条数
    anomalies_json = models.JSONField(default=list, blank=True)
    # —— 周报/月报 ——
    source_count = models.IntegerField(default=0)  # 拼进素材的报告份数
    missing_json = models.JSONField(default=list, blank=True)   # 缺哪几天
    anomalies_total = models.IntegerField(default=0)  # 素材期内的原始异常条数
    notified = models.BooleanField(default=False, blank=True)    # fail 通知只发一次
    # —— 共用 ——
    report_path = models.CharField(max_length=400, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    objects = DailySummaryRunManager()      # ★ 默认只看日报，见上方 docstring
    all_objects = models.Manager()          # 跨类型（周报、合并列表）用这个

    class Meta:
        unique_together = (("kind", "date"),)
        indexes = [
            models.Index(fields=["state", "date"], name="sr_state_date_idx"),
            models.Index(fields=["kind", "date"], name="sr_kind_date_idx"),
        ]
        # ★ 过滤式 manager 不能做 base manager（refresh_from_db 会找不到行）
        base_manager_name = "all_objects"
        default_manager_name = "objects"

    def __str__(self):
        return f"{self.kind} {self.date} {self.state}"


class AppSetting(models.Model):
    key = models.CharField(max_length=64, unique=True)
    value = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class FeedTranslation(models.Model):
    """feed 级标题翻译开关 + 回填进度（本地数据，不写 Miniflux）。

    ★ 默认关闭：没有行 / `enabled=False` 都算关。关闭只隐藏译文，不删译文。
    ★ `enabled_at` 是「回填 / 新文章」的水位：
        `created_at >= enabled_at` 的条目算新文章，不占回填 N 名额。
      否则一个高产源会把 N 全吃掉，真正预存的历史条目永远轮不到。
    ★ `backfill_max/backfill_days` 为空 = 用全局 `settings.TRANSLATE_*`；
      非空留给将来的 feed 级配置 UI。
    """

    feed_id = models.IntegerField(unique=True)
    enabled = models.BooleanField(default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    backfill_days = models.IntegerField(null=True, blank=True)
    backfill_max = models.IntegerField(null=True, blank=True)
    backfill_done_count = models.IntegerField(default=0)
    backfilled_at = models.DateTimeField(null=True, blank=True)
    last_scan_at = models.DateTimeField(null=True, blank=True)
    next_scan_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"feed:{self.feed_id} translate={'on' if self.enabled else 'off'}"


class EntryTranslation(models.Model):
    """条目标题译文。一个 entry 只有一行；重译是覆盖，不堆历史。

    ★ 只存 `done` / `failed`，**不存 pending**：没行 = 还没翻。
      进程崩了下一轮自然重来，不会留下永远「翻译中」的脏状态。
    ★ `source_hash` 是幂等依据：Miniflux 标题没变就不重复花钱；变了才重译。
    """

    STATUS = (("done", "完成"), ("failed", "失败"))

    entry_id = models.BigIntegerField(unique=True)
    feed_id = models.IntegerField(db_index=True)
    source_title = models.TextField(blank=True, default="")
    source_hash = models.CharField(max_length=64, blank=True, default="")
    title_zh = models.TextField(blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS, default="done")
    lang = models.CharField(max_length=8, blank=True, default="")
    model = models.CharField(max_length=64, blank=True, default="")
    prompt_version = models.IntegerField(default=1)
    attempts = models.IntegerField(default=0)
    next_try_at = models.DateTimeField(null=True, blank=True)
    translated_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["feed_id", "status"])]

    def __str__(self):
        return f"entry:{self.entry_id} {self.status}"


class EntryFullFetch(models.Model):
    """文章"抓取全文"状态：只存状态与长度，不存正文副本（仓库硬约束）。

    ★ Miniflux 是正文真相层，抓全文就是一次 `PUT /entries/<id>/fetch-content`，
      抓完再读即全文，本地无需缓存正文。这张表只回答两个问题：
      ① 这篇抓过没有（决定右键是否提示"重复抓取并覆盖"）；
      ② 上次抓取的耗时/长度变化（排障用，不参与业务逻辑）。
    ★ `entry_id` unique：重复抓取是覆盖同一行，不堆历史。
    """

    entry_id = models.BigIntegerField(unique=True)
    fetched_at = models.DateTimeField(auto_now=True)
    content_len_before = models.IntegerField(default=0)
    content_len_after = models.IntegerField(default=0)
    fetch_ms = models.IntegerField(default=0)

    def __str__(self):
        return "entry=%s 全文抓取 %s" % (self.entry_id, self.fetched_at)


class StarEvent(models.Model):
    """加星事件（本地时间戳 = Miniflux 给不到的那一半）。

    Miniflux 的 `entries` 只有 `starred` 布尔，**没有加星时间**（实测：
    `bookmarks` 表不存在；`changed_at` 是正文最后变更 —— 10-29 凌晨有 130+ 条
    非 star 条目的 changed_at 被刷新，不能拿来当加星时间）。
    所以时间戳只能本地记：

      · `source="reader"`  —— 从谛听 `hx/star` 点的，时间是**真实的**；
      · `source="inferred"` —— 在 Miniflux 原生 UI 点的（那条路径我们看不见），
        周报生成时按 `published_at` **补记**，好让它至少能归到正确的周。

    ★ 只记"加/取消"这一动作，不去维护"当前是否 star"的镜像 —— 真值永远是
      Miniflux 的 `starred`。本表回答的只是"什么时候发生的"。
    ★ 它同时是 LLM Wiki 的 Stage 0（同一张表，一表两用）。
    """

    SOURCES = (("reader", "谛听页面"), ("inferred", "按发布时间补记"))

    entry_id = models.BigIntegerField(db_index=True)
    feed_id = models.IntegerField(db_index=True)
    starred = models.BooleanField(default=True)
    ts = models.DateTimeField(db_index=True)
    source = models.CharField(max_length=12, choices=SOURCES, default="reader")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["ts", "starred"])]

    def __str__(self):
        act = "加星" if self.starred else "取消星"
        return f"entry:{self.entry_id} {act}@{self.ts:%m-%d %H:%M} {self.source}"


# ---------------------------------------------------------------------------
# 知识库（wiki）：划线 / 批注。对话（Conversation）与来源快照（WikiSource）
# 留到阶段 2 再接 LLM 时建 —— 这里先只做「人对文章/报告留下的判断」这条链。
# ---------------------------------------------------------------------------

# ★ source_type 的语义：一个「可被交互的内容块」，不是一篇文章也不是一个 DOM 选区。
#   entry  = RSS 文章（source_id = entry_id 的字符串，真相在 Miniflux）
#   report = 日报/周报 md（source_id = "daily-2026-10-31" / "weekly-2026-11-01"）
#   web    = 未来浏览器剪藏（source_id = 剪藏 id，阶段 4 起用）
#   chat   = 对话（source_id = Conversation.id，阶段 2 起用）
#   ★ web / chat 现在只是**枚举预留**，没有对应的写入逻辑 —— 别照想象提前建表，
#     到真正需要那个对象时再补 source_type 的实现，但枚举和软引用口径一次定死。
SOURCE_TYPES = (
    ("entry", "文章"),
    ("report", "日报/周报"),
    ("web", "网页"),
    ("chat", "对话"),
    # ★ 审批对话（第三步）：一批 draft = 当前 .drafts/ 全量，不建 batch 标识。
    #   source_id 固定 "current"，靠 Conversation 的 (source_type, source_id)
    #   唯一约束保证「同一时刻只有一条 active 审批对话」。审批对话不复用
    #   conversation.ask()：那个假设 source 是一个内容块，会去
    #   sourceblock.load() 读正文。
    ("draftbatch", "审批对话"),
)


class LiveMarkQuerySet(models.QuerySet):
    """「还活着的」痕迹 —— 软删语义的**唯一判据**。

    ★ 为什么不把 `objects` 换成过滤式 manager：项目里已有一堆
      `Highlight.objects.all().delete()` 的清理（测试库 / 验证脚本），默认过滤
      之后 `all()` 就只剩未删的，软删行会静默残留 —— 那种假绿比多写几个
      `.live()` 贵得多。所以默认 manager 保持原样，**要用「未删」的地方显式
      写 `.live()`**，判据本身只在这一处。
    """

    def live(self):
        return self.filter(deleted_at__isnull=True)


class Highlight(models.Model):
    """划线 / 摘录：从某个内容块里**摘出的一句话**（可以带一句我的备注）。

    ★ 与 Annotation 的区别（架构 §2.2）：Highlight 是「引用原文」，Annotation 是
      「我的判断」。前者是可追溯的素材，后者才是我的想法。两者都可能进 wiki，
      但语义不同：划线是「这段值得记」，批注是「我的看法是…」。

    ★ 阶段 1 的锚点粒度 = **整篇**（不定位 DOM 选区）：`quoted_text` 是手动填的
      摘录文字，`anchor` 留空。选区级锚点（Range/XPath）等 chrome 插件阶段正文
      由我们自己剪藏、DOM 可控时再上 —— 在 Miniflux 每次抓取都可能变的第三方
      HTML 上做文本定位，脆且维护成本高（已与用户对齐）。
    """

    source_type = models.CharField(max_length=12, choices=SOURCE_TYPES, db_index=True)
    source_id = models.CharField(max_length=120, db_index=True)
    quoted_text = models.TextField()                       # 摘录的原文（≠ 我的观点）
    # ★ 可选备注：**我自己敲的字**，是这条划线里唯一属于「我的思考」的部分。
    #   划线本身只表示「我在意这一段」——可能是重要事实，可能是我认可的他人判断，
    #   都不是我的结论（用户 2026-10-31 纠正）。别把它写成「我的批注」：批注是
    #   Annotation，另一张表。
    note = models.TextField(blank=True, default="")
    # ★ 锚点定位（阶段 1 留空）：report 的段落号 / 未来的选区坐标。存成字符串，
    #   不同 source_type 的锚点格式不同，不强行统一成结构化字段。
    anchor = models.CharField(max_length=200, blank=True, default="")
    # ★ 软删（2026-11-05）：删除 = 不想要。不想要的东西既不在页面展示、也不该
    #   再拿去提炼溯源 —— 两者是同一个语义。list_marks 一处排除即同时作用于
    #   展示与提炼，不做「展示排、提炼留」的区分。物理删会连 mark_id 指向的
    #   溯源一起抹掉，软删保留行，复活时直接复用旧 id。
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = LiveMarkQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["source_type", "source_id", "created_at"])]

    def __str__(self):
        return f"highlight {self.source_type}:{self.source_id} {self.quoted_text[:24]}"


class Annotation(models.Model):
    """批注：对某个内容块写的「我的判断」（区别于划线的「摘录原文」）。

    ★ 没有 quoted_text（那是划线的职责），但可以带 anchor 定位到具体位置。
    ★ 阶段 1 同样整篇级：body 就是我对这篇文章/这份报告的一段看法。
    """

    source_type = models.CharField(max_length=12, choices=SOURCE_TYPES, db_index=True)
    source_id = models.CharField(max_length=120, db_index=True)
    body = models.TextField()                              # 我的判断
    anchor = models.CharField(max_length=200, blank=True, default="")
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = LiveMarkQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["source_type", "source_id", "created_at"])]

    def __str__(self):
        return f"annotation {self.source_type}:{self.source_id} {self.body[:24]}"


# ---------------------------------------------------------------------------
# 对话（阶段 2.1）
# ---------------------------------------------------------------------------

class Conversation(models.Model):
    """一个内容块上的**长期**对话（不是一次性问答）。

    ★ 与 Highlight/Annotation 同样挂 `(source_type, source_id)` 软引用、不建 FK：
      条目真相在 Miniflux、报告真相在文件里，本地只存「在我这儿发生过什么」。
    ★ 为什么按内容块唯一（而不是每次开新会话）：wiki 的命题是「对话后才沉淀」，
      同一篇文章的追问是连续的 —— 换会话等于把上下文丢了，让人重新交代一遍。
      真要从头开始，由「清空」显式删掉（见 core/conversation.clear）。
    """

    source_type = models.CharField(max_length=12, choices=SOURCE_TYPES, db_index=True)
    source_id = models.CharField(max_length=120, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_type", "source_id"],
                                    name="uniq_conversation_per_source"),
        ]

    def __str__(self):
        return f"conversation {self.source_type}:{self.source_id}"


class ChatMessage(models.Model):
    """对话里的一条消息。`user` = 我问的，`assistant` = 模型答的。

    ★ 只存正文，**不存成本**：钱的事实源是 `LlmUsage`（按 kind=wiki_chat 汇总），
      在消息上再抄一份必然漂（缓存命中价、高低峰切换后历史价都会变）。
    """

    ROLES = (("user", "我"), ("assistant", "AI"))

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     related_name="messages")
    role = models.CharField(max_length=12, choices=ROLES)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["conversation", "id"])]

    def __str__(self):
        return f"{self.role} {self.body[:24]}"


#: 提取出来的候选，按「这值得留吗」的五类判据分。（意外/复用/决策/坑/可迁移）
INGEST_KINDS = (
    ("surprise", "意外"),
    ("reuse", "复用"),
    ("decision", "决策"),
    ("pitfall", "坑"),
    ("transfer", "可迁移"),
)

INGEST_STATUS = (("pending", "待审"), ("accepted", "已采纳"), ("discarded", "已丢弃"))

#: 「还活着」的候选状态 —— 编译候选（compile_backlog）与审批原语（wiki_agent.read）
#: 共用这一份。★ 白名单而非 `exclude(discarded)`：将来加新状态时，没显式列入的
#: 不会被悄悄吞进一次花钱的编译。两处各写一遍必然漂成「面板说有、read 说没有」。
INGEST_LIVE_STATUS = ("pending", "accepted")


class IngestCandidate(models.Model):
    """一次提取吐出的**一条候选**（阶段 2.2）。人审过了才进 2.3 编译。

    ★ 为什么不存文件：候选有状态流转（待审 → 采纳/丢弃）、要按来源查、要给
      2.3 编译按 id 取。它是「人机交互的产物」，和 Highlight/Annotation/
      Conversation 同类 —— 知识本体仍在 `wiki/` 的文件里，这里只放**还没成为
      知识的东西**。阶段 0 那条「只碰文件不碰 DB」的纪律管的是本体，不是它。
    ★ 为什么不建 FK：同样挂 `(source_type, source_id)` 软引用，理由与
      Highlight 一致（条目真相在 Miniflux、报告真相在文件里）。
    ★ `quote` 是**我敲的原话**（逐字），`claim` 是模型归纳的一句话判断。
      `quote` 由 `core/ingest.verify_quotes()` 做程序校验，对不上就丢 ——
      这是「AI 转述 vs 我的判断」的机械保证，光靠 prompt 约束不住。
    ⚠️ 不存成本：钱的事实源是 LlmUsage（kind=wiki_extract）。
    """

    batch = models.CharField(max_length=16, db_index=True)      # 一次提取 = 一个 batch
    source_type = models.CharField(max_length=12, choices=SOURCE_TYPES, db_index=True)
    source_id = models.CharField(max_length=120, db_index=True)
    kind = models.CharField(max_length=16, choices=INGEST_KINDS)
    claim = models.TextField()                                   # 我的一句话判断（模型归纳）
    quote = models.TextField()                                   # 我的原话（逐字，程序校验过）
    # ★ quote 从哪一层来（三层语义，见 2026-10-31 定稿）：
    #   mine      = 我敲的字（对话/划线备注/批注）→ 我的判断
    #   highlight = 我划出的原文 → 我在意/认可的事实或他人观点，**不是我的原创判断**
    #   2.3 编译时必须靠它区分：前者是「我的结论」，后者是「我摘出来的素材」，
    #   写 sources / 判断语气都不同。运行时推断会把这判据在编译处再复制一份。
    quote_from = models.CharField(max_length=12, default="mine")
    # ★ 深链锚点：这条 quote 具体出自**哪一条**记录，编译阶段直接拿它拼深链，
    #   不再靠文本反查（文本反查有两死穴：同话说过多次会配错、quote 是子串会误配）。
    #   提取阶段（verify_quotes 判定命中的那一刻）是唯一知道「来自哪一条」的时刻，
    #   在这里存下来，编译处读字段 —— 判据/真相只有一份（2026-11-02 定）。
    #   mark_id：定位到划线/批注（?mk=）；msg_id：定位到对话里的具体消息（?msg=）。
    #   旧候选两字段为空（migration 加列），深链退回来源层级。
    mark_id = models.IntegerField(null=True, blank=True, db_index=True)
    msg_id = models.IntegerField(null=True, blank=True, db_index=True)
    domain = models.CharField(max_length=32, blank=True, default="")
    confidence = models.CharField(max_length=8, blank=True, default="")
    # ★ 内容的**发布时间**（不是我划线/说话的时间，不是提炼时间，也不是抓取入库
    #   时间）。由 `core.sourceblock.load()` 的 `published_at` 带上，拿不到留 None。
    #   decay（衰减）全靠它排序：编一个「今天」等于把旧判断伪装成新判断
    #   （典型：一句带「明天/下周」的操作建议，半年后会被误读成当下仍有效）。
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # ★ 自由多值标签（folksonomy，与受控单继承的 domain 正交）。本步只打通
    #   字段 + 迁移 + 必填校验，一律落 []；模型建议 / 审批增删属后续环节。
    tags = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=INGEST_STATUS,
                              default="pending", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["source_type", "source_id"])]

    def __str__(self):
        return f"{self.get_kind_display()}·{self.claim[:24]}"


class CategoryLabel(models.Model):
    """GReader 数据源的分类（FreshRSS label）↔ 本地 int id 映射。

    ★ 为什么需要这张表：Google Reader API 的分类只有字符串
      `user/-/label/<名字>`，而 DataSource 接口要求 int 型 category_id。
      用字符串哈希凑 id 会在改名 / 碰撞时埋雷（同一名字换 id、不同名字撞 id），
      所以首次见到 label 时分配自增 id 并持久化。重启后 id 不变，
      本地按 category_id 存的分级配置（CategoryTriageConfig 等）才不会丢。

    ★ `local_only`：GReader 不能凭空创建空分类（只有把 feed 挂到 label 上，
      FreshRSS 才会建分类）。`create_category()` 只在本地登记一行并把
      `local_only=True`，等第一次有 feed 挂上来后自动转正。
      树里只显示「订阅里出现的 label」+「local_only 且 active 的本地分类」。

    ★ 删除用**软删除**（active=False）而不是删行：SQLite 的自增 id 会复用
      最大 id，硬删后再建一个同名/不同名分类可能拿到旧 id，历史分级配置就会
      悄悄挂到另一个分类上。软删除保留 id；同名 label 再出现时原 id 复活。
    """

    label = models.CharField(max_length=255, unique=True)
    active = models.BooleanField(default=True)
    local_only = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["active", "local_only"])]

    def __str__(self):
        return f"label:{self.label} → #{self.pk}"
