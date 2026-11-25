"""设置页（v3）：分组渲染 settingkit 注册表 + 保存/重置 + 全局层文件编辑 + .env 配置。

边界（2026-11-25 更新）：
  - **数据源与模型凭据在本页可改**，但它们写根目录 `.env`（0600），不进 AppSetting、
    不回显、不记日志；页面只显示「已设置 / 未设置」。
  - 预算设置留在 /costs 页（它跟开销数据长在一起，不放这里）；
  - 注册表（core/settingkit.py GROUPS）是键值类设置的唯一事实来源 —— 本页只负责
    渲染与提交，加设置项 = 改注册表，本页自动跟上。
  - 结构化分组没覆盖的 .env 调参项，用「高级 .env」整份编辑兜底。

★ v2 新增「全局层文件」分组（2026-10-31，wiki 阶段 0 的 7.3）：
  `interest-profile.md`（读者画像）与 `ontology.md`（知识本体）是**整篇 markdown
  的配置文件，不是键值**，所以不走 settingkit（那边限定 ≤120 字符、走 AppSetting 表），
  单独一组 + 单独一个保存端点，直接写回 `Meta/*.md`。
  两个文件都是「每人一份、可手改、git 可追溯」的东西：放本地文件而不是数据库，
  用户能直接 vim、能 diff、能备份 —— 这是刻意的。
"""
import logging
import os
import re
import shutil
from datetime import datetime

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core import envfile
from core import notify
from core import settingkit as sk
from core.models import AppSetting

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ .env 设置
#: 这一组**不经过 AppSetting 表**，直接写仓库根目录 `.env`：
#:   - 数据源与模型凭据属于「部署配置」，必须与命令行启动时读的是同一份；
#:   - 密钥只准落在 `.env`（0600），不进 SQLite、不进页面回显。
#: type: text | password | select | int | bool；effect: instant | restart。
ENV_GROUPS = [
    {
        "key": "datasource",
        "label": "数据源",
        "note": "FreshRSS 用个人资料页的专用 API 密码；Miniflux 用管理员账号。"
                "切换数据源会换一套 feed / 条目 id，本地的分级与划线记录对不上新数据源。",
        "fields": [
            {"key": "DATASOURCE_ENGINE", "label": "数据源引擎", "type": "select",
             "options": [("greader", "FreshRSS（GReader API）"),
                         ("miniflux", "Miniflux（默认）")],
             "effect": "instant",
             "help": "保存后新建的请求立即改用新数据源；正在跑的一轮任务会跑完。"},
            {"key": "GREADER_BASE_URL", "label": "FreshRSS API 地址", "type": "text",
             "effect": "instant", "placeholder": "https://主机/api/greader.php",
             "help": "必须指向 api/greader.php（FreshRSS 后台开启 API 后可见）。"},
            {"key": "GREADER_USERNAME", "label": "FreshRSS 用户名", "type": "text",
             "effect": "instant"},
            {"key": "GREADER_PASSWORD", "label": "FreshRSS 专用 API 密码",
             "type": "password", "effect": "instant",
             "help": "个人资料页的「API 密码」，不是网页登录密码。"},
            {"key": "MINIFLUX_URL", "label": "Miniflux 地址", "type": "text",
             "effect": "instant", "placeholder": "http://localhost:8080"},
            {"key": "MINIFLUX_ADMIN_USERNAME", "label": "Miniflux 用户名", "type": "text",
             "effect": "instant"},
            {"key": "MINIFLUX_ADMIN_PASSWORD", "label": "Miniflux 密码",
             "type": "password", "effect": "instant"},
        ],
    },
    {
        "key": "llm",
        "label": "AI 模型",
        "note": "分级、日报、翻译都走这里配置的模型；没填 key 时这些功能会降级为规则版。",
        "fields": [
            {"key": "LLM_PROVIDER", "label": "主力通道", "type": "select",
             "options": [("deepseek", "DeepSeek（默认，便宜稳定）"),
                         ("agnes", "Agnes（免费但可能 429）")],
             "effect": "instant"},
            {"key": "DEEPSEEK_API_KEY", "label": "DeepSeek API Key",
             "type": "password", "effect": "instant"},
            {"key": "DEEPSEEK_BASE_URL", "label": "DeepSeek Base URL", "type": "text",
             "effect": "instant", "placeholder": "https://api.deepseek.com/v1"},
            {"key": "DEEPSEEK_MODEL", "label": "DeepSeek 模型", "type": "text",
             "effect": "instant", "placeholder": "deepseek-chat"},
            {"key": "AGNES_API_KEY", "label": "Agnes API Key",
             "type": "password", "effect": "instant"},
            {"key": "AGNES_BASE_URL", "label": "Agnes Base URL", "type": "text",
             "effect": "instant", "placeholder": "https://apihub.agnes-ai.com/v1"},
            {"key": "AGNES_MODEL", "label": "Agnes 模型", "type": "text",
             "effect": "instant", "placeholder": "agnes-2.5-flash"},
        ],
    },
    {
        "key": "runtime",
        "label": "阅读与刷新",
        "note": "这些是 .env 里的运行参数；标「重启」的要重启应用才生效。"
                "分级/日报的开关与频率在左侧对应分组里。",
        "fields": [
            {"key": "READER_ENTRY_WINDOW", "label": "列表窗口（条）", "type": "int",
             "min": 10, "max": 1000, "effect": "instant"},
            {"key": "READER_MULTI_FEED_PER_SOURCE", "label": "多源视图·每源条数",
             "type": "int", "min": 1, "max": 500, "effect": "restart"},
            {"key": "READER_TREE_CACHE_TTL", "label": "树缓存（秒）", "type": "int",
             "min": 0, "max": 600, "effect": "restart"},
            {"key": "READER_UNRATED_LEVEL", "label": "未评级按 L?", "type": "int",
             "min": 0, "max": 3, "effect": "instant"},
            {"key": "GREADER_ENTRY_CACHE_TTL", "label": "FreshRSS 条目缓存（秒）",
             "type": "int", "min": 0, "max": 600, "effect": "restart"},
            {"key": "GREADER_ENTRY_CACHE_MAX", "label": "FreshRSS 缓存筛选数",
             "type": "int", "min": 0, "max": 100, "effect": "restart"},
            {"key": "GREADER_ICON_MISS_TTL", "label": "图标失败负缓存（秒）",
             "type": "int", "min": 0, "max": 3600, "effect": "restart"},
            {"key": "SUMMARY_FRESH_DEADLINE_MIN", "label": "日报等源刷新（分钟）",
             "type": "int", "min": 0, "max": 240, "effect": "instant",
             "help": "FreshRSS 没有抓取完成时间，0 = 不等待。"},
            {"key": "FAST_REFRESH_ENABLED", "label": "启用快速刷新", "type": "bool",
             "effect": "instant"},
        ],
    },
]
ENV_FIELDS = {f["key"]: f for g in ENV_GROUPS for f in g["fields"]}
ENV_GROUP_OF = {f["key"]: g["key"] for g in ENV_GROUPS for f in g["fields"]}
_BOOL_KEYS = {k for k, f in ENV_FIELDS.items() if f["type"] == "bool"}


