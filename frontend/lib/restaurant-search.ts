export const DEFAULT_NEARBY_RADIUS_M = 10000;
export const NEARBY_RADIUS_OPTIONS = [3000, 5000, 10000, 20000, 50000] as const;
export type RestaurantSearchIntent = { mode: 'city'; city: string } | { mode: 'nearby'; radiusM?: number };
export type RestaurantSearchPhase =
  | { kind: 'locating' }
  | { kind: 'searching'; mode: 'city'; city: string }
  | { kind: 'searching'; mode: 'nearby' };

export class RestaurantSearchError extends Error {
  constructor(public phase: 'locating' | 'searching', message: string) {
    super(message);
    this.name = 'RestaurantSearchError';
  }
}

function ensureActive(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException('本次等待已停止。', 'AbortError');
}

function usableLocation(value: string) {
  const parts = value.split(',').map(part => part.trim());
  if (parts.length !== 2 || parts.some(part => !part)) return false;
  const [longitude, latitude] = parts.map(Number);
  return Number.isFinite(longitude) && Number.isFinite(latitude)
    && longitude >= -180 && longitude <= 180 && latitude >= -90 && latitude <= 90;
}

/** Location permission and the restaurant query are distinct, cancellable stages.
 * A provider may finish locating after cancellation; that must never start a query.
 */
export async function runRestaurantSearch<T>(intent: RestaurantSearchIntent, signal: AbortSignal, operations: {
  locate: () => Promise<string>;
  query: (query: Record<string, string>, signal: AbortSignal) => Promise<T>;
  onPhase: (phase: RestaurantSearchPhase) => void;
}): Promise<{ payload: T; currentLocation: string | null }> {
  ensureActive(signal);
  let currentLocation: string | null = null;
  let query: Record<string, string>;
  if (intent.mode === 'nearby') {
    const radius = intent.radiusM ?? DEFAULT_NEARBY_RADIUS_M;
    if (!Number.isSafeInteger(radius) || radius < 250 || radius > 50000)
      throw new RestaurantSearchError('searching', '请选取有效的附近查询范围。');
    operations.onPhase({ kind: 'locating' });
    try {
      currentLocation = await operations.locate();
      ensureActive(signal);
      if (!usableLocation(currentLocation)) throw new Error('未取得有效的本次位置。');
    } catch (reason) {
      ensureActive(signal);
      throw new RestaurantSearchError('locating', reason instanceof Error ? reason.message : '未能获取本次位置。');
    }
    query = { location: currentLocation, radius_m: String(radius) };
    operations.onPhase({ kind: 'searching', mode: 'nearby' });
  } else {
    const city = intent.city.trim();
    if (!city) throw new RestaurantSearchError('searching', '请先填写想查询的城市或区县。');
    query = { city };
    operations.onPhase({ kind: 'searching', mode: 'city', city });
  }
  ensureActive(signal);
  try {
    const payload = await operations.query(query, signal);
    ensureActive(signal);
    return { payload, currentLocation };
  } catch (reason) {
    ensureActive(signal);
    throw new RestaurantSearchError('searching', reason instanceof Error ? reason.message : '餐厅查询未完成，请稍后重试。');
  }
}
