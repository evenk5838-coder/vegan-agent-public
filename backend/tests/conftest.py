import asyncio
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import core, providers, worker
from app.main import app


@pytest.fixture(scope='session', autouse=True)
def database(tmp_path_factory):
    core.settings.data_dir = tmp_path_factory.mktemp('shushi-test')
    core.settings.database_url = ''
    core.init_db()
    command.upgrade(Config(str(Path(__file__).resolve().parents[1]/'alembic.ini')), 'head')
    yield
    core.engine.dispose()


@pytest.fixture
def client(monkeypatch):
    # 停止自动消费，具体测试显式调用一次worker以验证状态。
    async def idle():
        await asyncio.Event().wait()
    monkeypatch.setattr(worker, 'loop', idle)
    with core.DB() as db:
        for table in (core.Job, core.Record, core.Session, core.User):
            db.execute(delete(table))
        db.commit()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    """测试一律禁止真实外部调用：清空凭据并封锁底层 HTTP。

    本机 .env 可能已配置真实 Key 且开启外部调用；若不在此拦截，
    跑测试会真的请求供应商并产生费用。需要假上游的用例可在测试内覆盖 request_json。
    """
    for name in ('deepseek_api_key', 'amap_api_key', 'amap_js_key', 'amap_js_security_code',
                 'transcription_api_key', 'transcription_endpoint',
                 'vapid_public_key', 'vapid_private_key'):
        monkeypatch.setattr(core.settings, name, '')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', False)

    class BlockedClient:
        """在 HTTP 层拦截：任何真实出网都会失败；用例可自行替换 httpx.Client 或 request_json。"""

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, *args, **kwargs):
            raise AssertionError('测试中禁止真实外部调用（请用替身替换请求层）')

    monkeypatch.setattr(httpx, 'Client', BlockedClient)
    yield


@pytest.fixture
def published_rules(monkeypatch):
    """测试专用：把规则条目标为已发布，用于验证发布后的正式期限链路。

    只修改隔离测试进程内的内存副本，不写入生产规则、不落库。
    """
    import copy
    from app import storage_rules
    rules = copy.deepcopy(list(storage_rules.RULES))
    for rule in rules:
        rule['status'] = 'published'
    monkeypatch.setattr(storage_rules, 'RULES', rules)
    return rules


@pytest.fixture
def signed(client):
    response = client.post('/api/v1/auth/register', json={'username': 'alice', 'password': 'correct-horse-123'})
    assert response.status_code == 201
    return client
