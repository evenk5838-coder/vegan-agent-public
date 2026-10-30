export type Coordinate = [number, number];
export const CURRENT_RESTAURANT_SCOPE_VERSION = 'veg-admission-v3';
export type Restaurant = {
  key: string; id: string | null; name: string; address: string;
  location: Coordinate | null; distance: string | null; rating: string | null;
  cost: string | null; businessHours: string | null;
  dietLabels: unknown;
};
export type RestaurantPagination = {
  hasMore: boolean; nextPage: number | null; page: number | null; pageSize: number | null;
  reachedWindowEnd: boolean; endReason: string | null;
};
export type RestaurantData = {
  items: Restaurant[]; center: Coordinate | null; city: string | null;
  area: string | null;
  radiusM: number | null;
  mode: string; queriedAt: string | null; notice: string; scopeChecked: boolean;
  scopeText: string | null; scopeVersion: string | null; scopeCurrent: boolean; pagination: RestaurantPagination;
};

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const scalar = (value: unknown): string | null =>
  typeof value === 'string' && value.trim() ? value.trim() :
    typeof value === 'number' && Number.isFinite(value) ? String(value) : null;
const positiveInteger = (value: unknown): number | null =>
  typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;

export function parseCoordinate(value: unknown): Coordinate | null {
  if (typeof value !== 'string' || !/^\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*$/.test(value)) return null;
  const [longitude, latitude] = value.split(',').map(Number);
  return Number.isFinite(longitude) && Number.isFinite(latitude) && Math.abs(longitude) <= 180 && Math.abs(latitude) <= 90
    ? [longitude, latitude] : null;
}

export function straightLineDistance(value: unknown, mode: string, type: unknown): string | null {
  if (mode !== 'around' || type !== 'straight_line' || value === null || value === '' || typeof value === 'boolean') return null;
  const meters = typeof value === 'number' || typeof value === 'string' ? Number(value) : NaN;
  if (!Number.isFinite(meters) || meters < 0) return null;
  return meters >= 1000 ? `直线距离 ${(meters / 1000).toFixed(1)} 公里` : `直线距离 ${Math.round(meters)} 米`;
}

export function restaurantData(payload: Record<string, unknown>): RestaurantData {
  const query = record(payload.query), scope = record(payload.scope), paging = record(payload.pagination);
  const mode = scalar(query.mode) || '';
  const compatible = !query.coordinate_system || query.coordinate_system === 'GCJ-02';
  const items = Array.isArray(payload.items) ? payload.items : [];
  const scopeChecked = scope.rule === 'veg_category_evidence'
    || (items.length > 0 && items.every(item => record(record(item).scope).admitted === true));
  const scopeVersion = scalar(scope.version);
  const scopeCurrent = scope.rule === 'veg_category_evidence'
    && scopeVersion === CURRENT_RESTAURANT_SCOPE_VERSION
    && items.every(item => {
      const evidence = record(record(item).scope);
      return evidence.admitted === true && evidence.version === CURRENT_RESTAURANT_SCOPE_VERSION;
    });
  const center = compatible && mode === 'around' ? parseCoordinate(query.location) : null;
  return {
    mode, center,
    city: scalar(query.city), area: [scalar(query.city), scalar(query.keywords)].filter(Boolean).join(' · ') || null,
    radiusM: mode === 'around' ? positiveInteger(query.radius_m) : null,
    queriedAt: scalar(query.at), scopeChecked,
    scopeText: scalar(scope.text), scopeVersion, scopeCurrent,
    notice: scalar(payload.notice) || '平台类别不等于严格纯素认证，具体配料与饮食要求请向店家核对。',
    pagination: {
      hasMore: paging.has_more === true,
      nextPage: positiveInteger(paging.next_page), page: positiveInteger(paging.page),
      pageSize: positiveInteger(paging.page_size), reachedWindowEnd: paging.reached_window_end === true,
      endReason: scalar(paging.end_reason),
    },
    items: items.map((value, index) => {
      const row = record(value), id = scalar(row.id);
      return {
        key: id ? `poi-${id}` : `restaurant-${index}`, id, name: scalar(row.name) || '餐厅名称暂缺',
        address: scalar(row.address) || '地址信息暂缺',
        location: compatible ? parseCoordinate(row.location) : null,
        distance: center ? straightLineDistance(row.distance_m, mode, row.distance_type) : null,
        rating: scalar(row.rating), cost: scalar(row.cost), businessHours: scalar(row.business_hours) || scalar(row.hours),
        dietLabels: row.diet_labels,
      };
    }),
  };
}

