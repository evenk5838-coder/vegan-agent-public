'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Barcode, Camera, ChevronRight, History, Keyboard, LoaderCircle, RefreshCw, ScanLine, Trash2, Upload } from 'lucide-react';
import { ApiError, api, safeExternalUrl } from '../lib/api';
import { barcodeCameraError, barcodeIngredientBody, normalizeBarcode, stopBarcodeVideo, type BarcodeLookup } from '../lib/barcode';
import { validatePhoto } from '../lib/chat-photo';
import IngredientResult, { type Evaluation } from './IngredientResult';
import styles from './ingredient-workbench.module.css';

type Mode = 'photo' | 'barcode' | 'manual';
type IngredientHistory = { id: string; text: string; result: Evaluation; created: string };
type SavedImage = { id: string; expires: string };
type PhotoJob = { status: string; result: Evaluation & { error?: { message?: string } } };
const modes = [{ id: 'photo' as const, label: '拍配料表', Icon: ScanLine }, { id: 'barcode' as const, label: '扫条形码', Icon: Barcode }, { id: 'manual' as const, label: '手动输入', Icon: Keyboard }];

function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const finish = () => { signal.removeEventListener('abort', abort); resolve(); };
    const timer = setTimeout(finish, ms);
    function abort() { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(new DOMException('Aborted', 'AbortError')); }
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}

