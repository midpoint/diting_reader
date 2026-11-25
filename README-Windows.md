# 谛听阅读器 · Windows 版

提供两种形式，功能完全一样：

| 形式 | 文件 | 适用 |
|---|---|---|
| **单文件 exe（推荐）** | `diting-reader-windows-<版本>.exe` | 双击即用，不释放一堆文件 |
| 便携包 | `diting-reader-windows-<版本>.zip` | 想固定数据目录、或 exe 被杀软拦截时 |

两种都**自带 Python 与全部依赖**，真机不需要安装任何东西。

## 一、单文件 exe 怎么用

1. 把 exe 放到一个**可写目录**，例如 `D:\diting\谛听阅读器.exe`。
   （不要放 `C:\Program Files`；若放了，数据会自动改存到
   `%LOCALAPPDATA%\DitingReader`，程序仍可使用。）
2. 双击运行。首次启动会：
   - 在同目录生成 `.env`、`data\`、`Meta\`；
   - 自动建库；
   - 打开浏览器到 **设置 → 数据源**。
3. 在页面里填写（保存后再点「测试连接」）：
   - 数据源：FreshRSS（默认），API 地址形如 `http://你的主机:8080/api/greader.php`；
   - 用户名；**专用 API 密码**（FreshRSS 个人资料页生成，不是网页登录密码）。
4. 切到「AI 模型」填 DeepSeek / Agnes 的 API key（不填也能用，分级与日报会降级）。
5. 关闭设置页即可开始阅读。以后启动会直接打开阅读页。

停止服务：在黑窗口按 `Ctrl+C`，或直接关闭窗口。exe 启动时会解压运行库，
首次启动比之后慢几秒，属正常现象。

## 二、便携包怎么用

1. 解压整个 `diting-windows` 文件夹到本地，例如 `C:\diting`。
2. 双击 **`启动谛听.cmd`**，其余步骤与上面第 2–5 步相同。
3. 数据在包内：数据库 `reader\data\reader.sqlite3`、日志
   `reader\data\logs\reader.log`、日报 `data\reports\`、知识库 `data\wiki\`。

## 三、所有设置都能在应用里改

设置页包含：

- **数据源**：FreshRSS / Miniflux 切换、地址、用户名、密码（带「测试连接」）
- **AI 模型**：Provider、API key、Base URL、模型名
- **阅读与刷新**：窗口大小、缓存、日报等待时间等
- **分级 / 日报 / 周报 / 通知 / 翻译**：开关与频率
- **全局层文件**：读者画像、知识本体
- **高级 .env**：整份原文编辑，兜底所有其它参数

保存会写进程序目录的 `.env`（0600），密钥不回显；密码框留空表示保持原值，
勾「清除」才删除。只有少数标「重启」的缓存参数需要重启程序。

## 四、安全提醒

- 阅读器**没有登录鉴权**，只监听 `127.0.0.1`，不要改成本机以外可访问。
- `.env` 里有 FreshRSS / 模型凭据，不要发给别人，也不要提交到 git。
- 未做数字签名，Windows SmartScreen 可能提示「未知发布者」：
  点「更多信息 → 仍要运行」。若文件被标记为来自网络，可在属性里
  勾选「解除锁定」。
- 少数杀毒软件会对 PyInstaller 生成的 exe 误报；介意的话用便携包。

## 五、常见问题

**双击后窗口一闪而过？**
从资源管理器地址栏输入 `cmd` 回车，把 exe 拖进窗口再回车，就能看到完整报错。

**页面显示「数据源不可用」？**
打开「设置 → 数据源」，检查 API 地址、用户名、专用 API 密码，点「测试连接」；
再确认 FreshRSS 正在运行、后台已开启 API。

**端口 8099 被占用？**
设环境变量 `DITING_PORT=其他端口` 再启动；已有实例在跑时，exe 只会打开现有页面。

**想用 Miniflux？**
在「设置 → 数据源」把引擎切到 Miniflux，填地址与管理员账号后保存。

## 六、构建方式

- exe：`python scripts/build_windows_exe.py --version 0.2.0`
  （PyInstaller + `packaging/diting.spec`，单文件）
- 便携包：`python scripts/build_windows_portable.py --version 0.2.0`
- CI：`.github/workflows/build-windows.yml`，推 `v*` 标签自动构建两种产物并发布 Release。
