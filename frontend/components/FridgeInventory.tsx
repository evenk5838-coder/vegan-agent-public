'use client';

import { useEffect, useRef, useState } from 'react';
import { Apple, Bean, Bell, Carrot, Check, ChevronDown, ChevronRight, Leaf, LoaderCircle, Package, Refrigerator, RefreshCw, Snowflake, Sun, Trash2, Wheat } from 'lucide-react';
import { api } from '../lib/api';
import { foodProgress, type Food } from '../lib/food-progress';
import { consumptionPlanView } from '../lib/consumption-plan';
import { PlanSummary, PlanDetails } from './ConsumptionPlan';
import { editValues, foodConditionBody, localDate, reevaluationBody, unappliedFoodFields } from '../lib/food-forms';
import { autoAssessmentMessage, foodUsageView } from '../lib/food-advice';
import FoodAdvice from './FoodAdvice';
import FoodReminderDialog from './FoodReminderDialog';
import StorageGuidanceDialog from './StorageGuidanceDialog';
import guidanceStyles from './storage-guidance.module.css';
import styles from './fridge-inventory.module.css';

const stores = {refrigerated:'冷藏',frozen:'冷冻',ambient:'常温',unknown:'未分类'};
function storageGroup(value?:string|null){return value&&Object.hasOwn(stores,value)?value:'unknown';}
const conditions = {raw:'未加工 / 生',cut:'已切配',cooked:'已熟制'};
const foodClasses = {unknown:'尚未确定',fresh:'新鲜食材',cooked:'自制熟食 / 剩菜',packaged:'包装成品 / 半成品'};
const dateTypes = {purchase:'购买日期',production:'生产日期',prepared:'制作日期',opened:'开封日期'};
function date(value?:string|null){const parsed=value?new Date(value):null;return parsed&&!Number.isNaN(parsed.getTime())?parsed.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}):'尚未设置';}
function shortDate(value?:string|null){const parsed=value?new Date(value):null;return parsed&&!Number.isNaN(parsed.getTime())?parsed.toLocaleDateString('zh-CN',{month:'numeric',day:'numeric'}):'日期待核对';}
function foodSymbol(name:string){
  if(/豆|腐|豌|扁豆/.test(name))return Bean;
  if(/萝卜|胡萝卜|土豆|薯|藕|姜/.test(name))return Carrot;
  if(/菜|菠菜|青葱|莴|生菜|西兰花/.test(name))return Leaf;
  if(/苹果|梨|橙|莓|果|桃|瓜|香蕉|柠檬|葡萄/.test(name))return Apple;
  if(/米|麦|面|谷|燕麦|藜/.test(name))return Wheat;
  return Package;
}

