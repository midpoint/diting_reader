# 数据源：FreshRSS（GReader API）

reader 默认仍用 Miniflux。把 `DATASOURCE_ENGINE` 改成 `greader` 后，订阅、条目、
已读、星标、分类的真相层换成 FreshRSS（通过它的 Google Reader 兼容 API），
reader 自己的分级 / 日报 / 翻译 / 划线 / wiki / 开销数据仍留在本地 SQLite。

## 1. FreshRSS 侧准备

1. 后台开启 API 访问（管理 → 认证 / API）。
2. 在个人资料页设置**专用 API 密码**（不是网页登录密码），并记下页面显示的
   API 地址，形如 `https://<host>/api/greader.php`。

## 2. reader 侧配置

**推荐在应用内改**：打开 `/prefs` →「数据源」，选 FreshRSS，填地址、用户名、专用 API
密码，点「保存本组」再点「测试连接」。保存会写进仓库根目录 `.env`（0600），
并立即重建数据源单例，不需要重启。AI 模型密钥在同一页的「AI 模型」组。

也可以直接编辑仓库根目录 `.env`（适合脚本化部署）：

```ini
DATASOURCE_ENGINE=greader
GREADER_BASE_URL=https://<host>/api/greader.php
GREADER_USERNAME=<用户名>
GREADER_PASSWORD=<专用 API 密码>
```

`.env` 已在 `.gitignore` 里；真实值不要写进代码、文档或提交历史。
缺任意一项时 reader 会以一条明确的配置错误启动失败，而不是悄悄退回 Miniflux。

切回 Miniflux 只需 `DATASOURCE_ENGINE=miniflux`（或删掉这一行），
Miniflux 相关变量与行为完全不变。

## 3. 映射与口径

| 概念 | GReader 实现 |
|---|---|
| feed id | 流 id `feed/<数字>` 的数字部分 |
| 条目 id | `tag:google.com,2005:reader/item/<16 位 hex>` → int64；`items/ids` 的十进制也接受 |
| 分类 id | 本地表 `core_categorylabel`：label 名 ↔ 自增 int id，首次见到分配、改名保留、删除软删 |
| 发布时间窗 | `ot` = 起点（oldest），`nt` = 终点（newest），Unix 秒；返回前再按 `published_at` 本地兜底 |
| 未读 | `xt=user/-/state/com.google/read` |
| 只读 | `it=user/-/state/com.google/read` |
| 搜索 | 优先 `search/items/ids`；FreshRSS 未实现该端点时退化为本地标题过滤（有界窗口） |

FreshRSS 一个 feed 只属于一个分类；若外部数据里出现多个 label，取第一个。

## 4. 协议做不到的三点（明确降级）

- **单源立即刷新**：没有该端点。`refresh_feed()` 记一条 info 日志并返回 True，
  等 FreshRSS 自己的抓取周期。
- **抓取原文全文**：没有该操作。`fetch_content()` 抛 `NotImplementedError`；
  请在 FreshRSS 里给该源开启「抓取全文」。
- **源的抓取错误**：协议不提供。`health()` 恒为正常，UI 上该源永远显示 ok。

另外：

- **空分类**：GReader 不能凭空建分类。`create_category()` 只在本地登记映射
  （`local_only=True`），第一次有 feed 挂上来时才在 FreshRSS 侧落地。
- **mark-read-before**：FreshRSS 的 `ts` 是条目的微秒级**摄取时间**，
  严格说是"摄取早于 N 天前"，与 Miniflux 的 `published_at` 口径略有差别。
- **counters**：`unread-count` 只有未读数；`reads` 恒为 0（UI 不使用它）。

## 5. 缓存与故障表现（P1）

- **条目流**：同一筛选（范围/状态/时间窗/offset/search）在
  `GREADER_ENTRY_CACHE_TTL`（默认 15 秒）内直接复用，首屏 + 局部刷新不再重复翻页。
  只缓存 UI 规模（limit ≤ 300）的结果，日报/分级一次 1000 条不驻留内存；
  最多保留 `GREADER_ENTRY_CACHE_MAX`（默认 12）个筛选。标已读/星标/全部已读/
  改订阅等写操作会立即清空。
- **图标**：成功结果缓存 24 小时；失败（无 URL / 超时 / 404 / 非图片 / 过大）
  负缓存 `GREADER_ICON_MISS_TTL`（默认 300 秒），避免每页十几个超时把首屏拖死。
  单个图标最多 `GREADER_ICON_MAX_BYTES`（默认 256 KB），且只对 FreshRSS 同主机
  请求携带 Authorization。
- **FreshRSS 挂了 / 密码错**：页面显示 502 的人话错误（含端点、HTTP 状态与
  下一步提示），不显示 Django 堆栈；HTMX 片段返回 200 的错误横幅（htmx 默认
  不交换 5xx，否则用户什么也看不到）；`/tree/`、`/summary/` 等写接口返回
  `{"ok": false, "error": "..."}`。
- **长时间运行**：401 自动重登一次；写操作 token 失效（400/401/403）重取 token
  重试一次；翻页遇到重复的 continuation 立即停止并记 warning，不会死循环。

## 6. 换数据源的数据提醒

条目 id 体系与 Miniflux 完全不同：本地按 Miniflux 条目 id 存的分级记录、
划线/批注在新数据源下对不上号，等于从零开始。订阅与已读/星标状态在 FreshRSS 侧，
不受影响。分类 id 也是 reader 本地新分配的一套（从 1 开始），
若同一个 reader 数据库里已有按 Miniflux 分类 id 存的分级配置，切到 greader 后
可能被数字相同的分类"误认"，建议切换前确认或另用一份 reader 数据库。
