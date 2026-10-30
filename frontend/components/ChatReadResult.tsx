'use client';

import { useEffect, useState } from 'react';
import { Bell, BookOpen, ChevronRight, ClipboardList, CookingPot, ExternalLink, History, Refrigerator } from 'lucide-react';
import { api, safeExternalUrl } from '../lib/api';
import { foodProgress, type Food } from '../lib/food-progress';
import { recipeView } from '../lib/recipe-display';
import { chatReadTypes, conditionLines, conditionNames, foodClassNames, foodStatusNames, knowledgeTypeNames, readDate, readObject, readRows, readText, reminderStatusNames, storageNames } from '../lib/chat-read';
import FoodDraft from './FoodDraft';
import RecipeDetail from './RecipeDetail';
import ConsumptionPlan from './ConsumptionPlan';
import styles from './chat-results.module.css';

export type ChatReadResultProps = { type: string; payload: unknown; onNavigate?: (page: string) => void; disabled?: boolean };

function CurrentFoodPlan({ id }: { id: string }) {
  const [current, setCurrent] = useState<Food | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    api<Food>('/foods/' + encodeURIComponent(id), { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setCurrent(value); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [id]);
  if (current?.status === 'active' && current.consumption_plan) return <ConsumptionPlan plan={current.consumption_plan} name={current.name}/>;
  return <p className={styles.note}>{failed ? '当前食用时间读取失败，请到冰箱刷新查看。' : current ? current.status === 'active' ? '当前尚未生成食用计划。' : '这份食材已结束管理。' : '正在核对当前食用时间…'}</p>;
}
function FoodRead({ row }: { row: Record<string, unknown> }) {
  const id = readText(row.id), hasPlan = !!row.consumption_plan;
  const basis = readObject(row.deadline_basis);
  const progress = foodProgress(row as Food);
  const source = safeExternalUrl(readText(basis.rule_source));
  const history = readRows(row.storage_history);
  return <article className={styles.item}>
    <div className={styles.heading}><h4>{readText(row.name) || '名称未提供'}</h4><span className={styles.badge}>{foodStatusNames[readText(row.status)] || '状态待核对'}</span></div>
    <p className={styles.meta}>{storageNames[readText(row.storage)] || '存放方式待核对'} · {conditionNames[readText(row.condition)] || '加工状态待核对'}</p>
    <p className={styles.meta}>{foodClassNames[readText(row.food_class)] || '类别待核对'} · {typeof row.opened === 'boolean' ? row.opened ? '已开封' : '未开封' : '开封状态未提供'}</p>
    {hasPlan && id ? <CurrentFoodPlan key={id} id={id}/> : row.status === 'active' && <>
      <p>{progress.label}</p>
      {progress.percent !== null && <progress className={styles.progress} aria-label="预计期限已用比例" max={100} value={progress.percent}/>}
      {progress.deadline && <p className={styles.meta}>{row.assessment_status === 'user_set' ? '自行设置的日期' : '适用截止日期'}：{readDate(progress.deadline)}</p>}
      <p className={styles.note}>{progress.pending ? '存放条件已变化，请重新核对期限。' : progress.draftRule ? '储存参考条目尚未发布，不据此展示正式期限。' : progress.deadline ? '时间进度不代表实际新鲜度或食用安全。' : '当前没有足够依据推算保质期。'}</p>
    </>}
    <details><summary>记录与依据<ChevronRight size={13}/></summary>
      <dl className={styles.facts}>
        <div><dt>记录起点</dt><dd>{readDate(row.started_at) || '未提供'}</dd></div>
        <div><dt>存放地点</dt><dd>{({ indoor: '室内', outdoor: '室外' } as Record<string, string>)[readText(row.location)] || '未确定'}{typeof row.temperature_c === 'number' && Number.isFinite(row.temperature_c) ? ` · ${row.temperature_c} ℃` : ''}</dd></div>
        <div><dt>提醒时间</dt><dd>{readDate(row.remind_at) || '尚未设置'}</dd></div>
        <div><dt>期限依据</dt><dd>{progress.pending ? '待重新核对' : ({ package: '适用包装说明', rule: '储存参考条目', user: '用户自行设置' } as Record<string, string>)[readText(basis.type)] || '尚无依据'}</dd></div>
      </dl>
      {readText(basis.note) && <p className={styles.note}>{readText(basis.note)}</p>}
      {source && <a className={styles.source} href={source} target="_blank" rel="noopener noreferrer">查看期限来源<ExternalLink size={12}/></a>}
      {history.length > 0 && <p className={styles.note}>保留了 {history.length} 次存放条件变更记录，存放时间不会因更换条件而归零。</p>}
    </details>
  </article>;
}

function DraftReadList({ rows, disabled }: { rows: Record<string, unknown>[]; disabled: boolean }) {
  const [opened, setOpened] = useState<string | null>(null);
  return <div className={styles.list}>{rows.map((row, index) => {
    const id = readText(row.draft_id);
    const items = readRows(row.items);
    const names = items.map(item => readText(item.name)).filter(Boolean);
    const status = ({ pending_confirmation: '待核对', partially_saved: '部分已保存', confirmed: '已保存', expired: '已过期', cancelled: '已取消' } as Record<string, string>)[readText(row.status)] || '状态待核对';
    return <article className={styles.item} key={id || index}>
      <div className={styles.heading}><h4>{items.length} 项食材草稿</h4><span className={styles.badge}>{status}</span></div>
      <p>{names.join('、') || '尚未提供食材名称'}</p>
      {readDate(row.expires_at) && <p className={styles.meta}>草稿保留至 {readDate(row.expires_at)}</p>}
      {id && !disabled && <button type="button" className="text-button" onClick={() => setOpened(opened === id ? null : id)}>{opened === id ? '收起核对卡片' : '读取最新草稿并核对'}<ChevronRight size={13}/></button>}
      {id && opened === id && !disabled && <FoodDraft key={id} draftId={id}/>}
    </article>;
  })}</div>;
}

/** Query cards display the persisted result; all writes stay in explicit confirmation components. */
export default function ChatReadResult({ type, payload, onNavigate, disabled = false }: ChatReadResultProps) {
  if (!chatReadTypes.has(type)) return null;
  const data = readObject(payload), rows = readRows(data.items);
  const titles: Record<string, string> = { ingredient_history: '配料核对历史', food_inventory: '你的食材库存', food_detail: '食材记录', reminder_status: '食材提醒状态', recipe_detail: '菜谱详情', food_draft_list: '待核对的食材草稿', knowledge_result: '查到的知识资料' };
  const Icon = type === 'ingredient_history' ? History : type === 'reminder_status' ? Bell : type === 'knowledge_result' ? BookOpen : type === 'recipe_detail' ? CookingPot : type === 'food_draft_list' ? ClipboardList : Refrigerator;
  const destination = type === 'ingredient_history' ? 'ingredients' : type === 'recipe_detail' ? 'recipes' : type === 'knowledge_result' ? null : 'foods';
  const recipe = type === 'recipe_detail' ? recipeView(data) : null;
  const empty: Record<string, string> = { ingredient_history: '暂未查到配料核对记录。', food_inventory: '当前条件下没有查到食材。', reminder_status: '暂未查到提醒任务。', food_draft_list: '暂未查到食材草稿。', knowledge_result: '没有查到匹配的已发布资料，可换个关键词继续查询。' };
  return <section className={styles.card} aria-label={titles[type]}>
    <h3><Icon size={17}/>{titles[type]}</h3>
    {readText(data.notice) && <p className={styles.note}>{readText(data.notice)}</p>}
    {type !== 'food_detail' && type !== 'recipe_detail' && rows.length === 0 && <p className={styles.empty}>{empty[type]}</p>}
    {type === 'food_inventory' && <div className={styles.list}>{rows.map((row, i) => <FoodRead row={row} key={readText(row.id) || i}/>)}</div>}
    {type === 'food_detail' && (readText(data.id) ? <FoodRead row={data}/> : <p className={styles.empty}>这条食材记录不完整，请重新查询。</p>)}
    {type === 'recipe_detail' && (recipe ? <><h4>{recipe.title}</h4><RecipeDetail recipe={recipe}/></> : <p className={styles.empty}>暂时无法读取这份菜谱，请重新查询。</p>)}
    {type === 'ingredient_history' && <div className={styles.list}>{rows.map((row, i) => {
      const results = readObject(row.results);
      const verdict = (value: unknown) => value === 'suitable' ? '按所示配料符合' : value === 'not_suitable' ? '不符合' : '待核实';
      return <article key={readText(row.history_id) || i} className={styles.item}>
        <p className={styles.meta}>{readDate(row.created) || '时间未提供'}</p>
        <p className={styles.body}>{readText(row.text) || '未提供配料文字'}</p>
        <div className={styles.verdicts}><span>纯净素 · {verdict(results.pure)}</span><span>Vegan · {verdict(results.vegan)}</span><span>蛋奶素 · {verdict(results.lacto_ovo)}</span></div>
        <p className={styles.meta}>五辛：{row.pungent === 'detected' ? '本次检出' : row.pungent === 'not_detected' ? '本次未检出' : '待核实'}</p>
        <p className={styles.meta}>{row.saved_original === true ? '本次识别原图已保存，可到看配料查看。' : row.saved_original === false ? '本次识别未保存原图。' : '原图保存状态未提供。'}</p>
      </article>;
    })}</div>}
    {type === 'reminder_status' && <><p className={styles.notice}>已设置提醒与设备收到通知是不同状态；“推送服务已接受”仍不代表设备已送达。</p><div className={styles.list}>{rows.map((row, i) => <article key={readText(row.task_id) || i} className={styles.item}>
      <div className={styles.heading}><h4>{readText(row.food_name) || '关联食材名称未提供'}</h4><span className={styles.badge}>{reminderStatusNames[readText(row.status)] || '状态待核对'}</span></div>
      <p>提醒时间：{readDate(row.due) || '未提供'}</p>
      {readText(readObject(row.result).message) && <p className={styles.note}>{readText(readObject(row.result).message)}</p>}
    </article>)}</div></>}
    {type === 'food_draft_list' && <DraftReadList rows={rows} disabled={disabled}/>}
    {type === 'knowledge_result' && <div className={styles.list}>{rows.map((row, i) => {
      const sourceText = readText(row.source), source = safeExternalUrl(sourceText), conditions = conditionLines(row.conditions);
      return <article key={readText(row.entry_id) || i} className={styles.item}>
        <div className={styles.heading}><h4>{readText(row.title) || '标题未提供'}</h4><span className={styles.badge}>{knowledgeTypeNames[readText(row.type)] || '知识资料'}</span></div>
        {readText(row.excerpt) && <p className={styles.body}>{readText(row.excerpt)}</p>}
        {conditions.length > 0 && <ul className={styles.conditions} aria-label="资料适用条件">{conditions.map((condition, index) => <li key={index}>{condition}</li>)}</ul>}
        {source ? <a className={styles.source} href={source} target="_blank" rel="noopener noreferrer">查看来源原文<ExternalLink size={12}/></a> : <p className={styles.note}>{sourceText ? `来源：${sourceText}` : '未提供可打开的来源链接。'}</p>}
      </article>;
    })}</div>}
    {disabled && type === 'food_draft_list' && <p className={styles.notice}>本轮对话未完整结束。草稿仍需到记食材读取并核对，不能据此认为已保存。</p>}
    {onNavigate && destination && <button type="button" className="text-button" onClick={() => onNavigate(destination)}>到{destination === 'ingredients' ? '看配料' : destination === 'recipes' ? '选菜谱' : '记食材'}查看最新记录<ChevronRight size={13}/></button>}
  </section>;
}
