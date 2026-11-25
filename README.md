# 谛听阅读器（reader）

从 [fred2045/diting](https://github.com/fred2045/diting) 提取的独立阅读器：本地跑一个
Django 服务，把 RSS 阅读、分级、日报、翻译、划线、wiki 放在同一套 SQLite 里。
本仓库额外提供 **FreshRSS 数据源**（Google Reader API），因此可以脱离 Miniflux /
Docker 单独运行；Miniflux 仍可随时切换回去。

> 本仓库只包含 reader（阅读 + 分级 + 日报 + 翻译 + 划线 + wiki），不含原项目的
> 采集/推送等外部脚本。

## 功能

- **两种数据源**：FreshRSS（GReader API）或 Miniflux，设置页里一键切换。
- **分级**：按分类/源配置判据，LLM 判定 L0–L3；没有 API key 时走零成本规则降级。
- **日报 / 周报**：按时间窗汇总，支持补跑与失败重试；预算与调用次数可配。
- **标题翻译、划线批注、知识库（wiki）**：数据都在本地 SQLite。
- **所有设置都能在应用里改**：`/prefs` 有数据源、AI 模型、刷新参数、分级/日报开关，
  以及整份 `.env` 的高级编辑。凭据只写进根目录 `.env`（0600），不进数据库。

## 快速开始（源码运行）

要求 Python 3.12+。

```bash
cp .env.example .env          # 也可以先不填，启动后在设置页填
python -m venv .venv && . .venv/bin/activate
pip install -r reader/requirements.txt
python reader/manage.py migrate
python reader/manage.py runserver 127.0.0.1:8099
```

打开 <http://127.0.0.1:8099>，首次会引导到设置页：

1. **设置 → 数据源**：选 FreshRSS，填 API 地址（`.../api/greader.php`）、用户名、
   专用 API 密码；点「保存本组」再点「测试连接」。
2. **设置 → AI 模型**：填 DeepSeek 或 Agnes 的 API key（不填也能用，功能降级）。
3. 其余分级/日报频率在左侧对应分组里调。

数据目录（相对仓库根）：

| 内容 | 路径 |
|---|---|
| SQLite | `reader/data/reader.sqlite3` |
| 日志 | `reader/data/logs/reader.log` |
| 日报 | `data/reports/` |
| 知识库 | `data/wiki/`（可用 `WIKI_DIR` 指到 Obsidian vault） |
| 读者画像 / 本体 | `Meta/interest-profile.md`、`Meta/ontology.md` |

## Windows

到 [Releases](https://github.com/midpoint/diting_reader/releases) 下载：

- **`diting-reader-windows-<版本>.exe`（推荐）**：单文件，双击即用；
- `diting-reader-windows-<版本>.zip`：便携包，解压后双击 `启动谛听.cmd`。

两者都自带 Python 与全部依赖，不需要安装任何东西。首次启动会生成 `.env` 并
直接打开设置页，填完数据源即可使用。详见
[`README-Windows.md`](README-Windows.md)。

## FreshRSS 配置与口径

见 [`docs/datasource-greader.md`](docs/datasource-greader.md)：API 准备、字段映射、
三个协议做不到的降级点（单源立即刷新 / 抓全文 / 源抓取错误）、缓存与故障表现。

## 开发

```bash
python reader/manage.py test core.tests.test_greader_units core.tests.test_prefs_env
python reader/manage.py test core.tests
```

Windows 单文件 exe 与便携包可在本地构建（PyInstaller 不支持交叉编译；
exe 建议在 Windows 上构建，Linux/macOS 构建出的同名文件仅用于冒烟验证）：

```bash
python scripts/build_windows_portable.py --version 0.2.0
python scripts/build_windows_exe.py --version 0.2.0   # 需要 pyinstaller>=6.10,<7
# 产物：dist/diting-reader-windows-0.2.0.zip / .exe
```

CI 在 [`.github/workflows/build-windows.yml`](.github/workflows/build-windows.yml)：
推送 `v*` 标签时自动构建并发布到 Releases；也可以在 Actions 页手动触发（只留 artifact）。

## 安全

- 阅读器**没有登录鉴权**，默认只监听 `127.0.0.1`；不要直接暴露到公网。
- `.env` 含凭据，已在 `.gitignore`；应用内保存会使用 0600 权限。
- 日志不打印密码与完整 Auth token；设置页不回显已保存的密钥。
- 详细说明见 [`SECURITY.md`](SECURITY.md)（若存在）。

## 许可

MIT，见 [`LICENSE`](LICENSE)。基于 [fred2045/diting](https://github.com/fred2045/diting)。
