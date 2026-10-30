import io
from datetime import datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from app import core, providers, worker
from app.core import Job, Record, now
from app.rules import evaluate


@pytest.mark.parametrize('text,expected', [
    ('小麦粉、植物油、白砂糖', ('suitable','suitable','suitable')),
    ('小麦粉、奶粉', ('not_suitable','not_suitable','suitable')),
    ('蜂蜜', ('not_suitable','not_suitable','suitable')),
    ('大蒜、豆腐', ('not_suitable','suitable','suitable')),
    ('猪油', ('not_suitable','not_suitable','not_suitable')),
    ('蚝油', ('not_suitable','not_suitable','not_suitable')),
    ('素蚝油', ('uncertain','uncertain','uncertain')),
    ('天然香精', ('uncertain','uncertain','uncertain')),
    ('奶粉、未知乳化剂', ('not_suitable','not_suitable','uncertain')),
    ('椰奶', ('suitable','suitable','suitable')),   # 椰子制植物奶，按基础食材处理
])
def test_diet_contract(text,expected):
    result=evaluate(text,True,[],False)
    assert tuple(result['results'][d]['status'] for d in ('pure','vegan','lacto_ovo'))==expected


def test_cross_contact_a_and_other_unknowns():
    warning=['与含乳制品共用生产线']
    result=evaluate('小麦粉、植物油',True,warning)
    assert result['results']['vegan']['status']=='suitable'
    assert result['label_warnings']==warning
    assert evaluate('小麦粉、神秘添加剂',True,warning)['results']['vegan']['status']=='uncertain'
    assert evaluate('小麦粉、奶粉',True,warning)['results']['vegan']['status']=='not_suitable'


def test_incomplete_and_coriander():
    assert evaluate('小麦粉',False,[])['results']['vegan']['status']=='uncertain'
    r=evaluate('香菜',True,[],True)
    assert r['results']['pure']['status']=='suitable'
    assert r['pungent']=='not_detected'
    assert r['personal_avoidances']==['香菜']


def test_registration_session_and_failure(client):
    r=client.post('/api/v1/auth/register',json={'username':'alice','password':'correct-horse-123'})
    assert r.status_code==201
    assert 'httponly' in r.headers['set-cookie'].lower()
    assert client.get('/api/v1/auth/me').json()['username']=='alice'
    assert 'password' not in str(r.json())
    client.post('/api/v1/auth/logout')
    assert client.get('/api/v1/auth/me').status_code==401
    r=client.post('/api/v1/auth/login',json={'username':'alice','password':'wrong-password'})
    assert r.status_code==401
    assert r.json()['error']['code']=='INVALID_CREDENTIALS'
    assert client.post('/api/v1/auth/login',json={'username':'alice','password':'correct-horse-123'}).status_code==200


def test_validation_no_password_echo(client):
    r=client.post('/api/v1/auth/register',json={'username':'a','password':'secret'})
    assert r.status_code==422
    assert 'secret' not in r.text


def test_origin_and_auth(signed):
    assert signed.put('/api/v1/preferences',headers={'Origin':'https://evil.example'},json={'diet':'pure'}).status_code==403
    assert signed.put('/api/v1/preferences',headers={'Origin':core.settings.app_origin},json={'diet':'pure','avoid_coriander':True}).status_code==200
    assert signed.get('/api/v1/auth/me').json()['preferences']['avoid_coriander'] is True


def test_persisted_session_and_preferences(signed):
    signed.put('/api/v1/preferences',json={'diet':'lacto_ovo','avoid_coriander':True})
    core.engine.dispose()  # 全新数据库连接后仍可恢复会话与偏好。
    assert signed.get('/api/v1/auth/me').json()['preferences']['diet']=='lacto_ovo'


def recipe():
    return {'title':'我的番茄豆腐','ingredients':'番茄、豆腐、橄榄油','steps':'豆腐200g，番茄1个，炒熟。','meal':'main','purpose':'everyday','cuisine':'chinese','confirmed':True}


def test_recipe_confirmation_and_isolation(signed):
    assert signed.post('/api/v1/recipes',json={**recipe(),'confirmed':False}).status_code==400
    created=signed.post('/api/v1/recipes',json=recipe()).json()
    assert created['title']=='我的番茄豆腐'
    assert len(signed.get('/api/v1/recipes?diet=vegan').json()['items'])==1
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register',json={'username':'bob','password':'correct-horse-456'})
    assert signed.get('/api/v1/recipes').json()['items']==[]
    assert signed.delete('/api/v1/recipes/'+created['id']).status_code==404


