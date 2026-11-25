@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=%~dp0python\python.exe"
set "APP=%~dp0reader\manage.py"
set "PORT=8099"
set "URL=http://127.0.0.1:%PORT%"
set "FIRST="

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONDONTWRITEBYTECODE=1
rem 避免系统代理把本机请求送出去（与 macOS 的 serve.command 同一考虑）
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set ALL_PROXY=
set all_proxy=

title 谛听阅读器

if not exist "%PY%" (
  echo [错误] 找不到内置 Python：%PY%
  echo 请确认压缩包完整解压，且没有单独移动 python 文件夹。
  pause
  exit /b 1
)

if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
  set "FIRST=1"
  echo [首次运行] 已生成配置文件 .env。
  echo 稍后浏览器会打开“设置 → 数据源”，直接在页面里填写 FreshRSS / Miniflux 即可。
  echo.
)

if not exist "reader\data" mkdir "reader\data"
echo 正在检查数据库（首次会初始化）...
"%PY%" "%APP%" migrate --noinput
if errorlevel 1 (
  echo.
  echo [错误] 数据库初始化失败，请把上面的报错发给开发者。
  pause
  exit /b 1
)

echo.
echo 谛听阅读器启动中：%URL%
echo 停止：按 Ctrl+C，或直接关闭本窗口。
echo.

if defined FIRST (
  start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%URL%/prefs?g=datasource'"
) else (
  start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%URL%'"
)
"%PY%" "%APP%" runserver 127.0.0.1:%PORT% --noreload

echo.
echo 服务已停止。
pause