def _env_value(key: str, file_vals: dict = None) -> str:
    """页面显示用：.env 优先，读不到再退回当前 settings（含代码默认）。"""
    raw = (file_vals if file_vals is not None else envfile.read_values()).get(key)
    if raw is not None:
        return raw
    attr = envfile.setting_name(key)
    val = getattr(settings, attr, "")
    if isinstance(val, bool):
        return "1" if val else "0"
    return "" if val is None else str(val)


def _env_rows() -> list:
    file_vals = envfile.read_values()
    groups = []
    for g in ENV_GROUPS:
        rows = []
        for f in g["fields"]:
            raw = file_vals.get(f["key"])
            if f["type"] == "password":
                # 密钥永不回显：只告诉用户「已设置 / 未设置」
                is_set = bool(raw or getattr(
                    settings, envfile.setting_name(f["key"]), ""))
                value = ""
            else:
                value = _env_value(f["key"], file_vals)
                is_set = bool(raw)
            rows.append({"f": f, "value": value, "is_set": is_set,
                         "from_file": raw is not None})
        groups.append({"g": g, "fields": rows})
    return groups


def _validate_env(key: str, raw: str):
    f = ENV_FIELDS[key]
    s = (raw or "").strip()
    if f["type"] == "password":
        if len(s) > 500:
            return None, f"{f['label']} 过长"
        return s, ""
    if f["type"] == "bool":
        return ("1" if s.lower() in ("1", "true", "on", "yes") else "0"), ""
    if f["type"] == "int":
        try:
            v = int(float(s))
        except (TypeError, ValueError):
            return None, f"{f['label']} 必须是整数"
        lo, hi = f.get("min"), f.get("max")
        if lo is not None and v < lo:
            return None, f"{f['label']} 不能小于 {lo}"
        if hi is not None and v > hi:
            return None, f"{f['label']} 不能大于 {hi}"
        return str(v), ""
    if f["type"] == "select":
        s = s.lower()
        allowed = {v for v, _ in f["options"]}
        if s not in allowed:
            return None, f"{f['label']} 取值非法"
        return s, ""
    if len(s) > 500:
        return None, f"{f['label']} 过长（≤500 字符）"
    if f["key"].endswith("_URL") and s:
        if not re.match(r"^https?://", s):
            return None, f"{f['label']} 必须以 http:// 或 https:// 开头"
        # 与 settings.py 的 `.rstrip("/")` 对齐：避免保存后出现 `...//accounts/...`
        s = s.rstrip("/")
    return s, ""


