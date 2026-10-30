'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, ExternalLink, Leaf, LoaderCircle, LocateFixed, MapPin, RefreshCw, X } from 'lucide-react';
import { api } from '../lib/api';
import { DEFAULT_NEARBY_RADIUS_M } from '../lib/restaurant-search';
import { loadAMapSDK, mapConfigurationReady, requestCurrentMapLocation, type AMapInstance, type AMapMarker, type MapConfiguration } from '../lib/amap-sdk';
import { appendRestaurantPage, createMapViewportQuery, fillRestaurantPages, mapRestaurantQuery, mapSearchArea, mapViewIdentity, parseCoordinate, restaurantData, restaurantDistance, restaurantNavigation, restaurantPageQuery, restaurantQueryOrigin, restaurantWorkSignal } from '../lib/restaurant-results';
import { RESTAURANT_DISCOVERY_KEYWORDS, restaurantDiscoveryKeyword, restaurantDiscoveryQuery, assertRestaurantDiscoveryResponse, mergeRestaurantDiscoveryChannels } from '../lib/restaurant-discovery';
import { restaurantMatchesDiet, type RestaurantDietFilter } from '../lib/restaurant-diets';
import RestaurantDietLabels from './RestaurantDietLabels';
import styles from './restaurant-results.module.css';

type MarkerEntry = { marker: AMapMarker; element: HTMLButtonElement; onClick: () => void };
type MapState = 'idle' | 'loading' | 'ready' | 'unavailable' | 'failed';
type MapPoint = { getLng: () => number; getLat: () => number };
type InteractiveMap = AMapInstance & {
  getCenter?: () => MapPoint; getZoom?: () => number;
  getBounds?: () => { getSouthWest: () => MapPoint; getNorthEast: () => MapPoint };
};
type Props = { payload: Record<string, unknown>; compact?: boolean; currentLocation?: string | null; autoFill?: boolean; suspendSignal?: AbortSignal; initialMapOpen?: boolean; initialSelectedKey?: string };
async function queryRestaurants(query: string, signal: AbortSignal) {
  return api<Record<string, unknown>>('/restaurants?' + query, { signal: AbortSignal.any([signal, AbortSignal.timeout(20000)]) });
}
function validPayload(value: Record<string, unknown>) {
  if (!Array.isArray(value.items) || !restaurantData(value).scopeCurrent) throw new Error('查询结果未通过当前筛选版本校验，已保留原有结果。');
  return value;
}
function currentViewport(instance: InteractiveMap | null, minimumRadiusM: number, keyword: string | null = null) {
  if (!instance?.getCenter || !instance.getZoom || !instance.getBounds) return null;
  try {
    const center = instance.getCenter(), bounds = instance.getBounds();
    if (!bounds?.getSouthWest || !bounds.getNorthEast) return null;
    const sw = bounds.getSouthWest(), ne = bounds.getNorthEast();
    const point = `${center.getLng()},${center.getLat()}`;
    const area = mapSearchArea(point, `${sw.getLng()},${sw.getLat()}`, `${ne.getLng()},${ne.getLat()}`, minimumRadiusM);
    if (!area) return null;
    const query = mapRestaurantQuery(point, area.radiusM), key = mapViewIdentity(point, instance.getZoom(), area.radiusM);
    if (!query || !key) return null;
    const params = new URLSearchParams(query);
    const identityKeyword = keyword && (RESTAURANT_DISCOVERY_KEYWORDS as readonly string[]).includes(keyword) ? keyword : null;
    if (identityKeyword) params.set('keywords', identityKeyword);
    return { query: params.toString(), key: `${key}:${identityKeyword ?? 'category'}` };
  } catch { return null; }
}

export default function RestaurantResults({ payload, compact = false, currentLocation, autoFill = false, suspendSignal, initialMapOpen = false, initialSelectedKey }: Props) {
  return <RestaurantResultBody key={JSON.stringify([payload, currentLocation, initialMapOpen, initialSelectedKey])} initialPayload={payload} compact={compact} currentLocation={currentLocation} autoFill={autoFill} suspendSignal={suspendSignal} initialMapOpen={initialMapOpen} initialSelectedKey={initialSelectedKey}/>;
}

