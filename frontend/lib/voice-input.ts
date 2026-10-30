export type TranscriptionLimits = {
  configured: boolean;
  mime_types: string[];
  max_bytes: number;
  max_seconds: number;
  min_seconds?: number;
};

export type TranscriptionJob = {
  id?: string;
  job_id?: string;
  status: string;
  text?: string | null;
  simulated?: boolean;
  error?: { message?: string; code?: string } | null;
  expires_at?: string | null;
  created?: string;
};

export type VoiceDraft = { jobId: string; text: string; simulated: boolean; expiresAt: string };
export const VOICE_POLL_LIMIT = 60;

export function transcriptionState(job: TranscriptionJob, now = Date.now()) {
  if (job.expires_at && Date.parse(job.expires_at) <= now) return 'expired';
  if (['pending', 'queued', 'running', 'succeeded', 'failed', 'cancelled'].includes(job.status)) return job.status;
  return 'unknown';
}

export function transcriptionLabel(status: string): string {
  return ({ pending: '等待转写', queued: '等待转写', running: '正在转写', succeeded: '文字可取回',
    failed: '转写失败', cancelled: '任务已取消', expired: '文字已过期', unknown: '任务状态待核对' } as Record<string, string>)[status] || '任务状态待核对';
}

export function voiceKeys(username: string) {
  return { job: 'shushi-voice-job:' + username, draft: 'shushi-voice-draft:' + username };
}

export function parseVoiceDraft(raw: string | null, now = Date.now()): VoiceDraft | null {
  try {
    const value: unknown = JSON.parse(raw || 'null');
    if (!value || typeof value !== 'object') return null;
    const draft = value as Partial<VoiceDraft>;
    if (typeof draft.jobId !== 'string' || !draft.jobId || typeof draft.text !== 'string' || draft.text.length > 4000
      || typeof draft.expiresAt !== 'string' || !Number.isFinite(Date.parse(draft.expiresAt)) || Date.parse(draft.expiresAt) <= now) return null;
    return { jobId: draft.jobId, text: draft.text, simulated: draft.simulated === true, expiresAt: draft.expiresAt };
  } catch { return null; }
}

export function draftExpiry(serverExpiry?: string | null, now = Date.now()): string {
  const maximum = now + 24 * 60 * 60 * 1000;
  const supplied = serverExpiry ? Date.parse(serverExpiry) : NaN;
  return new Date(Number.isFinite(supplied) ? Math.min(supplied, maximum) : maximum).toISOString();
}

export function audioMime(type: string, name: string): string {
  const mime = type.split(';')[0].trim().toLowerCase();
  if (['audio/x-wav', 'audio/wave'].includes(mime)) return 'audio/wav';
  if (mime === 'audio/m4a' || mime === 'audio/x-m4a') return 'audio/mp4';
  if (mime) return mime;
  const extension = name.split('.').pop()?.toLowerCase();
  return ({ wav: 'audio/wav', webm: 'audio/webm', mp4: 'audio/mp4', m4a: 'audio/mp4', mp3: 'audio/mpeg', ogg: 'audio/ogg' } as Record<string, string>)[extension || ''] || '';
}

/** All formats declare a measured or user-supplied duration; the server also checks the container. */
export function transcriptionForm(file: Blob, name: string, seconds: number, limits: TranscriptionLimits): FormData {
  if (!file.size) throw new Error('录音内容为空，请重新录制或选择文件。');
  if (file.size > limits.max_bytes) throw new Error('录音文件过大，请缩短后重试。');
  const mime = audioMime(file.type, name);
  if (!limits.mime_types.includes(mime)) throw new Error('这种录音格式暂不支持，请换一种格式或改用打字。');
  if (!Number.isFinite(seconds) || seconds < (limits.min_seconds ?? 0.2)) throw new Error('请填写有效的录音时长（秒），不能用上限代替实际时长。');
  if (seconds > limits.max_seconds) throw new Error(`单段录音不能超过 ${limits.max_seconds} 秒，请缩短后重试。`);
  const data = new FormData();
  data.set('file', file.type === mime ? file : new Blob([file], { type: mime }), name);
  data.set('duration_seconds', String(Math.round(seconds * 1000) / 1000));
  return data;
}

/** Local metadata only: no upload, playback, microphone access, or provider call. */
export function readAudioDuration(file: Blob): Promise<number | null> {
  return new Promise(resolve => {
    const audio = document.createElement('audio');
    const url = URL.createObjectURL(file);
    let settled = false;
    const finish = (duration: number | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      audio.removeAttribute('src');
      audio.load();
      URL.revokeObjectURL(url);
      resolve(duration);
    };
    const timeout = setTimeout(() => finish(null), 8000);
    audio.preload = 'metadata';
    audio.onloadedmetadata = () => finish(Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : null);
    audio.onerror = () => finish(null);
    audio.src = url;
  });
}
