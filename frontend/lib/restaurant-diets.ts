export const RESTAURANT_DIET_VERSION = 'restaurant-diets-2026-10-30.1';
export const RESTAURANT_DIETS = [
  { key: 'pure', label: '纯净素' },
  { key: 'vegan', label: 'Vegan' },
  { key: 'lacto_ovo', label: '蛋奶素' },
] as const;
export type RestaurantDietKey = typeof RESTAURANT_DIETS[number]['key'];
export type RestaurantDietFilter = 'all' | RestaurantDietKey | 'unknown';
export type RestaurantDietSource = { title: string; href: string; reviewedAt: string; expiresAt: string; current: boolean };
export type RestaurantDietLabel = {
  key: RestaurantDietKey; label: string; status: 'supported' | 'on_request' | 'unknown';
  statusText: string; basis: string; sources: RestaurantDietSource[];
};

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown): string => typeof value === 'string' ? value.trim() : '';

function calendarDate(value: unknown): string | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? value : null;
}

function shanghaiDay(now: Date): string | null {
  if (!Number.isFinite(now.getTime())) return null;
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(now);
  return ['year', 'month', 'day'].map(type => parts.find(part => part.type === type)?.value).join('-');
}

function source(value: unknown, today: string | null): RestaurantDietSource | null {
  const raw = record(value), reviewedAt = calendarDate(raw.reviewed_at), expiresAt = calendarDate(raw.expires_at);
  const title = text(raw.title);
  if (!title || !reviewedAt || !expiresAt || expiresAt <= reviewedAt) return null;
  try {
    const url = new URL(text(raw.source));
    if (url.protocol !== 'https:' || !url.hostname || url.username || url.password) return null;
    return { title, href: url.href, reviewedAt, expiresAt, current: !!today && reviewedAt <= today && today < expiresAt };
  } catch { return null; }
}

/** These are separately sourced menu claims, never an inference from a POI name or category. */
export function restaurantDietLabels(value: unknown, now = new Date()): { labels: RestaurantDietLabel[]; hasEvidence: boolean } {
  const raw = record(value), claims = record(raw.labels), today = shanghaiDay(now);
  const compatible = raw.kind === 'menu_availability' && raw.version === RESTAURANT_DIET_VERSION;
  const labels: RestaurantDietLabel[] = RESTAURANT_DIETS.map(({ key, label }) => {
    const claim = record(claims[key]);
    const rawSources = Array.isArray(claim.sources) ? claim.sources : [];
    const sources = compatible ? rawSources.map(item => source(item, today)).filter((item): item is RestaurantDietSource => !!item) : [];
    const basis = text(claim.basis);
    const supported = compatible && basis && rawSources.length > 0 && sources.length === rawSources.length && sources.every(item => item.current);
    const status = supported && (claim.status === 'supported' || claim.status === 'on_request') ? claim.status : 'unknown';
    const stale = compatible && sources.some(item => !item.current);
    return {
      key, label, status,
      statusText: status === 'supported' ? '有菜单依据' : status === 'on_request' ? '需提前说明' : '待确认',
      basis: status !== 'unknown' ? basis : stale ? '这份资料不在有效核对期内，请向店家确认。' : '暂无足够的本店菜单资料，待向店家确认。',
      sources,
    };
  });
  return { labels, hasEvidence: labels.some(label => label.status !== 'unknown') };
}

/** Filters only the loaded candidates. An unknown label is not a rejection. */
export function restaurantMatchesDiet(value: unknown, filter: RestaurantDietFilter, now = new Date()): boolean {
  if (filter === 'all') return true;
  const data = restaurantDietLabels(value, now);
  return filter === 'unknown' ? !data.hasEvidence : data.labels.some(label => label.key === filter && label.status !== 'unknown');
}
