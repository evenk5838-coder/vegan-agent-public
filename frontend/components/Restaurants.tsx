'use client';

import { useEffect, useRef, useState } from 'react';
import { LoaderCircle, LocateFixed, MapPin, RefreshCw, X } from 'lucide-react';
import { api } from '../lib/api';
import { requestCurrentMapLocation } from '../lib/amap-sdk';
import { DEFAULT_NEARBY_RADIUS_M, NEARBY_RADIUS_OPTIONS, RestaurantSearchError, runRestaurantSearch, type RestaurantSearchPhase } from '../lib/restaurant-search';
import { restaurantData } from '../lib/restaurant-results';
import RestaurantResults from './RestaurantResults';
import styles from './restaurant-results.module.css';

export default function Restaurants() {
  const [result, setResult] = useState<{ version: number; payload: Record<string, unknown>; currentLocation: string | null; autoFillAllowed?: boolean; suspendSignal?: AbortSignal } | null>(null);
  const [city, setCity] = useState('');
  const [nearbyRadius, setNearbyRadius] = useState(DEFAULT_NEARBY_RADIUS_M);
  const [phase, setPhase] = useState<RestaurantSearchPhase | null>(null);
  const [feedback, setFeedback] = useState<{ message: string; locationFailed?: boolean; error?: boolean } | null>(null);
  const request = useRef<AbortController | null>(null);
  const resultWork = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const cityInput = useRef<HTMLInputElement>(null);
  const busy = phase !== null;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; request.current?.abort(); resultWork.current?.abort(); };
  }, []);

  async function search(nearby = false) {
    if (!nearby && !city.trim()) return;
    if (request.current) {
      if (nearby || phase?.kind !== 'locating') return;
      // A region query is usable while the device is still locating. Stop
      // waiting for that older request; its late coordinates cannot query.
      request.current.abort();
      request.current = null;
    }
    const controller = new AbortController();
    // Invalidate the retained old result immediately, before React renders or
    // the new location/HTTP request resolves. A failed new query cannot resume it.
    resultWork.current?.abort();
    request.current = controller;
    setFeedback(null);
    try {
      const { payload, currentLocation } = await runRestaurantSearch(
        nearby ? { mode: 'nearby', radiusM: nearbyRadius } : { mode: 'city', city }, controller.signal, {
          locate: requestCurrentMapLocation,
          query: (query, signal) => api<Record<string, unknown>>('/restaurants?' + new URLSearchParams(query), {
            signal: AbortSignal.any([signal, AbortSignal.timeout(20000)]),
          }),
          onPhase: next => { if (mounted.current && request.current === controller) setPhase(next); },
        });
      if (!mounted.current || controller.signal.aborted || request.current !== controller) return;
      if (!Array.isArray(payload.items) || !restaurantData(payload).scopeCurrent) {
        throw new Error('本次餐厅结果未通过当前筛选版本校验，请刷新状态后重新查询。');
      }
      const work = new AbortController(); resultWork.current = work;
      setResult(previous => ({ version: (previous?.version ?? 0) + 1, payload, currentLocation, autoFillAllowed: true, suspendSignal: work.signal }));
    } catch (reason) {
      if (!mounted.current || controller.signal.aborted || request.current !== controller) return;
      const locationFailed = reason instanceof RestaurantSearchError && reason.phase === 'locating';
      const timedOut = reason instanceof Error && reason.message.toLowerCase().includes('timeout');
      const message = locationFailed
        ? `${reason.message} 本次未发起餐厅查询。请填写城市或区县，直接查询该区域。`
        : timedOut ? '餐厅查询等待超时，请稍后重试。' : reason instanceof Error ? reason.message : '餐厅查询未完成，请稍后重试。';
      setFeedback({ message, locationFailed, error: true });
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (mounted.current) setPhase(null);
      }
    }
  }

  function stopWaiting() {
    request.current?.abort();
    resultWork.current?.abort();
    request.current = null;
    setFeedback({ message: phase?.kind === 'locating'
      ? '已停止等待定位，本次未发起餐厅查询。你可以直接按城市查询。'
      : '已停止等待本次餐厅查询；迟到的结果不会替换当前页面。' });
    setPhase(null);
  }

  return <>
    <div className="section-heading"><div><span className="eyebrow">02 / 附近餐厅</span><h2>下一顿，换个地方吃。</h2><p className="muted">查询高德收录的素食餐厅，具体饮食要求请与店家确认。</p></div><MapPin className="section-icon"/></div>
    <section className="panel">
      <div className={styles.searchOptions}>
        <form className={styles.citySearch} onSubmit={event => { event.preventDefault(); void search(); }}>
          <label>按城市或区县查询<input ref={cityInput} value={city} onChange={event => setCity(event.target.value)} disabled={phase?.kind === 'searching'} required maxLength={40} placeholder="例如：北京朝阳" aria-describedby="restaurant-city-hint"/></label>
          <p id="restaurant-city-hint" className="small muted">不需要授权定位，填写你想去的区域即可。</p>
          <button type="submit" className="primary" disabled={phase?.kind === 'searching' || !city.trim()}><MapPin size={16}/>{phase?.kind === 'locating' ? '改为查询该区域' : '查询该区域'}</button>
        </form>
        <div className={styles.nearbySearch}>
          <h3>查我附近</h3>
          <p className="small muted">授权获取本次位置后查询周边；不会持续跟踪。</p>
          <label className={styles.radiusChoice}>附近查询范围<select value={nearbyRadius} disabled={busy} onChange={event => setNearbyRadius(Number(event.target.value))}>
            {NEARBY_RADIUS_OPTIONS.map(value => <option key={value} value={value}>{value / 1000} 公里{value === DEFAULT_NEARBY_RADIUS_M ? '（默认）' : ''}</option>)}
          </select></label>
          <button type="button" className="secondary" disabled={busy} onClick={() => void search(true)}>
            {result?.currentLocation ? <RefreshCw size={17}/> : <LocateFixed size={17}/>}
            {result?.currentLocation ? '刷新位置，查附近' : '获取位置，查附近'}
          </button>
        </div>
      </div>
      {phase && <div className={styles.searchStatus} role="status"><LoaderCircle size={17} className="spin"/><p>{phase.kind === 'locating'
        ? '正在获取本次位置，尚未开始查询餐厅…'
        : phase.mode === 'city' ? `正在查询“${phase.city}”的素食餐厅…` : '已取得本次位置，正在查询附近的素食餐厅…'}{result && <span>下方仍是上一次成功查询的结果。</span>}</p><button type="button" className="text-button" onClick={stopWaiting}><X size={15}/>停止等待</button></div>}
      {feedback && <div className={feedback.error ? styles.searchError : styles.searchFeedback} role={feedback.error ? 'alert' : 'status'}><p>{feedback.message}{result && ' 下方保留上一次成功查询的结果，尚未更新。'}</p>{feedback.locationFailed && <button type="button" className="secondary" onClick={() => cityInput.current?.focus()}>填写城市，不用定位</button>}</div>}
      <p className="muted small">查询后，点击结果上方的“加载页内地图”查看同批餐厅。按地区查询不需要定位；查看距离时再获取本次位置。</p>
    </section>
    {result && <div className="space-top"><RestaurantResults key={result.version} payload={result.payload} currentLocation={result.currentLocation} autoFill={result.autoFillAllowed===true} suspendSignal={result.suspendSignal}/></div>}
    <div className="note-card"><MapPin size={20}/><div><strong>按你的饮食习惯选</strong><p>纯净素、Vegan、蛋奶素分别按菜单资料标注；资料不足的保留“待确认”。出发前可向店家再确认。</p></div></div>
  </>;
}