def test_search_uses_local_without_external(signed):
    signed.post('/api/v1/recipes',json=recipe())
    r=signed.post('/api/v1/recipes/search',json={'query':'番茄'})
    assert r.json()['local'] is True
    assert signed.post('/api/v1/recipes/search',json={'query':'不存在的配方'}).status_code==503


def food(**kwargs):
    t=now()
    return {'name':'豆腐','storage':'refrigerated','location':'indoor','condition':'unopened','date_type':'purchase','started_at':t.isoformat(),'deadline_source':'unknown','status':'active',**kwargs}


def test_unknown_food_has_no_invented_deadline(signed):
    r=signed.post('/api/v1/foods',json=food()).json()
    assert r['expires_at'] is None
    assert r['display_status']=='unknown'
    assert signed.get('/api/v1/foods').json()['items'][0]['expires_at'] is None


def test_deadline_needs_source_and_timezone(signed):
    assert signed.post('/api/v1/foods',json=food(expires_at=(now()+timedelta(days=2)).isoformat())).status_code==422
    assert signed.post('/api/v1/foods',json=food(started_at='2026-10-21T12:00')).status_code==422
    assert signed.post('/api/v1/foods',json=food(deadline_source='package',expires_at=(now()+timedelta(days=2)).isoformat())).status_code==422


def test_storage_change_does_not_reset_lifetime(signed):
    # 冷藏包装说明不适用于冷冻：自动重评后清空系统期限、标待核对，且原始起点不被重置
    expiry=(now()+timedelta(days=1)).isoformat()
    original=food(deadline_source='package',expires_at=expiry,package_note='未开封冷藏包装标示',remind_at=(now()+timedelta(hours=20)).isoformat())
    created=signed.post('/api/v1/foods',json=original).json()
    updated=signed.put('/api/v1/foods/'+created['id'],json={**original,'storage':'frozen','started_at':(now()+timedelta(days=1)).isoformat(),'expires_at':(now()+timedelta(days=10)).isoformat()}).json()
    assert updated['expires_at'] is None                 # 无适用依据 → 清空对外有效期限
    assert updated['valid_deadline_at'] is None
    assert datetime.fromisoformat(updated['started_at'])==datetime.fromisoformat(original['started_at'])
    assert updated['display_status']=='needs_review'
    assert updated['auto_reevaluated'] is True
    assert len(updated['storage_history'])==1
    assert signed.get('/api/v1/notifications/tasks').json()[0]['status']=='cancelled'


def test_hour_reminder_and_cancel(signed):
    data=food(deadline_source='user',expires_at=(now()+timedelta(hours=2)).isoformat(),remind_at=(now()-timedelta(seconds=1)).isoformat())
    created=signed.post('/api/v1/foods',json=data).json()
    assert created['display_status']=='due'
    worker.process_once()
    assert signed.get('/api/v1/notifications/tasks').json()[0]['status']=='waiting_configuration'
    signed.put('/api/v1/foods/'+created['id'],json={**data,'status':'used'})
    assert signed.get('/api/v1/notifications/tasks').json()[0]['status']=='cancelled'


def test_reconfirm_after_storage_change(signed):
    original=food(deadline_source='user',expires_at=(now()+timedelta(hours=3)).isoformat())
    created=signed.post('/api/v1/foods',json=original).json()
    updated=signed.put('/api/v1/foods/'+created['id'],json={**original,'storage':'frozen'}).json()
    # 用户自设日期不因条件变化被覆盖或作废（自动重评只处理系统依据）
    assert updated['display_status']=='stored'
    assert updated['assessment_status']=='user_set'
    assert datetime.fromisoformat(updated['expires_at'])==datetime.fromisoformat(original['expires_at'])
    confirmed=signed.put('/api/v1/foods/'+created['id'],json={**updated,'reconfirmed':True,'package_note':'已重新核对当前条件','expires_at':(now()+timedelta(hours=5)).isoformat()}).json()
    assert confirmed['display_status']=='stored'
    assert len(confirmed['storage_history'])==1


def test_food_isolation(signed):
    item=signed.post('/api/v1/foods',json=food()).json()
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register',json={'username':'bob','password':'correct-horse-456'})
    assert signed.get('/api/v1/foods').json()['items']==[]
    assert signed.put('/api/v1/foods/'+item['id'],json=food()).status_code==404
    assert signed.delete('/api/v1/foods/'+item['id']).status_code==404


def test_missing_services_do_not_return_mock(signed):
    assert signed.get('/api/v1/restaurants?city=上海').status_code==503
    assert signed.get('/api/v1/capabilities').json()['storage_rules_ready'] is False
    assert signed.get('/api/v1/notifications/config').json()['public_key']==''