export default function IngredientWorkbench() {
  const [mode, setMode] = useState<Mode>('photo');
  const [result, setResult] = useState<Evaluation | null>(null);
  const [rows, setRows] = useState<IngredientHistory[]>([]);
  const [images, setImages] = useState<SavedImage[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState('');
  const [deleting, setDeleting] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [photoFile, setPhotoFile] = useState<File | null>(null);
  const [saveOriginal, setSaveOriginal] = useState(false);
  const [photoJobId, setPhotoJobId] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const [lookup, setLookup] = useState<BarcodeLookup | null>(null);
  const [barcodeText, setBarcodeText] = useState('');
  const [barcodeComplete, setBarcodeComplete] = useState(false);
  const [manualText, setManualText] = useState('');
  const [manualWarnings, setManualWarnings] = useState('');
  const [manualComplete, setManualComplete] = useState(false);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [cameraStarting, setCameraStarting] = useState(false);
  const [cameraKey, setCameraKey] = useState(0);
  const [imageReading, setImageReading] = useState(false);
  const photoInput = useRef<HTMLInputElement>(null);
  const barcodeInput = useRef<HTMLInputElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const scanner = useRef<{ stop: () => void } | null>(null);
  const localStream = useRef<MediaStream | null>(null);
  const cameraGeneration = useRef(0);
  const scanImageGeneration = useRef(0);
  const cameraAcquiring = useRef(false);
  const imageAcquiring = useRef(false);
  const request = useRef<AbortController | null>(null);
  const historyRequest = useRef<AbortController | null>(null);
  const requestGeneration = useRef(0);
  const actionRunning = useRef(false);
  const mounted = useRef(true);
  const resultSection = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!result) return;
    resultSection.current?.focus({ preventScroll: true });
    resultSection.current?.scrollIntoView({ block: 'start', behavior: 'instant' });
  }, [result]);

  const haltCamera = useCallback(() => {
    cameraGeneration.current++;
    scanner.current?.stop(); scanner.current = null;
    localStream.current?.getTracks().forEach(track => track.stop()); localStream.current = null;
    stopBarcodeVideo(video.current);
    cameraAcquiring.current = false;
  }, []);
  const haltImage = useCallback(() => { scanImageGeneration.current++; imageAcquiring.current = false; }, []);

  const refreshHistory = useCallback(async () => {
    const ac = new AbortController(); historyRequest.current?.abort(); historyRequest.current = ac;
    setHistoryLoading(true); setHistoryError('');
    const responses = await Promise.allSettled([
      api<IngredientHistory[]>('/ingredients/history', { signal: AbortSignal.any([ac.signal, AbortSignal.timeout(15000)]) }),
      api<SavedImage[]>('/images', { signal: AbortSignal.any([ac.signal, AbortSignal.timeout(15000)]) }),
    ]);
    if (ac.signal.aborted || !mounted.current) return;
    if (responses[0].status === 'fulfilled') setRows(responses[0].value);
    if (responses[1].status === 'fulfilled') setImages(responses[1].value);
    const failed = responses.find(response => response.status === 'rejected');
    if (failed?.status === 'rejected') setHistoryError(failed.reason instanceof Error ? failed.reason.message : '核对记录暂时无法读取。');
    setHistoryLoading(false);
  }, []);

  useEffect(() => {
    let disposed = false; mounted.current = true;
    const suspendCamera = () => { haltCamera(); setCameraOpen(false); setCameraStarting(false); };
    const visibility = () => { if (document.hidden) suspendCamera(); };
    window.addEventListener('pagehide', suspendCamera);
    document.addEventListener('visibilitychange', visibility);
    void Promise.resolve().then(() => { if (!disposed) void refreshHistory(); });
    return () => {
      window.removeEventListener('pagehide', suspendCamera);
      document.removeEventListener('visibilitychange', visibility);
      disposed = true; mounted.current = false; haltCamera(); haltImage();
      request.current?.abort(); historyRequest.current?.abort();
    };
  }, [haltCamera, haltImage, refreshHistory]);

  function stopWaiting() {
    requestGeneration.current++; request.current?.abort(); actionRunning.current = false;
    setBusy(false); setNotice('已停止本地等待，后台任务可能仍在处理；可读取原任务或稍后查看核对记录。');
  }

  function changeMode(next: Mode) {
    haltCamera(); setCameraOpen(false); setCameraStarting(false);
    haltImage(); setImageReading(false);
    if (busy) stopWaiting();
    setMode(next); setError('');
  }

  function begin() {
    if (actionRunning.current) return null;
    actionRunning.current = true;
    request.current?.abort();
    const ac = new AbortController(); request.current = ac;
    const generation = ++requestGeneration.current;
    setBusy(true); setError(''); setNotice('');
    return { ac, generation, current: () => mounted.current && !ac.signal.aborted && generation === requestGeneration.current };
  }
  function finish(operation: NonNullable<ReturnType<typeof begin>>) {
    if (operation.current()) { actionRunning.current = false; setBusy(false); }
  }
  function report(reason: unknown, operation: NonNullable<ReturnType<typeof begin>>) {
    if (!operation.current()) return;
    setError(reason instanceof Error && reason.name === 'TimeoutError' ? '响应较慢，请稍后读取原任务或查看核对记录，不必重复提交。' : reason instanceof Error ? reason.message : '本次操作没有完成，请稍后再试。');
  }

  async function readPhoto(jobId: string, operation: NonNullable<ReturnType<typeof begin>>) {
    for (let reads = 0; reads < 24 && operation.current(); reads++) {
      const job = await api<PhotoJob>('/jobs/' + encodeURIComponent(jobId), { signal: AbortSignal.any([operation.ac.signal, AbortSignal.timeout(15000)]) });
      if (!operation.current()) return;
      if (job.status === 'succeeded') { setResult(job.result); setNotice('配料提取完成，逐项依据见下方。'); setPhotoJobId(null); setPhotoFile(null); setSaveOriginal(false); await refreshHistory(); return; }
      if (['failed', 'cancelled'].includes(job.status)) throw new Error(job.result?.error?.message || '这次配料提取没有完成，请核对照片后再试。');
      setNotice(job.status === 'running' ? '正在提取配料…' : '图片已受理，正在排队…');
      await delay(document.hidden ? 5000 : 1500, operation.ac.signal);
    }
    if (operation.current()) setNotice('识别仍在处理。可以稍后读取这次结果，不会重新上传。');
  }

  async function uploadPhoto() {
    if (!photoFile) return;
    const problem = validatePhoto(photoFile); if (problem) { setError(problem); return; }
    const operation = begin(); if (!operation) return;
    const body = new FormData(); body.set('file', photoFile); body.set('save_original', String(saveOriginal));
    try {
      setNotice('正在提交配料照片…');
      const created = await api<{ job_id: string }>('/ingredients/photo', { method: 'POST', body, signal: AbortSignal.any([operation.ac.signal, AbortSignal.timeout(20000)]) });
      if (!operation.current()) return;
      if (!created.job_id) throw new Error('上传结果暂时无法确认，请先查看核对记录，勿重复提交。');
      setPhotoJobId(created.job_id);
      await readPhoto(created.job_id, operation);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 0 && operation.current()) setError('上传响应中断，任务可能已受理。请先查看核对记录，勿重复上传同图。');
      else report(reason, operation);
    } finally { finish(operation); }
  }

  async function resumePhoto() {
    if (!photoJobId) return;
    const operation = begin(); if (!operation) return;
    try { await readPhoto(photoJobId, operation); } catch (reason) { report(reason, operation); } finally { finish(operation); }
  }

  async function lookupCode(value: string) {
    const normalized = normalizeBarcode(value);
    if (!normalized) { setError('条码校验未通过，请核对 8、12、13 或 14 位商品码。'); return; }
    const operation = begin(); if (!operation) return;
    setCode(normalized); setLookup(null); setBarcodeText(''); setBarcodeComplete(false);
    try {
      const data = await api<BarcodeLookup>('/ingredients/barcode/' + normalized, { signal: AbortSignal.any([operation.ac.signal, AbortSignal.timeout(20000)]) });
      if (!operation.current()) return;
      setLookup(data); setBarcodeText(data.ingredients_text || ''); setBarcodeComplete(false);
    } catch (reason) { report(reason, operation); } finally { finish(operation); }
  }

  async function startCamera() {
    if (cameraAcquiring.current || busy || imageReading) return;
    haltCamera();
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) { setError('相机需要 HTTPS 或 localhost。也可以选择条码图片或输入数字。'); return; }
    const generation = ++cameraGeneration.current;
    cameraAcquiring.current = true; setCameraKey(generation); setCameraOpen(true); setCameraStarting(true); setError('');
    try {
      const { BrowserMultiFormatOneDReader } = await import('@zxing/browser');
      if (!mounted.current || generation !== cameraGeneration.current) return;
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false });
      if (!mounted.current || generation !== cameraGeneration.current) { stream.getTracks().forEach(track => track.stop()); return; }
      localStream.current = stream;
      const preview = video.current;
      if (!preview) { haltCamera(); return; }
      const reader = new BrowserMultiFormatOneDReader();
      const controls = await reader.decodeFromStream(stream, preview, (found, _failure, currentControls) => {
        if (!mounted.current || generation !== cameraGeneration.current) { currentControls.stop(); return; }
        if (!found) return;
        const normalized = normalizeBarcode(found.getText());
        if (!normalized) return;
        currentControls.stop(); haltCamera(); setCameraOpen(false); setCameraStarting(false);
        void lookupCode(normalized);
      });
      if (!mounted.current || generation !== cameraGeneration.current) { controls.stop(); return; }
      scanner.current = controls; setCameraStarting(false);
    } catch (reason) {
      if (mounted.current && generation === cameraGeneration.current) { haltCamera(); setCameraOpen(false); setCameraStarting(false); setError(barcodeCameraError(reason)); }
    } finally { if (generation === cameraGeneration.current) cameraAcquiring.current = false; }
  }

  async function readBarcodeImage(file: File | undefined) {
    if (!file || imageAcquiring.current || busy) return;
    const problem = validatePhoto(file); if (problem) { setError(problem); return; }
    haltCamera(); setCameraOpen(false); setCameraStarting(false); setError('');
    imageAcquiring.current = true; setImageReading(true);
    const generation = ++scanImageGeneration.current;
    const objectUrl = URL.createObjectURL(file);
    try {
      const { BrowserMultiFormatOneDReader } = await import('@zxing/browser');
      if (!mounted.current || generation !== scanImageGeneration.current) return;
      const reader = new BrowserMultiFormatOneDReader();
      const decoded = await reader.decodeFromImageUrl(objectUrl);
      if (!mounted.current || generation !== scanImageGeneration.current) return;
      const normalized = normalizeBarcode(decoded.getText());
      if (!normalized) throw new Error('图片中的条码未通过校验，请输入包装上的数字。');
      await lookupCode(normalized);
    } catch (reason) {
      if (mounted.current && generation === scanImageGeneration.current) setError(reason instanceof Error && reason.message.includes('校验') ? reason.message : '没有读出清晰的商品条码，请换张图片或输入数字。');
    } finally {
      URL.revokeObjectURL(objectUrl);
      if (mounted.current && generation === scanImageGeneration.current) { imageAcquiring.current = false; setImageReading(false); }
    }
  }

  async function evaluateText(fromBarcode: boolean) {
    let body: { text: string; complete: boolean; label_warnings: string[] };
    try {
      body = fromBarcode ? barcodeIngredientBody(barcodeText, barcodeComplete, lookup?.label_warnings || [])
        : { text: manualText.trim(), complete: manualComplete, label_warnings: manualWarnings.split('\n').map(value => value.trim()).filter(Boolean) };
      if (!body.text) throw new Error('请先填写配料。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '请核对配料。'); return; }
    const operation = begin(); if (!operation) return;
    try {
      const data = await api<Evaluation>('/ingredients/evaluate', { method: 'POST', body: JSON.stringify(body), signal: AbortSignal.any([operation.ac.signal, AbortSignal.timeout(20000)]) });
      if (!operation.current()) return;
      setResult(data); setNotice('已按核对后的配料生成三类判断。'); await refreshHistory();
    } catch (reason) { report(reason, operation); } finally { finish(operation); }
  }

  async function remove(kind: 'history' | 'image', id: string) {
    const operation = begin(); if (!operation) return;
    try {
      await api(kind === 'image' ? '/images/' + encodeURIComponent(id) : '/ingredients/history/' + encodeURIComponent(id), { method: 'DELETE', signal: AbortSignal.any([operation.ac.signal, AbortSignal.timeout(15000)]) });
      if (!operation.current()) return;
      setDeleting(null); await refreshHistory();
    } catch (reason) { report(reason, operation); } finally { finish(operation); }
  }

  const sourceUrl = safeExternalUrl(lookup?.source.url);
  return <div className={styles.workbench}>
    <div className={styles.modeTabs} role="group" aria-label="选择配料核对方式">{modes.map(({ id, label, Icon }) => <button type="button" key={id} className={mode === id ? styles.selected : ''} aria-pressed={mode === id} onClick={() => changeMode(id)}><Icon size={17} />{label}</button>)}</div>
    <section className={styles.inputPanel} aria-label={modes.find(item => item.id === mode)?.label}>
      {mode === 'photo' && <>
        <div className={styles.uploadArea}><ScanLine size={31} strokeWidth={1.3} /><strong>{photoFile?.name || '拍清配料表，看看适不适合你'}</strong><p>JPG、PNG、WebP · 最大 10MB</p><button type="button" className="secondary" disabled={busy} onClick={() => photoInput.current?.click()}><Camera size={16} />{photoFile ? '更换照片' : '拍照或选图'}</button><input ref={photoInput} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={event => { const file = event.target.files?.[0]; if (file) { const issue = validatePhoto(file); setError(issue || ''); if (!issue) { setPhotoFile(file); setSaveOriginal(false); setPhotoJobId(null); } } event.target.value = ''; }} /></div>
        <label className="check-row"><input type="checkbox" checked={saveOriginal} disabled={busy} onChange={event => setSaveOriginal(event.target.checked)} />本次保存原图 30 天</label>
        <p className={styles.hint}>未勾选时，处理后清理原图。</p>
        <div className="button-row"><button type="button" className="primary" disabled={!photoFile || busy || !!photoJobId} onClick={() => void uploadPhoto()}>{busy ? <LoaderCircle size={16} className="spin" /> : <ScanLine size={16} />}识别配料</button>{photoJobId && <button type="button" className="secondary" disabled={busy} onClick={() => void resumePhoto()}><RefreshCw size={15} />读取本次结果</button>}{busy && <button type="button" className="text-button" onClick={stopWaiting}>停止等待</button>}</div>
      </>}
      {mode === 'barcode' && <>
        <div className={styles.scanActions}><button type="button" className="secondary" disabled={busy || imageReading || cameraStarting} onClick={() => void startCamera()}><Camera size={17} />{cameraStarting ? '正在打开相机' : '开启相机扫码'}</button><button type="button" className="secondary" disabled={busy || imageReading} onClick={() => barcodeInput.current?.click()}>{imageReading ? <LoaderCircle size={16} className="spin" /> : <Upload size={16} />}选条码图片</button><input ref={barcodeInput} hidden type="file" accept="image/jpeg,image/png,image/webp" onChange={event => { void readBarcodeImage(event.target.files?.[0]); event.target.value = ''; }} /></div>
        <p className={styles.hint}>条码图片在本机识别，仅商品码用于查询公开资料。</p>
        <div className={styles.camera} hidden={!cameraOpen}><video key={cameraKey} ref={video} autoPlay muted playsInline aria-label="商品条码相机取景" /><div className={styles.cameraGuide} aria-hidden="true" /><p>将条形码放进框内</p><button type="button" className="secondary" onClick={() => { haltCamera(); setCameraOpen(false); setCameraStarting(false); }}>停止相机</button></div>
        <form className={styles.codeForm} onSubmit={event => { event.preventDefault(); haltCamera(); setCameraOpen(false); setCameraStarting(false); void lookupCode(code); }}><label>也可以输入条码数字<input value={code} disabled={busy} onChange={event => setCode(event.target.value)} inputMode="numeric" autoComplete="off" placeholder="包装上一排 8—14 位数字" required maxLength={40} /></label><button type="submit" className="primary" disabled={busy || imageReading}>查询</button></form>
        {lookup && <div className={styles.product}>
          {lookup.product && <div className={styles.productHeading}><Barcode size={25} strokeWidth={1.2} /><div><strong>{lookup.product.product_name_zh || lookup.product.product_name || '商品资料'}</strong><p>{[lookup.product.brands, lookup.product.quantity].filter(Boolean).join(' · ') || lookup.code}</p></div></div>}
          <p className="small muted">{lookup.message}</p>{sourceUrl && <a className="source-link" href={sourceUrl} target="_blank" rel="noreferrer">来源：{lookup.source.name}<ChevronRight size={12} /></a>}
          {lookup.product && <form onSubmit={event => { event.preventDefault(); void evaluateText(true); }}><label>对照包装核对配料<textarea rows={4} value={barcodeText} disabled={busy} maxLength={12000} onChange={event => { setBarcodeText(event.target.value); setBarcodeComplete(false); }} placeholder="暂无配料时，可以抄录包装上的完整配料" /></label><label className="check-row"><input type="checkbox" checked={barcodeComplete} disabled={busy} onChange={event => setBarcodeComplete(event.target.checked)} />我已对照包装，核对完整可见配料</label><button className="primary" disabled={busy || !barcodeText.trim() || !barcodeComplete}>{busy ? <LoaderCircle size={16} className="spin" /> : <ScanLine size={16} />}核对三类适配</button></form>}
          {lookup.status !== 'found' && <button type="button" className="text-button" onClick={() => changeMode('photo')}>改拍配料表<ChevronRight size={14} /></button>}
        </div>}
      </>}
      {mode === 'manual' && <form onSubmit={event => { event.preventDefault(); void evaluateText(false); }}>
        <label>包装上的配料<textarea value={manualText} disabled={busy} onChange={event => { setManualText(event.target.value); setManualComplete(false); }} required rows={4} maxLength={12000} placeholder="例如：小麦粉、植物油、白砂糖" /></label>
        <label>包装额外提示（可选）<input value={manualWarnings} disabled={busy} onChange={event => setManualWarnings(event.target.value)} placeholder="例如：可能含有乳制品" maxLength={1000} /></label>
        <label className="check-row"><input type="checkbox" checked={manualComplete} disabled={busy} onChange={event => setManualComplete(event.target.checked)} />我已完整录入可见配料</label>
        <button className="primary" disabled={busy}>{busy ? <LoaderCircle size={16} className="spin" /> : <ScanLine size={16} />}查看三类适配</button>
      </form>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}{error && <p className="error" role="alert">{error}</p>}
    </section>
    {result && <section ref={resultSection} tabIndex={-1} className={styles.resultSection} aria-label="本次配料核对结果"><div className={styles.extracted}><span className="small muted">本次核对的配料</span><p>{result.items.map(item => item.name).join('、') || '暂无清晰配料'}</p></div><IngredientResult value={result} /></section>}
    <details className={styles.history}>
      <summary><span><History size={15} />最近核对{!historyLoading && ` · ${rows.length}`}</span><ChevronRight size={15} /></summary>
      <div className="row-between"><p className="small muted">识别历史保留 90 天</p><button className="text-button" disabled={historyLoading || busy} onClick={() => void refreshHistory()}><RefreshCw size={14} />刷新</button></div>
      {historyLoading && <p className="small muted" role="status">正在读取记录…</p>}{historyError && <p className="error" role="alert">{historyError}</p>}
      {!historyLoading && !historyError && !rows.length && <p className="small muted">还没有核对记录。</p>}
      {rows.map(row => <div className={styles.historyRow} key={row.id}><button type="button" className={styles.historyOpen} onClick={() => { setResult(row.result); setNotice(''); }}>{row.text}<ChevronRight size={14} /></button>{deleting === row.id ? <div className="button-row"><button className="text-button danger" disabled={busy} onClick={() => void remove('history', row.id)}>确认删除</button><button className="text-button" onClick={() => setDeleting(null)}>保留</button></div> : <button type="button" className="icon-button" aria-label={'删除核对记录 '+row.text} disabled={busy} onClick={() => setDeleting(row.id)}><Trash2 size={15} /></button>}</div>)}
      {!!images.length && <details><summary>已保存的原图 · {images.length}</summary>{images.map(image => <div className={styles.historyRow} key={image.id}><a className="text-button" href={'/api/v1/images/'+encodeURIComponent(image.id)} target="_blank" rel="noreferrer">查看原图 · {new Date(image.expires).toLocaleDateString('zh-CN')} 前保留</a>{deleting === image.id ? <div className="button-row"><button className="text-button danger" disabled={busy} onClick={() => void remove('image', image.id)}>确认删除</button><button className="text-button" onClick={() => setDeleting(null)}>保留</button></div> : <button className="icon-button" disabled={busy} aria-label="删除原图" onClick={() => setDeleting(image.id)}><Trash2 size={15} /></button>}</div>)}</details>}
    </details>
  </div>;
}
