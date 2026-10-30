import { foodAdviceView, foodReferenceView, foodUsageView, type FoodSuggestion } from '../lib/food-advice';
import { safeExternalUrl } from '../lib/api';
import styles from './food-advice.module.css';

export default function FoodAdvice({ suggestion, stale = false }: { suggestion?: FoodSuggestion | null; stale?: boolean }) {
  const view = foodAdviceView(suggestion);
  const usage = foodUsageView(suggestion);
  const source = safeExternalUrl(view.source);
  const reference = foodReferenceView(suggestion);
  const referenceSource = safeExternalUrl(reference?.source);
  if (suggestion?.reason === 'missing_info' && !view.date && !view.warning && !reference && !stale) {
    return <section className={styles.advice} aria-label="食用与储存建议"><strong>暂未确定食用日期</strong><p>需要建议时，可以和 Agent 聊聊实际情况。</p></section>;
  }
  return <section className={`${styles.advice} ${view.warning ? styles.warning : ''}`} aria-label="食用与储存建议">
    <strong>{stale ? '修改尚未核对，以下仍是上次建议' : view.date ? view.title : usage?.title || view.title}</strong>
    {usage && <>
      {view.date && <p>{usage.title}</p>}
      <p>{usage.text}</p>
      <p>按已记录情况：{usage.facts.join(' · ')}</p>
    </>}
    {view.date && <p>参考日期：<time dateTime={view.date}>{new Date(view.date).toLocaleString('zh-CN')}</time></p>}
    <p>{view.text}</p>
    {suggestion?.basis?.note && <p>依据说明：{suggestion.basis.note}</p>}
    {source && <a href={source} target="_blank" rel="noreferrer">查看规则来源</a>}
    {suggestion?.basis?.rule_version && <small>规则版本：{suggestion.basis.rule_version}</small>}
    {reference && <div className={styles.reference} aria-label="食用安排参考">
      <strong>{reference.title}</strong>
      <p>{reference.text}</p>
      <small>按所列条件对照，不是安全期限。</small>
      {!!reference.conditions.length && <details><summary>适用条件</summary><ul>{reference.conditions.map((condition, index) => <li key={index}>{condition}</li>)}</ul></details>}
      {referenceSource ? <a href={referenceSource} target="_blank" rel="noreferrer">来源：{reference.sourceTitle || '查看参考资料'}</a>
        : reference.sourceTitle && <small>来源：{reference.sourceTitle}</small>}
    </div>}
  </section>;
}