export default function FridgeInventory({ rows, onChanged, onTalk }: { rows: Food[]; onChanged: () => Promise<void>; onTalk?: (prompt: string) => void }) {
  const [storage,setStorage]=useState('');
  const [editing,setEditing]=useState<string|null>(null);
  const [deleting,setDeleting]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [expanded,setExpanded]=useState<string|null>(null);
  const [reminderFood,setReminderFood]=useState<string|null>(null);
  const [guidanceId,setGuidanceId]=useState<string|null>(null);
  const guidanceFood=rows.find(row=>row.id===guidanceId&&row.status==='active');
  const actionRunning=useRef(false);
  const [clock,setClock]=useState(()=>Date.now());
  useEffect(()=>{const timer=setInterval(()=>setClock(Date.now()),60000);return()=>clearInterval(timer);},[]);
  const active=rows.filter(r=>r.status==='active');
  async function action(fn:()=>Promise<void>){if(actionRunning.current)return;actionRunning.current=true;setBusy(true);setError('');setNotice('');try{await fn();await onChanged();}catch(e){setError(e instanceof Error?e.message:'没有完成，请重试。');await onChanged().catch(()=>{});}finally{actionRunning.current=false;setBusy(false);}}

  function card(food:Food){
    const progress=foodProgress(food,clock);
    const plan=food.status==='active'?consumptionPlanView(food.consumption_plan,clock):null;
    const usage=foodUsageView(food.suggestion,clock);
    const store=storageGroup(food.storage);
    const Symbol=foodSymbol(food.name);
    const isOpen=expanded===food.id;
    const initial=editValues({...food,item_id:food.id,missing:[],expires_at:food.expires_at??undefined,remind_at:food.remind_at??undefined,temperature_c:food.temperature_c??undefined});
    return <article className={`${styles.foodCard} ${isOpen?styles.expanded:''} ${(plan?plan.due||!plan.target:progress.pending)?styles.attention:''}`} key={food.id}>
      <button type="button" className={styles.cardTrigger} aria-expanded={isOpen} aria-controls={'food-detail-'+food.id}
        onClick={()=>{if(busy)return;setExpanded(isOpen?null:food.id);setError('');}}>
        <span className={`${styles.illustration} ${store==='frozen'?styles.frozen:store==='ambient'?styles.ambient:store==='unknown'?styles.unclassified:styles.refrigerated}`} aria-hidden="true">
          <span className={styles.storeBadge}>{store==='ambient'?<Sun size={12}/>:store==='frozen'?<Snowflake size={12}/>:store==='unknown'?<Package size={12}/>:<Refrigerator size={12}/>}{store==='unknown'?'待补存放':stores[store as keyof typeof stores]}</span>
          <Symbol size={51} strokeWidth={1.3}/><span className={styles.leafAccent}><Leaf size={44} strokeWidth={.7}/></span>
        </span>
        <span className={styles.cardSummary}>
          <span className={styles.cardName}>{food.name}<ChevronDown size={15} className={isOpen?styles.turned:''}/></span>
          {food.status==='active'?<>
            {plan?.target?<PlanSummary view={plan} name={food.name}/>:<>
            {plan&&<PlanSummary view={plan} name={food.name}/>}
            <span className={`${styles.progressLabel} ${progress.pending?styles.pendingLabel:''}`}>{progress.label}</span>
            {usage&&food.suggestion?.reason!=='missing_info'&&<><span className={styles.cardDate}>{usage.title}</span><span className={styles.cardDate}>{usage.text}</span></>}
            {progress.percent!==null&&<progress className={styles.miniProgress} aria-label={`${food.name}预计期限已用比例`} value={progress.percent} max={100}/>}
            </>}
            <span className={styles.cardDate}>{progress.deadline?`${food.assessment_status==='user_set'?'自设':'预计'}日期 ${shortDate(progress.deadline)}`:food.created&&Number.isFinite(Date.parse(food.created))?`登记于 ${shortDate(food.created)}`:'日期可后补'}{food.remind_at&&<Bell size={11} aria-label="已设提醒"/>}</span>
          </>:<span className={styles.progressLabel}>{food.status==='used'?'已用完':'已丢弃'}</span>}
        </span>
      </button>
      {food.status==='active'&&<button type="button" className={guidanceStyles.entry} aria-label={food.name+'的储存建议'} onClick={()=>setGuidanceId(food.id)}><Leaf size={14}/>储存建议<ChevronRight size={13}/></button>}
      {isOpen&&<div className={styles.cardDetail} id={'food-detail-'+food.id}>
      <div className="row-between"><h3>{food.name}</h3><span className="small muted">{food.status==='used'?'已用完':food.status==='discarded'?'已丢弃':conditions[food.condition as keyof typeof conditions]||'加工状态待核对'}</span></div>
      {plan?.target&&<PlanDetails view={plan}/>}
      {food.status==='active'&&!plan?.target&&<div className="expiry-progress"><strong>{progress.label}</strong>{progress.percent!==null&&<><progress aria-label={`${food.name}预计期限已用比例`} value={progress.percent} max={100}/><div className="row-between small muted"><span>记录起点 {date(food.valid_started_at)}</span><span>预计日期 {date(progress.deadline)}</span></div></>}{progress.percent===null&&progress.deadline&&<p className="small muted">{food.assessment_status==='user_set'?'你自行设置的日期':'适用截止日期'}：{date(progress.deadline)}</p>}<p className="small muted">{progress.draftRule?'参考条目尚未审核，不据此展示正式期限进度。':progress.pending?'请按当前存放方式重新核对。原日期不再作为当前依据。':food.assessment_status==='user_set'?'仅显示距你设置日期的时间，不表示系统判断的新鲜度。':progress.percent!==null?'按适用期限显示时间进度，不代表实际新鲜度或食用安全。':'没有足够依据时，不推算保质期。'}</p></div>}
      {food.status==='active'&&!plan?.target&&<FoodAdvice suggestion={food.suggestion}/>}
      {typeof food.stored_for_days==='number'&&Number.isFinite(food.stored_for_days)&&food.stored_for_days>=0&&<p className="small muted">{food.date_origin==='prepared'?'制作后经过':'距记录起点'} {food.stored_for_days} 天</p>}
      <p className="small muted">{foodClasses[(food.food_class||'unknown') as keyof typeof foodClasses]||'类别待核对'} · {initial.opened==='true'?'已开封':initial.opened==='false'?'未开封':'开封状态未知 / 不适用'}</p>
      <details><summary>记录与期限依据<ChevronRight size={14}/></summary><p className="small muted">已知日期：{date(food.started_at)}<br/>登记时间：{date(food.created)}<br/>地点：{food.location==='indoor'?'室内':food.location==='outdoor'?'室外':'尚未确定'}{food.temperature_c!=null?` · ${food.temperature_c} ℃`:''}<br/>期限依据：{progress.pending?'待核对':food.deadline_source==='package'?'适用包装说明':food.deadline_source==='user'?'用户自设':food.deadline_source==='rule'?'储存参考条目':'尚无依据'}</p>{!progress.pending&&food.package_note&&<p className="small muted">包装说明：{food.package_note}</p>}{(food.storage_history||[]).length>0&&<p className="small muted">保留了 {food.storage_history.length} 次条件变更记录；存放时间不会因转冷冻而归零。</p>}</details>
      <div className={styles.foodActions}>{food.status==='active'&&<><button className="text-button" disabled={busy} onClick={()=>{setEditing(editing===food.id?null:food.id);setError('');}}>编辑记录</button><button type="button" className="text-button" disabled={busy} onClick={()=>setReminderFood(food.id)}><Bell size={14}/>{food.remind_at?'更改提醒':'添加提醒'}</button><button className="text-button" disabled={busy} onClick={()=>void action(async()=>{await api('/foods/'+food.id,{method:'PUT',body:JSON.stringify({...food,status:'used'})});})}>已用完</button><button className="text-button" disabled={busy} onClick={()=>void action(async()=>{await api('/foods/'+food.id,{method:'PUT',body:JSON.stringify({...food,status:'discarded'})});})}>已丢弃</button></>}<button className="icon-button danger" aria-label={'删除'+food.name} disabled={busy} onClick={()=>setDeleting(food.id)}><Trash2 size={15}/></button></div>
      {deleting===food.id&&<div className="warning"><p>确认删除“{food.name}”这条记录？</p><div className="button-row"><button className="text-button danger" disabled={busy} onClick={()=>void action(async()=>{await api('/foods/'+food.id,{method:'DELETE'});setDeleting(null);})}>确认删除</button><button className="text-button" onClick={()=>setDeleting(null)}>保留</button></div></div>}
      {editing===food.id&&<>
        <form className="reevaluate-form" onSubmit={event=>{
          event.preventDefault();const data=new FormData(event.currentTarget);
          const values=Object.fromEntries(['storage','condition','food_class','opened','date_type','location','temperature'].map(key=>[key,String(data.get(key)||'').trim()]));
          void action(async()=>{
            const body=foodConditionBody(food as unknown as Record<string,unknown>,values);
            const result=await api<Food & {auto_reevaluated?:boolean;reason?:string}>('/foods/'+food.id,{method:'PUT',body:JSON.stringify(body)});
            const check=Object.fromEntries(['storage','condition','food_class','opened','location','temperature_c','date_type'].map(key=>[key,body[key as keyof typeof body]]));
            const unapplied=unappliedFoodFields(check,result as unknown as Record<string,unknown>);
            if(unapplied.length)throw new Error(`服务器尚未应用：${unapplied.join('、')}。请重新核对，不能视为新条件已生效。`);
            setNotice(autoAssessmentMessage(result));setEditing(null);
          });
        }}>
          <h3>记录现在的实际情况</h3><fieldset disabled={busy} className={`form-grid ${styles.fieldset}`}>
            <label>食物类别<select name="food_class" defaultValue={initial.food_class}>{Object.entries(foodClasses).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
            <label>存放方式<select name="storage" defaultValue={store}>{Object.entries(stores).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
            <label>加工状态<select name="condition" defaultValue={initial.condition}><option value="unknown">尚未确定</option>{Object.entries(conditions).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
            <label>是否开封<select name="opened" defaultValue={initial.opened}><option value="">未知 / 不适用</option><option value="false">未开封</option><option value="true">已开封</option></select></label>
            <label>原记录的日期类型<select name="date_type" defaultValue={initial.date_type}><option value="unknown">暂不确定</option>{Object.entries(dateTypes).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
            <label>存放地点<select name="location" defaultValue={food.location}><option value="unknown">不确定</option><option value="indoor">室内</option><option value="outdoor">室外</option></select></label>
            <label>温度 ℃（已知时填写）<input name="temperature" type="number" min={-80} max={70} step="0.1" defaultValue={food.temperature_c??''}/></label>
          </fieldset>
          {['opened','unopened'].includes(food.condition)&&<p className="small muted">旧记录仅记了开封状态，请另选实际加工状态；不会默认当作生鲜。</p>}
          <p className="small muted">保留原记录起点。保存后由服务器按当前条件重评；没有依据就标待核对。用户自行设置的日期单独保留。</p>
          <button className="primary" disabled={busy}>{busy?<LoaderCircle size={16} className="spin"/>:<Check size={16}/>}保存条件并查看重评结果</button>
        </form>
        <form className="reevaluate-form" onSubmit={event=>{
          event.preventDefault();const data=new FormData(event.currentTarget);
          const values={storage:food.storage,condition:food.condition,location:food.location,temperature:food.temperature_c==null?'':String(food.temperature_c),...Object.fromEntries(['basis','deadline','note','reminder'].map(key=>[key,String(data.get(key)||'').trim()]))};
          void action(async()=>{await api('/foods/'+food.id+'/reevaluate',{method:'POST',body:JSON.stringify(reevaluationBody(food,values))});setEditing(null);setNotice('期限与提醒设置已提交核对；当前结果以食材记录为准。');});
        }}>
          <h3>另行核对期限与提醒</h3><p className="small muted">这里使用当前已保存的实际条件。如果上方条件有修改，请先保存，再核对日期。</p>
          <fieldset disabled={busy} className={`form-grid ${styles.fieldset}`}>
            <label>期限依据<select name="basis" defaultValue={food.deadline_source==='user'?'user':'unknown'}><option value="unknown">暂时没有适用依据</option><option value="package">适用当前条件的包装说明</option><option value="rule">按已发布且适用的规则核对</option><option value="user">我自行设置日期</option></select></label>
            <label>所设日期<input name="deadline" type="datetime-local" defaultValue={food.deadline_source==='user'?localDate(food.expires_at):''}/></label>
            <label>提醒时刻（可选）<input name="reminder" type="datetime-local" defaultValue={localDate(food.remind_at)}/></label>
            <label>适用包装说明（选包装时必填）<input name="note" maxLength={1000} placeholder="请核对是否适用当前状态和存放方式"/></label>
          </fieldset>
          <p className="small muted">没有适用规则时不会生成日期。清空提醒并确认会取消原提醒。</p><button className="primary" disabled={busy}>{busy?<LoaderCircle size={16} className="spin"/>:<Check size={16}/>}确认期限与提醒</button>
        </form>
      </>}
      </div>}
    </article>;
  }

  return <section className={styles.inventory} aria-label="我的电子冰箱"><div className={styles.inventoryHeading}><div><span className={styles.inventoryCount}>{active.length} 份食材，慢慢做成好吃的。</span></div><button className="text-button" disabled={busy} onClick={()=>void action(async()=>{})}><RefreshCw size={14}/>刷新</button></div><div className={styles.storageTabs} aria-label="按存放方式查看"><button className={storage===''?styles.selected:''} aria-pressed={storage===''} onClick={()=>setStorage('')}>全部 <span>{active.length}</span></button>{Object.entries(stores).map(([id,label])=><button className={storage===id?styles.selected:''} key={id} aria-pressed={storage===id} onClick={()=>setStorage(id)}>{id==='ambient'?<Sun size={15}/>:id==='frozen'?<Snowflake size={15}/>:id==='unknown'?<Package size={15}/>:<Refrigerator size={15}/>} {label} <span>{active.filter(r=>storageGroup(r.storage)===id).length}</span></button>)}</div>
    {error&&<p role="alert" className="error">{error}</p>}{notice&&<p role="status" className="warning">{notice}</p>}
    <div className={styles.inventoryGrid}>{active.filter(r=>!storage||storageGroup(r.storage)===storage).map(card)}</div>{!active.filter(r=>!storage||storageGroup(r.storage)===storage).length&&<section className={styles.empty}><Refrigerator size={38} strokeWidth={1}/><h3>{storage?'这里还没有'+stores[storage as keyof typeof stores]+'食材':'冰箱里，留一点新鲜的位置'}</h3><p>添加食材后，就能在这里找到它。</p></section>}
    {rows.some(r=>r.status!=='active')&&<details className={styles.archive}><summary>已用完 / 已丢弃记录<ChevronRight size={15}/></summary><div className={styles.inventoryGrid}>{rows.filter(r=>r.status!=='active').map(card)}</div></details>}
    {guidanceFood&&<StorageGuidanceDialog food={guidanceFood} onUpdated={onChanged} onClose={()=>setGuidanceId(null)} onTalk={onTalk}/>}
    <FoodReminderDialog open={!!reminderFood} onClose={()=>setReminderFood(null)} foods={rows} onChanged={onChanged} initialFoodId={reminderFood||undefined}/>
  </section>;
}
