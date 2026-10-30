from __future__ import annotations

import hashlib
import os
import re
import atexit
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import DateTime, Index, Integer, JSON, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT / '.env', extra='ignore')
    app_origin: str = 'http://127.0.0.1:3000'
    app_environment: str = 'local-development'
    cookie_secure: bool = False
    database_url: str = ''
    deepseek_api_key: str = ''
    deepseek_model: str = 'deepseek-flash'
    amap_api_key: str = ''
    # 页内地图：公开 JS 标识 + 同源受限代理地址；服务端 Key 与安全密钥不下发。
    amap_js_key: str = ''
    amap_js_security_code: str = ''
    amap_service_host: str = ''
    amap_js_verified: bool = False
    # 语音显式选择供应商；旧配置默认百炼，不自动跨供应商回退。
    transcription_provider: str = 'bailian'
    transcription_api_key: str = ''
    transcription_endpoint: str = ''
    transcription_model: str = ''
    external_calls_enabled: bool = False
    daily_external_limit: int = 10
    weekly_external_limit: int = 30
    global_daily_external_limit: int = 30
    global_weekly_external_limit: int = 100
    vapid_public_key: str = ''
    vapid_private_key: str = ''
    vapid_subject: str = ''
    data_dir: Path = PROJECT / 'data'


settings = Settings()


def now() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return uuid.uuid4().hex


def digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def chat_lock(db, owner: str, session_id: str):
    """按 owner+session 派生的 Postgres advisory lock，串行化发送/删除/回复提交。"""
    advisory_lock(db, 'chat', owner, session_id)


def advisory_lock(db, *parts: str):
    """事务级 advisory lock；相同 parts 的并发事务串行执行，退出自动释放。"""
    h = digest('\x1f'.join(str(p) for p in parts))
    db.execute(text('SELECT pg_advisory_xact_lock(:k1, :k2)'), {'k1': int(h[:7], 16), 'k2': int(h[7:14], 16)})


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    preferences: Mapped[dict] = mapped_column(JSON, default=lambda: {'diet': 'vegan', 'avoid_coriander': False})


class Session(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(32), index=True)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Record(Base):
    __tablename__ = 'records'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index('ix_records_owner_kind', 'owner', 'kind'),)


class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default='pending', index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    due: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


engine = None
DB = None
_local_pg = None


def local_postgres_url():
    """只绑定私有Unix socket；数据留在项目，支持含空格的项目路径。"""
    from pgserver._commands import POSTGRES_BIN_PATH
    from sqlalchemy.engine import URL
    pgdata = settings.data_dir / 'postgres'
    pgdata.mkdir(parents=True, exist_ok=True, mode=0o700)
    pgdata.chmod(0o700)
    sockets = Path('/private/tmp') / ('shushi-pg-' + digest(str(pgdata.resolve()))[:16])
    sockets.mkdir(mode=0o700, exist_ok=True)
    sockets.chmod(0o700)
    env = {**os.environ, 'LC_ALL': 'C'}
    ctl = str(POSTGRES_BIN_PATH / 'pg_ctl')
    if not (pgdata / 'PG_VERSION').exists():
        subprocess.run([str(POSTGRES_BIN_PATH/'initdb'), '-D', str(pgdata), '--encoding=UTF8', '--locale=C', '--auth-local=trust', '--auth-host=scram-sha-256', '-U', 'postgres'], env=env, check=True, capture_output=True)
    running = subprocess.run([ctl, '-D', str(pgdata), 'status'], env=env, capture_output=True).returncode == 0
    if not running:
        subprocess.run([ctl, '-D', str(pgdata), '-w', '-l', str(pgdata/'log'), '-o', f"-h '' -k {shlex.quote(str(sockets))}", 'start'], env=env, check=True, capture_output=True)
        def stop():
            subprocess.run([ctl, '-D', str(pgdata), '-m', 'fast', '-w', 'stop'], env=env, capture_output=True)
        atexit.register(stop)
    return URL.create('postgresql+psycopg', username='postgres', database='postgres', query={'host': str(sockets)})


def init_db():
    global engine, DB, _local_pg
    if engine is not None:
        return
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    url = settings.database_url
    if not url:
        url = local_postgres_url()
    if isinstance(url, str):
        url = re.sub(r'^postgres(?:ql)?://', 'postgresql+psycopg://', url)
    engine = create_engine(url, pool_pre_ping=True)
    DB = sessionmaker(engine, expire_on_commit=False)


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status


def record_dict(r: Record):
    return {'id': r.id, 'created': r.created.isoformat(), **r.payload}