def _check_engine(values: dict) -> str:
    """保存后能否真正启动：只查必需项，不联网。返回错误文案或空串。"""
    engine = (values.get("DATASOURCE_ENGINE") or "miniflux").strip().lower()
    if engine == "greader":
        need = [("GREADER_BASE_URL", "FreshRSS API 地址"),
                ("GREADER_USERNAME", "FreshRSS 用户名"),
                ("GREADER_PASSWORD", "FreshRSS 专用 API 密码")]
    else:
        need = [("MINIFLUX_URL", "Miniflux 地址")]
    missing = [label for key, label in need if not (values.get(key) or "").strip()]
    return ("缺少：" + "、".join(missing)) if missing else ""


def _apply_and_reset(updates: dict):
    """写盘 → 热更新 settings → 重置数据源单例。返回 (ok, err)。"""
    try:
        values = envfile.write_updates(updates)
    except (OSError, ValueError) as e:
        log.warning("写入 .env 失败：%s", e)
        return False, f"写入 .env 失败：{e}"
    envfile.apply_to_settings({k: values.get(k, "") for k in updates})
    if any(k.startswith(("DATASOURCE_ENGINE", "GREADER_", "MINIFLUX_"))
           for k in updates):
        envfile.reset_datasource()
    return True, ""


# ------------------------------------------------------------------ 全局层文件
#: ★ 白名单写死，**不接受前端传路径**：只认 key。一旦路径可由前端拼就是任意文件
#:   读写 —— 本地工具也不能免这个。
#: ★ 两个文件进模型的方式不同：画像**全文**进分级/日报 prompt；本体只有「领域」表
#:   被 `wiki.ontology_domains()` 解析（其余文字只喂 `ontology_hash()`，无人调用）。
#:   别照「都进 prompt」写注释，否则等于承诺一段不会生效的配置。
#: ★ 「不进 settingkit」的理由见模块 docstring：字数量级差两个数量级（120 字符 vs 数千字）。
GLOBAL_FILES = [
    {
        "key": "profile",
        "label": "读者画像",
        "file": "interest-profile.md",
        "example": "interest-profile.example.md",
        "help": "回答「读者是谁、什么对他有价值」，用于分级与日报札记。"
                "★ 不要写 L0–L3 判据（会被拼进每一个源的 prompt）。",
        "hint": "建议 ≤ 2000 字（分级侧按 2000 截断）",
    },
    {
        "key": "ontology",
        "label": "知识本体",
        "file": "ontology.md",
        "example": "ontology.example.md",
        "help": "回答「知识分哪几个域」：表格里的受控 domain 标签决定每页落在 wiki/"
                "的哪个子目录，也是模型唯一可选的范围。"
                "★ 只写领域表；概念聚类用页里的 tags，不在这里维护。",
        "hint": "改标签 = 改分类法；tags 是自由的，不受本文件约束",
    },
]
BY_FILE_KEY = {f["key"]: f for f in GLOBAL_FILES}

