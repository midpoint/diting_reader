"""Django settings for the diting reader."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ★ 便携 exe（PyInstaller）把源码解压到临时目录，而数据/.env/Meta 必须落在
#   **exe 旁边**、可写且不会随进程退出被清掉。启动器设置 DITING_HOME 指向
#   exe 所在目录；源码运行不设置它，行为与原来完全一致。
_DITING_HOME = os.environ.get("DITING_HOME", "").strip()
PROJECT_ROOT_HINT = (Path(_DITING_HOME).expanduser().resolve()
                     if _DITING_HOME else BASE_DIR.parent)

sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "apps"))


def _load_dotenv(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        env[key.strip()] = val
    return env


_FILE_ENV = _load_dotenv(PROJECT_ROOT_HINT / ".env")


def env(key: str, default: str = "") -> str:
    """Process env wins over .env file."""
    return os.environ.get(key) or _FILE_ENV.get(key) or default


# ---------------------------------------------------------------- Django 基线
# ★ 本机自用默认：DEBUG 开、只听回环（README 的合规立场建立在「本地部署、
#   不会意外变成可公网访问」上）。哪天要从局域网/VPN 访问，用环境变量显式放开：
#     DJANGO_DEBUG=0                       关 DEBUG（对外访问前必须关）
#     DJANGO_ALLOWED_HOSTS=hosta,hostb     允许的主机名（默认只听 127.0.0.1,localhost）
#     DJANGO_SECRET_KEY=<随机串>           关 DEBUG 时必须换掉默认 key，否则拒绝启动
SECRET_KEY = env("DJANGO_SECRET_KEY", "diting-reader-dev-only-insecure")
DEBUG = env("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [h.strip() for h in
                 env("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
                 if h.strip()]
if not DEBUG and SECRET_KEY == "diting-reader-dev-only-insecure":
    # 便宜保险：DEBUG 页会吐 settings/环境/URL 全貌，带默认 key 对外等于裸奔
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "DJANGO_DEBUG=0 但还在用开发默认 SECRET_KEY —— 对外访问前必须设置 DJANGO_SECRET_KEY")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "core",
    "reader",
    "triage",
    "costs",
    "summary",
    # ★ 虚拟分组层（阅读页树的"分类 → 分组 → feed"中间层）。独立成一个 app 是为了
    #   让"它不参与分级/总结/开销"这件事在目录结构上就自解释。
    "groups",
    # ★ 标题翻译（feed 级开关 / 回填 / 重译 / 状态轮询）。模型在 core，
    #   独立 app 只放读写接口。
    "translate",
    # ★ 日志查看页（只读 data/logs/*.log）。独立 app：它不碰库、不碰 Miniflux，
    #   放这里一眼能看出"日志页没有副作用"。
    "logs",
    # ★ 设置页（渲染 core/settingkit.py 注册表 + 保存/回退，以及数据源/模型的
    #   .env 配置）。行为设置走 AppSetting；密钥写根目录 .env，不进数据库。
    "prefs",
    # ★ 知识库页（/wiki：草稿审批 + 已入库页浏览）。本 app 不调 LLM、不碰库，
    #   一切文件读写在 core/wiki.py —— 所以「审批这条链」能离线单测。
    "wiki",
]

MIDDLEWARE = [
    # ★ 放最外层：它要量的是**整个链路**的耗时（含后续中间件与视图）
    "core.reqlog.RequestLogMiddleware",
    # 数据源不可用 → 人话 502（HTMX 片段用 200 横幅），不把堆栈丢给用户
    "core.middleware.DataSourceErrorMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        # ★ Django 6.1 起，OPTIONS 里**没有显式 loaders** 时会**无条件**把 loader 包进
        #   `cached.Loader`（`django/template/engine.py` Engine.__init__：老版本那句
        #   `if not debug:` 已经删了）—— 也就是 `OPTIONS.debug=True` **不再能关掉模板缓存**，
        #   改了 .html 不重启服务永远看不到（2026-10-15 实测：加了个 HTML 注释标记，
        #   响应里根本不出现）。所以这里**显式列出 loaders** 绕开缓存。
        #   注意：一旦显式定义 loaders，Django 就要求 APP_DIRS 必须为 False。
        "APP_DIRS": False,
        "OPTIONS": {
            "debug": True,
            "loaders": [
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ],
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.debug",
            ],
        },
    },
]

DATA_DIR = ((PROJECT_ROOT_HINT / "data") if _DITING_HOME else (BASE_DIR / "data"))
if not DATA_DIR.exists():
    DATA_DIR.mkdir(parents=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DATA_DIR / "reader.sqlite3"),
        # WAL / busy_timeout 由 core.apps 的 connection_created 钩子设置
        "OPTIONS": {"timeout": 20},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "Asia/Shanghai"

LANGUAGE_CODE = "zh-hans"

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

# ---------------------------------------------------------------- 业务配置
# 数据源：miniflux（默认，行为不变）| greader（FreshRSS 的 Google Reader 兼容 API）
DATASOURCE_ENGINE = env("DATASOURCE_ENGINE", "miniflux").strip().lower()

MINIFLUX_BASE_URL = env("MINIFLUX_URL", "http://localhost:8080").rstrip("/")
MINIFLUX_USERNAME = env("MINIFLUX_ADMIN_USERNAME", "")
MINIFLUX_PASSWORD = env("MINIFLUX_ADMIN_PASSWORD", "")

# FreshRSS / Google Reader API（GREADER_BASE_URL 形如 https://host/api/greader.php）
# ★ 密码是 FreshRSS 个人资料页的「专用 API 密码」，不是网页登录密码；
#   只放在 .env（已 gitignore），日志里不打印密码与 Auth token。
GREADER_BASE_URL = env("GREADER_BASE_URL", "").rstrip("/")
GREADER_USERNAME = env("GREADER_USERNAME", "")
GREADER_PASSWORD = env("GREADER_PASSWORD", "")

# 列表窗口：/v1/entries 单次 limit 上限 1000，这里取一个够用又不重的数
ENTRY_WINDOW = int(env("READER_ENTRY_WINDOW", "200"))

# ★ 虚拟分组视图（多源列表）：Miniflux 只支持单 feed_id，只能逐源拉取后归并。
#   这是**每源**取多少条 —— 源多时先减这个数，别减 ENTRY_WINDOW（那是最终展示上限）。
MULTI_FEED_PER_SOURCE = int(env("READER_MULTI_FEED_PER_SOURCE", "60"))

# 树 / 计数缓存秒数（避免每次交互都打 Miniflux）
TREE_CACHE_TTL = int(env("READER_TREE_CACHE_TTL", "20"))

# GReader 条目流缓存：同一筛选的短时间重复请求（首屏 + 局部刷新）不再重复翻页。
# ★ 只缓存 UI 规模的结果（limit <= 300），日报/分级那种 1000 条的大列表不驻留内存。
GREADER_ENTRY_CACHE_TTL = int(env("GREADER_ENTRY_CACHE_TTL", "15"))
GREADER_ENTRY_CACHE_MAX = int(env("GREADER_ENTRY_CACHE_MAX", "12"))
# 图标抓取失败（404/超时/非图片）的负缓存秒数：避免每页都重试拖慢首屏。
GREADER_ICON_MISS_TTL = int(env("GREADER_ICON_MISS_TTL", "300"))
GREADER_ICON_MAX_BYTES = int(env("GREADER_ICON_MAX_BYTES", str(256 * 1024)))

# M2 级别过滤：未评级条目按此级别参与过滤（M3 后由 default_level 决定）
UNRATED_LEVEL = int(env("READER_UNRATED_LEVEL", "2"))

# ---------------------------------------------------------------- LLM（M3/M4）
# 主力通道 DeepSeek（便宜且稳）；Agnes 免费但会 429
# ★ 环境变量名与设置名对齐：新名 LLM_PROVIDER；旧名 TRIAGE_PROVIDER 仍可读
#   （早期「provider 只服务分级」时的名字，两者都给时新名优先）
LLM_PROVIDER = env("LLM_PROVIDER", env("TRIAGE_PROVIDER", "deepseek")).lower()
DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEEPSEEK_MODEL = env("DEEPSEEK_MODEL", "deepseek-chat")
AGNES_API_KEY = env("AGNES_API_KEY", "")
AGNES_BASE_URL = env("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_MODEL = env("AGNES_MODEL", "agnes-2.5-flash")

# DeepSeek 官方价（USD / 1M tokens）；高峰时段翻倍
DEEPSEEK_PRICE = {"cache_miss": 0.15, "cache_hit": 0.003, "output": 0.60}
USD_CNY = float(env("USD_CNY", "7.1"))

# ---------------------------------------------------------------- 分级（M3）
TRIAGE_ENABLED = env("TRIAGE_ENABLED", "1") == "1"
TRIAGE_INTERVAL_MIN = int(env("TRIAGE_INTERVAL_MIN", "5"))
TRIAGE_SCAN_LIMIT = int(env("TRIAGE_SCAN_LIMIT", "1000"))   # /v1/entries 单次上限就是 1000
TRIAGE_BATCH_SIZE = int(env("TRIAGE_BATCH_SIZE", "10"))
TRIAGE_REPEAT = int(env("TRIAGE_REPEAT", "3"))              # 每条最多几票
TRIAGE_ADAPTIVE = env("TRIAGE_ADAPTIVE", "1") == "1"        # 只有 L0 才补满票
TRIAGE_MAX_CALLS = int(env("TRIAGE_MAX_CALLS", "60"))       # 单次 run 的调用上限
TRIAGE_CHAR_LIMIT = int(env("TRIAGE_CHAR_LIMIT", "900"))    # 单条喂给模型的字数

# ★ 判级窗口：只看最近 N 天内的条目（超出时间窗的历史文章不再判级，防止海量历史拖垮 token）
TRIAGE_LOOKBACK_DAYS = int(env("TRIAGE_LOOKBACK_DAYS", "7"))
# ★ feed 粒度上限：单个 feed 每轮最多判 N 条（超出留到下一轮，防某个源一次性涌入巨量文章）
TRIAGE_PER_FEED_LIMIT = int(env("TRIAGE_PER_FEED_LIMIT", "50"))

# 日预算（¥）：超限后本轮不再调 LLM，直接停止（条目留到下一轮）
# ★ 分级与总结**各一个池子**。原先共用一个上限，补跑报告会挤掉当天分级
#   （实测：补跑 8 天花 ¥1.28 → 分级按单池上限直接停摆，而总结还有额度）。
#   /costs 页可分别改，写 AppSetting（triage_budget_cny / summary_budget_cny）。
# ★ 2026-11-17：下列默认值已按真实库实测值校准（AppSetting > .env > 代码默认）。
#   开源部署没有 AppSetting 行，会直接吃这些默认值 —— 别把它们当占位数字随手改大。
DAILY_BUDGET_CNY = float(env("DAILY_BUDGET_CNY", "3"))      # 旧单池键，仅兼容
TRIAGE_BUDGET_CNY = float(env("TRIAGE_BUDGET_CNY", "0.6"))
SUMMARY_BUDGET_CNY = float(env("SUMMARY_BUDGET_CNY", "1.5"))

# ---------------------------------------------------------------- 快速刷新（M6）
# 只刷 FeedOverride.fast_refresh=True 的源；走 API（绕过 worker 池与 host 限额）
FAST_REFRESH_ENABLED = env("FAST_REFRESH_ENABLED", "1") == "1"
FAST_REFRESH_TICK_SEC = int(env("FAST_REFRESH_TICK_SEC", "60"))   # 检查间隔（精度）
# ★ 默认 15 分钟：雪球有 WAF，先慢后快（设计风险 #8）
FAST_REFRESH_DEFAULT_MIN = int(env("FAST_REFRESH_DEFAULT_MIN", "15"))

# ---------------------------------------------------------------- 周期总结（M7）
SUMMARY_ENABLED = env("SUMMARY_ENABLED", "1") == "1"
SUMMARY_HOUR = int(env("SUMMARY_HOUR", "7"))                  # 07:30 编排
SUMMARY_MINUTE = int(env("SUMMARY_MINUTE", "30"))
SUMMARY_WINDOW_HOUR = int(env("SUMMARY_WINDOW_HOUR", "6"))    # 窗口边界 06:00
# ★ 补救刷新后最多等多久让 feed 变新鲜（不是"盲睡"，而是每 POLL_SEC 复检一次）。
#   2026-10-22 实测：机器 20:27 睡到 07:54，日报 08:12 采集时仍有 36 个源停在 20:17；
#   Miniflux 的下一批直到 08:15:43 才开闸 —— 固定 sleep(15min) 根本无法对齐 60 分钟开闸周期。
#   旧键 SUMMARY_WAIT_MIN 仍兼容（作为本项 fallback），但语义已从"睡多久"改成"最多等到多久"。
SUMMARY_FRESH_DEADLINE_MIN = int(env(
    "SUMMARY_FRESH_DEADLINE_MIN", env("SUMMARY_WAIT_MIN", "30")))
SUMMARY_FRESH_POLL_SEC = int(env("SUMMARY_FRESH_POLL_SEC", "20"))  # 复检间隔
# 补救刷新候选上限：0 = 全部 stale（2026-10-22 前写死 10，导致 36 个源被静默跳过）
SUMMARY_REFRESH_MAX = int(env("SUMMARY_REFRESH_MAX", "0"))
# 刷新并发：Miniflux 的 refresh API 是同步的，串行 36 个可能 20 分钟以上。
SUMMARY_REFRESH_WORKERS = int(env("SUMMARY_REFRESH_WORKERS", "4"))
# ★ 窗口后瞬时错误重试：这类错误不影响本期窗口，但会留在 Miniflux 的
#   parsing_error_count 里，旧日报会把它们逐条写成“解析错误”（2026-10-23 实测 14 条噪音）。
#   现在日报只报“窗口内缺口”，同时在这里顺手重试 DNS/EOF/timeout/5xx 这类瞬时错误。
SUMMARY_RETRY_TRANSIENT_ERRORS = env("SUMMARY_RETRY_TRANSIENT_ERRORS", "1") == "1"
SUMMARY_RETRY_TRANSIENT_MAX = int(env("SUMMARY_RETRY_TRANSIENT_MAX", "20"))
SUMMARY_WAIT_MIN = SUMMARY_FRESH_DEADLINE_MIN   # 兼容旧名（已弃用，勿在新代码里用）
SUMMARY_GUARD_MIN = int(env("SUMMARY_GUARD_MIN", "30"))       # 兜底检查间隔
SUMMARY_STALE_MIN = int(env("SUMMARY_STALE_MIN", "90"))       # 死线程残留超此挂钟年龄 → 重置（活线程永不重置，见 summary._ACTIVE_RUNS）

# ---------------------------------------------------------------- 周报（M9）
# 周报 = 过去 7 天日报的合集（不重采、不重判），每周日中午跑一次。
WEEKLY_ENABLED = env("WEEKLY_ENABLED", "1") == "1"
WEEKLY_WEEKDAY = int(env("WEEKLY_WEEKDAY", "6"))             # 0=周一 … 6=周日
WEEKLY_HOUR = int(env("WEEKLY_HOUR", "12"))
WEEKLY_MINUTE = int(env("WEEKLY_MINUTE", "0"))
# ★ 缺日报时的重试窗口（小时）：只针对**当天**日报（它还在被 guard 补跑，很可能马上出来）；
#   缺的是历史某天 → 立即 fail，交给手动（历史日报不会自己出现）。
WEEKLY_RETRY_HOURS = int(env("WEEKLY_RETRY_HOURS", "2"))
# ★ 独立预算池：周日 12:00 时当天日报已花掉一部分，周报挤在总结池里会撞闸
#   （撞闸的表现是"周报没生成也没报错"）。单次实测 ≈¥0.13。
WEEKLY_BUDGET_CNY = float(env("WEEKLY_BUDGET_CNY", "0.5"))
WEEKLY_MAX_CALLS = int(env("WEEKLY_MAX_CALLS", "2"))
# ★ 输出预算必须够大：7 份日报 ≈30k 输入 tokens，而**推理链与正文共享 max_tokens**
#   —— 4k 时正文在第三段就被硬截断（2026-10-29 实测）；推理链波动很大
#   （实测 5k ~ 22k，加了「star 里的线索」后更爱长考）。
#   ★ 2026-10-29 二次实测：16k 仍然顶满（completion=16383，正文在第五段中间断开），
#     提到 32k。判断"够不够"看 `LlmUsage.completion_tokens` 是否**正好等于**上限 ——
#     等于就是被截断，不是模型刚好写完。
WEEKLY_MAX_TOKENS = int(env("WEEKLY_MAX_TOKENS", "32768"))
# ★ 已无「star 清单」一章（2026-10-29 移除：明细交给阅读器的 /?sel=starred），
#   所以 `WEEKLY_STAR_SHOW`（每类列几条）随之作废，别再配它。
SUMMARY_LOOKBACK_DAYS = int(env("SUMMARY_LOOKBACK_DAYS", "3"))   # 自动补跑只扫最近 N 天
SUMMARY_MAX_CATCHUP = int(env("SUMMARY_MAX_CATCHUP", "3"))       # 一轮最多补几天
SUMMARY_MAX_ATTEMPT = int(env("SUMMARY_MAX_ATTEMPT", "3"))       # 单日自动重试上限
# ★ 缺口清单的回看深度：比自动补跑窗口深得多，只用于**列出来给人看**，
#   每行带手动补跑按钮 —— 自动补跑永远只碰 LOOKBACK 天。
SUMMARY_GAP_LOOKBACK_DAYS = int(env("SUMMARY_GAP_LOOKBACK_DAYS", "30"))
# 分层阈值（S3 单篇 → S2 按源 → S1 按分类 → S0 全局；S=summary，不是分级 L）
# ★ 命名于 2026-10-29 从 L1/L2/L3/L0 改为 S3/S2/S1/S0：与分级 L 前缀彻底分开。
SUMMARY_S3_CHARS = int(env("SUMMARY_S3_CHARS", "3000"))       # 单篇 > 此字数走 S3 压缩
SUMMARY_S2_MIN = int(env("SUMMARY_S2_MIN", "8"))              # 单源 ≥ 此条数走 S2
# ★ 单源条目超过此数 → 先分批各自汇总、再合并成一段（core/summary.py `_s2_pass`）。
#   起因（实测）：某源一个窗口 34 条同源动态，一次做同主题去重/合并
#   会把推理链撑到 1 万 token 预算都装不下（completion=9999 被硬截断）。
#   代价：单源调用数从 1 变成 ceil(n/BATCH)+1，注意 SUMMARY_MAX_CALLS 与总结池预算。
SUMMARY_S2_BATCH = int(env("SUMMARY_S2_BATCH", "20"))
SUMMARY_S1_MIN_SOURCES = int(env("SUMMARY_S1_MIN_SOURCES", "2"))
SUMMARY_S1_MIN_ENTRIES = int(env("SUMMARY_S1_MIN_ENTRIES", "5"))
SUMMARY_MATERIAL_CHARS = int(env("SUMMARY_MATERIAL_CHARS", "1200"))  # 素材阶段单条正文上限
# ★ 单源条目过多时只丢 L1（低价值/普通），L2/L3 一条不丢；保留最近 K 条 L1。
#   只影响是否进入日报素材，不标已读、不删 Miniflux、不改评级。
SUMMARY_FEED_MAX_ITEMS = int(env("SUMMARY_FEED_MAX_ITEMS", "40"))
SUMMARY_FEED_L1_KEEP = int(env("SUMMARY_FEED_L1_KEEP", "20"))
# ★ 引用转发（RSSHub twitter 的 `<div class="rsshub-quote">`）保留的引文字数。
#   起因（实测）：某源一个窗口 34 条里，本人只写 1960 字、被引用方写了
#   7225 字（79%）—— 引文是**别人**的观点，不是他的，全量入 prompt 纯属噪声。
#   只留"提子"够模型判断这条在讲什么，不必读完整篇。
SUMMARY_QUOTE_CHARS = int(env("SUMMARY_QUOTE_CHARS", "120"))
SUMMARY_MAX_ENTRIES = int(env("SUMMARY_MAX_ENTRIES", "400"))  # 窗口条目上限（防爆）
SUMMARY_MAX_CALLS = int(env("SUMMARY_MAX_CALLS", "120"))      # 单次运行调用上限（40→120，2026-10-29 三倍）
# 下层预留 1 次给 S0 全局合成：否则 S3/S2/S1 会把调用数吃光，整份日报降级成规则版。
SUMMARY_S0_RESERVE = int(env("SUMMARY_S0_RESERVE", "1"))
# 产物缓存（S3/S2/S1；S0 不缓存）：输入指纹不变才复用，TTL 只是上限。
SUMMARY_CACHE_ENABLED = env("SUMMARY_CACHE_ENABLED", "1") == "1"
SUMMARY_CACHE_TTL_DAYS = int(env("SUMMARY_CACHE_TTL_DAYS", "7"))

# ★ 输出预算（tokens）。推理模型（deepseek-flash / *-reasoner）的推理链**计入**
#   completion_tokens 并与正式回答共享 max_tokens —— 预算必须按"推理 + 正文"两倍以上给，
#   否则推理吃光配额、正文为空（实测：300 预算时 content 完全为空）。
#   ★ 取值依据（2026-10-16 实测，run 18）：S2 5000 / S1 6000 会撞上限 ——
#     某源（34 条同源动态）第一次 5000 正文为空、翻倍到 10000 仍被硬截断
#     （completion=9999）；cat=2 投资一次打满 6000。故上调到 12000 / 9000。
#   ★★ S3 3000 → 8000（逐行查 `LlmUsage` 得出，起因是日报告警
#     「S3 单篇 …（预算 3000 tokens）」）：
#     · 649 次 S3 调用里 29 次输出正好卡在 3000（4.5%）—— 卡在上限的堆积就是截断指纹；
#     · 其中 25 次是**推理链单独吃光 3000、正文一个字没出**（触发翻倍），翻到 6000
#       **全部写完**（最高 5877，从没二次撞顶）；中位数只有 801 tokens。
#     ⇒ 3000 卡在推理链的腰上，不是「够用」。S3 的输出有**硬上限**（100–200 字 ≈
#       300 tokens），它随推理长度涨、不随 max_tokens 涨，所以抬额度**不额外花钱**
#       （按实际输出计费）：96% 的调用一分不多花，撞顶那 4.5% 从「3000 白花 + 6000
#       重打」变成一次跑完，反而更省。
#     ★ 别把这条结论套到 wiki 分组：那里输出量随**批内候选条数**线性涨，抬额度只是
#       把墙挪一格（8192 已证伪），正解是拆批。两处形状不同。
SUMMARY_S3_MAX_TOKENS = int(env("SUMMARY_S3_MAX_TOKENS", "8000"))
SUMMARY_S2_MAX_TOKENS = int(env("SUMMARY_S2_MAX_TOKENS", "12000"))
SUMMARY_S1_MAX_TOKENS = int(env("SUMMARY_S1_MAX_TOKENS", "9000"))
SUMMARY_S0_MAX_TOKENS = int(env("SUMMARY_S0_MAX_TOKENS", "16000"))

# ---------------------------------------------------------------- 标题翻译（中英对照）
# ★ 只翻标题，不翻正文；feed 级开关默认关；独立 kind / 独立预算池。
#   回填「最近 7 天、每 feed 最多 50 条」；后续新文章由独立 tick 自动翻。
TRANSLATE_ENABLED = env("TRANSLATE_ENABLED", "1") == "1"
TRANSLATE_LOOKBACK_DAYS = int(env("TRANSLATE_LOOKBACK_DAYS", "7"))
TRANSLATE_BACKFILL_MAX = int(env("TRANSLATE_BACKFILL_MAX", "50"))   # 每 feed 回填上限 N
TRANSLATE_PER_TICK_MAX = int(env("TRANSLATE_PER_TICK_MAX", "20"))   # 每 feed 每 tick 增量上限
TRANSLATE_BATCH_SIZE = int(env("TRANSLATE_BATCH_SIZE", "20"))       # 一次调用翻多少条标题
TRANSLATE_BATCH_CHARS = int(env("TRANSLATE_BATCH_CHARS", "4000"))   # 单批标题总字符上限（超了拆批）
TRANSLATE_TICK_SEC = int(env("TRANSLATE_TICK_SEC", "300"))          # 增量扫描间隔
TRANSLATE_SCAN_MIN = int(env("TRANSLATE_SCAN_MIN", "5"))            # 同一 feed 两次扫描的最小间隔（分钟）
TRANSLATE_CHAR_LIMIT = int(env("TRANSLATE_CHAR_LIMIT", "400"))      # 超过此长度的标题跳过（不截半句）
TRANSLATE_MAX_TOKENS = int(env("TRANSLATE_MAX_TOKENS", "2048"))     # 单批输出预算
TRANSLATE_MAX_ATTEMPTS = int(env("TRANSLATE_MAX_ATTEMPTS", "3"))    # 单条自动重试上限
# ★ 翻译与分级/总结各一个池子：翻译是低优先级、可中断的链路，
#   不能因为回填把当天的分级预算挤掉（与 2026-10-15 两池拆分的教训一致）。
TRANSLATE_BUDGET_CNY = float(env("TRANSLATE_BUDGET_CNY", "0.3"))
# 非 DeepSeek provider 的 cost_cny 为 None，预算池无法累计 → 用调用次数兜底
TRANSLATE_MAX_CALLS_PER_DAY = int(env("TRANSLATE_MAX_CALLS_PER_DAY", "200"))
# 增量扫描单次取多少条（Miniflux 单次上限 1000；7 天窗口通常远小于此）
TRANSLATE_SCAN_LIMIT = int(env("TRANSLATE_SCAN_LIMIT", "500"))

# ---------------------------------------------------------------- 系统通知
# macOS 通知（terminal-notifier）。用于三件事：L3 点名、日报生成完成、日报生成失败。
# ★ 为什么默认**开**：通知没有副作用、可逆、不写任何外部系统；
#   而且这个开关本身就是"我要这个功能"的意思。
NOTIFY_ENABLE = env("NOTIFY_ENABLE", "1") == "1"
# 后端：mac = terminal-notifier（默认行为不变）；win = 用 sys.executable 跑 shim → winotify。
# auto 按平台选（win32 → win）。Windows 用户还需 pip install winotify（纯 Python 零编译依赖）。
NOTIFY_BACKEND = env("NOTIFY_BACKEND", "auto")
NOTIFY_BIN = env("NOTIFY_BIN", "terminal-notifier")
# Windows shim 路径，留空 = 用随包的 core/notify_win.py（一般不用改）
NOTIFY_SHIM = env("NOTIFY_SHIM", "")
NOTIFY_SOUND = env("NOTIFY_SOUND", "default")
# 失败单独一个提示音：成功是"知道了"，失败是"得看一眼"。留空 = 失败静音。
NOTIFY_FAIL_SOUND = env("NOTIFY_FAIL_SOUND", "Basso")
NOTIFY_TIMEOUT = int(env("NOTIFY_TIMEOUT", "10"))
# 分项开关：只想安静一半的时候用
NOTIFY_L3 = env("NOTIFY_L3", "1") == "1"
NOTIFY_DAILY = env("NOTIFY_DAILY", "1") == "1"
NOTIFY_WEEKLY = env("NOTIFY_WEEKLY", "1") == "1"
# ★ 失败的开关**独立**于成功那条：静默失败会造成 FOMO（一整天没有日报，没人告诉你），
#   所以默认开；想去噪（比如正在手工排查）可以单独关掉它而不影响完成通知。
NOTIFY_DAILY_FAIL = env("NOTIFY_DAILY_FAIL", "1") == "1"
# 勿扰/专注模式默认会吞掉通知（尊重系统设置）；打开这个让它穿透
NOTIFY_IGNORE_DND = env("NOTIFY_IGNORE_DND", "0") == "1"
# 通知图标（-appIcon）。留空 → 自动用 static/favicon-256.png（白犬，与 favicon 同源）
NOTIFY_APP_ICON = env("NOTIFY_APP_ICON", "")
# L3 通知点击后要打开的阅读器地址（`?sel=feed:<源>&open=<条目>#row-<条目>` 拼在这后面）
NOTIFY_READER_BASE = env("NOTIFY_READER_BASE", "http://127.0.0.1:8099").rstrip("/")

# ---------------------------------------------------------------- 常驻调度
ENABLE_SCHEDULER = env("ENABLE_SCHEDULER", "1") == "1"

# 项目根（Meta/interest-profile.md 在这里；便携 exe 下 = exe 所在目录）
PROJECT_ROOT = PROJECT_ROOT_HINT
REPORT_DIR = PROJECT_ROOT / "data" / "reports"
SUMMARY_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "summary"
# wiki 层（raw 快照 / 草稿 / 已审批页 / index / log）。★ 放 data/ 与 reports 同级：
# 都是运行时产物，天然被 .gitignore 挡住；想单独版本化，在 data/wiki 里 git init
# 即可（嵌套仓库不受父仓库 ignore 影响）。可用 WIKI_DIR 指到 Obsidian vault。
WIKI_DIR = Path(env("WIKI_DIR", str(PROJECT_ROOT / "data" / "wiki")))

# ★ 知识库独立预算池（第 5 池，2026-10-31）。wiki 的编译/对话/提取/lint 全是
#   **事件驱动**的开销（人什么时候用什么时候花），与日报/周报的定时批处理节奏完全不同：
#   混进总结池会重演「补跑报告把分级饿死」那一幕（只是方向反过来 —— 聊两句就吃掉当天日报的钱）。
# ★ 默认 0.5 已按真实库实测校准（2026-11-17）：最近 7 天 wiki 峰值 ¥0.427，
#   0.5 够用；此前「wiki 池预算已用尽」是老报告无退避重试刷日志，不是额度不足。
WIKI_BUDGET_CNY = float(env("WIKI_BUDGET_CNY", "0.5"))

# ---------------------------------------------------------------- 知识库对话（阶段 2.1）
# ★ 先做**非流式**闭环（POST + htmx 局部替换），流式（SSE）留作迭代：
#   流式要同时改 llm 层（现在请求里写死 `"stream": False`）+ 端点 + 前端读流，
#   三处一起动才有意义，半截做出来只会「看着像在打字，其实早返回完了」。
WIKI_CHAT_ENABLED = env("WIKI_CHAT_ENABLED", "1") == "1"
# 单片正文的目标字数。★ 2026-11-15 起**不再是「把长文截断到这里」**：超过它的
#   文章会由 `conversation.split_body()` 均衡分成若干片，逐片取材再汇总作答，
#   全文一字不丢（用户：「长文总结不可用了」）。语料实测（最近 300 条 Miniflux）：
#   p50 202 字 / p90 4735 字 / p95 7001 字 / 最长 27840 字，超过 6000 的 23 条
#   （7.7%）—— 也就是说 92% 的提问仍是「一次调用、零额外成本」的老路径。
# ★ 不是"文章多长"而是"每轮都重发一遍"的开销：6000 字 ≈ 6k tokens，
#   一轮 ¥0.01 量级；故长文才分片，短文不抬这个数。
WIKI_CHAT_ARTICLE_CHARS = int(env("WIKI_CHAT_ARTICLE_CHARS", "6000"))
# 长文最多分几片（2026-11-15，用户拍板 6）。★ 这是**单次提问的调用数闸**，
#   不是正文长度闸：真超过 6 片时把每片自动加长，`split_body` 仍覆盖全文，
#   绝不因为到了上限就把后半篇丢掉（截断正是这次要修的 bug）。
#   单次提问最多 n 片 + 1 次汇总；真实库最长 27840 字 ≈ 5 片 + 1 汇总。
WIKI_CHAT_MAX_CHUNKS = int(env("WIKI_CHAT_MAX_CHUNKS", "6"))
# 带多少条**历史消息**进 messages（UI 仍显示全量）。多轮越长越贵，且早期内容
# 早就在正文/划线的上下文里，没必要重复喂。
WIKI_CHAT_HISTORY = int(env("WIKI_CHAT_HISTORY", "6"))
WIKI_CHAT_MAX_TOKENS = int(env("WIKI_CHAT_MAX_TOKENS", "3072"))
WIKI_CHAT_TEMPERATURE = float(env("WIKI_CHAT_TEMPERATURE", "0.3"))
# 单条提问的长度上限（防把整篇文章粘进输入框当提问）
WIKI_CHAT_QUESTION_CHARS = int(env("WIKI_CHAT_QUESTION_CHARS", "2000"))

# ---------------------------------------------------------------- 入库分诊 + 提取（2.2）
# ★ 分诊和提取是**同一次调用**（设计 §2.6：不单独建一次分诊调用）——分诊的判据
#   （是我的判断 vs AI 转述）没法脱离材料判断，拆两次只会多花一遍正文钱。
WIKI_INGEST_ENABLED = env("WIKI_INGEST_ENABLED", "1") == "1"
# ★ 提炼门槛：**「我敲的字」+「我划出的原文」合计**低于这个值就**不调模型**（直接拒）。
#   理由：没有可沉淀的信号，让模型去提炼，得到的必然是它替我编的观点。
#   这是确定性的省钱闸，不是可调的风格参数。
#   ★ 2026-11-01 两次放宽：先从「仅我的字 ≥15」改「我的字+划线 ≥8」，再改成
#     「有一个字就认」（默认 1）—— 重要判断不一定长，一句「好」就代表认可，
#     一个字的判断也是判断。真正的信号是「有 vs 没有」，不是字数多少。
#     所以只有「完全没有划线、批注、对话判断」= 信号 0 才忽略，其余都放行。
WIKI_INGEST_MIN_CHARS = int(env("WIKI_INGEST_MIN_CHARS", "1"))
# 送进 prompt 的「我的原话」总字数上限（超了截断，并在材料里明说截断了）
WIKI_INGEST_WORDS_CHARS = int(env("WIKI_INGEST_WORDS_CHARS", "4000"))
# 正文只作上下文，不参与判断 —— 4000 字够模型理解话题，不必全文
WIKI_INGEST_ARTICLE_CHARS = int(env("WIKI_INGEST_ARTICLE_CHARS", "4000"))
# ★ 1600 → 4096（2026-11-02）：提取用的是推理模型，推理链 token 与正文
#   **共享**这一个额度（见 `citeguard._call` 模块注释）。1600 在真实材料上被
#   推理吃光，正文返回空串 → 触发「翻倍重试」，等于每条材料**必花两遍钱**，
#   重试还可能再撞上限。与周报分片的 4096 取同一档：这是「够不够用」不是
#   「贵不贵」—— 真正的花费闸是 POOL_WIKI 的日预算，把 max_tokens 卡小只会
#   让同一个结果多花一次调用。
# ★ 4096 → 8192（2026-11-09，实测后改）：提炼这一侧「预算调高」比「撞了再翻倍」
#   **更省**，两个理由都来自真实数据（清零重来那次 30 次提炼的 LlmUsage）：
#     1) 输出量有硬上限：一次最多 6 条候选（`WIKI_INGEST_MAX_CANDIDATES`），
#        撞顶几乎都是**推理链**吃掉配额，不是候选太多 —— 上限抬高不会让正文变长；
#     2) 计费按**实际输出**的 token 算，不按上限。所以高上限在正常情形下**零成本**，
#        只在真撞顶时才多花 —— 而「撞了再翻倍」要多花一整次调用
#        （实测 i=3214 / out=4096 / 27.7 s / ¥0.0399，那次 JSON 还是半截）。
#   对照组（同一个库、同一天）：编译分组撞 4096 → 翻倍 8192 **又撞顶**，
#   12288 output tokens 零收益 —— 那边输出量随候选条数涨，抬上限只是把墙往后挪，
#   所以那边改的是「拆批」（见 `ingest.plan_groups`）。两处结论相反，别互相套用。
WIKI_INGEST_MAX_TOKENS = int(env("WIKI_INGEST_MAX_TOKENS", "8192"))
WIKI_INGEST_TEMPERATURE = float(env("WIKI_INGEST_TEMPERATURE", "0.2"))
# 一次最多收几条候选。★ 上限不是省钱，是防「一篇文章吐 20 条」把审批变成苦役：
# 人的审批带宽是这套系统真正的瓶颈，宁少勿多。
WIKI_INGEST_MAX_CANDIDATES = int(env("WIKI_INGEST_MAX_CANDIDATES", "6"))
# 审批浮层「找找相关页」的单次输出预算（2026-11-11 设，2026-11-23 随 2→5 条放宽）。
# 输出最多 5 条短 JSON；推理链与正文共享 max_tokens（2048 会被推理吃光，S1 老坑），
# 3072 历史上 85 次里正好撞过顶一次，放宽条数后更容易撞 ⇒ 抬到 4096。
# 按实际输出计费，高上限在正常情形下零成本。
# ★ 这次**不开**「撞顶自动翻倍重试」：它是人主动点的一次性动作，失败/截断会
#   在浮层里说清楚并让人再点一次；静默多打一次调用正是用户明确不想要的。
WIKI_RELATED_MAX_TOKENS = int(env("WIKI_RELATED_MAX_TOKENS", "4096"))

# ---------------------------------------------------------------- 日志
# ★ 级别约定 / 字段命名 / 脱敏规则都在 `core/logkit.py` 模块头 —— 加日志前先读它。
#   一句话：DEBUG=高频细节｜INFO=状态变化｜WARNING=可恢复异常｜ERROR=要人介入。
#
# 三路输出（同一个 formatter，行格式是契约，见 logkit.LINE_FMT）：
#   console  → stderr，看屏幕（阈值 LOG_LEVEL，默认 INFO）
#   file     → data/logs/reader.log，轮转，**默认收全量含 DEBUG**（给 /logs 页与事后复盘）
#   errors   → data/logs/error.log，只收 ERROR+，长期留（轮转档数少、想翻旧账就有）
LOG_DIR = DATA_DIR / "logs"
LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
LOG_FILE_LEVEL = env("LOG_FILE_LEVEL", "DEBUG").upper()
LOG_FORMAT = env("LOG_FORMAT", "human").lower()
LOG_MAX_BYTES = int(env("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(env("LOG_BACKUPS", "5"))
# 每个 HTTP 请求一行（默认只在文件里 DEBUG；设 1 则控制台也出 INFO 行）
LOG_REQUESTS = env("LOG_REQUESTS", "0") == "1"
LOG_REQUEST_LEVEL = "INFO" if LOG_REQUESTS else "DEBUG"
# 超过这个毫秒数的请求无条件记 WARNING（与 LOG_REQUESTS 无关）：卡顿要能看见
LOG_SLOW_MS = int(env("LOG_SLOW_MS", "800"))
# 自动轮询端点不进请求日志 —— /logs 页自己每几秒轮询一次，记了就是自我刷屏。
# /icon/ 是树上的源图标（没图标的源返回 404，一次页面加载几十条，
# 全是"预期内的 404"，记了只会把 WARNING 冲成噪音）。
LOG_IGNORE_PATHS = ["/logs/rows", "/favicon.ico", "/icon/", "/translate/status"]

# ★ root 的阈值必须 ≤ 两个 handler 里更低的那个，否则 DEBUG 根本到不了 file handler
#   （logger/handler 两级都要过，这是 logging 最常见的"配了没生效"原因）
_LOG_LEVEL_OF = {"CRITICAL": 50, "ERROR": 40, "WARNING": 30, "INFO": 20, "DEBUG": 10}
_ROOT_LOG_LEVEL = min(_LOG_LEVEL_OF.get(LOG_LEVEL, 20),
                      _LOG_LEVEL_OF.get(LOG_FILE_LEVEL, 10))

# 盘不可写（只读挂载之类）时退化成只有控制台 —— 日志不该让服务起不来
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR_OK = True
except OSError:  # pragma: no cover
    _LOG_DIR_OK = False

from core import logkit as _logkit  # noqa: E402  （要在 sys.path 设置之后才能导入）

_HANDLERS = {
    "console": {"class": "logging.StreamHandler", "level": LOG_LEVEL,
                "formatter": LOG_FORMAT},
}
if _LOG_DIR_OK:
    _HANDLERS["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "level": LOG_FILE_LEVEL, "formatter": LOG_FORMAT,
        "filename": str(LOG_DIR / "reader.log"), "encoding": "utf-8",
        # delay=True：第一次真的写日志时才开文件（不然导入 settings 就落一个空文件）
        "delay": True, "maxBytes": LOG_MAX_BYTES, "backupCount": LOG_BACKUPS,
    }
    _HANDLERS["errors"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "level": "ERROR", "formatter": LOG_FORMAT,
        "filename": str(LOG_DIR / "error.log"), "encoding": "utf-8",
        "delay": True, "maxBytes": 2 * 1024 * 1024, "backupCount": 3,
    }
_ROOT_HANDLERS = ["console"] + (["file", "errors"] if _LOG_DIR_OK else [])

# 降噪名单：这些 logger 在 DEBUG 下水漫金山，直接钉在 WARNING。
# ★ 尤其 `django.db.backends` —— root 一开 DEBUG，它会把每一条 SQL 都写进日志文件。
_NOISY = ["django.db.backends", "django.template", "django.utils.autoreload",
          "urllib3", "urllib3.connectionpool", "requests", "apscheduler",
          "charset_normalizer", "PIL"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": _logkit.FORMATTERS,
    "handlers": _HANDLERS,
    "root": {"handlers": _ROOT_HANDLERS, "level": _ROOT_LOG_LEVEL},
    "loggers": {
        # Django 自己的请求日志：**只**把 5xx 的 traceback 收进 error.log。
        # 4xx / 慢请求由 core.reqlog 中间件统一记（带耗时和 query），
        # 两边都记会出现"一条请求两条日志"，所以这里把 4xx 让出去。
        "django.request": {"level": "ERROR", "handlers": _ROOT_HANDLERS,
                           "propagate": False},
        # runserver 的 access log 与 reqlog 中间件重复 → 关掉（中间件信息更全）
        "django.server": {"level": "WARNING", "handlers": [], "propagate": False},
        **{name: {"level": "WARNING", "propagate": True} for name in _NOISY},
    },
}