/** Only caller-supplied area/coordinates are reused; a geocoded city is not the user's position. */
export function restaurantPageQuery(payload: Record<string, unknown>, firstPage = false): string | null {
  const query = record(payload.query), data = restaurantData(payload), paging = data.pagination;
  // A fresh first-page query replaces old results; it must never append to them.
  if (!firstPage && !data.scopeCurrent) return null;
  const page = firstPage ? 1 : paging.hasMore ? paging.nextPage : null;
  const pageSize = paging.pageSize ?? positiveInteger(query.page_size) ?? (firstPage ? 20 : null);
  if (!page || !pageSize || pageSize > 25 || page > 100 || page * pageSize > 200) return null;
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (data.mode === 'around' && data.center) params.set('location', data.center.join(','));
  else if (data.mode === 'text' && data.city) params.set('city', data.city);
  else return null;
  const keywords = scalar(query.keywords);
  if (keywords) params.set('keywords', keywords);
  if (data.mode === 'around' && data.radiusM !== null) {
    if (data.radiusM < 250 || data.radiusM > 50000) return null;
    params.set('radius_m', String(data.radiusM));
  }
  return params.toString();
}

function queryIdentity(payload: Record<string, unknown>): string {
  const data = restaurantData(payload), query = record(payload.query);
  return JSON.stringify([data.mode, data.mode === 'around' ? data.center : data.city,
    scalar(query.keywords),
    query.coordinate_system || 'GCJ-02', query.radius_m ?? null, query.sortrule ?? null]);
}

/** Aggregate only after a valid next-page response; callers keep their previous payload on error. */
export function appendRestaurantPage(current: Record<string, unknown>, incoming: Record<string, unknown>): Record<string, unknown> {
  const before = restaurantData(current), after = restaurantData(incoming);
  if (!Array.isArray(incoming.items) || !before.scopeCurrent || !after.scopeCurrent
    || before.scopeVersion !== after.scopeVersion || queryIdentity(current) !== queryIdentity(incoming)
    || !before.pagination.hasMore || after.pagination.page !== before.pagination.nextPage
    || before.pagination.pageSize !== after.pagination.pageSize) {
    throw new Error('下一页结果与当前查询不一致，已保留原有结果，请重新查询。');
  }
  const combined: unknown[] = [], ids = new Set<string>();
  for (const item of [...(Array.isArray(current.items) ? current.items : []), ...incoming.items]) {
    const id = scalar(record(item).id);
    if (id && ids.has(id)) continue;
    if (id) ids.add(id);
    combined.push(item);
  }
  return { ...incoming, items: combined };
}

/** Only an explicitly obtained one-off location matching this query is labelled as the user's position. */
export function restaurantQueryOrigin(payload: Record<string, unknown>, currentLocation?: string | null) {
  const data = restaurantData(payload);
  if (!data.scopeCurrent || !data.center) return null;
  const current = parseCoordinate(currentLocation);
  const isUserLocation = !!current && current.every((value, index) => Math.abs(value - data.center![index]) < 1e-7);
  return { position: data.center, isUserLocation, label: isUserLocation ? '你的位置（本次定位）' : '本次查询位置' };
}

/** Device distance is computed locally in GCJ-02; a map center is never labelled as the user. */
export function restaurantDistance(item: Restaurant, center: Coordinate | null, device: Coordinate | null): string | null {
  if (device && item.location) {
    const atDevice = center?.every((value, index) => Math.abs(value - device[index]) < 1e-7);
    const distance = atDevice && item.distance ? item.distance
      : straightLineDistance(haversineMeters(device, item.location), 'around', 'straight_line');
    return distance?.replace('直线距离', '距你（直线）') ?? null;
  }
  return item.distance?.replace('直线距离', '距查询中心（直线）') ?? null;
}

export const RESTAURANT_INITIAL_TARGET = 10;
export const RESTAURANT_INITIAL_MAX_PAGES = 3;

/** A superseding parent intent invalidates all work for the retained old result. */
export function restaurantWorkSignal(local: AbortSignal, parent?: AbortSignal): AbortSignal {
  return parent ? AbortSignal.any([local, parent]) : local;
}

/** Bounded sequential fill. Each successful page is exposed before another request. */
export async function fillRestaurantPages(initial: Record<string, unknown>, options: {
  signal: AbortSignal;
  fetch: (query: string, signal: AbortSignal) => Promise<Record<string, unknown>>;
  onPage?: (payload: Record<string, unknown>) => void;
}) {
  let payload = initial;
  let pages = 1;
  const requested = new Set<string>();
  while (restaurantData(payload).scopeCurrent && restaurantData(payload).items.length < RESTAURANT_INITIAL_TARGET
    && pages < RESTAURANT_INITIAL_MAX_PAGES && !options.signal.aborted) {
    const query = restaurantPageQuery(payload);
    if (!query || requested.has(query)) break;
    requested.add(query);
    try {
      const incoming = await options.fetch(query, options.signal);
      if (options.signal.aborted) break;
      payload = appendRestaurantPage(payload, incoming);
      pages++;
      options.onPage?.(payload);
    } catch (error) {
      if (options.signal.aborted) break;
      return { payload, pages, stopped: false, error: error instanceof Error ? error.message : '继续查询未完成。' };
    }
  }
  return { payload, pages, stopped: options.signal.aborted, error: null };
}

