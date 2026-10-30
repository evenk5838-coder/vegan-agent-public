export type PhotoAttachment = {
  kind: 'ingredient_photo';
  job_id: string;
  save_original: boolean;
  file_name?: string;
};

export type PhotoJob = {
  id: string;
  status: string;
  result?: { error?: { code?: string; message?: string } };
};

export const PHOTO_MAX_BYTES = 10 * 1024 * 1024;
export const PHOTO_MIME_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

export function validatePhoto(file: Pick<File, 'size' | 'type'>): string | null {
  if (!file.size) return '图片为空，请重新选择。';
  if (file.size > PHOTO_MAX_BYTES) return '图片请控制在 10MB 以内。';
  if (!PHOTO_MIME_TYPES.includes(file.type)) return '请上传 JPG、PNG 或 WebP 图片。';
  return null;
}

export function photoJobLabel(job: PhotoJob | null): string {
  if (!job) return '图片已受理，正在读取识别状态。';
  if (job.status === 'succeeded') return '配料识别已完成，可以随消息发送。';
  if (job.status === 'pending' || job.status === 'queued') return '图片已受理，正在排队。可随消息发送，结果完成后接续对话。';
  if (job.status === 'running') return '正在提取配料。可随消息发送，结果完成后接续对话。';
  if (job.status === 'failed') return job.result?.error?.message || '这次识别没有完成，请移除附件后重新选择。';
  if (job.status === 'cancelled') return '识别任务已停止，请移除附件后重新选择。';
  return '识别状态暂时无法确认，请重新读取。';
}

export function photoIsTerminal(job: PhotoJob): boolean {
  return ['succeeded', 'failed', 'cancelled'].includes(job.status);
}

export function readPhotoState(payload: Record<string, unknown>) {
  const raw = payload.photo;
  const photo = raw && typeof raw === 'object' ? raw as Record<string, unknown> : null;
  const assetId = typeof photo?.asset_id === 'string' && photo.asset_id ? photo.asset_id : null;
  return {
    present: photo !== null,
    saved: photo?.saved_original === true,
    viewable: photo?.viewable === true && assetId !== null,
    assetId,
    expiresAt: typeof photo?.expires_at === 'string' ? photo.expires_at : null,
  };
}
