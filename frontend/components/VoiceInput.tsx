'use client';

import { useEffect, useRef, useState, type ReactNode, type PointerEvent, type KeyboardEvent } from 'react';
import { Keyboard, LoaderCircle, Mic, X } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { transcriptionForm, voiceKeys, type TranscriptionJob, type TranscriptionLimits } from '../lib/voice-input';
import { pollVoiceJob, VoiceSessionGate } from '../lib/voice-session';
import { isPhoneMicrophone, preferredMicrophone, suspiciousTranscript, VoiceSignalMeter, type AudioInput } from '../lib/voice-capture';
import styles from './voice-input.module.css';

type Props = {
  enabled: boolean; disabled?: boolean; username: string; mode: 'keyboard' | 'voice';
  onModeChange: (mode: 'keyboard' | 'voice') => void;
  onBusyChange: (busy: boolean) => void;
  onSubmit: (text: string) => Promise<'sent' | 'draft' | 'recover'>; onRestore: (text: string) => void;
  children: ReactNode; tools: ReactNode;
};
type Phase = 'idle' | 'requesting' | 'recording' | 'uploading' | 'recognizing' | 'delivering';

export default function VoiceInput(props: Props) {
  const { enabled, disabled, username, mode, onModeChange, onBusyChange, children, tools } = props;
  const [limits, setLimits] = useState<TranscriptionLimits | null>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [seconds, setSeconds] = useState(0);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [recoverJob, setRecoverJob] = useState<string | null>(null);
  const [uncertainUpload, setUncertainUpload] = useState(false);
  const [inputs, setInputs] = useState<AudioInput[]>([]);
  const [deviceId, setDeviceId] = useState('');
  const [actualMicrophone, setActualMicrophone] = useState('');
  const [hasSignal, setHasSignal] = useState(false);
  const explicitDevice = useRef(false);
  const signalMonitor = useRef<{ stop: () => void } | null>(null);
  const gate = useRef(new VoiceSessionGate());
  const callbacks = useRef(props);
  const media = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const controller = useRef<AbortController | null>(null);
  const stopTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const held = useRef<{ pointer: number | null; y: number } | null>(null);
  const cancelSlide = useRef(false);
  const started = useRef(0);
  const locked = useRef(false);
  const keys = voiceKeys(username);
  const busy = phase !== 'idle';
  const allowed = enabled && limits?.configured === true;

  useEffect(() => { callbacks.current = props; });
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);

  function stopMedia() {
    if (stopTimer.current) clearTimeout(stopTimer.current);
    stopTimer.current = null;
    signalMonitor.current?.stop(); signalMonitor.current = null;
    if (recorder.current?.state === 'recording') recorder.current.stop();
    media.current?.getTracks().forEach(track => track.stop());
    media.current = null;
  }
  function cancel(message = '已取消，没有发送。') {
    gate.current.cancel(); controller.current?.abort(); held.current = null; locked.current = false;
    cancelSlide.current = false; stopMedia(); setCancelling(false); setPhase('idle'); setNotice(message);
  }
  function clearSavedJob(id: string, ticket: number) {
    if (gate.current.isCurrent(ticket)) { setRecoverJob(null); setUncertainUpload(false); }
    try {
      // An old component must not erase a newer recording's recovery pointer.
      if (sessionStorage.getItem(keys.job) === id) { sessionStorage.removeItem(keys.job); sessionStorage.removeItem(keys.draft); }
    } catch { /* Memory-only mode. */ }
  }

  useEffect(() => {
    const ac = new AbortController();
    const sessionGate = gate.current;
    void api<TranscriptionLimits>('/capabilities/transcription', { signal: ac.signal }).then(value => {
      if (!ac.signal.aborted) setLimits(value);
    }).catch(() => { if (!ac.signal.aborted) setError('暂时无法连接语音服务，可以先打字。'); });
    void Promise.resolve().then(() => {
      if (ac.signal.aborted) return;
      try { const previous = sessionStorage.getItem(keys.job); if (previous) setRecoverJob(previous); } catch { /* Recovery is optional. */ }
    });
    function interrupt() { if (held.current) cancel('录音已取消，没有发送。'); }
    function visibility() { if (document.hidden) interrupt(); }
    window.addEventListener('blur', interrupt); document.addEventListener('visibilitychange', visibility);
    return () => {
      ac.abort(); sessionGate.cancel(); controller.current?.abort(); held.current = null;
      stopMedia(); onBusyChange(false);
      window.removeEventListener('blur', interrupt); document.removeEventListener('visibilitychange', visibility);
    };
    // The parent remounts on account/conversation changes; restoration never sends a message.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys.job, onBusyChange]);

  useEffect(() => {
    if (phase !== 'recording') return;
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - started.current) / 1000)), 250);
    return () => clearInterval(timer);
  }, [phase]);

  async function refreshMicrophones() {
    if (!navigator.mediaDevices?.enumerateDevices) return [];
    const values = (await navigator.mediaDevices.enumerateDevices()).filter(value => value.kind === 'audioinput' && value.deviceId);
    setInputs(values);
    setDeviceId(previous => {
      if (explicitDevice.current && !values.some(value => value.deviceId === previous)) return '';
      return preferredMicrophone(values, previous)?.deviceId || '';
    });
    return values;
  }

  useEffect(() => {
    if (mode !== 'voice') return;
    let active = true;
    async function update() {
      try {
        const values = (await navigator.mediaDevices?.enumerateDevices?.() || []).filter(value => value.kind === 'audioinput' && value.deviceId);
        if (!active) return;
        setInputs(values);
        setDeviceId(previous => explicitDevice.current && !values.some(value => value.deviceId === previous) ? '' : preferredMicrophone(values, previous)?.deviceId || '');
      } catch { if (active) setError('暂时无法读取麦克风设备，请检查浏览器录音权限。'); }
    }
    void update(); navigator.mediaDevices?.addEventListener?.('devicechange', update);
    return () => { active = false; navigator.mediaDevices?.removeEventListener?.('devicechange', update); };
  }, [mode]);

  async function prepareMicrophone() {
    if (busy || disabled || locked.current) return;
    locked.current = true; const ticket = gate.current.begin(); setPhase('requesting'); setError('');
    try {
      // Only requested by an explicit setup click, before recording. Never uploads.
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach(track => track.stop());
      if (!gate.current.isCurrent(ticket)) return;
      const devices = await refreshMicrophones();
      if (!gate.current.isCurrent(ticket)) return;
      setNotice(preferredMicrophone(devices) ? '已准备好，请按住说话。' : '请选择本次要用的麦克风，再按住说话。');
    } catch { if (gate.current.isCurrent(ticket)) setError('未获得麦克风权限，请在浏览器中允许录音。'); }
    finally { if (gate.current.isCurrent(ticket)) { locked.current = false; setPhase('idle'); } }
  }

  async function receive(id: string, ticket: number, ac: AbortController, autoSend: boolean) {
    setPhase('recognizing');
    const result = await pollVoiceJob(signal => api<TranscriptionJob>('/transcriptions/' + encodeURIComponent(id), {
      signal: AbortSignal.any([signal, AbortSignal.timeout(20000)]),
    }), { signal: ac.signal });
    if (!gate.current.isCurrent(ticket) || result.state === 'aborted') return;
    if (result.state === 'ready') {
      if (!gate.current.claimDelivery(ticket)) return;
      if (result.text.length > 4000) { setError('这段话超过了4000字，请缩短后再发送。'); return; }
      if (result.job.simulated) { setError('当前返回的是模拟文字，未自动发送。'); return; }
      if (suspiciousTranscript(result.text)) {
        callbacks.current.onRestore(result.text); onModeChange('keyboard'); clearSavedJob(id, ticket);
        setError('识别结果像视频片尾内容，已放入输入框核对，没有自动发送。'); return;
      }
      if (autoSend) {
        setPhase('delivering');
        const outcome = await callbacks.current.onSubmit(result.text);
        if (outcome !== 'recover') clearSavedJob(id, ticket);
        if (gate.current.isCurrent(ticket)) setNotice(outcome === 'sent' ? '' : outcome === 'draft' ? '尚未发送，可在输入框继续。' : '输入框内容较多，这段语音仍可取回。');
      } else {
        callbacks.current.onRestore(result.text); onModeChange('keyboard'); clearSavedJob(id, ticket);
        setNotice('已取回文字，尚未发送。');
      }
    } else if (result.state === 'unconfirmed') setError(result.reason);
    else { clearSavedJob(id, ticket); setError(result.reason); }
  }

  async function process(blob: Blob, duration: number, ticket: number) {
    if (!limits || !gate.current.claimUpload(ticket)) return;
    const ac = new AbortController(); controller.current = ac;
    let requested = false; let received = false;
    try {
      const mime = blob.type.split(';')[0];
      const extension = mime === 'audio/mp4' ? 'mp4' : mime === 'audio/ogg' ? 'ogg' : 'webm';
      const data = transcriptionForm(blob, 'voice.' + extension, duration, limits);
      setPhase('uploading');
      // Exactly one POST per recording. A missing receipt never triggers a second upload.
      requested = true;
      const created = await api<{ job_id: string }>('/transcriptions', {
        method: 'POST', body: data, signal: AbortSignal.any([ac.signal, AbortSignal.timeout(45000)]),
      });
      if (!gate.current.isCurrent(ticket)) return;
      if (!created.job_id) throw new Error('未能确认语音任务，请先取回最近语音。');
      received = true;
      setRecoverJob(created.job_id);
      try { sessionStorage.setItem(keys.job, created.job_id); } catch { /* Continue in memory. */ }
      await receive(created.job_id, ticket, ac, true);
    } catch (e) {
      if (!gate.current.isCurrent(ticket)) return;
      setError(e instanceof Error && e.name !== 'TimeoutError' ? e.message : '语音提交结果暂未确认，请先取回最近语音。');
      if (requested && !received && (!(e instanceof ApiError) || e.status === 0 || e.status >= 500)) setUncertainUpload(true);
    } finally { if (gate.current.isCurrent(ticket)) { locked.current = false; setPhase('idle'); } }
  }

  async function start() {
    if (!allowed || disabled || locked.current || !held.current) return;
    locked.current = true; const ticket = gate.current.begin();
    setError(''); setNotice(''); setCancelling(false); cancelSlide.current = false; setPhase('requesting');
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setError('当前环境不支持录音，请使用可信HTTPS或本机页面，也可以切回打字。');
      held.current = null; locked.current = false; setPhase('idle'); return;
    }
    const mime = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'].find(value =>
      limits.mime_types.includes(value.split(';')[0]) && MediaRecorder.isTypeSupported(value));
    if (!mime) { setError('当前浏览器不支持语音录入，可以切回打字。'); held.current = null; locked.current = false; setPhase('idle'); return; }
    try {
      const chosen = preferredMicrophone(inputs, deviceId);
      if (!chosen) throw new Error('请先启用并选择麦克风，再按住说话。');
      // An explicit device constraint cannot silently fall back to a saved browser default.
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: { exact: chosen.deviceId }, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      if (!gate.current.isCurrent(ticket) || !held.current) { stream.getTracks().forEach(track => track.stop()); return; }
      media.current = stream;
      const track = stream.getAudioTracks()[0];
      if (!track || track.readyState !== 'live') throw new Error('所选麦克风没有可用输入，请换一个设备。');
      const actualDeviceId = track.getSettings().deviceId;
      if (actualDeviceId && actualDeviceId !== chosen.deviceId) throw new Error('浏览器没有使用所选麦克风，请重新选择设备。');
      if (!explicitDevice.current && isPhoneMicrophone(track.label)) throw new Error('浏览器选中了手机麦克风，请选择电脑内置麦克风后再录。');
      setActualMicrophone(track.label || chosen.label); setHasSignal(false);
      const Audio = window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Audio) throw new Error('当前浏览器无法检查麦克风输入，请换用支持录音的浏览器。');
      const context = new Audio();
      signalMonitor.current = { stop() { void context.close().catch(() => {}); } };
      const meter = new VoiceSignalMeter();
      const analyser = context.createAnalyser(); analyser.fftSize = 2048;
      const source = context.createMediaStreamSource(stream); source.connect(analyser);
      const samples = new Float32Array(analyser.fftSize);
      let stopped = false;
      const sample = (publish = true) => { if (!stopped) { analyser.getFloatTimeDomainData(samples); meter.add(samples, context.sampleRate); if (publish) setHasSignal(meter.summary().hasSignal); } };
      const sampleTimer = setInterval(sample, 50);
      const monitor = { stop() { if (stopped) return; sample(false); stopped = true; clearInterval(sampleTimer); source.disconnect(); analyser.disconnect(); void context.close().catch(() => {}); } };
      signalMonitor.current = monitor;
      await context.resume();
      if (!gate.current.isCurrent(ticket) || !held.current) { monitor.stop(); stream.getTracks().forEach(track => track.stop()); return; }
      if (context.state !== 'running') throw new Error('麦克风输入尚未就绪，请重新按住说话。');
      const interrupted = () => { if (gate.current.isCurrent(ticket) && held.current) { cancel(''); setError('麦克风输入中断，未发送。请重新选择麦克风后再说。'); } };
      track.addEventListener('ended', interrupted, {once:true});
      track.addEventListener('mute', interrupted, {once:true});
      const chunks: Blob[] = []; let bytes = 0;
      const record = new MediaRecorder(stream, { mimeType: mime }); recorder.current = record;
      record.ondataavailable = event => {
        if (event.data.size) { chunks.push(event.data); bytes += event.data.size; }
        if (bytes > limits.max_bytes && gate.current.isCurrent(ticket)) cancel('录音太长，已取消。请分成几句话说。');
      };
      record.onerror = () => { if (gate.current.isCurrent(ticket)) { cancel(''); setError('录音中断了，请再试一次或切回打字。'); } };
      record.onstop = () => {
        monitor.stop();
        stream.getTracks().forEach(track => track.stop());
        if (!gate.current.isCurrent(ticket)) return;
        if (signalMonitor.current === monitor) signalMonitor.current = null;
        if (stopTimer.current) clearTimeout(stopTimer.current);
        if (recorder.current === record) recorder.current = null;
        if (media.current === stream) media.current = null;
        const duration = Math.min((Date.now() - started.current) / 1000, limits.max_seconds);
        if (duration < Math.max(0.6, limits.min_seconds ?? 0.2)) {
          locked.current = false; setPhase('idle'); setNotice('说话时间太短，请按住再说一次。'); return;
        }
        if (!meter.summary().hasSignal) {
          locked.current = false; setPhase('idle'); setError('没有检测到清晰声音，未上传、未发送。请检查所选麦克风，再按住说话。'); return;
        }
        // Preserve the browser's actual MIME/codec instead of relabelling the recording.
        void process(new Blob(chunks, { type: record.mimeType || chunks[0]?.type || mime }), duration, ticket);
      };
      record.start(); started.current = Date.now(); setSeconds(0); setPhase('recording');
      stopTimer.current = setTimeout(() => finish(), limits.max_seconds * 1000);
    } catch (e) {
      if (!gate.current.isCurrent(ticket)) return;
      stopMedia(); held.current = null; locked.current = false; setPhase('idle');
      setError(e instanceof Error && !['NotAllowedError', 'NotFoundError', 'OverconstrainedError', 'NotReadableError'].includes(e.name) ? e.message : '所选麦克风不可用或权限未允许，请重新选择电脑麦克风后再试。');
    }
  }

  function finish() {
    if (!held.current) return;
    held.current = null;
    if (cancelSlide.current) { cancel(); return; }
    if (recorder.current?.state === 'recording') stopMedia();
    else cancel('允许麦克风后，请重新按住说话。');
  }
  function pointerDown(event: PointerEvent<HTMLButtonElement>) {
    if (event.button !== 0 || disabled || locked.current || !allowed) return;
    event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId);
    held.current = { pointer: event.pointerId, y: event.clientY }; void start();
  }
  function pointerMove(event: PointerEvent<HTMLButtonElement>) {
    if (held.current?.pointer !== event.pointerId) return;
    cancelSlide.current = held.current.y - event.clientY > 60; setCancelling(cancelSlide.current);
  }
  function keyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === 'Escape') { event.preventDefault(); cancel(); }
    if (![' ', 'Enter'].includes(event.key) || event.repeat || disabled || locked.current || !allowed) return;
    event.preventDefault(); held.current = { pointer: null, y: 0 }; void start();
  }

  async function recover() {
    if (locked.current || disabled) return;
    locked.current = true; const ticket = gate.current.begin();
    const ac = new AbortController(); controller.current = ac; setError(''); setNotice(''); setPhase('recognizing');
    try {
      let id = recoverJob;
      if (!id) {
        const recent = await api<{ items: TranscriptionJob[] }>('/transcriptions', { signal: AbortSignal.any([ac.signal, AbortSignal.timeout(20000)]) });
        if (!gate.current.isCurrent(ticket)) return;
        const job = recent.items.find(job => ['pending', 'queued', 'running', 'succeeded'].includes(job.status));
        id = job?.id || job?.job_id || null;
      }
      if (!id) { setError('暂未找到可取回的语音，请稍后再查看。'); return; }
      setRecoverJob(id); try { sessionStorage.setItem(keys.job, id); } catch { /* Memory-only mode. */ }
      await receive(id, ticket, ac, false);
    } catch (e) { if (gate.current.isCurrent(ticket)) setError(e instanceof Error ? e.message : '暂时没取到文字，可以稍后再取回。'); }
    finally { if (gate.current.isCurrent(ticket)) { locked.current = false; setPhase('idle'); } }
  }

  const holding = phase === 'recording' || phase === 'requesting';
  return <div className={styles.root}>
    <div className={styles.row}>
      <button type="button" className={styles.toggle} aria-label={mode === 'voice' ? '切换到键盘输入' : '切换到语音输入'} title={mode === 'voice' ? '键盘输入' : '语音输入'} disabled={busy || disabled} onClick={() => {
        setError(''); setNotice(''); onModeChange(mode === 'voice' ? 'keyboard' : 'voice');
      }}>{mode === 'voice' ? <Keyboard size={23} strokeWidth={1.6}/> : <Mic size={23} strokeWidth={1.6}/>}</button>
      {mode === 'voice' ? <button type="button" aria-label="按住说话" aria-describedby="voice-hint" className={[styles.hold, holding ? styles.recording : '', cancelling ? styles.cancelling : ''].join(' ')} disabled={!!disabled || (!holding && busy) || !allowed || !deviceId}
        onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={event => { if (held.current?.pointer === event.pointerId) { event.preventDefault(); finish(); } }}
        onPointerCancel={() => { if (held.current) cancel(); }} onLostPointerCapture={() => { if (held.current) cancel(); }}
        onKeyDown={keyDown} onKeyUp={event => { if ([' ', 'Enter'].includes(event.key) && held.current?.pointer === null) { event.preventDefault(); finish(); } }}
        onContextMenu={event => event.preventDefault()}>
        {holding ? <><span className={styles.waves} style={{opacity:hasSignal ? 1 : .35}} aria-hidden="true"><i/><i/><i/><i/><i/></span>{phase === 'requesting' ? '正在打开麦克风…' : cancelling ? '松开取消' : !hasSignal && seconds > 1 ? '未检测到声音…' : `松开发送 · ${seconds}秒`}</> : busy ? <><LoaderCircle size={17} className="spin"/>正在处理…</> : '按住说话'}
      </button> : <div className={styles.field}>{children}</div>}
      {tools}
    </div>
    {mode === 'voice' && <div className={styles.deviceRow}>
      <label>麦克风<select aria-label="录音麦克风" value={deviceId} disabled={busy || disabled} onChange={event => { explicitDevice.current = true; setDeviceId(event.target.value); setActualMicrophone(''); setError(''); setNotice(''); }}>
        {!deviceId && <option value="">请选择麦克风</option>}
        {inputs.filter(input => !['default','communications'].includes(input.deviceId)).map((input,index) => <option key={input.deviceId} value={input.deviceId}>{input.label || `麦克风 ${index+1}（需授权）`}</option>)}
      </select></label>
      {!inputs.some(input => input.label) && <button type="button" className={styles.smallButton} disabled={busy || disabled} onClick={() => void prepareMicrophone()}>启用麦克风</button>}
      {phase === 'recording' && actualMicrophone && <span role="status">正在使用：{actualMicrophone}</span>}
    </div>}
    <div className={styles.feedback}>
      {busy && !holding ? <span role="status"><LoaderCircle size={13} className="spin"/>{phase === 'delivering' ? '正在发送…' : '正在识别…'}</span> : mode === 'voice' && !error && !notice ? <span id="voice-hint">{!limits ? '正在连接语音…' : !allowed ? '语音暂未启用，可以切回打字' : !deviceId ? '先允许麦克风，再选择本机设备' : cancelling ? '松开取消本次语音' : '按住说话，松开发送；上滑取消'}</span> : null}
      {busy && phase !== 'delivering' && <button type="button" className={styles.smallButton} aria-label="取消本次语音" onClick={() => cancel()}><X size={13}/>取消</button>}
      {!busy && (recoverJob || uncertainUpload) && <button type="button" className={styles.smallButton} disabled={disabled} onClick={() => void recover()}>取回{recoverJob ? '上次语音文字' : '最近语音'}</button>}
      {error && <span className={styles.error} role="alert">{error}</span>}
      {notice && <span role="status">{notice}</span>}
    </div>
  </div>;
}
