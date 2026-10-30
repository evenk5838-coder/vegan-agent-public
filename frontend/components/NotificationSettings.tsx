'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Bell, LoaderCircle } from 'lucide-react';
import { api } from '../lib/api';
import { notificationLevels, notificationTaskState, pushApplicationKey, samePushKey, type NotificationTask } from '../lib/notifications';
import styles from './notification-settings.module.css';

type Config = { public_key: string; notice?: string };
type SubscriptionState = 'unknown' | 'browser_only' | 'saved' | 'closed';

function formatDue(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '提醒时间待核对' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

async function activeWorker(registration: ServiceWorkerRegistration): Promise<void> {
  if (registration.active) return;
  await new Promise<void>((resolve, reject) => {
    const worker = registration.installing || registration.waiting;
    const timeout = setTimeout(() => { cleanup(); reject(new Error('设备通知初始化较慢，请稍后重试；没有重复创建提醒任务。')); }, 10000);
    const changed = () => {
      if (registration.active || worker?.state === 'activated') { cleanup(); resolve(); }
      else if (worker?.state === 'redundant') { cleanup(); reject(new Error('通知初始化未完成，请刷新后重试。')); }
    };
    function cleanup() { clearTimeout(timeout); worker?.removeEventListener('statechange', changed); }
    worker?.addEventListener('statechange', changed);
    changed();
  });
}

/** Mounting only reads status. Permission/subscription changes require an explicit button click. */
export default function NotificationSettings() {
  const [config, setConfig] = useState<Config | null>(null);
  const [tasks, setTasks] = useState<NotificationTask[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [subscription, setSubscription] = useState<SubscriptionState>('unknown');
  const [permission, setPermission] = useState<string>('尚未读取');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const alive = useRef(true);
  const actionRunning = useRef(false);
  const readController = useRef<AbortController | null>(null);
  const writeController = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    const ac = new AbortController(); readController.current?.abort(); readController.current = ac;
    setLoading(true); setError('');
    const results = await Promise.allSettled([
      api<Config>('/notifications/config', { signal: AbortSignal.any([ac.signal, AbortSignal.timeout(15000)]) }),
      api<NotificationTask[]>('/notifications/tasks', { signal: AbortSignal.any([ac.signal, AbortSignal.timeout(15000)]) }),
    ]);
    if (ac.signal.aborted || !alive.current) return;
    if (results[0].status === 'fulfilled') setConfig(results[0].value);
    if (results[1].status === 'fulfilled') setTasks(results[1].value);
    const failure = results.find(result => result.status === 'rejected');
    if (failure?.status === 'rejected') setError(failure.reason instanceof Error ? failure.reason.message : '读取通知状态失败，请刷新重试。');
    setPermission('Notification' in window ? Notification.permission : '此浏览器不支持通知');
    try {
      const registration = 'serviceWorker' in navigator ? await navigator.serviceWorker.getRegistration('/sw.js') : undefined;
      const existing = registration && 'pushManager' in registration ? await registration.pushManager.getSubscription() : null;
      if (alive.current && !ac.signal.aborted) setSubscription(previous => previous === 'saved' && existing ? 'saved' : existing ? 'browser_only' : 'unknown');
    } catch { /* Status inspection cannot establish whether the server has the device subscription. */ }
    if (alive.current && !ac.signal.aborted) setLoading(false);
  }, []);

  useEffect(() => {
    let disposed = false;
    alive.current = true;
    void Promise.resolve().then(() => { if (!disposed) void refresh(); });
    return () => { disposed = true; alive.current = false; readController.current?.abort(); writeController.current?.abort(); };
  }, [refresh]);

  async function enable() {
    if (actionRunning.current) return;
    setError(''); setNotice('');
    if (!config?.public_key) { setError('主动通知服务尚未配置，请先使用应用内的食材提醒列表。'); return; }
    if (!window.isSecureContext) { setError('设备通知需要可信 HTTPS 或 localhost。手机请通过可信 HTTPS 打开。'); return; }
    if (!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)) {
      setError('此环境暂不支持网页推送。iPhone 请添加到主屏幕后打开；仍可在应用内查看提醒。'); return;
    }
    if (Notification.permission === 'denied') { setPermission('denied'); setError('浏览器已拒绝通知，请在系统或浏览器设置中更改权限。应用内提醒仍可使用。'); return; }
    let applicationKey: Uint8Array<ArrayBuffer>;
    try { applicationKey = pushApplicationKey(config.public_key); }
    catch (e) { setError(e instanceof Error ? e.message : '通知配置无法读取。'); return; }
    actionRunning.current = true; setBusy(true);
    const ac = new AbortController(); writeController.current = ac;
    try {
      // Call inside the user gesture, before awaiting registration/config requests (Safari).
      const granted = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
      if (!alive.current || ac.signal.aborted) return;
      setPermission(granted);
      if (granted !== 'granted') throw new Error('未获得通知权限；可以稍后重试，或继续在应用内查看提醒。');
      const registration = await navigator.serviceWorker.register('/sw.js');
      await activeWorker(registration);
      if (!alive.current || ac.signal.aborted) return;
      let device = await registration.pushManager.getSubscription();
      if (device && !samePushKey(device.options.applicationServerKey, applicationKey)) {
        throw new Error('此浏览器的订阅使用了旧通知配置。请先关闭此账号所有设备通知，再重新启用。');
      }
      if (!device) device = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: applicationKey });
      if (!alive.current || ac.signal.aborted) return;
      setSubscription('browser_only');
      await api<{ ok: boolean }>('/notifications/subscriptions', { method: 'POST', body: JSON.stringify(device.toJSON()), signal: AbortSignal.any([ac.signal, AbortSignal.timeout(20000)]) });
      if (!alive.current || ac.signal.aborted) return;
      setSubscription('saved'); setNotice('此设备订阅已保存到账号。提醒到时会尝试推送，服务接受和手机显示需分别核对。');
      await refresh();
    } catch (e) {
      if (alive.current && !ac.signal.aborted) setError(e instanceof Error ? e.message : '通知订阅未完成，请重试。');
    } finally {
      actionRunning.current = false;
      if (alive.current) setBusy(false);
    }
  }

  async function disable() {
    if (actionRunning.current) return;
    actionRunning.current = true; setBusy(true); setError(''); setNotice('');
    const ac = new AbortController(); writeController.current = ac;
    try {
      // This API removes every subscription for the account, so the button says so explicitly.
      await api('/notifications/subscriptions', { method: 'DELETE', signal: AbortSignal.any([ac.signal, AbortSignal.timeout(20000)]) });
      if (!alive.current || ac.signal.aborted) return;
      setSubscription('closed');
      setNotice('此账号所有设备的主动通知订阅已关闭，食材及应用内提醒仍保留。');
      if ('serviceWorker' in navigator) {
        const registration = await navigator.serviceWorker.getRegistration('/sw.js');
        const device = registration && 'pushManager' in registration ? await registration.pushManager.getSubscription() : null;
        if (device && !await device.unsubscribe()) throw new Error('账号订阅已关闭；此浏览器的本地订阅未清理，可稍后重试关闭。');
      }
      await refresh();
    } catch (e) {
      if (alive.current && !ac.signal.aborted) setError(e instanceof Error ? e.message : '关闭通知未完成，请重试。');
    } finally {
      actionRunning.current = false;
      if (alive.current) setBusy(false);
    }
  }

  const levels = notificationLevels(tasks, subscription === 'saved');
  return <section className={'panel space-top ' + styles.panel} aria-label="主动提醒设置">
    <div className="row-between"><h3><Bell size={18}/> 主动提醒</h3><button type="button" className="text-button" disabled={loading || busy} onClick={() => void refresh()}>刷新状态</button></div>
    <p className="small muted">iPhone 需添加到主屏幕、允许通知，并使用可信 HTTPS。实际显示受网络与系统设置影响。</p>
    {loading && <p className="small muted" role="status"><LoaderCircle size={14} className="spin"/>正在读取通知状态…</p>}
    {config && !config.public_key && <p className="small muted">主动通知服务尚未配置，应用内的食材和提醒列表仍可使用。</p>}
    <p className="small muted">浏览器权限 · {({ granted: '已允许', denied: '已拒绝', default: '等待你授权' } as Record<string, string>)[permission] || permission}{subscription === 'browser_only' && '；已有浏览器订阅，账号保存状态待确认'}{subscription === 'closed' && '；账号通知订阅已关闭'}</p>
    <div className="button-row"><button type="button" className="primary" disabled={busy || loading || !config?.public_key} onClick={() => void enable()}>{busy ? '正在处理…' : subscription === 'saved' ? '重新保存此设备订阅' : '启用此设备通知'}</button><button type="button" className="secondary" disabled={busy} onClick={() => void disable()}>关闭此账号所有设备通知</button></div>
    {notice && <p className="small" role="status">{notice}</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {levels && <ol className={styles.levels} aria-label="提醒的四级状态">{levels.map((level, index) => <li key={level.title}><strong>{index + 1}. {level.title}</strong><span>{level.detail}</span></li>)}</ol>}
    {tasks && !tasks.length && <p className="small muted">还没有提醒任务。设置食材的提醒时间后，再来这里核对状态。</p>}
    {!!tasks?.length && <details><summary>查看最近提醒任务（{tasks.length}）</summary><div className={styles.tasks}>{tasks.map(task => {
      const state = notificationTaskState(task);
      return <article className={styles.task} key={task.id}><strong>{formatDue(task.due)}</strong><span>{state.label}</span><small>设备显示 · {state.deviceDisplay}</small>{state.error && <p className="small muted">{state.error}</p>}</article>;
    })}</div></details>}
  </section>;
}
