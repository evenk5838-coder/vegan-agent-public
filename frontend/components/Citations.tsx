import { ExternalLink, BookOpen } from 'lucide-react';
import { citationViews } from '../lib/chat-read';
import styles from './chat-results.module.css';

/** Only render the server-verified citations supplied with the assistant message. */
export default function Citations({ items }: { items: unknown }) {
  const rows = citationViews(items);
  if (!rows.length) return null;
  return <aside className={styles.citations} aria-label="本次回答引用的知识来源">
    <span className={styles.meta}><BookOpen size={13}/>本次回答参考</span>
    <ol>{rows.map(row => <li key={row.entryId}>{row.href
      ? <a href={row.href} target="_blank" rel="noopener noreferrer">{row.title}<ExternalLink size={12}/></a>
      : <span>{row.title}{row.source ? ` · ${row.source}` : ' · 未提供可打开的来源链接'}</span>}</li>)}</ol>
  </aside>;
}