function RestaurantResultBody({ initialPayload, compact, currentLocation, autoFill, suspendSignal, initialMapOpen, initialSelectedKey }: { initialPayload: Record<string, unknown>; compact: boolean; currentLocation?: string | null; autoFill: boolean; suspendSignal?: AbortSignal; initialMapOpen: boolean; initialSelectedKey?: string }) {
  const [payload, setPayload] = useState(initialPayload);
  const [displayPayload, setDisplayPayload] = useState(initialPayload);
  const payloadRef = useRef(initialPayload);
  const channels = useRef(new Map([[restaurantDiscoveryKeyword(initialPayload), initialPayload]]));
  const [queriedKeywords, setQueriedKeywords] = useState(() => [restaurantDiscoveryKeyword(initialPayload)]);
  const data = useMemo(() => restaurantData(displayPayload), [displayPayload]);
  const [dietFilter, setDietFilter] = useState<RestaurantDietFilter>('all');
  const [dietNow, setDietNow] = useState(() => new Date());
  const filteredItems = useMemo(() => data.items.filter(item => restaurantMatchesDiet(item.dietLabels, dietFilter, dietNow)), [data.items, dietFilter, dietNow]);
  const filteredData = useMemo(() => ({ ...data, items: filteredItems }), [data, filteredItems]);
  const [discoveryKeyword, setDiscoveryKeyword] = useState('蔬食');
  const activeKeyword = restaurantDiscoveryKeyword(payload);
  const origin = useMemo(() => restaurantQueryOrigin(payload, currentLocation), [payload, currentLocation]);
  const [device, setDevice] = useState(() => {
    const initial = restaurantQueryOrigin(initialPayload, currentLocation);
    return initial?.isUserLocation ? initial.position : null;
  });
  const deviceRef = useRef(device);
  const [locating, setLocating] = useState(false);
  const [locationError, setLocationError] = useState('');
  const locationRequest = useRef<AbortController | null>(null);
  const dataRef = useRef(filteredData), originRef = useRef(origin);
  const [expanded, setExpanded] = useState(!!initialSelectedKey);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [mapRange, setMapRange] = useState(false);
  const [pageBusy, setPageBusy] = useState<'more' | 'refresh' | 'auto' | 'range' | 'supplement' | null>(null);
  const [pageError, setPageError] = useState('');
  const [pageNotice, setPageNotice] = useState('');
  const [suspended, setSuspended] = useState(suspendSignal?.aborted === true);
  const pageRequest = useRef<AbortController | null>(null);
  const mounted = useRef(true), autoStarted = useRef(false), autoMapStarted = useRef(false);
  const collapsedLimit = compact ? 3 : 10;
  const visibleItems = expanded ? filteredItems : filteredItems.slice(0, collapsedLimit);
  const showItems = data.scopeCurrent || historyOpen;
  const nextQuery = restaurantPageQuery(payload), refreshQuery = restaurantPageQuery(payload, true);
  const [selected, setSelected] = useState<string | null>(initialSelectedKey ?? null);
  const [mapState, setMapState] = useState<MapState>('idle');
  const [mapNotice, setMapNotice] = useState('');
  const [configuration, setConfiguration] = useState<MapConfiguration | null>(null);
  const [sdkAttempt, setSdkAttempt] = useState(0);
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<InteractiveMap | null>(null);
  const markers = useRef<Map<string, MarkerEntry>>(new Map());
  const rows = useRef<Map<string, HTMLDivElement>>(new Map());
  const request = useRef<AbortController | null>(null);
  const selectedRef = useRef<string | null>(initialSelectedKey ?? null);
  const renderMarkers = useRef<(() => void) | null>(null);
  const viewportGate = useRef<ReturnType<typeof createMapViewportQuery> | null>(null);
  const validCount = filteredItems.filter(item => item.location).length;
  const hasMapPoints = validCount > 0 || !!origin;
  const paused = suspended || suspendSignal?.aborted === true;
  const radius = data.radiusM ?? 3000;
  // Nearby keeps the selected floor; city results start with a 10km map floor.
  // Zooming in to inspect streets must not silently narrow the restaurant search.
  const minimumRadiusM = Math.max(3000, Math.min(50000, restaurantData(initialPayload).radiusM ?? DEFAULT_NEARBY_RADIUS_M));
  const radiusLabel = radius < 1000 ? `${radius} 米` : `约 ${(radius / 1000).toFixed(1).replace(/\.0$/, '')} 公里`;

  const commit = useCallback((next: Record<string, unknown>, reset = false) => {
    const updated = reset ? new Map() : new Map(channels.current);
    updated.set(restaurantDiscoveryKeyword(next), next);
    const combined = updated.size === 1 ? next : mergeRestaurantDiscoveryChannels([...updated.values()], next);
    // Missing POI ids have positional display keys; adding a channel/page can
    // shift them. Never transfer a selection to a different unnamed identity.
    if (selectedRef.current?.startsWith('restaurant-')) { selectedRef.current = null; setSelected(null); }
    channels.current = updated; setQueriedKeywords([...updated.keys()]); payloadRef.current = next; setPayload(next); setDisplayPayload(combined);
  }, []);
  useEffect(() => { dataRef.current = filteredData; originRef.current = origin; deviceRef.current = device; renderMarkers.current?.(); }, [filteredData, origin, device]);
  useEffect(() => {
    const refresh = () => setDietNow(new Date());
    const timer = setInterval(refresh, 60000);
    document.addEventListener('visibilitychange', refresh);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; request.current?.abort(); pageRequest.current?.abort(); locationRequest.current?.abort(); viewportGate.current?.dispose(); };
  }, []);
  useEffect(() => {
    if (!suspendSignal) return;
    const stopOldIntent = () => {
      pageRequest.current?.abort(); request.current?.abort(); locationRequest.current?.abort(); viewportGate.current?.cancel();
      setLocating(false);
      setSuspended(true); setPageBusy(null);
      setMapState(previous => previous === 'loading' && !map.current ? 'idle' : previous);
      setPageNotice('上一次查询已停止，已加载的餐厅仍保留。请在上方重新查询。');
    };
    if (suspendSignal.aborted) { void Promise.resolve().then(() => { if (mounted.current) stopOldIntent(); }); }
    else suspendSignal.addEventListener('abort', stopOldIntent, { once: true });
    return () => suspendSignal.removeEventListener('abort', stopOldIntent);
  }, [suspendSignal]);

  const fillInitial = useCallback(async () => {
    if (suspendSignal?.aborted || pageRequest.current || !restaurantData(payloadRef.current).scopeCurrent) return;
    const controller = new AbortController(); pageRequest.current = controller;
    const signal = restaurantWorkSignal(controller.signal, suspendSignal);
    setPageBusy('auto'); setPageError(''); setPageNotice('正在补充首屏结果，最多查询 3 页…');
    try {
      const filled = await fillRestaurantPages(payloadRef.current, { signal, fetch: queryRestaurants,
        onPage: next => { if (mounted.current && pageRequest.current === controller && !signal.aborted) commit(next); } });
      if (!mounted.current || signal.aborted || pageRequest.current !== controller) return;
      if (filled.error) setPageError(filled.error);
      setPageNotice('');
    } finally {
      if (pageRequest.current === controller) { pageRequest.current = null; if (mounted.current) setPageBusy(null); }
    }
  }, [commit, suspendSignal]);
  useEffect(() => {
    if (!autoFill) return;
    const timer = setTimeout(() => { if (!autoStarted.current) { autoStarted.current = true; void fillInitial(); } }, 0);
    return () => clearTimeout(timer);
  }, [autoFill, fillInitial]);

  function stopPages() {
    viewportGate.current?.cancel();
    pageRequest.current?.abort(); pageRequest.current = null; setPageBusy(null);
    setPageNotice('已停止继续查询，已加载的餐厅仍保留。');
  }
  function suppressMapQuery() { viewportGate.current?.suppress(); }

  async function fetchPage(firstPage = false) {
    const query = restaurantPageQuery(payloadRef.current, firstPage);
    if (suspendSignal?.aborted || !query || pageRequest.current) return;
    const controller = new AbortController(); pageRequest.current = controller;
    const signal = restaurantWorkSignal(controller.signal, suspendSignal);
    setPageBusy(firstPage ? 'refresh' : 'more'); setPageError(''); setPageNotice('');
    try {
      const incoming = validPayload(await queryRestaurants(query, signal));
      if (!mounted.current || signal.aborted || pageRequest.current !== controller) return;
      commit(firstPage ? incoming : appendRestaurantPage(payloadRef.current, incoming), firstPage);
      if (firstPage) { setHistoryOpen(false); setExpanded(false); setSelected(null); selectedRef.current = null; }
      else setExpanded(true);
    } catch (error) {
      if (mounted.current && !signal.aborted && pageRequest.current === controller) setPageError(error instanceof Error ? error.message : '查询未完成。');
    } finally {
      if (pageRequest.current === controller) { pageRequest.current = null; if (mounted.current) setPageBusy(null); }
    }
  }

  async function supplement() {
    if (paused || pageRequest.current) return;
    const base = payloadRef.current;
    const query = restaurantDiscoveryQuery(base, discoveryKeyword);
    if (!query) return;
    suppressMapQuery();
    const cached = channels.current.get(discoveryKeyword);
    if (cached) {
      commit(cached); setPageError('');
      setPageNotice(`已切换到“${discoveryKeyword}”的已查结果，可继续加载后续页。`);
      return;
    }
    const controller = new AbortController(); pageRequest.current = controller;
    const signal = restaurantWorkSignal(controller.signal, suspendSignal);
    const keyword = discoveryKeyword;
    setPageBusy('supplement'); setPageError(''); setPageNotice('');
    try {
      const incoming = validPayload(await queryRestaurants(query, signal));
      if (!mounted.current || signal.aborted || pageRequest.current !== controller) return;
      assertRestaurantDiscoveryResponse(base, incoming, keyword);
      const before = restaurantData(displayPayload).items.length;
      const combined = mergeRestaurantDiscoveryChannels([...channels.current.values(), incoming], incoming);
      commit(incoming); setExpanded(true);
      setPageNotice(`“${keyword}”本页新增 ${restaurantData(combined).items.length - before} 家，已与原结果合并。${restaurantData(incoming).pagination.hasMore ? '可继续加载。' : '该关键词已查完。'}`);
    } catch (error) {
      if (mounted.current && !signal.aborted && pageRequest.current === controller) setPageError(error instanceof Error ? error.message : '补充查询未完成。');
    } finally {
      if (pageRequest.current === controller) { pageRequest.current = null; if (mounted.current) setPageBusy(null); }
    }
  }

  const searchMap = useCallback(async (query: string) => {
    if (suspendSignal?.aborted) return false;
    pageRequest.current?.abort();
    const controller = new AbortController(); pageRequest.current = controller;
    const signal = restaurantWorkSignal(controller.signal, suspendSignal);
    setPageBusy('range'); setPageError(''); setPageNotice('正在查询地图中心附近；下方暂时保留原结果。');
    try {
      const first = validPayload(await queryRestaurants(query, signal));
      if (!mounted.current || signal.aborted || pageRequest.current !== controller) return false;
      commit(first, true); setMapRange(true); setSelected(null); selectedRef.current = null; setExpanded(false);
      const filled = await fillRestaurantPages(first, { signal, fetch: queryRestaurants,
        onPage: next => { if (mounted.current && pageRequest.current === controller && !signal.aborted) commit(next); } });
      if (!mounted.current || signal.aborted || pageRequest.current !== controller) return false;
      if (filled.error) setPageError(filled.error);
      setPageNotice('');
      return !filled.error;
    } catch (error) {
      if (mounted.current && !signal.aborted && pageRequest.current === controller) setPageError(error instanceof Error ? error.message : '地图范围查询未完成。');
      return false;
    } finally {
      if (pageRequest.current === controller) { pageRequest.current = null; if (mounted.current) setPageBusy(null); }
    }
  }, [commit, suspendSignal]);

  async function showDeviceDistance() {
    if (suspendSignal?.aborted || locationRequest.current) return;
    const controller = new AbortController(); locationRequest.current = controller;
    setLocating(true); setLocationError('');
    try {
      const point = parseCoordinate(await requestCurrentMapLocation());
      if (!mounted.current || controller.signal.aborted || suspendSignal?.aborted) return;
      if (!point) throw new Error('暂未取得可用位置，请重试。');
      setDevice(point);
    } catch (error) {
      if (mounted.current && !controller.signal.aborted && !suspendSignal?.aborted) setLocationError(error instanceof Error ? error.message : '定位未完成，餐厅结果仍保留。');
    } finally {
      if (locationRequest.current === controller) { locationRequest.current = null; if (mounted.current) setLocating(false); }
    }
  }

  const loadMap = useCallback(async () => {
    if (suspendSignal?.aborted || request.current || !data.scopeCurrent) return;
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 15000);
    request.current = controller; setMapState('loading'); setMapNotice('正在检查地图配置…');
    try {
      const config = await api<MapConfiguration>('/map/config', { signal: controller.signal });
      if (!mounted.current || controller.signal.aborted) return;
      setConfiguration(config);
      if (!mapConfigurationReady(config, window.location.origin)) { setMapState('unavailable'); setMapNotice('页内地图尚未配置完成，列表仍可使用。'); return; }
      setMapNotice('正在加载地图…'); setSdkAttempt(value => value + 1);
    } catch (error) {
      if (mounted.current && !suspendSignal?.aborted && request.current === controller) { setMapState('failed'); setMapNotice(controller.signal.aborted ? '地图配置响应超时，列表仍可使用。' : error instanceof Error ? error.message : '地图暂时不可用。'); }
    } finally { clearTimeout(timeout); if (request.current === controller) request.current = null; }
  }, [suspendSignal, data.scopeCurrent]);

  useEffect(() => {
    if (!initialMapOpen || !hasMapPoints) return;
    // Deferred start lets Strict Mode cancel its probe before requesting the SDK.
    const timer = setTimeout(() => { if (!autoMapStarted.current) { autoMapStarted.current = true; void loadMap(); } }, 0);
    return () => clearTimeout(timer);
  }, [initialMapOpen, hasMapPoints, loadMap]);

  useEffect(() => {
    if (!sdkAttempt || !configuration || !container.current) return;
    let disposed = false;
    let instance: InteractiveMap | null = null;
    let complete: (() => void) | null = null;
    let mapTimer: ReturnType<typeof setTimeout> | undefined;
    let deviceMarker: AMapMarker | null = null, queryMarker: AMapMarker | null = null;
    const entries = new Map<string, MarkerEntry>();
    const eventHandlers: [string, () => void][] = [];
    const element = container.current;
    const gate = createMapViewportQuery({
      // The displayed around result already covers this center/radius. Merely
      // zooming in should not repeat its first page and spend another call.
      initialKey: originRef.current ? `${mapViewIdentity(originRef.current.position.join(','), 0, dataRef.current.radiusM ?? 3000)}:${restaurantDiscoveryKeyword(payloadRef.current) ?? 'category'}` : null,
      read: () => suspendSignal?.aborted ? null : currentViewport(instance, minimumRadiusM, restaurantDiscoveryKeyword(payloadRef.current)),
      search: query => searchMap(query),
    });
    viewportGate.current = gate;
    const armGesture = (event: Event) => {
      gate.gesture({ trusted: event.isTrusted, type: event.type,
        key: event instanceof KeyboardEvent ? event.key : undefined,
        marker: event.target instanceof Element && !!event.target.closest('[data-shushi-map-marker]') });
    };
    const viewportChanged = () => gate.changed();
    function clearMarkers() {
      entries.forEach(entry => { entry.marker.off('click', entry.onClick); entry.marker.setMap(null); }); entries.clear();
      deviceMarker?.setMap(null); queryMarker?.setMap(null); deviceMarker = null; queryMarker = null;
    }
    function disposeMap() {
      clearTimeout(mapTimer); gate.dispose();
      if (viewportGate.current === gate) viewportGate.current = null;
      if (instance && complete) instance.off('complete', complete);
      eventHandlers.forEach(([name, callback]) => instance?.off(name, callback));
      ['pointerdown', 'pointerup', 'touchstart', 'touchend', 'wheel', 'keydown'].forEach(name => element.removeEventListener(name, armGesture, true));
      clearMarkers(); renderMarkers.current = null;
      if (map.current === instance) { map.current = null; markers.current = new Map(); }
      instance?.destroy(); instance = null;
    }
    void loadAMapSDK(configuration).then(sdk => {
      if (disposed || suspendSignal?.aborted) return;
      const first = dataRef.current;
      setMapState('loading'); setMapNotice('正在显示餐厅位置…');
      instance = new sdk.Map(element, { viewMode: '2D', zoom: 14, center: first.center || first.items.find(item => item.location)?.location, resizeEnable: true }) as InteractiveMap;
      map.current = instance;
      function replaceMarkers(fit = false) {
        if (!instance) return;
        clearMarkers();
        const current = dataRef.current;
        current.items.forEach((item, index) => {
          if (!item.location) return;
          const markerElement = document.createElement('button');
          markerElement.type = 'button'; markerElement.className = styles.marker; markerElement.dataset.shushiMapMarker = 'true';
          markerElement.textContent = String(index + 1); markerElement.title = item.name; markerElement.setAttribute('aria-label', `在列表查看${item.name}`);
          if (selectedRef.current === item.key) markerElement.classList.add(styles.activeMarker);
          const marker = new sdk.Marker({ position: item.location, title: item.name, content: markerElement });
          const onClick = () => {
            gate.suppress();
            selectedRef.current = item.key; setSelected(item.key); setExpanded(true);
            requestAnimationFrame(() => { rows.current.get(item.key)?.scrollIntoView({ block: 'nearest', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' }); });
          };
          marker.on('click', onClick); entries.set(item.key, { marker, element: markerElement, onClick });
        });
        const allMarkers = [...entries.values()].map(entry => entry.marker);
        const devicePosition = deviceRef.current;
        if (devicePosition) {
          const dot = document.createElement('div'); dot.className = styles.originMarker; dot.textContent = '你'; dot.title = '你的位置（本次定位）'; dot.dataset.shushiMapMarker = 'true';
          deviceMarker = new sdk.Marker({ position: devicePosition, title: dot.title, content: dot, zIndex: 200 }); allMarkers.push(deviceMarker);
        }
        const queryOrigin = originRef.current;
        if (queryOrigin && (!devicePosition || devicePosition.some((value, index) => Math.abs(value - queryOrigin.position[index]) > 1e-7))) {
          const dot = document.createElement('div'); dot.className = styles.queryMarker; dot.textContent = '查'; dot.title = '本次查询中心'; dot.dataset.shushiMapMarker = 'true';
          queryMarker = new sdk.Marker({ position: queryOrigin.position, title: dot.title, content: dot, zIndex: 190 }); allMarkers.push(queryMarker);
        }
        instance.add(allMarkers); markers.current = entries;
        if (fit) { gate.suppress(); instance.setFitView(allMarkers, true, [35, 35, 35, 35], 16); }
      }
      renderMarkers.current = () => replaceMarkers(false);
      replaceMarkers(true);
      const selectedItem = first.items.find(item => item.key === selectedRef.current);
      if (selectedItem?.location) { gate.suppress(); instance.setCenter(selectedItem.location); }
      complete = () => {
        if (disposed) return;
        clearTimeout(mapTimer); setMapState('ready'); setMapNotice('');
        if (instance && complete) instance.off('complete', complete);
      };
      mapTimer = setTimeout(() => { if (!disposed) { disposeMap(); setMapState('failed'); setMapNotice('地图响应超时，列表仍可使用。'); } }, 15000);
      instance.on('complete', complete);
      ['moveend', 'zoomend'].forEach(name => { instance!.on(name, viewportChanged); eventHandlers.push([name, viewportChanged]); });
      // Arm before the SDK processes the gesture and emits moveend/zoomend.
      ['pointerdown', 'pointerup', 'touchstart', 'touchend', 'wheel', 'keydown'].forEach(name => element.addEventListener(name, armGesture, { passive: true, capture: true }));
    }).catch(error => { if (!disposed) { disposeMap(); setMapState('failed'); setMapNotice(error instanceof Error ? error.message : '地图加载失败，列表仍可使用。'); } });
    return () => { disposed = true; disposeMap(); };
  }, [configuration, sdkAttempt, searchMap, suspendSignal, minimumRadiusM]);

  useEffect(() => { markers.current.forEach((entry, key) => entry.element.classList.toggle(styles.activeMarker, key === selected)); }, [selected, data]);
  function choose(key: string) {
    suppressMapQuery(); selectedRef.current = key; setSelected(key);
    const item = data.items.find(item => item.key === key); if (item?.location) map.current?.setCenter(item.location);
  }
  function filterByDiet(value: RestaurantDietFilter) {
    suppressMapQuery(); setDietFilter(value); setExpanded(false); setSelected(null); selectedRef.current = null;
  }
  function searchVisibleCenter() {
    const viewport = currentViewport(map.current, minimumRadiusM, restaurantDiscoveryKeyword(payloadRef.current));
    suppressMapQuery();
    if (viewport && !paused) void viewportGate.current?.request(viewport, true);
    else setMapNotice('暂时无法读取地图范围，请重新加载地图。');
  }

  return <section className={`${styles.root} ${compact ? styles.compact : ''}`} aria-label="餐厅查询结果">
    <div className={styles.heading}><span className={styles.headingIcon}><Leaf size={17}/></span><div><h3>{data.scopeCurrent ? '餐厅查询结果' : '旧规则餐厅记录'}</h3>
      <p className={styles.summary}>{mapRange ? `地图中心附近 ${radiusLabel}` : data.mode === 'around' ? `${origin?.isUserLocation ? '你的位置' : '查询位置'}附近 ${radiusLabel}` : data.city ? `${data.city}区域` : '本次查询'}{data.scopeCurrent ? ` · 已加载 ${data.items.length} 家${data.pagination.hasMore ? '，还可继续查' : ''}` : ''}</p></div></div>
    {!data.scopeCurrent && <div className={styles.historyNotice}><p>这批记录未采用当前筛选规则，仅保留作历史。请重新查询后再选择餐厅。</p><div className={styles.actions}>
      {refreshQuery && <button type="button" onClick={() => void fetchPage(true)} disabled={!!pageBusy || paused}><RefreshCw size={14}/>按当前规则重新查询</button>}
      <button type="button" onClick={() => setHistoryOpen(value => !value)} aria-expanded={historyOpen}>{historyOpen ? '收起历史记录' : '查看历史记录'}</button></div></div>}
    {data.scopeCurrent && <>
      {!compact && <div className={styles.dietFilters} aria-label="筛选已加载餐厅">
        {([['all', '全部'], ['pure', '纯净素'], ['vegan', 'Vegan'], ['lacto_ovo', '蛋奶素'], ['unknown', '待确认']] as const).map(([key, label]) => <button key={key} type="button" aria-pressed={dietFilter === key} onClick={() => filterByDiet(key)}>{label}<span>{data.items.filter(item => restaurantMatchesDiet(item.dietLabels, key, dietNow)).length}</span></button>)}
      </div>}
      {dietFilter !== 'all' && <p className={styles.mapNotice} role="status">已加载 {data.items.length} 家中，当前显示 {filteredItems.length} 家。只筛选已加载结果，不额外查询；地图同步显示。</p>}
      <div className={styles.mapSection}><div className={styles.mapActions}>
      <button type="button" onClick={() => { suppressMapQuery(); if (mapState === 'ready') map.current?.setFitView(undefined, true, [35, 35, 35, 35], 16); else void loadMap(); }} disabled={!hasMapPoints || mapState === 'loading' || (paused && mapState !== 'ready')}>
        {mapState === 'loading' ? <LoaderCircle size={14} className="spin"/> : <MapPin size={14}/>}{!hasMapPoints ? '暂无可用地图坐标' : mapState === 'loading' ? '地图加载中' : mapState === 'ready' ? dietFilter === 'all' ? '查看全部位置' : '查看筛选位置' : mapState === 'idle' ? '加载页内地图' : '重试地图'}</button>
      {mapState === 'ready' && <button type="button" onClick={searchVisibleCenter} disabled={!!pageBusy || paused}><RefreshCw size={14}/>查此地图位置</button>}
      <button type="button" onClick={() => void showDeviceDistance()} disabled={locating || paused || !validCount}>{locating ? <LoaderCircle size={14} className="spin"/> : <LocateFixed size={14}/>}{locating ? '正在定位…' : device ? '更新我的距离' : '显示距我距离'}</button>
      {nextQuery && <button type="button" onClick={() => void fetchPage()} disabled={!!pageBusy || paused}>{pageBusy ? <LoaderCircle size={14} className="spin"/> : <ChevronDown size={14}/>}加载更多</button>}
      {pageBusy && <button type="button" onClick={stopPages}><X size={14}/>停止查询</button>}
    </div>
      {pageBusy && <p className={styles.mapNotice} role="status">{pageBusy === 'auto' ? '正在补充首屏结果，目标 10 家，最多 3 页…' : pageBusy === 'range' ? '正在查询地图中心附近…' : pageBusy === 'supplement' ? `正在用“${discoveryKeyword}”补充查找，原结果保留…` : '正在查询餐厅…'}</p>}
      {pageNotice && <p className={styles.mapNotice} role="status">{pageNotice}</p>}
      {mapNotice && <p className={styles.mapNotice} role="status">{mapNotice}</p>}
      {mapState === 'ready' && <p className={styles.mapNotice}>拖动或缩放地图，停下后查中心附近；至少查 {minimumRadiusM / 1000} 公里，放大地图不会缩小到这个范围以内。</p>}
      {mapRange && radius >= 50000 && <p className={styles.mapNotice}>当前最多查询中心附近 50 公里，不保证覆盖全部视野或该区域全部餐厅。</p>}
      {!hasMapPoints && <p className={styles.mapNotice}>当前结果没有可用坐标，可继续加载列表。</p>}
      {device ? <p className={styles.mapNotice}><span className={styles.originLegend} aria-hidden="true"/>你的位置（本次定位）；距你为直线距离，不是步行里程。{mapRange && '“查”标记是查询中心。'}</p>
        : <p className={styles.mapNotice}>{origin ? '距离以查询中心为起点，尚未获取你的位置。' : '区域查询未使用设备位置，地图中心不代表你的位置。'}点“显示距我距离”可查看。</p>}
      {locationError && <p className={styles.error} role="alert">{locationError} 原餐厅列表已保留。</p>}
      <div ref={container} className={`${styles.map} ${hasMapPoints && sdkAttempt > 0 && !['unavailable', 'failed'].includes(mapState) ? '' : styles.hiddenMap}`} tabIndex={0} aria-label="餐厅位置地图"/>
      {mapState === 'ready' && validCount !== filteredItems.length && <p className={styles.detail}>部分餐厅缺少坐标，仅在列表展示。</p>}
    </div>
      {!compact && refreshQuery && <details className={styles.discovery}><summary>换个关键词，补充找餐厅</summary>
        <p>在同一区域补充查询，重复餐厅会合并保留。关键词用于找店，不代表饮食分类。</p>
        <div className={styles.discoveryControls}><label>搜索关键词<select value={discoveryKeyword} disabled={!!pageBusy || paused} onChange={event => setDiscoveryKeyword(event.target.value)}>{RESTAURANT_DISCOVERY_KEYWORDS.map(keyword => <option key={keyword} value={keyword}>{keyword}</option>)}</select></label>
          <button type="button" disabled={!!pageBusy || paused || !restaurantDiscoveryQuery(payload, discoveryKeyword)} onClick={() => void supplement()}>{queriedKeywords.includes(discoveryKeyword) ? '查看已查结果' : '补充查找'}</button></div>
        <p>当前继续加载：{activeKeyword ? `“${activeKeyword}”` : '素食类别'}。每次补充只查一页。</p>
      </details>}
      {data.pagination.reachedWindowEnd && <p className={styles.mapNotice}>当前关键词最多可查 200 条，不代表该地区全部餐厅。</p>}</>}
    {data.scopeCurrent && data.pagination.hasMore && !nextQuery && <p className={styles.mapNotice}>暂时无法继续加载，请按当前区域重新查询。</p>}
    {showItems && <div className={styles.list}>{visibleItems.length ? visibleItems.map((item, index) => {
      const navigation = data.scopeCurrent ? restaurantNavigation(item) : undefined;
      const distance = restaurantDistance(item, data.center, device);
      return <div key={item.key} ref={node => { if (node) rows.current.set(item.key, node); else rows.current.delete(item.key); }} className={`${styles.row} ${selected === item.key ? styles.selected : ''}`}>
        <button type="button" className={styles.choose} onClick={() => choose(item.key)} aria-pressed={selected === item.key} aria-label={`选择${item.name}${item.location ? '，在地图中定位' : '，暂无地图坐标'}`}><span className={styles.number}>{index + 1}</span><strong>{item.name}</strong>{data.scopeCurrent && distance && <span className={styles.distance}>{distance}</span>}</button>
        <div className={styles.rowBody}><p className={styles.address}>{item.address}</p>{data.scopeCurrent && <RestaurantDietLabels value={item.dietLabels} compact={compact}/>} {(item.rating || item.cost) && <p className={styles.detail}>{[item.rating && `评分 ${item.rating}`, item.cost && `人均 ¥${item.cost}`].filter(Boolean).join(' · ')}</p>}{item.businessHours && <p className={styles.detail}>营业时间 {item.businessHours}</p>}{navigation && <a className={styles.navigation} href={navigation} target="_blank" rel="noreferrer">高德导航<ExternalLink size={12}/></a>}{!item.location && <p className={styles.detail}>暂无地图坐标</p>}</div>
      </div>;
    }) : <p className={styles.empty}>{dietFilter !== 'all' ? '已加载的餐厅暂无此类资料，可切回“全部”或继续查找。待确认不代表不适配。' : pageBusy ? '正在继续核对后续结果…' : data.pagination.hasMore ? '已查页面暂没有符合条件的店铺，可以继续加载。' : '这个范围暂未查到符合条件的店铺，可以换个区域或补充关键词。'}</p>}</div>}
    {showItems && filteredItems.length > collapsedLimit && <button type="button" className={styles.expand} onClick={() => setExpanded(value => !value)} aria-expanded={expanded}>{expanded ? <ChevronUp size={15}/> : <ChevronDown size={15}/>}{expanded ? `收起，只看前 ${collapsedLimit} 家` : `展开已加载结果（还有 ${filteredItems.length - collapsedLimit} 家）`}</button>}
    {pageError && <p className={styles.error} role="alert">{pageError} 已加载内容已保留。</p>}
    {data.scopeCurrent && <div className={styles.notice}><p>仅展示已查到且符合筛选条件的店铺，不代表该地区全部餐厅。{!data.pagination.hasMore && data.items.length > 0 && '当前关键词已查完，可补充查找或换区域。'}</p><details><summary>查询范围与来源</summary><p>首屏最多自动查 3 页，后续可点“加载更多”；补充关键词每次只查一页。地图查询半径上限 50 公里。三类标签表示菜单适配线索，不是整店认证，出发前请与店家确认。</p><p>{data.notice}</p></details></div>}
  </section>;
}
