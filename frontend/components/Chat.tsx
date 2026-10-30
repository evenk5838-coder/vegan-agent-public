'use client';

import { useEffect, useRef, useState } from 'react';
import Image from 'next/image';
import { ArrowDown, ArrowUp, ChevronRight, CookingPot, History, Leaf, LoaderCircle, LocateFixed, MapPin, Plus, Refrigerator, RefreshCw, ScanLine, Trash2, X } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { prepareChatAttempt, type ChatAttempt } from '../lib/chat-submission';
import { messageLocation, needsRestaurantLocation, regionalRestaurantRequest, visibleChatText, withCurrentLocation } from '../lib/chat-location';
import { requestCurrentMapLocation } from '../lib/amap-sdk';
import ChatPhotoInput, { type PhotoAttachment } from './ChatPhotoInput';
import { restaurantData } from '../lib/restaurant-results';
import VoiceInput from './VoiceInput';
import ChatResults from './ChatResults';
import { requestedRestaurantMap, type FeatureResult } from '../lib/chat-presentation';
import styles from './chat.module.css';

type Session = { id: string; title: string; updated: string; created: string; message_count: number };
type Card = { id: string; type: string; status: string; payload: Record<string, unknown> };
type RestaurantGuard = {
  status?: 'ok' | 'missing_area' | 'not_queried' | 'empty' | 'failed' | 'not_restaurant';
  error_code?: string | null; has_restaurant_result?: boolean; allowed_place_ids?: string[]; allowed_place_names?: string[];
  source?: string | null; recommendation_source?: 'server' | 'model'; restaurant_turn?: boolean;
  restaurant_attempted?: boolean; missing_area?: boolean; model_text_suppressed?: boolean; clarification_added?: boolean;
};
type Citation = { entry_id: string; title: string | null; source: string | null };
type ReplyEvidence = { citations?: Citation[]; restaurant_guard?: RestaurantGuard | null; places?: unknown[] };
type Message = ReplyEvidence & { id: string; role: string; text: string; cards: Card[] | null; turn_id: string | null; status: string; needs_input: boolean; input_prompt: string; attachments?: {kind: string; job_id: string}[] };
type Turn = { id: string; status: string; reply: (ReplyEvidence & { text: string; cards: Card[]; needs_input: boolean; input_prompt: string }) | null; cards: Card[]; error: { code: string; message: string } | null };
type Capabilities = { chat_configured: boolean; external_calls_enabled: boolean; transcription_configured: boolean };
type Destination = 'ingredients' | 'restaurants' | 'recipes' | 'foods';
const destinations = [
  { id: 'ingredients', title: '看配料', detail: '拍下配料，核对饮食适配', icon: ScanLine },
  { id: 'restaurants', title: '找餐厅', detail: '为下一顿找一个好去处', icon: MapPin },
  { id: 'recipes', title: '选菜谱', detail: '看看今天可以做什么', icon: CookingPot },
  { id: 'foods', title: '记食材', detail: '核对库存、储存与提醒', icon: Refrigerator },
] as const;
const prompts = [{text:'找附近蔬食', value:'想找附近的素食餐厅', icon: MapPin}, {text:'今天做什么', value:'推荐一道含豆类的蔬食菜谱', icon: CookingPot}];
const isWorking = (status: string) => status === 'queued' || status === 'running';
const errorText = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试。';

