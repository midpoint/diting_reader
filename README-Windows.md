# 谛听阅读器 · Windows 便携版

这个包**自带 Windows 版 Python 3.12 和全部依赖**，真机不需要安装 Python、
不需要联网装包，解压后双击即可运行。

## 一、第一次使用

1. 把整个 `diting-windows` 文件夹解压到本地，例如 `C:\diting`。
   （路径带中文一般也能用；遇到怪问题先换到纯英文路径。）
2. 双击 **`启动谛听.cmd`**。首次启动会自动生成 `.env` 并打开数据库，然后浏览器
   自动打开 **设置 → 数据源**。
3. 在页面里填写（保存后再点「测试连接」）：
   - 数据源：FreshRSS（默认），API 地址形如 `http://你的主机:8080/api/greader.php`；
   - 用户名；**专用 API 密码**（FreshRSS 个人资料页生成，不是网页登录密码）。
4. 切到「AI 模型」填 DeepSeek / Agnes 的 API key（不填也能用，分级与日报会降级）。
5. 关闭设置页即可开始阅读。以后启动会直接打开阅读页。

> 所有设置都能在应用里改：设置页包含数据源、AI 模型、阅读/刷新参数、分级与日报
> 开关，以及「高级 .env」整份编辑。凭据只写进根目录 `.env`（自动 0600），
> 不会显示在页面上，也不会进数据库。

停止服务：在黑色窗口按 `Ctrl+C`，或直接关闭窗口。

## 二、数据与日志

- 本地数据库：`reader\data\reader.sqlite3`
- 日志：`reader\data\logs\reader.log`
- 日报：`data\reports\`（包根目录下，与 `reader` 同级）
- 知识库：`data\wiki\`
- 升级/换包时保留 `reader\data` 和根目录 `.env` 即可。

首次启动会自动建库；已有旧库会直接沿用。

## 三、安全提醒

- 阅读器**没有登录鉴权**，只监听 `127.0.0.1`，不要改成本机以外可访问。
- `.env` 里有 FreshRSS / 模型凭据，不要发给别人，也不要提交到 git。
- 本包未做数字签名，Windows SmartScreen 可能提示「未知发布者」：
  点「更多信息 → 仍要运行」。若压缩包被标记为来自网络，可在文件属性里
  勾选「解除锁定」后再解压。

## 四、这个包是怎么构建的

- Python：官方 `python-3.12.10-embed-amd64.zip`（Windows 64 位）
- 依赖（固定版本）：Django 6.1.3、requests 2.34.2、APScheduler 3.11.3、
  pypinyin 0.55.0、winotify 1.1.0、tzdata 2026.6
- 源码：本仓库 `reader/` 及文档，已排除 `.env`、本地数据库、日志和缓存
- 启动方式：内置 Python 直接运行 `reader\manage.py runserver 127.0.0.1:8099`
- 构建脚本：`scripts/build_windows_portable.py`（CI 见 `.github/workflows/build-windows.yml`）

## 五、常见问题

**双击后窗口一闪而过？**
从资源管理器地址栏输入 `cmd` 回车，再执行 `启动谛听.cmd`，就能看到完整报错。

**页面显示「数据源不可用」？**
打开「设置 → 数据源」，检查 API 地址、用户名、专用 API 密码，点「测试连接」；
再确认 FreshRSS 正在运行、后台已开启 API。

**端口 8099 被占用？**
编辑 `启动谛听.cmd`，把开头的 `set "PORT=8099"` 换成别的端口即可。

**想用 Miniflux？**
在「设置 → 数据源」把引擎切到 Miniflux，填地址与管理员账号后保存。
