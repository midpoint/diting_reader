#!/bin/bash
# 谛听阅读器 —— 双击即启动（macOS）
# ★ 为什么要有这个文件：从助手会话里起的后台进程活不过会话转场（nohup / screen /
#   launchd submit 都被回收）。在自己终端（或这里）起的进程才常驻。
#   停止：关掉这个终端窗口，或按 Ctrl+C。
cd "$(dirname "$0")" || exit 1
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY
exec .venv/bin/python manage.py runserver 127.0.0.1:8099