#: 每次保存前留一份历史（防误覆盖）。保留份数不宜大：这是救急，不是版本管理。
HISTORY_KEEP = 10


def _meta_dir():
    return settings.PROJECT_ROOT / "Meta"


def _global_file_rows() -> list:
    """渲染结构：当前内容 + 是否存在 + 字节数。缺失时给 cp 命令而不是空白。"""
    rows = []
    for spec in GLOBAL_FILES:
        p = _meta_dir() / spec["file"]
        try:
            text = p.read_text(encoding="utf-8")
            exists = True
        except OSError:
            text, exists = "", False
        rows.append({
            **spec,
            "text": text,
            "exists": exists,
            "chars": len(text),
            "path": str(p),
            "cp_cmd": f"cp Meta/{spec['example']} Meta/{spec['file']}",
        })
    return rows


def _backup(p) -> str:
    """把**旧内容**存进 Meta/.history/，返回相对路径（没有旧文件则空串）。"""
    try:
        old = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not old.strip():
        return ""
    hist = _meta_dir() / ".history"
    hist.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = hist / f"{p.stem}-{stamp}.md"
    try:
        dst.write_text(old, encoding="utf-8")
    except OSError:
        log.warning("全局层文件备份失败：%s", dst)
        return ""
    # 只留最近 N 份（按修改时间），多了删旧的
    try:
        olds = sorted(hist.glob(f"{p.stem}-*.md"),
                      key=lambda x: x.stat().st_mtime, reverse=True)
        for f in olds[HISTORY_KEEP:]:
            f.unlink(missing_ok=True)
    except OSError:
        pass
    return str(dst.relative_to(settings.PROJECT_ROOT))


def _rows():
    """注册表 → 渲染结构。每项带当前生效值 + 是否已被设置页覆盖。"""
    overridden = set(AppSetting.objects.values_list("key", flat=True))
    groups = []
    for g in sk.GROUPS:
        fields = []
        for f in g.fields:
            fields.append({
                "f": f,
                "value": sk.effective(f.key),
                "overridden": f.key in overridden,
            })
        groups.append({"g": g, "fields": fields})
    return groups


def _ctx(request):
    """渲染上下文。active = 默认展开哪个分组（支持 ?g=<key> 深链）。"""
    want = (request.GET.get("g") or "").strip()
    keys = ([g["key"] for g in ENV_GROUPS] + [g.key for g in sk.GROUPS]
            + ["globals", "envraw"])
    active = want if want in keys else ENV_GROUPS[0]["key"]
    # 通知组的「未安装提示」：按后端区分，如实告知，不替用户装。
    # ★ 直接复用 core.notify 的 _backend()/_ready()，别再手写一份平台判断 ——
    #   mac 依赖 terminal-notifier（brew 装）；win 依赖随包 shim + pip 装的
    #   winotify（requirements.txt 已按 win32 标记自动带上）。
    if notify._backend() == "win":
        notify_ctx = {
            "bin": "winotify",
            "installed": notify._ready(),
            "hint": "pip install winotify（装完需重启 reader 进程）",
        }
    else:
        notify_bin = (getattr(settings, "NOTIFY_BIN", "terminal-notifier")
                      or "terminal-notifier")
        notify_ctx = {
            "bin": notify_bin,
            "installed": bool(shutil.which(notify_bin)),
            "hint": "brew install terminal-notifier",
        }
    return {
        "groups": _rows(),
        "env_groups": _env_rows(),
        "files": _global_file_rows(),
        "active": active,
        "notify": notify_ctx,
        "env_path": str(envfile.env_path()),
    }


