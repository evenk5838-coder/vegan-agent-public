// Official 2.0 SDK: security proxy must be configured before loading the script.
// https://developer.amap.com/api/javascript-api-v2/guide/abc/jscode
import type { Coordinate } from './restaurant-results';
import { api } from './api';

export type MapConfiguration = {
  enabled: boolean; js_ready: boolean; verified: boolean; js_key: string;
  service_host: string; coordinate_system: string; notice: string;
};
export type AMapMarker = {
  on: (event: string, callback: () => void) => void;
  off: (event: string, callback: () => void) => void;
  setMap: (map: AMapInstance | null) => void;
};
export type AMapInstance = {
  add: (markers: AMapMarker[]) => void;
  setFitView: (markers?: AMapMarker[], immediately?: boolean, avoid?: number[], maxZoom?: number) => void;
  setCenter: (position: Coordinate) => void;
  on: (event: string, callback: () => void) => void;
  off: (event: string, callback: () => void) => void;
  destroy: () => void;
};
export type AMapSDK = {
  Map: new (container: HTMLElement, options: Record<string, unknown>) => AMapInstance;
  Marker: new (options: Record<string, unknown>) => AMapMarker;
  plugin?: (name: string, callback: () => void) => void;
  Geolocation?: new (options: Record<string, unknown>) => {
    getCurrentPosition: (callback: (status: string, result: {
      position?: { getLng: () => number; getLat: () => number };
      info?: string; message?: string;
    }) => void) => void;
  };
};
declare global {
  interface Window {
    AMap?: AMapSDK; _AMapSecurityConfig?: { serviceHost: string };
    __shushiAMapIdentity?: string;
  }
}

export function mapConfigurationReady(config: MapConfiguration, origin: string): boolean {
  if (!config.enabled || !config.js_ready || !config.js_key || config.coordinate_system !== 'GCJ-02') return false;
  try {
    const host = new URL(config.service_host, origin);
    return host.origin === origin && !host.username && !host.password && !host.search && !host.hash &&
      /\/_AMapService\/?$/.test(host.pathname);
  } catch { return false; }
}

let loadedIdentity: string | null = null;
let pending: { identity: string; promise: Promise<AMapSDK> } | null = null;

export function loadAMapSDK(config: MapConfiguration, timeoutMs = 15000): Promise<AMapSDK> {
  if (typeof window === 'undefined' || !mapConfigurationReady(config, window.location.origin))
    return Promise.reject(new Error('页内地图尚未就绪，仍可查看餐厅列表。'));
  const identity = JSON.stringify([config.js_key, config.service_host]);
  if (pending) return pending.identity === identity ? pending.promise : Promise.reject(new Error('地图配置已变化，请刷新页面后重新加载。'));
  if (window.AMap) {
    // Fast Refresh replaces this module's local variables but keeps the
    // already loaded window SDK. Reuse only our successful exact configuration.
    if ((loadedIdentity || window.__shushiAMapIdentity) === identity) {
      loadedIdentity = identity;
      return Promise.resolve(window.AMap);
    }
    return Promise.reject(new Error('地图配置已变化，请刷新页面后重新加载。'));
  }
  const promise = new Promise<AMapSDK>((resolve, reject) => {
    window._AMapSecurityConfig = { serviceHost: new URL(config.service_host, window.location.origin).href };
    const script = document.createElement('script');
    script.async = true;
    script.src = `https://webapi.amap.com/maps?${new URLSearchParams({ v: '2.0', key: config.js_key })}`;
    script.dataset.shushiMapSdk = 'true';
    let finished = false;
    const cleanup = () => { clearTimeout(timer); script.onload = null; script.onerror = null; };
    const fail = () => {
      if (finished) return;
      finished = true; cleanup(); script.remove(); pending = null;
      reject(new Error('地图暂时加载失败，列表仍可使用。可以重试加载。'));
    };
    const timer = window.setTimeout(fail, timeoutMs);
    script.onerror = fail;
    script.onload = () => {
      if (finished) return;
      if (!window.AMap?.Map || !window.AMap?.Marker) { fail(); return; }
      finished = true; cleanup(); loadedIdentity = identity; window.__shushiAMapIdentity = identity;
      pending = null; resolve(window.AMap);
    };
    document.head.appendChild(script);
  });
  pending = { identity, promise };
  return promise;
}

