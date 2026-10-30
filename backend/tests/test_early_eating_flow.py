"""Conversation-first eating goals and confirmed context updates; no external I/O."""
from datetime import timedelta
import copy
import pytest
from sqlalchemy import select
from app import agent, core, providers
from app.services import consumption_plan as plans, storage_guidance as guidance, food_dialogue

QUOTE='买回来的豆腐应立即放入冰箱冷藏保存，并尽快食用。'
SOURCE={'id':1,'title':'豆腐消费提示','url':'https://amr.sz.gov.cn/test-tofu','content':QUOTE*12}

def facts(**changes):
    return {'name':'散装豆腐','food_class':'fresh','storage':'refrigerated','condition':'raw',
            'date_type':'purchase','started_at':(core.now()-timedelta(hours=1)).isoformat(),**changes}

def fake_upstream(monkeypatch):
    monkeypatch.setattr(core.settings,'deepseek_api_key','test-only')
    monkeypatch.setattr(core.settings,'external_calls_enabled',True)
    calls=[]
    def request(method,url,**kwargs):
        calls.append(url)
        if url.endswith('/search'):return {'results':[SOURCE]}
        return {'choices':[{'message':{'content':'{"summary":"豆腐冷藏后尽快食用。","conditions_to_check":[],"source_ids":[1],"consumption_window":null}'}}]}
    monkeypatch.setattr(providers,'request_json',request)
    return calls

def test_goal_anchors_to_reported_day_not_refresh_or_query_day():
    f=facts()
    p=plans.build(None,f,[SOURCE],{1})
    assert p['basis']=='conservative_schedule' and p['source_window'] is None
    assert '不是保质期' in p['key_reminders'][0]
    local=plans._parse(p['target_at'])
    assert (local.hour,local.minute,local.second)==(23,59,59)
    assert local.date()==plans._parse(f['started_at']).astimezone(local.tzinfo).date()
    assert plans.view(f,p)['target_at']==p['target_at']
    older=facts(started_at=(core.now()-timedelta(days=2)).isoformat())
    assert plans.view(older,plans.build(None,older,[SOURCE],{1}))['status']=='due'

@pytest.mark.parametrize('change',[{'storage':'ambient'},{'storage':'frozen'},{'condition':'unknown'},
 {'food_class':'packaged'},{'started_at':None},{'date_type':'unknown'}])
def test_missing_or_different_context_cannot_get_a_same_day_goal(change):
    assert plans.build(None,facts(**change),[SOURCE],{1})['target_at'] is None

def test_prompt_use_for_other_food_or_conditions_does_not_transfer():
    for text in ['芒果要尽快食用。','豆腐开封后应尽快食用。','豆腐切开后应尽快食用。','豆腐煎后冷藏应尽快食用。']:
        assert plans.build(None,facts(),[{**SOURCE,'content':text}],{1})['target_at'] is None

def test_search_drops_dried_tofu_and_prefers_the_actual_food():
    rows=[{**SOURCE,'title':'散装豆腐干身份标签','url':'https://www.gov.cn/dried'},SOURCE,
          {**SOURCE,'title':'其他食材知识','url':'https://www.gov.cn/other'}]
    result=guidance.sources_from_response({'results':rows},facts())
    assert result[0]['title']=='豆腐消费提示'
    assert not any('豆腐干' in row['title'] for row in result)
    assert '"散装豆腐"' not in guidance.search_query(facts())

def test_saved_food_missing_facts_asks_before_paid_search(signed,monkeypatch):
    calls=fake_upstream(monkeypatch)
    food=signed.post('/api/v1/foods',json={'name':'散装豆腐','storage':'refrigerated'}).json()
    result=signed.post(f"/api/v1/foods/{food['id']}/storage-guidance").json()
    assert not calls and result['consumption_plan']['status']=='needs_context'
    assert 1<=len(result['guidance']['conditions_to_check'])<=3
    receipt=food_dialogue.planning_reply(result)
    assert receipt['needs_input'] and len(receipt['text'])<180

