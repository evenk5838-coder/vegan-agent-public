'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { ImagePlus, LoaderCircle, X } from 'lucide-react';
import { ApiError, api } from '../lib/api';
import { PHOTO_MIME_TYPES, photoIsTerminal, photoJobLabel, validatePhoto, type PhotoAttachment, type PhotoJob } from '../lib/chat-photo';
import styles from './chat-photo.module.css';

export type { PhotoAttachment } from '../lib/chat-photo';
type Props = {
  value: PhotoAttachment | null;
  onChange: (value: PhotoAttachment | null) => void;
  enabled: boolean;
  disabled?: boolean;
  onBusyChange?: (busy: boolean) => void;
  compact?: boolean;
};

export default function ChatPhotoInput({ value, onChange, enabled, disabled, onBusyChange, compact }: Props) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [save, setSave] = useState<boolean | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [jobState, setJobState] = useState<{ id: string; job: PhotoJob | null; notice: string }>({ id: '', job: null, notice: '' });
  const [resume, setResume] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const uploadController = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const mounted = useRef(true);
  const busyCallback = useRef(onBusyChange);
  const radioName = useId();
  const jobId = value?.job_id;

  useEffect(() => { busyCallback.current = onBusyChange; }, [onBusyChange]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      uploadController.current?.abort();
      if (busy.current) busyCallback.current?.(false);
      busy.current = false;
    };
  }, []);

  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let reads = 0;
    let failures = 0;
    async function read() {
      try {
        const job = await api<PhotoJob>('/jobs/' + encodeURIComponent(jobId!), {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12000)]),
        });
        if (controller.signal.aborted) return;
        failures = 0;
        reads++;
        const terminal = photoIsTerminal(job);
        const notice = !terminal && reads >= 20 ? '识别仍在处理，已暂停自动查询。可重新读取状态，不会重新上传。' : '';
        setJobState({ id: jobId!, job, notice });
        if (!terminal && reads < 20) timer = setTimeout(read, document.hidden ? 6000 : 1800);
      } catch (cause) {
        if (controller.signal.aborted) return;
        failures++;
        const notice = cause instanceof Error ? cause.message : '读取识别状态失败。';
        setJobState(previous => ({ id: jobId!, job: previous.id === jobId ? previous.job : null, notice }));
        if (failures < 3) timer = setTimeout(read, 2500 * failures);
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [jobId, resume]);

  function choose(selected: File | undefined) {
    if (!selected || busy.current) return;
    const problem = validatePhoto(selected);
    setOpen(true);
    setError(problem || '');
    if (problem) return;
    onChange(null); // 用户明确更换附件；父组件创建会话时不触发此操作。
    setFile(selected);
    setSave(null); // 每张图都重新选择，不能继承上一次的保存同意。
  }

  async function upload() {
    if (!file || save === null || !enabled || disabled || busy.current) return;
    const controller = new AbortController();
    uploadController.current = controller;
    busy.current = true;
    setUploading(true);
    busyCallback.current?.(true);
    setError('');
    const body = new FormData();
    body.set('file', file);
    body.set('save_original', String(save));
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const created = await api<{ job_id: string }>('/ingredients/photo', { method: 'POST', body, signal: controller.signal });
      if (!mounted.current || controller.signal.aborted) return;
      if (typeof created.job_id !== 'string' || !created.job_id) throw new Error('暂时无法确认上传结果，请先查看配料记录，勿反复上传同图。');
      onChange({ kind: 'ingredient_photo', job_id: created.job_id, save_original: save, file_name: file.name });
      setFile(null);
      setSave(null);
    } catch (cause) {
      if (!mounted.current) return;
      setError(controller.signal.aborted
        ? '已停止本地等待，后台任务可能仍在处理。请先查看配料记录，勿反复上传同图。'
        : cause instanceof ApiError && cause.status === 0
          ? '上传响应中断，任务可能已受理。请先查看配料记录，勿反复上传同图。'
          : cause instanceof Error ? cause.message : '图片提交未完成，请稍后再试。');
    } finally {
      clearTimeout(timeout);
      if (mounted.current) {
        busy.current = false;
        setUploading(false);
        busyCallback.current?.(false);
      }
    }
  }

  const currentJob = jobState.id === jobId ? jobState.job : null;
  const jobNotice = jobState.id === jobId ? jobState.notice : '';
  return <div className={styles.input}>
    <button type="button" className={compact ? 'chat-tool' : 'text-button'} aria-label={value ? '配料照片，已附加' : '添加配料照片'} title="添加配料照片" aria-expanded={open} onClick={() => setOpen(!open)}>
      <ImagePlus size={compact ? 21 : 17} />{!compact && <>配料照片{value ? ' · 已附加' : ''}</>}
    </button>
    {open && <section className={styles.panel} aria-label="添加配料照片">
      <div className="row-between">
        <strong>拍下或选择完整配料表</strong>
        <button type="button" className="icon-button" aria-label="收起配料照片" onClick={() => setOpen(false)}><X size={16} /></button>
      </div>
      <p className="small muted">支持 JPG、PNG、WebP，最大 10MB。iPhone 的 HEIC 照片请先转为 JPG。</p>
      {!enabled && <p className="warning">拍照识别服务尚未启用，当前可以手动输入配料。</p>}
      <div className="button-row">
        <button type="button" className="secondary" disabled={!enabled || disabled || uploading} onClick={() => input.current?.click()}>
          <ImagePlus size={16} />{value || file ? '更换照片' : '拍照或选图'}
        </button>
        <input ref={input} hidden type="file" accept={PHOTO_MIME_TYPES.join(',')}
          onChange={event => { choose(event.target.files?.[0]); event.target.value = ''; }} />
      </div>
      {file && <div className={styles.selection}>
        <p className="small">待提交：{file.name}</p>
        <fieldset disabled={uploading || disabled} className={styles.choices}>
          <legend>本次是否保存原图？</legend>
          <label><input type="radio" name={radioName} checked={save === false} onChange={() => setSave(false)} />识别后清理原图</label>
          <label><input type="radio" name={radioName} checked={save === true} onChange={() => setSave(true)} />保存原图 30 天，可提前删除</label>
        </fieldset>
        <div className="button-row">
          <button type="button" className="secondary" disabled={!enabled || disabled || uploading || save === null} onClick={() => void upload()}>
            {uploading ? <><LoaderCircle className="spin" size={15} />正在上传</> : '确认本次选择并上传'}
          </button>
          {uploading && <button type="button" className="text-button" onClick={() => uploadController.current?.abort()}>停止等待</button>}
        </div>
      </div>}
      {value && <div className={styles.attachment} role="status">
        <strong>配料照片已附加</strong>
        <p className="small">{value.file_name || '配料表图片'} · {value.save_original ? '本次保存原图' : '识别后清理原图'}</p>
        <p className="small">{photoJobLabel(currentJob)}</p>
        {jobNotice && <p className="warning">{jobNotice}</p>}
        <div className="button-row">
          <button type="button" className="text-button" disabled={disabled} onClick={() => setResume(n => n + 1)}>重新读取状态</button>
          <button type="button" className="text-button" disabled={disabled} onClick={() => onChange(null)}>移除附件</button>
        </div>
        <p className="small muted">移除只取消随消息发送，不会删除或取消后台识别任务。发送消息重试时复用这次识别。</p>
      </div>}
      {error && <p className="error" role="alert">{error}</p>}
    </section>}
  </div>;
}
