import { parseCoordinate, restaurantData } from './restaurant-results';

export const RESTAURANT_DISCOVERY_KEYWORDS = ['素食', '蔬食', '素斋', '植物料理', 'vegan', 'vegetarian'] as const;
type Payload = Record<string, unknown>;
const object = (value: unknown): Payload => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Payload : {};
const text = (value: unknown): string | null => typeof value === 'string' && value.trim() ? value.trim() : null;
const integer = (value: unknown): number | null => typeof value === 'number' && Number.isSafeInteger(value) ? value : null;
const mismatch = () => new Error('补充结果与当前查询范围不一致，已保留原有餐厅。');

/** Preserve explicit legacy category searches; do not mistake old cards for the new text default. */
export function restaurantDiscoveryKeyword(payload: Payload): string | null {
  const query = object(payload.query);
  const keyword = text(query.keywords);
  if (keyword) return keyword;
  if (query.search_strategy === 'vegetarian_category' || payload.typecode === '050120') return null;
  return query.mode === 'text' ? '素食' : null;
}

/** Exact search scope, excluding only keyword / provider category and page number. */
function scope(payload: Payload) {
  const query = object(payload.query), data = restaurantData(payload);
  const size = data.pagination.pageSize;
  const page = data.pagination.page;
  if (!Array.isArray(payload.items) || !data.scopeCurrent || !size || size > 25
    || !page || page > 100 || page * size > 200
    || (query.page_size != null && integer(query.page_size) !== size)
    || (query.page != null && integer(query.page) !== page)
    || (query.coordinate_system != null && query.coordinate_system !== 'GCJ-02')) return null;
  if (query.mode === 'text') {
    const city = text(query.city);
    if (!city || city.length > 40 || query.location != null || query.radius_m != null) return null;
    let area: [string, string] | null = null;
    if (query.area_resolution != null) {
      const resolution = object(query.area_resolution);
      const adcode = text(resolution.adcode), level = text(resolution.level);
      if (!adcode || !/^\d{6}$/.test(adcode) || !level) return null;
      area = [adcode, level];
    }
    return { mode: 'text', city, size, identity: JSON.stringify(['text', city, area, size, query.sortrule ?? null, data.scopeVersion]) } as const;
  }
  if (query.mode === 'around') {
    const point = parseCoordinate(query.location), radius = integer(query.radius_m);
    if (!point || radius === null || radius < 250 || radius > 50000) return null;
    return { mode: 'around', location: point.join(','), radius, size,
      identity: JSON.stringify(['around', point, radius, size, query.sortrule ?? null, data.scopeVersion]) } as const;
  }
  return null;
}

/** One explicit keyword requests one first page. Never fans out or silently widens the area. */
export function restaurantDiscoveryQuery(payload: Payload, keyword: string): string | null {
  if (!(RESTAURANT_DISCOVERY_KEYWORDS as readonly string[]).includes(keyword)) return null;
  const current = scope(payload);
  if (!current) return null;
  const params = new URLSearchParams({ keywords: keyword, page: '1', page_size: String(current.size) });
  if (current.mode === 'text') params.set('city', current.city);
  else { params.set('location', current.location); params.set('radius_m', String(current.radius)); }
  return params.toString();
}

/** Call before switching the active channel; malformed or drifted replies leave state untouched. */
export function assertRestaurantDiscoveryResponse(current: Payload, incoming: Payload, keyword: string): void {
  const before = scope(current), after = scope(incoming);
  if (!restaurantDiscoveryQuery(current, keyword) || !before || !after || before.identity !== after.identity
    || text(object(incoming.query).keywords) !== keyword || restaurantData(incoming).pagination.page !== 1) throw mismatch();
}

/**
 * Each channel owns its cumulative pages. Replace that channel after appendRestaurantPage,
 * then merge snapshots for display only. The returned pagination describes the active
 * channel, not a new combined provider total. Unknown IDs stay distinct across channels.
 */
export function mergeRestaurantDiscoveryChannels(channels: Payload[], activePayload: Payload): Payload {
  const activeScope = scope(activePayload);
  if (!activeScope) throw mismatch();
  const latest = new Map<string | null, Payload>();
  for (const payload of [...channels, activePayload]) {
    if (scope(payload)?.identity !== activeScope.identity) throw mismatch();
    latest.set(restaurantDiscoveryKeyword(payload), payload);
  }
  const items: Payload[] = [], positions = new Map<string, number>();
  for (const payload of latest.values()) {
    for (const item of payload.items as unknown[]) {
      const row = object(item), id = text(row.id);
      const normalized = { ...row, id };
      const at = id ? positions.get(id) : undefined;
      if (at !== undefined) items[at] = normalized;
      else { if (id) positions.set(id, items.length); items.push(normalized); }
    }
  }
  // Prefer the freshest active record without moving its position or repeating
  // its unknown-ID rows, which already appeared once in its channel snapshot.
  for (const item of activePayload.items as unknown[]) {
    const row = object(item), id = text(row.id), at = id ? positions.get(id) : undefined;
    if (at !== undefined) items[at] = { ...row, id };
  }
  return { ...activePayload, items };
}