def test_saved_context_and_plan_update_together_only_after_review(signed,monkeypatch):
    calls=fake_upstream(monkeypatch)
    food=signed.post('/api/v1/foods',json={'name':'散装豆腐','storage':'refrigerated'}).json()
    with core.DB() as db:owner=db.scalar(select(core.User)).id
    value,card=agent._execute_tool('propose_food_action',{'food_id':food['id'],'action':'food_condition',
       'date_phrase':'今天','date_type':'purchase','food_class':'fresh','condition':'raw',
       'storage_note':'买回后立即密封冷藏','include_storage_guidance':True},owner,{})
    pending=value['pending_action'];target=value['preview_guidance']['consumption_plan']['target_at']
    assert target and len(calls)==2
    assert signed.get('/api/v1/foods').json()['items'][0]['started_at'] is None
    endpoint=f"/api/v1/actions/{pending['record_id']}/confirm"
    assert signed.post(endpoint,json={'confirm_key':'user_confirmed'}).status_code==422
    result=signed.post(endpoint,json={'confirm_key':'user_confirmed','idempotency_key':'once',
       'acknowledged_fields':pending['required_ack_fields']})
    assert result.status_code==200,result.text
    rows=signed.get('/api/v1/foods').json()['items']
    assert len(rows)==1 and rows[0]['id']==food['id']
    assert rows[0]['consumption_plan']['target_at']==target and rows[0]['started_at']
    assert rows[0]['expires_at'] is None and rows[0]['remind_at'] is None
    assert len(calls)==2
    again=signed.post(endpoint,json={'confirm_key':'user_confirmed','idempotency_key':'once',
       'acknowledged_fields':pending['required_ack_fields']}).json()
    assert again['replayed']
    assert len(signed.get('/api/v1/foods').json()['items'])==1

def test_action_fingerprints_include_the_new_date(signed):
    food=signed.post('/api/v1/foods',json={'name':'散装豆腐'}).json()
    with core.DB() as db:owner=db.scalar(select(core.User)).id
    a,_=agent._execute_tool('propose_food_action',{'food_id':food['id'],'action':'food_condition','date_phrase':'今天','date_type':'purchase'},owner,{})
    b,_=agent._execute_tool('propose_food_action',{'food_id':food['id'],'action':'food_condition','date_phrase':'昨天','date_type':'purchase'},owner,{})
    assert a['pending_action']['record_id']!=b['pending_action']['record_id']

def test_prompt_use_quote_omits_unrelated_room_temperature_duration():
    text='新鲜的豆腐在常温下最多存放6-7个小时，不管哪种豆腐如不是当天食用，都应立即放入冰箱保存，尽快食用。'
    p=plans.build(None,facts(),[{**SOURCE,'content':text}],{1})
    assert p['basis']=='conservative_schedule'
    assert '6-7' not in p['evidence_quote'] and p['evidence_quote'] in text
    assert len(food_dialogue.planning_reply({'consumption_plan':p})['text'])<180

def test_date_context_is_not_silently_accepted_in_status_action(signed):
    row=signed.post('/api/v1/foods',json={'name':'豆腐'}).json()
    r=signed.post('/api/v1/actions',json={'food_id':row['id'],'action':'food_status','status':'used','date_type':'purchase'})
    assert r.status_code==422
    assert signed.get('/api/v1/foods').json()['items'][0]['status']=='active'

def test_perishable_ambient_duration_is_not_promoted_to_a_safe_countdown():
    q='豆腐常温下最多存放6-7小时。'
    f=facts(storage='ambient')
    w=plans.SourceWindow(source_id=1,quote=q,minimum=6.0,maximum=7.0,unit='hours',anchor='purchase',storage='ambient',food_class='fresh',condition='raw')
    p=plans.build(w,f,[{**SOURCE,'content':q}],{1})
    assert p['status']=='needs_context' and p['target_at'] is None
    r=food_dialogue.planning_reply({'consumption_plan':p})
    assert '6' not in r['text'] and '食用安排目标' not in r['text']