/** Guidance only changes input focus. It never manufactures a place or dispatches a query. */
export function RestaurantGuidance({ guard, onContinue, disabled = false }: { guard?: RestaurantGuard | null; onContinue: () => void; disabled?: boolean }) {
  if (!guard?.status || guard.status === 'not_restaurant') return null;
  const hints = {
    ok: { note: '店铺详情以本轮结果卡片为准，具体配料与饮食要求请向店家核对。', button: '' },
    missing_area: { note: '补充城市或区县后，由你发送消息继续查找。', button: '补充城市或区县' },
    not_queried: { note: '本轮尚未实际查询。可在输入框明确要查的区域，再发送查询请求。', button: '补充查询请求' },
    empty: { note: '可换个区域继续查找；当前范围无结果不代表当地没有素食餐厅。', button: '换个查询区域' },
    failed: { note: ['NOT_CONFIGURED', 'EXTERNAL_DISABLED', 'QUOTA_EXCEEDED', 'GLOBAL_QUOTA_EXCEEDED', 'UPSTREAM_RATE_LIMITED'].includes(guard.error_code || '')
      ? '请按上方服务状态稍后再试，也可以继续其他蔬食问题。'
      : '可检查区域或位置后，再发送查询请求；本次不会自动重试。', button: '回到输入框' },
  };
  const hint = hints[guard.status];
  if (!hint) return null;
  return <aside className={styles.followup} aria-label="餐厅查询下一步">
    <p>{hint.note}</p>
    {hint.button && <button type="button" className="text-button" disabled={disabled} onClick={onContinue}>{hint.button}<ChevronRight size={13}/></button>}
  </aside>;
}

export function historicalRestaurantReply(message: Pick<Message, 'cards' | 'restaurant_guard'>) {
  const restaurantCards = message.cards?.filter(card => card.type === 'restaurant_list') || [];
  return restaurantCards.some(card => !restaurantData(card.payload || {}).scopeCurrent)
    || ((restaurantCards.length > 0 || message.restaurant_guard?.restaurant_turn === true)
      && message.restaurant_guard?.recommendation_source !== 'server');
}

type ChatProps = { onNavigate: (page: string, result?: FeatureResult) => void; username: string; initialText?: string; resultsVersion?: number };

export default function Chat(props: ChatProps) {
  return <ChatConversation key={props.username} {...props}/>;
}