def index(request):
    """★ 同一份面板，两种外壳：带 HX-Request（openModal 打开）返回模态片段，
    直接访问 URL 返回整页兜底（书签 / 刷新不会掉进一个没有样式的裸片段）。"""
    if request.headers.get("HX-Request") == "true":
        return render(request, "prefs/_panel.html", _ctx(request))
    return render(request, "prefs/index.html", _ctx(request))


@require_POST
def save(request):
    key = (request.POST.get("key") or "").strip()
    if not key:
        return JsonResponse({"ok": False, "err": "缺少 key"})
    try:
        ok, err = sk.write(key, request.POST.get("value", ""))
    except KeyError:
        return JsonResponse({"ok": False, "err": "未知设置项"})
    if not ok:
        log.info("设置保存被拒：%s（%s）", key, err)
    # ★ 回带生效值：模态里不能 reload（重载会关掉弹窗），前端拿它回填输入框
    return JsonResponse({"ok": ok, "err": err, "value": sk.effective(key)})


@require_POST
def reset(request):
    key = (request.POST.get("key") or "").strip()
    if not key:
        return JsonResponse({"ok": False, "err": "缺少 key"})
    try:
        sk.reset(key)
    except KeyError:
        return JsonResponse({"ok": False, "err": "未知设置项"})
    return JsonResponse({"ok": True, "err": "", "value": sk.effective(key)})


@require_POST
def save_env(request):
    """保存数据源 / 模型 / 运行参数到 `.env`（整组提交）。

    ★ 密码框留空 = **保持原值**，不是清空；要清空勾选「清除」。
      否则页面每次渲染都是空密码框，一保存就把密钥抹了。
    ★ 只校验本次改动的组：首装用户先填模型、后填数据源时不该被另一组卡住。
    """
    updates = {}
    for key, f in ENV_FIELDS.items():
        if key not in request.POST and key + "__clear" not in request.POST:
            continue
        raw = request.POST.get(key, "")
        if f["type"] == "password":
            if request.POST.get(key + "__clear") == "1":
                updates[key] = ""
            elif not raw.strip():
                continue
            else:
                val, err = _validate_env(key, raw)
                if err:
                    return JsonResponse({"ok": False, "err": err})
                updates[key] = val
        else:
            val, err = _validate_env(key, raw)
            if err:
                return JsonResponse({"ok": False, "err": err})
            updates[key] = val

    if not updates:
        return JsonResponse({"ok": True, "err": "", "message": "没有改动"})

    merged = {**envfile.read_values(), **updates}
    touched_ds = any(k.startswith(("DATASOURCE_", "GREADER_", "MINIFLUX_"))
                     for k in updates)
    if touched_ds:
        err = _check_engine(merged)
        if err:
            return JsonResponse({"ok": False, "err": err})

    ok, err = _apply_and_reset(updates)
    if not ok:
        return JsonResponse({"ok": False, "err": err})
    return JsonResponse({"ok": True, "err": "", "message": "已保存并生效"})


@require_POST
def test_datasource(request):
    """用**当前已保存的配置**真连一次数据源。"""
    from core.datasource import new_datasource

    try:
        ds = new_datasource()
        tree = ds.list_tree()
    except Exception as e:  # noqa: BLE001  连接失败是预期结果，不抛 500
        return JsonResponse({"ok": False, "err": _friendly_error(e)})
    feeds = sum(len(getattr(c, "feeds", []) or []) for c in tree)
    return JsonResponse({"ok": True, "err": "",
                         "message": f"连接成功：{len(tree)} 个分类，{feeds} 个源"})


