'use client';

import { useEffect, useRef, useState } from 'react';
import { Image as ImageIcon, LoaderCircle } from 'lucide-react';
import { ApiError, apiBlob } from '../lib/api';
import { readPhotoState } from '../lib/chat-photo';
import IngredientResult, { type Evaluation } from './IngredientResult';
import styles from './chat-photo.module.css';

export default function IngredientPhotoResult({ payload, onNavigate }: { payload: Record<string, unknown>; onNavigate?: () => void }) {
  const photo = readPhotoState(payload);
  const result = payload.result as Evaluation | undefined;
  const [preview, setPreview] = useState<{ asset: string; url: string } | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const viewing = useRef(false);
  const canView = photo.viewable && gone !== photo.assetId;
  const previewUrl = preview?.asset === photo.assetId && canView ? preview.url : null;

  useEffect(() => () => { request.current?.abort(); viewing.current = false; }, [photo.assetId]);
  useEffect(() => () => { if (preview?.url) URL.revokeObjectURL(preview.url); }, [preview]);

  async function view() {
    if (!canView || !photo.assetId || viewing.current) return;
    viewing.current = true;
    setBusy(true);
    setError('');
    const controller = new AbortController();
    request.current = controller;
    try {
      const blob = await apiBlob('/images/' + encodeURIComponent(photo.assetId), { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20000)]) });
      if (controller.signal.aborted) return;
      if (!blob.type.startsWith('image/')) throw new Error('原图响应暂时无法读取，请稍后再试。');
      setPreview({ asset: photo.assetId, url: URL.createObjectURL(blob) });
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (cause instanceof ApiError && cause.status === 404) { setGone(photo.assetId); setPreview(null); }
      else setError(cause instanceof Error ? cause.message : '暂时无法查看原图。');
    } finally {
      if (!controller.signal.aborted) { viewing.current = false; setBusy(false); }
    }
  }

  return <div>
    {result?.results ? <IngredientResult value={result} /> : <p className="warning">配料结果尚不可用，请重新读取对话状态。</p>}
    {photo.present && <section className={styles.photoState} aria-label="原图保存状态">
      <p className="small muted">{!photo.saved ? '本次未保存原图。' : canView ? '本次已保存原图，查看时会重新核对是否仍可用。' : '曾保存，现已清理。'}</p>
      <div className="button-row">
        {canView && <button type="button" className="text-button" disabled={busy} onClick={() => void view()}>
          {busy ? <LoaderCircle size={15} className="spin" /> : <ImageIcon size={15} />}查看原图
        </button>}
        {onNavigate && <button type="button" className="text-button" onClick={onNavigate}>前往配料页管理记录与原图</button>}
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {previewUrl && <div className={styles.preview}>
        <div className="row-between">
          <strong>已保存的配料原图</strong>
          <button type="button" className="text-button" onClick={() => setPreview(null)}>收起原图</button>
        </div>
        <img src={previewUrl} alt="本次保存的配料表原图" />
      </div>}
    </section>}
  </div>;
}