/** Map browsing is a new around query, never a replacement for device location. */
export function mapRestaurantQuery(center: unknown, radiusM = 3000): string | null {
  const point = parseCoordinate(center);
  if (!point || !Number.isSafeInteger(radiusM) || radiusM < 250 || radiusM > 50000) return null;
  return new URLSearchParams({ location: point.map(value => value.toFixed(6)).join(','), radius_m: String(radiusM), page: '1', page_size: '20' }).toString();
}

export function mapViewIdentity(center: unknown, zoom: unknown, radiusM = 3000): string | null {
  const point = parseCoordinate(center);
  if (!point || typeof zoom !== 'number' || !Number.isFinite(zoom) || !Number.isSafeInteger(radiusM) || radiusM < 250 || radiusM > 50000) return null;
  // Zoom changes affect the derived radius. At the 50km cap, an unchanged
  // center/radius is still the exact same API query and must not be repeated.
  return `${point.map(value => value.toFixed(5)).join(',')}:${radiusM}`;
}

export function haversineMeters(start: Coordinate, end: Coordinate): number {
  const rad = Math.PI / 180;
  const latitude = (end[1] - start[1]) * rad, longitude = (end[0] - start[0]) * rad;
  const a = Math.sin(latitude / 2) ** 2 + Math.cos(start[1] * rad) * Math.cos(end[1] * rad) * Math.sin(longitude / 2) ** 2;
  return 6371000 * 2 * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** Cover the farthest viewport corner, with the explicitly bounded API radius. */
export function mapSearchArea(center: unknown, southWest: unknown, northEast: unknown, minimumRadiusM = 250) {
  const point = parseCoordinate(center), sw = parseCoordinate(southWest), ne = parseCoordinate(northEast);
  if (!point || !sw || !ne || !Number.isSafeInteger(minimumRadiusM) || minimumRadiusM < 250 || minimumRadiusM > 50000) return null;
  const corners: Coordinate[] = [sw, ne, [sw[0], ne[1]], [ne[0], sw[1]]];
  const requested = Math.ceil(Math.max(...corners.map(corner => haversineMeters(point, corner))));
  return { radiusM: Math.min(50000, Math.max(minimumRadiusM, requested)), capped: requested > 50000 };
}

/** Only a real user gesture can arm a debounced viewport query. */
export function createMapViewportQuery(options: {
  read: () => { query: string; key: string } | null;
  search: (query: string, key: string) => boolean | Promise<boolean>;
  initialKey?: string | null;
  waitMs?: number;
}) {
  let armedUntil = 0, disposed = false;
  let succeededKey: string | null = options.initialKey ?? null, inflightKey: string | null = null;
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let gestureStartKey: string | null = null;
  function suppress() { armedUntil = 0; gestureStartKey = null; clearTimeout(timer); }
  function cancel() {
    suppress(); generation++;
    succeededKey = null;
    inflightKey = null;
  }
  async function request(viewport: { query: string; key: string }, force = false) {
    if (disposed || viewport.key === inflightKey || (!force && viewport.key === succeededKey)) return false;
    const ticket = ++generation;
    inflightKey = viewport.key;
    succeededKey = null;
    try {
      const succeeded = await options.search(viewport.query, viewport.key);
      if (disposed || ticket !== generation) return false;
      if (succeeded === true) succeededKey = viewport.key;
      else if (succeededKey === viewport.key) succeededKey = null;
      return succeeded === true;
    } catch {
      if (ticket === generation && succeededKey === viewport.key) succeededKey = null;
      return false;
    } finally {
      if (ticket === generation) inflightKey = null;
    }
  }
  function changed() {
    if (disposed || Date.now() > armedUntil) return;
    clearTimeout(timer);
    const viewport = options.read();
    if (!viewport || viewport.key === succeededKey || viewport.key === inflightKey) return;
    timer = setTimeout(() => {
      if (disposed) return;
      armedUntil = 0; void request(viewport);
    }, options.waitMs ?? 650);
  }
  return {
    gesture(event: { trusted: boolean; marker: boolean; type: string; key?: string }) {
      if (disposed || !event.trusted || event.marker) return;
      if (event.type === 'keydown' && !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', '+', '-', '='].includes(event.key || '')) return;
      if (['pointerdown', 'touchstart'].includes(event.type)) gestureStartKey = options.read()?.key ?? null;
      if (!['pointerup', 'touchend'].includes(event.type)) clearTimeout(timer);
      armedUntil = Date.now() + 2500;
      // Some SDKs emit moveend before the DOM release event. A long drag can
      // outlive the initial arm window; release must still query the changed view.
      if (['pointerup', 'touchend'].includes(event.type)) {
        if (gestureStartKey !== null && options.read()?.key !== gestureStartKey) changed();
        gestureStartKey = null;
      }
    },
    changed,
    request,
    suppress,
    cancel,
    dispose() { disposed = true; cancel(); },
  };
}

export function restaurantNavigation(row: Restaurant): string | undefined {
  if (!row.location) return undefined;
  const query = new URLSearchParams({ position: row.location.join(','), name: row.name, coordinate: 'gaode', callnative: '1' });
  return `https://uri.amap.com/marker?${query}`;
}
