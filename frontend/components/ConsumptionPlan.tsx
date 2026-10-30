'use client';
import { useEffect, useState } from 'react';
import { consumptionPlanView, planDate, type ConsumptionPlan as Plan, type ConsumptionPlanView } from '../lib/consumption-plan';
import styles from './consumption-plan.module.css';

// Phrasing content also fits the inventory's expandable button, without nested controls.
export function PlanSummary({ view, name = '食材' }: { view: ConsumptionPlanView; name?: string }) {
  return <span className={`${styles.summary} ${view.due ? styles.due : ''}`}>
    {view.conservative && <span className={styles.badge}>保守安排 · {view.due ? '请先核对当前情况' : '下一餐优先'}</span>}
    <span className={styles.label}>{view.label}</span>
    {view.target && <span className={styles.date}>{view.conservative ? '安排目标 ' : '建议 '}<time dateTime={view.target}>{planDate(view.target)}</time>{view.conservative ? ' · 非保质期' : ' 前食用'}</span>}
    {view.percent !== null && <progress className={styles.progress} value={view.percent} max={100} aria-label={`${name}建议食用时间已过比例`}/>}
  </span>;
}
export function PlanDetails({ view }: { view: ConsumptionPlanView }) {
  if (!view.target) return null;
  const origin = ({ purchase: '购买', prepared: '制作 / 切配', opened: '开封', production: '生产' } as Record<string, string>)[view.dateOrigin || ''] || '记录';
  return <div>
    {view.reminders.length > 0 && <div className={styles.reminders} aria-label="关键提醒"><strong>关键提醒</strong>{view.reminders.map((text, i) => <p key={i}>{text}</p>)}</div>}
    <details className={styles.details}><summary>食用时间依据</summary>
    <p>按{origin}日期 {planDate(view.start!, true)} 起算。仅提供食用安排参考，不代表实际新鲜度。</p>
    {view.conservative && <p>{view.message || '购买或制作当天优先安排下一餐；来源未给出保存天数，这个目标不是保质期。'}</p>}
    {view.quote && <blockquote>{view.quote}</blockquote>}
    {view.source && <a href={view.source.url} target="_blank" rel="noopener noreferrer">{view.source.title} ↗</a>}
  </details></div>;
}
export default function ConsumptionPlan({ plan, name, stale = false }: { plan?: Plan | null; name?: string; stale?: boolean }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 60000); return () => clearInterval(timer); }, []);
  const view = consumptionPlanView(stale && plan ? { ...plan, status: 'outdated' } : plan, now);
  return view ? <div className={styles.plan}><PlanSummary view={view} name={name}/><PlanDetails view={view}/></div> : null;
}
