'use client';

import { useEffect, useState } from 'react';
import { restaurantDietLabels } from '../lib/restaurant-diets';
import styles from './restaurant-diet-labels.module.css';

export default function RestaurantDietLabels({ value, compact = false }: { value: unknown; compact?: boolean }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    // A history card left open overnight must not keep showing expired claims.
    const refresh = () => setNow(new Date());
    const timer = window.setInterval(refresh, 60000);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, []);
  const { labels, hasEvidence } = restaurantDietLabels(value, now);
  if (compact) {
    return <span className={styles.compact}>{hasEvidence
      ? labels.filter(label => label.status !== 'unknown').map(label => `${label.label} · ${label.statusText}`).join(' / ')
      : '饮食分类待确认'}</span>;
  }
  return <div className={styles.root}>
    <div className={styles.tags} aria-label="菜单适配情况">{labels.map(label =>
      <span key={label.key} className={`${styles.tag} ${styles[label.status]}`}><strong>{label.label}</strong><span>{label.statusText}</span></span>
    )}</div>
    <details className={styles.details}>
      <summary>查看分类依据</summary>
      <p className={styles.notice}>按本店菜单资料判断，出发前请与店家确认。待确认不代表不适合；这些标签不是整店认证。</p>
      {labels.map(label => <div className={styles.claim} key={label.key}>
        <p><strong>{label.label}</strong> · {label.basis}</p>
        {label.sources.map((source, index) => <p className={styles.source} key={`${source.href}-${index}`}>
          <a href={source.href} target="_blank" rel="noopener noreferrer">{source.title} ↗</a>
          <span>核对 {source.reviewedAt} · {source.expiresAt} 起需重新确认{!source.current ? '（当前待复核）' : ''}</span>
        </p>)}
      </div>)}
    </details>
  </div>;
}
