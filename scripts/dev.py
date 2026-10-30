"""单命令启动本地后台、迁移与前端；Ctrl+C 正常停止。"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'backend'))
os.chdir(PROJECT)
from app import core
from alembic import command
from alembic.config import Config

core.init_db()
command.upgrade(Config(str(PROJECT / 'backend/alembic.ini')), 'head')
children = []
try:
    children.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--app-dir', str(PROJECT/'backend'), '--host', '127.0.0.1', '--port', '8000']))
    children.append(subprocess.Popen(['npm', 'run', 'dev'], cwd=PROJECT/'frontend', env={**os.environ, 'NEXT_TELEMETRY_DISABLED': '1'}))
    print('正在启动蔬食，启动后访问：http://127.0.0.1:3000', flush=True)
    while all(child.poll() is None for child in children):
        time.sleep(1)
finally:
    for child in children:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
    for child in children:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
if any(child.returncode not in (0, -signal.SIGTERM) for child in children):
    raise SystemExit(1)
