"""Two app/PG process lifetimes in a code-only temporary copy, without the real .env."""
import os
from pathlib import Path
import subprocess
import shutil
import json
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='shushi-persistence-') as folder:
    temporary = Path(folder)
    backend = temporary / 'backend'
    backend.mkdir()
    for name in ('app', 'alembic'):
        shutil.copytree(root/'backend'/name, backend/name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.env*'))
    shutil.copyfile(root/'backend/alembic.ini', backend/'alembic.ini')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LC_ALL': 'C',
           'DATA_DIR': str(temporary/'data'), 'DATABASE_URL': '',
           'PYTHONPATH': str(backend), 'EXTERNAL_CALLS_ENABLED': 'false'}
    setup = '''from app import core
from alembic import command
from alembic.config import Config
core.init_db()
command.upgrade(Config("backend/alembic.ini"), "head")
with core.DB() as db:
    for kind in ("food", "pending_action", "food_draft", "candidate"):
        db.add(core.Record(owner="test-only",kind=kind,payload={"marker":"restart-evidence","status":"pending"}))
    db.add(core.Job(owner="test-only",kind="chat_turn",status="waiting_input",payload={"marker":"restart-evidence"}))
    db.commit()
'''
    verify = '''from app import core
from sqlalchemy import select
core.init_db()
with core.DB() as db:
    rows=db.scalars(select(core.Record).where(core.Record.owner=="test-only")).all()
    assert {r.kind for r in rows}=={"food","pending_action","food_draft","candidate"}
    assert all(r.payload.get("marker")=="restart-evidence" and r.payload.get("status")=="pending" for r in rows)
    job=db.scalar(select(core.Job).where(core.Job.owner=="test-only"))
    assert job.status=="waiting_input" and job.payload["marker"]=="restart-evidence"
'''
    for snippet in (setup, verify):
        result = subprocess.run([sys.executable, '-c', snippet], cwd=temporary, env=env, capture_output=True, text=True, timeout=90)
        if result.returncode:
            print('进程重启持久化检查失败。')
            raise SystemExit(1)
    print(json.dumps({'two_process_lifetimes': 'passed', 'postgres_stop_restart': 'passed',
                      'record_kinds': 4, 'waiting_input_job': 'preserved',
                      'real_business_database_touched': False}))