function ChatConversation({ onNavigate, username, initialText = '', resultsVersion = 0 }: ChatProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [turns, setTurns] = useState<Record<string, Turn>>({});
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [text, setText] = useState(initialText);
  const [photo, setPhoto] = useState<PhotoAttachment | null>(null);
  const [uploading, setUploading] = useState(false);
  const [voiceMode, setVoiceMode] = useState<'keyboard' | 'voice'>('keyboard');
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [locating, setLocating] = useState(false);
  const [locationQuestion, setLocationQuestion] = useState<string | null>(null);
  const [turnLocations, setTurnLocations] = useState<Record<string, string>>({});
  const [composerVersion, setComposerVersion] = useState(0);
  const [revision, setRevision] = useState(0);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const historyDialog = useRef<HTMLDialogElement>(null);
  const messageViewport = useRef<HTMLDivElement>(null);
  const messageContent = useRef<HTMLDivElement>(null);
  const followBottom = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const sendingRef = useRef(false);
  const selectedRef = useRef<string | null>(null);
  const attempt = useRef<ChatAttempt | null>(null);
  const locationVersion = useRef(0);
  const locatedSubmission = useRef<{ draft: string; content: string; location: string } | null>(null);
  const turnLocationRef = useRef<Record<string, string>>({});
  const focusedAreaPrompt = useRef('');
  const submittedTurns = useRef(new Set<string>());
  const navigateRef = useRef(onNavigate);
  useEffect(() => { navigateRef.current = onNavigate; }, [onNavigate]);
  const mounted = useRef(true);
  const ready = capabilities?.chat_configured && capabilities.external_calls_enabled;
  const active = messages.some(m => isWorking(m.status));
  const last = messages.at(-1);
  const waiting = last?.role === 'assistant' && last.needs_input ? last.input_prompt : '';
  const areaPromptId = last?.role === 'assistant' && last.restaurant_guard?.status === 'missing_area' ? last.id : null;

  function choose(id: string | null) {
    if (sendingRef.current || uploading || locating || voiceBusy) return;
    if (id && id === selected) { setHistoryOpen(false); return; }
    selectedRef.current = id;
    submittedTurns.current.clear();
    followBottom.current = true; setShowJump(false);
    setSelected(id); setMessages([]); setTurns({}); setLoading(!!id); setError(''); setText('');
    setLocationQuestion(null); locatedSubmission.current = null;
    setTurnLocations({}); turnLocationRef.current = {};
    setHistoryOpen(false); attempt.current = null; setPhoto(null); setComposerVersion(n => n + 1); locationVersion.current++;
    const url = new URL(window.location.href);
    if (id) url.searchParams.set('chat', id); else url.searchParams.delete('chat');
    window.history.replaceState(null, '', url);
  }

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; locationVersion.current++; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      api<{ items: Session[] }>('/chat/sessions', { signal: controller.signal }),
      api<Capabilities>('/capabilities', { signal: controller.signal }),
    ]).then(([list, caps]) => {
      setSessions(list.items); setCapabilities(caps);
      const id = new URL(window.location.href).searchParams.get('chat');
      if (id && list.items.some(s => s.id === id)) { selectedRef.current = id; setSelected(id); }
      else setLoading(false);
    }).catch(e => { if (!controller.signal.aborted) { setError(errorText(e)); setLoading(false); } });
    return () => controller.abort();
  }, [username]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    async function sync() {
      try {
        const data = await api<{ items: Message[] }>(`/chat/sessions/${selected}/messages`, { signal: controller.signal });
        const pending = [...new Set(data.items.filter(m => m.turn_id && (isWorking(m.status) || m.status === 'failed' || m.status === 'cancelled')).map(m => m.turn_id!))];
        const values = await Promise.all(pending.map(id => api<Turn>(`/chat/sessions/${selected}/turns/${id}`, { signal: controller.signal })));
        if (controller.signal.aborted) return;
        setMessages(data.items); setTurns(Object.fromEntries(values.map(v => [v.id, v]))); setLoading(false); setError(''); failures = 0;
        for (const message of data.items) {
          const destination = requestedRestaurantMap(message, submittedTurns.current);
          if (message.role === 'assistant' && message.turn_id && !isWorking(message.status)) submittedTurns.current.delete(message.turn_id);
          if (destination) navigateRef.current(destination.page, { ...destination, currentLocation: message.turn_id ? turnLocationRef.current[message.turn_id] : undefined });
        }
        if (data.items.some(m => isWorking(m.status))) timer = setTimeout(sync, document.hidden ? 12000 : 1800);
      } catch (e) {
        if (controller.signal.aborted) return;
        setLoading(false); setError(errorText(e));
        if (!(e instanceof ApiError && [401, 404].includes(e.status)) && ++failures < 4) timer = setTimeout(sync, 2000 * 2 ** failures);
      }
    }
    void sync();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [selected, revision]);

  useEffect(() => {
    const dialog = historyDialog.current;
    if (historyOpen && dialog && !dialog.open) dialog.showModal();
    if (!historyOpen && dialog?.open) dialog.close();
  }, [historyOpen]);

  useEffect(() => {
    const viewport = window.visualViewport;
    function resize() {
      if (window.innerWidth < 768 && viewport) document.documentElement.style.setProperty('--app-height', `${viewport.height}px`);
      else document.documentElement.style.removeProperty('--app-height');
    }
    resize(); viewport?.addEventListener('resize', resize); window.addEventListener('resize', resize);
    return () => { viewport?.removeEventListener('resize', resize); window.removeEventListener('resize', resize); document.documentElement.style.removeProperty('--app-height'); };
  }, []);

  useEffect(() => {
    const field = textarea.current;
    if (!field) return;
    field.style.height = 'auto';
    field.style.height = `${Math.min(field.scrollHeight, 112)}px`;
  }, [text, voiceMode]);

  useEffect(() => {
    if (voiceMode === 'keyboard') textarea.current?.focus({ preventScroll: true });
  }, [voiceMode]);

  useEffect(() => {
    if (initialText && !loading) textarea.current?.focus({ preventScroll: true });
  }, [initialText, loading]);

  useEffect(() => {
    if (locationQuestion && !sending) textarea.current?.focus({ preventScroll: true });
  }, [locationQuestion, sending]);

  useEffect(() => {
    if (!areaPromptId || !selected || loading || sending || active || uploading || locating || historyOpen) return;
    const prompt = `${selected}:${areaPromptId}`;
    if (focusedAreaPrompt.current === prompt) return;
    focusedAreaPrompt.current = prompt;
    textarea.current?.focus({ preventScroll: true });
  }, [areaPromptId, selected, loading, sending, active, uploading, locating, historyOpen]);

  useEffect(() => {
    const viewport = messageViewport.current;
    const content = messageContent.current;
    if (!viewport || !content) return;
    function settle() { if (followBottom.current && viewport) viewport.scrollTop = viewport.scrollHeight; }
    const frame = requestAnimationFrame(settle);
    const observer = new ResizeObserver(settle); observer.observe(content);
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [messages, turns, waiting, locationQuestion]);

  async function refresh() {
    setError('');
    try {
      const [list, caps] = await Promise.all([api<{items: Session[]}>('/chat/sessions'), api<Capabilities>('/capabilities')]);
      setSessions(list.items); setCapabilities(caps); setRevision(n => n + 1);
    } catch (e) { setError(errorText(e)); }
  }

  async function send(findNearby = false, spokenText?: string): Promise<boolean> {
    if (voiceBusy && !spokenText) return false;
    const input = spokenText ? [text.trim(), spokenText.trim()].filter(Boolean).join('\n') : text;
    if (input.length > 4000) { setError('输入内容超过4000字，请先缩短文字；这段语音仍可取回。'); return false; }
    // Keep the exact combined voice/keyboard draft for an explicit, idempotent retry.
    if (spokenText) setText(input);
    if ((!input.trim() && !photo && !findNearby) || !ready || sendingRef.current || active || uploading || locating || loading) return false;
    sendingRef.current = true; followBottom.current = true; setShowJump(false); setSending(true); setError('');
    const version = ++locationVersion.current;
    const current = () => mounted.current && version === locationVersion.current;
    const attachmentSnapshot = photo ? [{kind: 'ingredient_photo' as const, job_id: photo.job_id}] : [];
    const draft = input.trim() || (findNearby ? '请查找附近的素食餐厅。' : '请核对这张配料表的三类饮食适配，并说明五辛和判断依据。');
    const restaurantContext = last?.role === 'assistant' && !!last.restaurant_guard?.restaurant_turn;
    let content = locationQuestion && !findNearby
      ? regionalRestaurantRequest(draft, locationQuestion) : draft;
    if (findNearby && !needsRestaurantLocation(content, restaurantContext)) content += '\n也请查找我附近的素食餐厅。';
    let currentLocation = messageLocation(content);
    try {
      if (needsRestaurantLocation(content, restaurantContext) || findNearby) {
        // An uncertain HTTP receipt must replay the same coordinates and
        // idempotency key, rather than acquire another position and charge twice.
        const cached = locatedSubmission.current;
        if (cached?.draft === content) {
          currentLocation = cached.location; content = cached.content;
        } else {
          const locationDraft = content;
          setLocating(true);
          try {
            currentLocation = await requestCurrentMapLocation();
          } catch {
            if (current()) { setLocationQuestion(content); setText(''); textarea.current?.focus(); }
            return false;
          }
          if (!current()) return false;
          content = withCurrentLocation(content, currentLocation);
          locatedSubmission.current = { draft: locationDraft, content, location: currentLocation };
        }
        if (!current()) return false;
        setLocating(false);
      }
      let id = selected;
      if (!id) {
        const created = await api<{ id: string }>('/chat/sessions', { method: 'POST', body: JSON.stringify({ title: draft.slice(0, 28) }) });
        if (!current()) return false;
        id = created.id; selectedRef.current = id; setSelected(id);
        const url = new URL(window.location.href); url.searchParams.set('chat', id); window.history.replaceState(null, '', url);
      }
      attempt.current = prepareChatAttempt(attempt.current, {owner: username, session: id, content, attachments: attachmentSnapshot}, () => crypto.randomUUID());
      const accepted = await api<{turn_id: string | null; status: string}>(`/chat/sessions/${id}/messages`, { method: 'POST', body: JSON.stringify({ content: attempt.current.content, idempotency_key: attempt.current.key, attachments: attempt.current.attachments }) });
      if (!current()) return false;
      if (accepted.turn_id) submittedTurns.current.add(accepted.turn_id);
      // The receipt establishes a real task; block another send while history catches up.
      if (accepted.turn_id && currentLocation) {
        const turnId = accepted.turn_id;
        turnLocationRef.current[turnId] = currentLocation;
        setTurnLocations({...turnLocationRef.current});
      }
      if (accepted.turn_id) setMessages(rows => rows.some(m => m.turn_id === accepted.turn_id) ? rows : [...rows, {
        id: 'receipt-' + accepted.turn_id, role: 'user', text: content, cards: null,
        turn_id: accepted.turn_id, status: accepted.status, needs_input: false, input_prompt: '', attachments: attachmentSnapshot,
      }]);
      setText(''); setPhoto(null); setLocationQuestion(null); locatedSubmission.current = null;
      setComposerVersion(n => n + 1); attempt.current = null;
      setRevision(n => n + 1);
      // History refresh failure must not convert an accepted submission into a failed one.
      void api<{items: Session[]}>('/chat/sessions').then(data => setSessions(data.items)).catch(() => {});
      return true;
    } catch (e) {
      if (!current()) return false;
      setError(errorText(e));
      if (e instanceof ApiError && e.code === 'TURN_ACTIVE') setRevision(n => n + 1);
      return false;
    } finally {
      if (current()) { sendingRef.current = false; setSending(false); setLocating(false); }
    }
  }

  function stopLocation() {
    locationVersion.current += 1;
    sendingRef.current = false; setSending(false); setLocating(false);
    setError('已停止等待定位。也可以直接输入城市或区县，请我查找餐厅。');
    textarea.current?.focus();
  }

  async function remove(id: string) {
    try {
      setError('');
      await api(`/chat/sessions/${id}`, { method: 'DELETE' });
      if (selectedRef.current === id) choose(null);
      setSessions(rows => rows.filter(s => s.id !== id)); setDeleting(null);
    } catch (e) { setError(errorText(e)); }
  }

  return <div className={styles.root}>
    <div className={styles.topline}>
      <span className={styles.dailyLabel}>你的蔬食日常</span>
      <div className={styles.actions}>
        <button type="button" className="text-button" disabled={sending||uploading||locating||voiceBusy} onClick={() => setHistoryOpen(true)} aria-haspopup="dialog"><History size={17}/>记录</button>
        <button type="button" className="text-button" disabled={sending||uploading||locating||voiceBusy} onClick={() => choose(null)}><Plus size={17}/>新对话</button>
      </div>
    </div>
    <nav className={styles.shortcuts} aria-label="四个功能入口">
      {destinations.map(({id,title,icon:Icon}) => <button type="button" key={id} className={styles.shortcut} disabled={voiceBusy} onClick={() => onNavigate(id as Destination)}><Icon size={23} strokeWidth={1.5}/><span>{title}</span></button>)}
    </nav>
    <dialog ref={historyDialog} className={styles.history} aria-labelledby="chat-history-title" onClose={() => setHistoryOpen(false)} onClick={e => {if(e.target === e.currentTarget) setHistoryOpen(false);}}>
      <div className="row-between"><h2 id="chat-history-title">最近的对话</h2><button type="button" className="icon-button" aria-label="关闭对话记录" onClick={() => setHistoryOpen(false)}><X size={20}/></button></div>
      <p className="small muted">文字与结果卡片保留 90 天，在手机和电脑上接着聊。</p>
      <button type="button" className="text-button" onClick={() => void refresh()}><RefreshCw size={15}/>刷新记录</button>
      <div className={styles.historyList}>{sessions.length ? sessions.map(s => <div key={s.id} className={styles.historyRow}>
        <button type="button" className={styles.historyTitle} disabled={sending} onClick={() => choose(s.id)}>{s.title || '新对话'}<small>{s.message_count} 条消息</small></button>
        {deleting === s.id ? <div className={styles.deleteConfirm}><span className="small">删除这段对话？</span><button type="button" className="text-button danger" onClick={() => void remove(s.id)}>确认删除</button><button type="button" className="text-button" onClick={() => setDeleting(null)}>保留</button></div> : <button type="button" className="icon-button" aria-label={`删除对话 ${s.title}`} disabled={sending} onClick={() => setDeleting(s.id)}><Trash2 size={17}/></button>}
      </div>) : <p className="empty">还没有对话，从今天的一餐开始。</p>}</div>
    </dialog>
    <div className={styles.conversation}>
      <div ref={messageViewport} className={styles.messages} aria-label="对话内容" tabIndex={0} onScroll={e => {
        const el=e.currentTarget; const near=el.scrollHeight-el.scrollTop-el.clientHeight < 72;
        followBottom.current=near; setShowJump(!near);
      }}>
        <div ref={messageContent} className={`${styles.messageList} ${!messages.length ? styles.emptyList : ''}`}>
          {loading && <p role="status" className={styles.connection}><LoaderCircle size={16} className="spin"/>正在读取你的对话…</p>}
          {!loading && !messages.length && !locationQuestion && <section className={styles.hero} aria-label="欢迎使用蔬食">
            <div className={styles.heroCopy}><span className={styles.heroEyebrow}>A LITTLE GREENER, EVERY DAY</span><h1>今天，想怎么吃？</h1><p>从一餐开始，照顾自己。</p></div>
            <Image className={styles.botanical} src="/botanical-branch.svg" width={1024} height={1536} alt="" sizes="(max-width: 700px) 80vw, 420px" priority/>
            <div className={styles.prompts}>{prompts.map(({text:label,value,icon:Icon}) => <button type="button" key={label} disabled={sending||uploading||locating||voiceBusy} onClick={() => {setText(value); textarea.current?.focus();}}><Icon size={16}/>{label}<ChevronRight size={13}/></button>)}</div>
          </section>}
          {messages.map(m => <div key={m.id} className={styles.messageGroup}>
            <article className={`${styles.message} ${m.role === 'user' ? styles.user : styles.assistant}`}>
              <span className={styles.speaker}>{m.role === 'user' ? '你' : <><Leaf size={17} strokeWidth={1.6}/>蔬食</>}</span>
              {m.text && (m.role === 'assistant' && historicalRestaurantReply(m)
                ? <details><summary>查看历史回答（餐厅结果需重新核对）</summary><p className="small muted">以下保留当时的回答，不能视为按当前规则核对的推荐。请重新查询餐厅。</p><p className="pre-line">{m.text}</p></details>
                : <p className="pre-line">{m.role === 'user' ? visibleChatText(m.text) : m.text}</p>)}
              {m.role === 'user' && messageLocation(m.text) && <p className="small muted">已使用本次位置</p>}
              {m.attachments?.some(item => item.kind === 'ingredient_photo') && <p className="small muted">已附配料表图片。</p>}
              {m.role === 'assistant' && <ChatResults cards={m.cards || []} citations={m.citations} version={resultsVersion} failed={m.status === 'failed' || m.status === 'cancelled'} onOpen={result => onNavigate(result.page, result)} currentLocation={m.turn_id ? turnLocations[m.turn_id] : undefined}/>}
            </article>
            {m.role === 'user' && m.turn_id && (() => {
              const turn = turns[m.turn_id];
              if (!turn) return null;
              if (isWorking(turn.status)) return <p role="status" className={styles.connection}><LoaderCircle size={15} className="spin"/>{turn.status === 'queued' ? '消息已收到，等待处理…' : '正在处理你的问题…'} 回来后可以接着查看。</p>;
              if (turn.status === 'failed') return <section className={styles.failed}><p role="alert">{turn.error?.message || '这次回答未完成。'}</p><p className="small muted">已完成的结果保留在下方；本次任务已结束，不会自动重新提交。</p><ChatResults cards={turn.cards} citations={turn.reply?.citations} failed onOpen={result => onNavigate(result.page, result)}/></section>;
              if (turn.status === 'cancelled') return <p className="small muted">这次任务已取消。</p>;
              return null;
            })()}
          </div>)}
          {waiting && !last?.text?.trim() && <p className={styles.followup}>{waiting}</p>}
          {locationQuestion && <div className={styles.messageGroup}>
            <article className={`${styles.message} ${styles.user}`}><span className={styles.speaker}>你</span><p className="pre-line">{locationQuestion}</p></article>
            <article className={`${styles.message} ${styles.assistant}`} role="status"><span className={styles.speaker}><Leaf size={17}/>蔬食</span><p>没能取得本次位置。你想查哪个城市或区县？</p><button type="button" className="text-button" disabled={sending} onClick={() => {setText(locationQuestion); setLocationQuestion(null);}}>先不查了</button></article>
          </div>}
        </div>
      </div>
      {showJump && messages.length > 0 && <button type="button" className={styles.jumpButton} onClick={() => {followBottom.current=true;setShowJump(false);messageViewport.current?.scrollTo({top:messageViewport.current.scrollHeight,behavior:'auto'});}}><ArrowDown size={16}/>回到最新</button>}
    </div>
    {locating && <div role="status" className={styles.locationNotice}><LoaderCircle size={15} className="spin"/><span>正在获取本次位置，随后直接查找附近餐厅…</span><button type="button" className="text-button" onClick={stopLocation}>取消</button></div>}
    {error && <div role="alert" className={styles.errorNotice}>{error}<button type="button" className="text-button" onClick={() => void refresh()}><RefreshCw size={14}/>刷新状态</button></div>}
    <form className={styles.composer+' chat-composer'} onSubmit={e => { e.preventDefault(); void send(); }}>
      <VoiceInput key={`${username}:${composerVersion}`} username={username} mode={voiceMode} onModeChange={setVoiceMode} onBusyChange={setVoiceBusy}
        enabled={!!(capabilities?.transcription_configured&&capabilities.external_calls_enabled)} disabled={!ready||sending||active||uploading||locating||loading}
        onSubmit={async value => {
          const tooLong = [text.trim(), value.trim()].filter(Boolean).join('\n').length > 4000;
          const accepted = await send(false, value);
          if (!accepted) setVoiceMode('keyboard');
          return accepted ? 'sent' : tooLong ? 'recover' : 'draft';
        }}
        onRestore={value => {
          const restored = [text.trim(), value.trim()].filter(Boolean).join('\n');
          if (restored.length > 4000) throw new Error('输入框内容较多，请先发送或清空，再取回语音文字。');
          setText(restored); locatedSubmission.current = null;
        }} tools={<>
          <ChatPhotoInput key={`${username}:${composerVersion}`} value={photo} onChange={setPhoto} onBusyChange={setUploading} enabled={!!ready} disabled={sending||active||loading||locating||voiceBusy} compact/>
          <button type="button" className={styles.toolButton} aria-label="定位并查附近餐厅" title="获取本次位置并直接查找附近餐厅" disabled={!ready||sending||active||uploading||locating||loading||voiceBusy} onClick={() => void send(true)}>{locating ? <LoaderCircle size={21} className="spin"/> : <LocateFixed size={21} strokeWidth={1.6}/>}</button>
          {voiceMode === 'keyboard' && <button type="submit" className={styles.sendButton} aria-label="发送消息" title="发送消息" disabled={!ready || (!text.trim()&&!photo) || sending || active || uploading || locating || loading || voiceBusy}>{sending ? <LoaderCircle size={22} className="spin"/> : <ArrowUp size={24} strokeWidth={1.7}/>}</button>}
        </>}>
        <label htmlFor="daily-chat" className={styles.inputLabel}>{areaPromptId || locationQuestion ? '想查哪个城市或区县' : waiting ? '补充信息，接着聊' : '聊聊你的蔬食日常'}</label>
        <textarea ref={textarea} id="daily-chat" value={text} onChange={e => {setText(e.target.value); locatedSubmission.current = null;}} maxLength={4000} rows={1} placeholder={areaPromptId || locationQuestion ? '例如：北京朝阳' : waiting ? '补充信息，接着聊…' : '聊聊你的蔬食日常…'} disabled={sending||voiceBusy}/>
      </VoiceInput>
      {photo && <span className={styles.attachmentBadge}>已附照片</span>}
    </form>
    <div className={styles.serviceNote}><span className={ready ? styles.readyDot : styles.pendingDot}/><span>{capabilities ? ready ? '先核对，再由你确认保存' : '对话暂未启用，四个功能入口仍可使用' : '正在连接你的蔬食日常'}</span>{!ready && <button type="button" className="text-button" onClick={() => void refresh()}>刷新</button>}</div>
  </div>;
}