def _friendly_error(e: Exception) -> str:
    """错误文案脱敏：绝不回显配置里的密码或 Auth token。"""
    msg = str(e) or e.__class__.__name__
    for key in ("GREADER_PASSWORD", "MINIFLUX_ADMIN_PASSWORD",
                "DEEPSEEK_API_KEY", "AGNES_API_KEY"):
        secret = envfile.read_values().get(key) or getattr(
            settings, envfile.setting_name(key), "")
        if secret:
            msg = msg.replace(str(secret), "***")
    msg = re.sub(r"(Auth=)[^\s&]+", r"\1***", msg)
    return msg[:400]


@require_GET
def env_raw(request):
    """高级：载入整份 `.env` 原文（点按钮才取，不在首屏 HTML 里带密钥）。"""
    text = envfile.read_text()
    return JsonResponse({"ok": True, "text": text, "path": str(envfile.env_path())})


@require_POST
def save_env_raw(request):
    """高级：整份 `.env` 原样保存（注释与顺序随文本一起保留）。

    ★ 只做最低限度的结构校验（每行必须是注释或 KEY=VALUE），不逐项解释内容；
      用户写的就是真相。写盘后把**认识**的键热应用到 settings，其余重启生效。
    """
    text = request.POST.get("text", "")
    if len(text) > 200_000:
        return JsonResponse({"ok": False, "err": "内容过大（上限 200KB）"})
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", s):
            return JsonResponse({"ok": False,
                                 "err": f"第 {i} 行不是 KEY=VALUE 或注释：{s[:40]}"})
    try:
        envfile.write_text(text.rstrip("\n") + "\n")
    except OSError as e:
        log.warning("写入 .env 失败：%s", e)
        return JsonResponse({"ok": False, "err": f"写入失败：{e}"})
    values = envfile.parse(text)
    envfile.apply_to_settings(values)
    envfile.reset_datasource()
    log.info("高级模式整份保存 .env（%d 行）", len(text.splitlines()))
    return JsonResponse({"ok": True, "err": "",
                         "message": "已保存；标「重启」的项目需重启应用"})


@require_POST
def save_file(request):
    """保存全局层文件（画像 / 本体）。返回 {ok, err, chars, backup}。

    ★ 空内容一律拒绝：这里是「清空全局层」—— 画像空了分级就失去依据、本体空了
      编译就没有骨架。而「误提交」（textarea 没加载出来就点了保存 / 复制漏了）
      比「真想清空」常见得多；真要清空，直接删文件即可。
    ★ 先备份再原子替换：`os.replace` 保证要么旧版要么新版，不会留半截文件
      （半截本体喂给编译 = 结构被截断，比报错难查得多）。
    """
    key = (request.POST.get("key") or "").strip()
    spec = BY_FILE_KEY.get(key)
    if not spec:
        return JsonResponse({"ok": False, "err": "未知文件"})
    text = request.POST.get("value", "")
    if not text.strip():
        return JsonResponse({"ok": False, "err": "内容为空，已拒绝保存（要清空请直接删文件）"})

    # 统一成 LF + 单个结尾换行：编辑器/curl 都可能带来 CRLF 或没有结尾换行，
    # 让文件内容只取决于「你写了什么」，不取决于用什么工具提交的。
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"

    p = _meta_dir() / spec["file"]
    p.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(p)
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:  # noqa: BLE001  磁盘满 / 权限：如实回报，不假装成功
        log.warning("全局层文件写入失败：%s（%s）", p, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return JsonResponse({"ok": False, "err": f"写入失败：{e}"})

    log.info("全局层文件已保存：%s（%d 字，备份=%s）", p.name, len(text), backup or "无")
    return JsonResponse({"ok": True, "err": "", "chars": len(text), "backup": backup})