@pytest.mark.parametrize('url', ['https://127.0.0.1/a','http://web.push.apple.com/a','https://evil.example/a','https://web.push.apple.com.evil.example/a','https://web.push.apple.com:8443/a','https://user:pass@web.push.apple.com/a'])
def test_notification_endpoint_denies_arbitrary_targets(url):
    assert not worker.valid_push_endpoint(url)


def test_photo_validation_privacy_cleanup(signed,monkeypatch):
    original_files=set((core.settings.data_dir/'images').glob('*.png'))
    monkeypatch.setattr(core.settings,'deepseek_api_key','test-only')
    monkeypatch.setattr(core.settings,'external_calls_enabled',True)
    assert signed.post('/api/v1/ingredients/photo',files={'file':('fake.jpg',b'not-image','image/jpeg')}).status_code==422
    content=io.BytesIO();Image.new('RGB',(40,40),'white').save(content,format='PNG')
    response=signed.post('/api/v1/ingredients/photo',files={'file':('private.png',content.getvalue(),'image/png')},data={'save_original':'false'})
    assert response.status_code==202
    monkeypatch.setattr(providers,'extract_image',lambda *_: (_ for _ in ()).throw(core.AppError('MOCK_FAILURE','提取失败')))
    worker.process_once()
    assert signed.get('/api/v1/jobs/'+response.json()['job_id']).json()['status']=='failed'
    with core.DB() as db:
        assert db.scalars(select(Record).where(Record.kind=='image')).all()==[]
    assert set((core.settings.data_dir/'images').glob('*.png'))==original_files


def test_private_history_and_delete(signed):
    item=signed.post('/api/v1/ingredients/evaluate',json={'text':'小麦粉','complete':True}).json()
    assert len(signed.get('/api/v1/ingredients/history').json())==1
    signed.delete('/api/v1/ingredients/history/'+item['id'])
    assert signed.get('/api/v1/ingredients/history').json()==[]


def test_restart_recovery_of_persisted_task(signed):
    with core.DB() as db:
        user=db.scalar(select(core.User))
        task=Job(owner=user.id,kind='photo',status='running',payload={})
        db.add(task);db.commit();identifier=task.id
    worker.recover_and_clean()
    assert signed.get('/api/v1/jobs/'+identifier).json()['status']=='failed'


def test_push_failure_retries_are_bounded(signed,monkeypatch):
    from pywebpush import WebPushException
    monkeypatch.setattr(core.settings,'external_calls_enabled',True)
    monkeypatch.setattr(core.settings,'vapid_private_key','test-only')
    monkeypatch.setattr(core.settings,'vapid_public_key','test-only')
    monkeypatch.setattr(core.settings,'vapid_subject','mailto:test@example.invalid')
    def fail(**kwargs):
        raise WebPushException('test-only simulated service failure')
    monkeypatch.setattr('pywebpush.webpush',fail)
    signed.post('/api/v1/foods',json=food(deadline_source='user',expires_at=(now()+timedelta(hours=1)).isoformat(),
                                       remind_at=(now()-timedelta(seconds=1)).isoformat()))
    with core.DB() as db:
        user=db.scalar(select(core.User))
        db.add(Record(owner=user.id,kind='subscription',payload={'endpoint':'https://web.push.apple.com/test','keys':{}}))
        db.commit()
    for _ in range(3):
        with core.DB() as db:
            job=db.scalar(select(Job).where(Job.kind=='reminder'))
            job.due=now()-timedelta(seconds=1)
            db.commit()
        worker.process_once()
    result=signed.get('/api/v1/notifications/tasks').json()[0]
    assert result['status']=='failed'
    assert result['result']['device_display']=='unverified'
    with core.DB() as db:
        assert db.scalar(select(Job).where(Job.kind=='reminder')).attempts==3


def test_global_usage_limit_cannot_be_bypassed_with_new_account(signed,monkeypatch):
    monkeypatch.setattr(core.settings,'deepseek_api_key','test-only')
    monkeypatch.setattr(core.settings,'external_calls_enabled',True)
    monkeypatch.setattr(core.settings,'global_daily_external_limit',1)
    with core.DB() as db:
        alice=db.scalar(select(core.User).where(core.User.username=='alice'))
    providers.reserve_call(alice.id,'photo')
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register',json={'username':'bob','password':'correct-horse-456'})
    with core.DB() as db:
        bob=db.scalar(select(core.User).where(core.User.username=='bob'))
    with pytest.raises(core.AppError) as exc:
        providers.reserve_call(bob.id,'photo')
    assert exc.value.code=='GLOBAL_QUOTA_EXCEEDED'
