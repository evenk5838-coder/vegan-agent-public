export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  let response: Response;
  try {
    response = await fetch('/api/v1' + path, {
      ...init, headers, credentials: 'same-origin', cache: 'no-store',
      signal: init?.signal ?? AbortSignal.timeout(20000),
    });
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    throw new ApiError('CONNECTION_LOST', '连接暂时中断，输入仍保留。请刷新状态后再试。', 0);
  }
  const result = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(result?.error?.code || 'REQUEST_FAILED', result?.error?.message || '操作没有完成，请稍后重试。', response.status);
  }
  if (result === null) throw new ApiError('INVALID_RESPONSE', '暂时无法读取结果，请刷新重试。', response.status);
  return result as T;
}

/** Authenticated, same-origin binary responses (for saved ingredient photos). */
export async function apiBlob(path: string, init?: RequestInit): Promise<Blob> {
  let response: Response;
  try {
    response = await fetch('/api/v1' + path, {
      ...init, credentials: 'same-origin', cache: 'no-store',
      signal: init?.signal ?? AbortSignal.timeout(20000),
    });
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    throw new ApiError('CONNECTION_LOST', '连接暂时中断，请稍后重新查看。', 0);
  }
  if (!response.ok) {
    const result = await response.json().catch(() => null);
    throw new ApiError(result?.error?.code || 'REQUEST_FAILED', result?.error?.message || '暂时无法读取原图。', response.status);
  }
  return response.blob();
}

export function safeExternalUrl(value: string | null | undefined): string | undefined {
  try {
    const url = new URL(value || '');
    return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
}
