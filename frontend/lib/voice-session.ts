import type { TranscriptionJob } from './voice-input';

/** A ticket is local to one recording/recovery attempt, never a server job ID. */
export class VoiceSessionGate {
  private generation = 0;
  private active = false;
  private uploaded = false;
  private delivered = false;

  begin(): number {
    this.active = true;
    this.uploaded = false;
    this.delivered = false;
    return ++this.generation;
  }

  isCurrent(ticket: number): boolean {
    return this.active && ticket === this.generation;
  }

  /** Claim before POST; even an uncertain upload must not be retried automatically. */
  claimUpload(ticket: number): boolean {
    if (!this.isCurrent(ticket) || this.uploaded || this.delivered) return false;
    this.uploaded = true;
    return true;
  }

  /** Recovery can deliver an existing job without claiming a new upload. */
  claimDelivery(ticket: number): boolean {
    if (!this.isCurrent(ticket) || this.delivered) return false;
    this.delivered = true;
    return true;
  }

  cancel(): void {
    this.active = false;
    this.generation++;
  }
}

export type VoicePollResult =
  | { state: 'ready'; job: TranscriptionJob; text: string }
  | { state: 'failed' | 'empty' | 'expired'; job?: TranscriptionJob; reason: string }
  | { state: 'unconfirmed'; job?: TranscriptionJob; reason: string;
      cause: 'read_failed' | 'unknown_state' | 'taking_longer' | 'cancelled' }
  | { state: 'aborted' };

export type VoicePollOptions = {
  signal: AbortSignal;
  maxReads?: number;
  intervalMs?: number;
  maxWaitMs?: number;
  now?: () => number;
  sleep?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
  onState?: (job: TranscriptionJob) => void;
};

function wait(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('读取已停止。', 'AbortError')); return; }
    const aborted = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', aborted);
      reject(new DOMException('读取已停止。', 'AbortError'));
    };
    const timer = setTimeout(() => { signal.removeEventListener('abort', aborted); resolve(); }, milliseconds);
    signal.addEventListener('abort', aborted, { once: true });
  });
}

/** Read an existing job only. This function cannot upload audio or send a chat. */
export async function pollVoiceJob(
  readJob: (signal: AbortSignal) => Promise<TranscriptionJob>, options: VoicePollOptions,
): Promise<VoicePollResult> {
  const { signal, onState } = options;
  const maxReads = options.maxReads ?? 60;
  const intervalMs = options.intervalMs ?? 1800;
  const maxWaitMs = options.maxWaitMs ?? 120000;
  if (!Number.isSafeInteger(maxReads) || maxReads < 1 || maxReads > 120
      || !Number.isFinite(intervalMs) || intervalMs < 0
      || !Number.isFinite(maxWaitMs) || maxWaitMs < 0) throw new RangeError('语音读取需要有效的次数和时间上限。');
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? wait;
  const started = now();
  let lastJob: TranscriptionJob | undefined;
  let readFailed = false;
  for (let count = 0; count < maxReads; count++) {
    if (signal.aborted) return { state: 'aborted' };
    if (now() - started >= maxWaitMs) break;
    let job: TranscriptionJob | undefined;
    try {
      job = await readJob(signal);
      readFailed = false;
    } catch (error) {
      if (signal.aborted) return { state: 'aborted' };
      if (error && typeof error === 'object' && 'status' in error && error.status === 404)
        return { state: 'expired', reason: '找不到这次转写，可能已清理。可以重新录制或改用打字。' };
      // A failed GET does not tell us whether the provider has completed the job.
      readFailed = true;
    }
    if (signal.aborted) return { state: 'aborted' };
    if (job) {
      lastJob = job;
      onState?.(job);
      if (signal.aborted) return { state: 'aborted' };
      if (job.expires_at && Date.parse(job.expires_at) <= now())
        return { state: 'expired', job, reason: '这次转写文字已清理，请重新录制或改用打字。' };
      if (job.status === 'succeeded') {
        const text = typeof job.text === 'string' ? job.text.trim() : '';
        return text ? { state: 'ready', job, text }
          : { state: 'empty', job, reason: '这段录音没有可取回的文字，请重新录制或改用打字。' };
      }
      if (job.status === 'failed')
        return { state: 'failed', job, reason: job.error?.message || '这次转写未完成，请重新录制或改用打字。' };
      if (job.status === 'cancelled')
        return { state: 'unconfirmed', job, cause: 'cancelled', reason: '这次转写任务已取消，可以重新录制或改用打字。' };
      if (!['pending', 'queued', 'running'].includes(job.status))
        return { state: 'unconfirmed', job, cause: 'unknown_state', reason: '暂时无法确认转写状态，请重新读取，不要重复上传。' };
    } else if (!readFailed) {
      return { state: 'unconfirmed', cause: 'unknown_state', reason: '暂时无法确认转写状态，请重新读取，不要重复上传。' };
    }
    const remaining = maxWaitMs - (now() - started);
    if (count + 1 >= maxReads || remaining <= 0) break;
    try { await sleep(Math.min(intervalMs, remaining), signal); }
    catch {
      if (signal.aborted) return { state: 'aborted' };
      return { state: 'unconfirmed', job: lastJob, cause: 'read_failed',
        reason: '读取已暂停，任务仍可能完成。稍后重新读取，不要重复上传。' };
    }
  }
  if (signal.aborted) return { state: 'aborted' };
  return readFailed
    ? { state: 'unconfirmed', job: lastJob, cause: 'read_failed',
        reason: '暂时无法读取转写结果，任务仍可能完成。请重新读取，不要重复上传。' }
    : { state: 'unconfirmed', job: lastJob, cause: 'taking_longer',
        reason: '转写仍在处理中，已暂停自动读取。稍后重新读取即可，无需重新上传。' };
}