let locationPending: Promise<string> | null = null;

export function locationFailure(info = '', message = '') {
  const reason = `${info} ${message}`;
  const diagnostics = {
    permissionDenied: /permission.*denied|denied.*(?:permission|geolocation)|授权|拒绝/i.test(reason),
    unsupported: /not support|unavailable|不支持/i.test(reason),
    timedOut: /time[\s_]*out|timeout|超时/i.test(reason),
    ipFailed: /ip.?location.*fail/i.test(reason),
    browserFailed: /get geolocation.*fail/i.test(reason),
    conversionFailed: /convert.*fail|transform.*fail|转换.*失败/i.test(reason),
  };
  const text = diagnostics.permissionDenied
    ? '设备或浏览器未允许定位。请开启系统与浏览器的定位权限后再试。'
    : diagnostics.conversionFailed ? '已请求位置，但地图坐标转换未成功，请稍后重试。'
    : diagnostics.unsupported || diagnostics.browserFailed ? '当前浏览器没有取得设备位置，请检查系统定位权限，或在 Safari / Chrome 打开后重试。'
    : diagnostics.timedOut ? '获取设备位置超时，请检查系统定位服务后重试。'
    : '定位服务暂未返回可用位置，请稍后重试。';
  return { text, diagnostics };
}

// Call only after the user requests this one-off location. The SDK can ask for
// browser permission and contact AMap; it never starts a background watcher.
// https://lbs.amap.com/api/javascript-api-v2/guide/services/geolocation
// convert:true returns AMap coordinates, matching the backend's GCJ-02 contract.
export function requestCurrentMapLocation(): Promise<string> {
  if (locationPending) return locationPending;
  locationPending = (async () => {
    if (typeof window === 'undefined') throw new Error('请在浏览器中获取本次位置。');
    const config = await api<MapConfiguration>('/map/config');
    if (!mapConfigurationReady(config, window.location.origin))
      throw new Error('定位所需的高德地图尚未配置完成。可以先输入城市或区域查餐厅。');
    const sdk = await loadAMapSDK(config);
    return new Promise<string>((resolve, reject) => {
      let finished = false;
      const finish = (value?: string, message?: string) => {
        if (finished) return;
        finished = true; clearTimeout(timer);
        if (value) resolve(value); else reject(new Error(message || '本次定位未成功，可以重试或输入城市区域。'));
      };
      const timer = setTimeout(() => finish(undefined, '获取设备位置超时，请检查系统定位服务后重试。'), 25000);
      if (!sdk.plugin) { finish(undefined, '定位插件暂时不可用，请重试或输入城市区域。'); return; }
      try {
        sdk.plugin('AMap.Geolocation', () => {
          if (finished) return;
          if (!sdk.Geolocation) { finish(undefined, '定位插件加载失败，请重试或输入城市区域。'); return; }
          try {
            const locator = new sdk.Geolocation({ convert: true, enableHighAccuracy: true, timeout: 20000,
              GeoLocationFirst: true, getCityWhenFail: false,
              showButton: false, showMarker: false, showCircle: false, needAddress: false });
            locator.getCurrentPosition((status, result) => {
              if (finished) return;
              if (status !== 'complete' || !result?.position) {
                const failure = locationFailure(result?.info, result?.message);
                // Deliberately log only categorical flags, never provider URLs,
                // keys, coordinates, raw messages or the user's location.
                console.warn('蔬食定位诊断', JSON.stringify(failure.diagnostics));
                finish(undefined, failure.text);
                return;
              }
              const lng = result.position.getLng(), lat = result.position.getLat();
              if (!Number.isFinite(lng) || !Number.isFinite(lat) || Math.abs(lng) > 180 || Math.abs(lat) > 90) {
                finish(undefined, '暂时没有取得可用位置，请重试或输入城市区域。'); return;
              }
              finish(`${lng},${lat}`);
            });
          } catch { finish(undefined, '本次定位未成功，请重试或输入城市区域。'); }
        });
      } catch { finish(undefined, '定位插件暂时不可用，请重试或输入城市区域。'); }
    });
  })().finally(() => { locationPending = null; });
  return locationPending;
}
