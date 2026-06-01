"""飞桨 AI Studio 部署入口：自动读取平台分配的端口"""
import os
import subprocess
import sys

# AI Studio 通过环境变量分配端口
port = os.getenv("PORT", "8501")

# 启动 Streamlit
args = [
    sys.executable, "-m", "streamlit", "run",
    os.path.join(os.path.dirname(__file__), "app.py"),
    "--server.port", port,
    "--server.address", "0.0.0.0",
    "--server.headless", "true",
    "--browser.serverAddress", "0.0.0.0",
]
subprocess.run(args)
