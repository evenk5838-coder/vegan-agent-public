'use client';
import { useEffect, useId, useRef } from 'react';
import { MessageCircle, X } from 'lucide-react';
import type { Food } from '../lib/food-progress';
import { guidanceContext } from '../lib/storage-guidance';
import StorageGuidance from './StorageGuidance';
import styles from './storage-guidance.module.css';
export default function StorageGuidanceDialog({ food, onClose, onTalk, onUpdated }: { food: Food; onClose: () => void; onTalk?: (prompt: string) => void; onUpdated?: () => Promise<void> }) {
  const dialog = useRef<HTMLDialogElement>(null), title = useId();
  useEffect(() => { const node = dialog.current; if (node && !node.open) node.showModal(); }, []);
  return <dialog ref={dialog} className={styles.dialog} aria-labelledby={title} onClose={onClose}>
    <div className={styles.dialogHeader}><h2 id={title}>{food.name}</h2><button type="button" className="icon-button" aria-label="关闭储存建议" onClick={()=>dialog.current?.close()}><X size={20}/></button></div>
    <div className={styles.dialogContent}><StorageGuidance foodId={food.id} contextKey={guidanceContext(food)} onUpdated={onUpdated}/>
      {onTalk && <button type="button" className={`secondary ${styles.talk}`} onClick={() => onTalk(`帮我核对冰箱里“${food.name}”的储存情况和食用安排。`)}><MessageCircle size={16}/>和 Agent 聊这份食材</button>}
    </div>
  </dialog>;
}
