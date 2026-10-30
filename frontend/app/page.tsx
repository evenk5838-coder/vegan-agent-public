'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { api } from '../lib/api';
import IngredientWorkbench from '../components/IngredientWorkbench';
import FoodReminderDialog from '../components/FoodReminderDialog';
import Chat from '../components/Chat';
import ChatFeatureDetail from '../components/ChatFeatureDetail';
import type { FeatureResult } from '../lib/chat-presentation';
import DraftInbox from '../components/DraftInbox';
import Recipes from '../components/Recipes';
import Restaurants from '../components/Restaurants';
import FridgeInventory from '../components/FridgeInventory';
import FoodEntry from '../components/FoodEntry';
import NotificationSettings from '../components/NotificationSettings';
import { type Food } from '../lib/food-progress';
import { Leaf, ScanLine, MapPin, CookingPot, Refrigerator, ArrowRight, LogOut, Plus, Bell, LoaderCircle, Settings2, ChevronRight, ArrowLeft, X, MessageCircle } from 'lucide-react';

type Diet = 'pure' | 'vegan' | 'lacto_ovo';
type User = { username: string; preferences: { diet: Diet; avoid_coriander: boolean } };
const diets: Record<Diet, string> = { pure: '纯净素', vegan: 'Vegan', lacto_ovo: '蛋奶素' };
const tabs = [ {id:'ingredients', title:'看配料', subtitle:'看清成分，了解依据', Icon:ScanLine}, {id:'restaurants', title:'找餐厅', subtitle:'看看附近的蔬食选择', Icon:MapPin}, {id:'recipes', title:'选菜谱', subtitle:'多一点搭配的灵感', Icon:CookingPot}, {id:'foods', title:'记食材', subtitle:'把储存和提醒记下来', Icon:Refrigerator} ];

function field(form: FormData, key: string) { return String(form.get(key)||'').trim(); }
function useAction() {
  const [busy,setBusy]=useState(false); const [error,setError]=useState('');
  const run=useCallback(async (fn:()=>Promise<void>)=>{setBusy(true);setError('');try{await fn();}catch(e){setError(e instanceof Error?e.message:'操作未完成');}finally{setBusy(false);}},[]);
  return {busy,error,run};
}
function ErrorLine({message}:{message:string}) { return message ? <p className="error" role="alert">{message}</p> : null; }
function Submit({busy,children}:{busy:boolean;children:React.ReactNode}) { return <button className="primary" disabled={busy} type="submit">{busy?<LoaderCircle size={17} className="spin"/>:null}{children}</button>; }

function Auth({onLogin}:{onLogin:(user:User)=>void}) {
  const [register,setRegister]=useState(false);const {busy,error,run}=useAction();
  return <div className="auth-shell"><div className="auth-story"><div className="eyebrow"><Leaf size={16}/> 每天安心一点</div><h1>蔬食日常，<br/>慢慢有了着落。</h1><p>从一张配料表，到今天吃什么。<br/>把日常的几个小问题，放在一起解决。</p><div className="intro-grid">{tabs.map(({id,title,subtitle,Icon})=><div key={id}><Icon size={23}/><strong>{title}</strong><span>{subtitle}</span></div>)}</div><small>本地开发版 · 配料规则及资料仍在逐项核对</small></div><section className="auth-card"><span className="eyebrow">SHUSHI / 蔬食</span><h2>{register?'开始你的蔬食日常':'欢迎回来'}</h2><p className="muted">登录后，在手机和电脑上接着使用。</p><form onSubmit={e=>{e.preventDefault();const data=new FormData(e.currentTarget);void run(async()=>onLogin(await api<User>('/auth/'+(register?'register':'login'),{method:'POST',body:JSON.stringify({username:field(data,'username'),password:field(data,'password')})})));}}><label>账号<input name="username" required minLength={3} maxLength={32} pattern="[A-Za-z0-9_]+" autoComplete="username" placeholder="3—32 位字母、数字或下划线"/></label><label>密码<input name="password" type="password" required minLength={8} maxLength={128} autoComplete={register?'new-password':'current-password'} placeholder="至少 8 位"/></label><ErrorLine message={error}/><Submit busy={busy}>{register?'注册并进入':'登录'}<ArrowRight size={17}/></Submit></form><button className="text-button auth-switch" onClick={()=>setRegister(!register)}>{register?'已有账号？去登录':'第一次使用？注册账号'}</button><p className="privacy-note">菜谱、食材和偏好属于你的个人空间。配料原图每次由你选择是否保存。</p></section></div>;
}


function Foods({ onTalk }: { onTalk: (prompt?: string) => void }) {
  const [rows, setRows] = useState<Food[]>([]);
  const [adding, setAdding] = useState(false);
  const [reminder, setReminder] = useState(false);
  const [draftVersion, setDraftVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    const data = await api<{ items: Food[] }>('/foods');
    setRows(data.items); setError('');
  }, []);
  useEffect(() => {
    let alive = true;
    void api<{ items: Food[] }>('/foods').then(data => { if (alive) setRows(data.items); })
      .catch(reason => { if (alive) setError(reason instanceof Error ? reason.message : '库存读取失败，请刷新重试。'); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);
  return <>
    <div className="inventory-toolbar">
      <h2><Refrigerator size={23}/>我的冰箱</h2>
      <div className="button-row">
        <button className="primary" onClick={() => onTalk()}><MessageCircle size={16}/>对话登记</button>
        <button className="secondary" onClick={() => setAdding(!adding)} aria-expanded={adding}><Plus size={16}/>{adding ? '收起登记' : '手动登记'}</button>
        <button className="text-button" onClick={() => setReminder(true)}><Bell size={16}/>提醒</button>
      </div>
    </div>
    {adding && <FoodEntry onSaved={refresh} onDraftCreated={() => setDraftVersion(value => value + 1)}/>}
    <ErrorLine message={error}/>
    {error && <button className="text-button" onClick={() => void refresh().catch(reason => setError(reason.message))}>重新读取库存</button>}
    {loading ? <p role="status" className="small muted">正在打开冰箱…</p> : <FridgeInventory rows={rows} onChanged={refresh} onTalk={onTalk}/>}
    <details className="inventory-drafts"><summary>批量登记与待确认食材<ChevronRight size={15}/></summary><DraftInbox key={draftVersion} onSaved={refresh}/></details>
    <FoodReminderDialog open={reminder} onClose={() => setReminder(false)} foods={rows} onChanged={refresh}/>
  </>;
}

function PreferencesDialog({ user, open, onClose, onChange }: {
  user: User;
  open: boolean;
  onClose: () => void;
  onChange: (user: User) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const { busy, error, run } = useAction();
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    if (!open && dialog.current?.open) dialog.current?.close();
  }, [open]);
  function save(next: User['preferences']) {
    void run(async () => {
      await api('/preferences', { method: 'PUT', body: JSON.stringify(next) });
      onChange({ ...user, preferences: next });
    });
  }
  return <dialog ref={dialog} className="preferences-dialog" onClose={onClose} aria-labelledby="preferences-title" onClick={e => {
    if (e.target === e.currentTarget) dialog.current?.close();
  }}>
    <div className="preferences-dialog-card">
      <div className="row-between">
        <div><span className="eyebrow">属于你的选择</span><h2 id="preferences-title">饮食偏好</h2></div>
        <button className="icon-button" aria-label="关闭饮食偏好" onClick={() => dialog.current?.close()}><X size={20}/></button>
      </div>
      <p className="muted small">在手机和电脑上，沿用同一份偏好。</p>
      <label>饮食标准<select value={user.preferences.diet} disabled={busy} onChange={e => save({ ...user.preferences, diet: e.target.value as Diet })}>
        {(Object.keys(diets) as Diet[]).map(d => <option key={d} value={d}>{diets[d]}</option>)}
      </select></label>
      <label className="check-row"><input type="checkbox" disabled={busy} checked={user.preferences.avoid_coriander} onChange={e => save({ ...user.preferences, avoid_coriander: e.target.checked })}/> 额外避用香菜（默认允许）</label>
      <p className="small muted preference-explanation">纯净素不含动物成分及五辛；Vegan 不含动物成分、允许五辛；蛋奶素允许蛋、奶、蜂蜜及五辛。</p>
      <ErrorLine message={error}/>
      <details className="space-top"><summary>提醒与设备通知</summary><NotificationSettings/></details>
    </div>
  </dialog>;
}

function FeatureNavigation({ active, onNavigate }: { active: string; onNavigate: (page: string) => void }) {
  return <aside className="sidebar">
    <button className="back-to-chat" onClick={() => onNavigate('chat')}><ArrowLeft size={17}/><span>返回聊一聊</span></button>
    <div className="sidebar-label">蔬食日常</div>
    <nav aria-label="四项日常功能">
      {tabs.map(({ id, title, Icon }) => <button key={id} onClick={() => onNavigate(id)} aria-current={active === id ? 'page' : undefined} className={active === id ? 'nav-item active' : 'nav-item'}>
        <Icon size={21} strokeWidth={1.5}/><span>{title}</span>{active === id && <ChevronRight size={14}/>}
      </button>)}
    </nav>
    <div className="sidebar-note" aria-hidden="true"><Leaf size={28} strokeWidth={1.2}/><span>从一餐开始，照顾自己。</span></div>
  </aside>;
}

export default function Home() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [active, setActive] = useState('chat');
  const [featureResult, setFeatureResult] = useState<FeatureResult | null>(null);
  const [resultsVersion, setResultsVersion] = useState(0);
  const [chatPrompt, setChatPrompt] = useState('');
  const [preferences, setPreferences] = useState(false);
  const { error, run } = useAction();
  const isChat = !!user && !loading && active === 'chat';
  function navigate(page: string, result?: FeatureResult) {
    if(page==='chat'&&featureResult) setResultsVersion(version=>version+1);
    setChatPrompt(''); setFeatureResult(result || null); setActive(page);
  }
  useEffect(() => { void api<User>('/auth/me').then(setUser).catch(() => {}).finally(() => setLoading(false)); }, []);

  return <div className={`app-shell${isChat ? ' is-chat' : ''}`}>
    <header className="site-header">
      <Link className="brand" href="/" aria-label="蔬食首页"><span className="brand-mark"><Leaf size={27} strokeWidth={1.5}/></span><strong>蔬食</strong><span className="brand-line">SHUSHI</span></Link>
      <div className="header-right">
        <span className="local-badge">开发预览</span>
        {user && <>
          <button className="account-button" aria-label={`饮食偏好，当前账号 ${user.username}`} title="饮食偏好" onClick={() => setPreferences(true)}><Settings2 size={18}/><span>{user.username}</span></button>
          <button className="icon-button" aria-label="退出登录" title="退出登录" onClick={() => void run(async () => { await api('/auth/logout', { method: 'POST' }); setUser(null); setPreferences(false); setFeatureResult(null); setActive('chat'); setChatPrompt(''); })}><LogOut size={18}/></button>
        </>}
      </div>
    </header>
    {loading ? <main className="loading"><LoaderCircle className="spin"/>正在打开你的蔬食日常…</main> : !user ? <Auth onLogin={value=>{setFeatureResult(null);setActive('chat');setUser(value);}}/> : <>
      <PreferencesDialog user={user} open={preferences} onClose={() => setPreferences(false)} onChange={setUser}/>
      <div className={isChat ? 'workspace workspace-home' : 'workspace'}>
        {!isChat && <FeatureNavigation active={active} onNavigate={navigate}/>}
        <main className={isChat ? 'main-content main-content-chat' : 'main-content'}>
          {!isChat && active !== 'foods' && active !== 'ingredients' && <div className="welcome"><span>你好，{user.username}</span><span className="diet-badge"><Leaf size={13}/>{diets[user.preferences.diet]}</span></div>}
          <ErrorLine message={error}/>
          <div className="chat-stage" hidden={!isChat}>{(active==='chat'||featureResult)&&<Chat onNavigate={navigate} username={user.username} initialText={chatPrompt} resultsVersion={resultsVersion}/>}</div>
          {!isChat&&featureResult&&<ChatFeatureDetail result={featureResult} onNavigate={navigate} onClose={()=>setFeatureResult(null)}/>}
          {!featureResult&&active === 'ingredients' && <IngredientWorkbench key={String(user.preferences.avoid_coriander)}/>}
          {!featureResult&&active === 'restaurants' && <Restaurants/>}
          {!featureResult&&active === 'recipes' && <Recipes key={String(user.preferences.avoid_coriander)} diet={user.preferences.diet}/>}
          {!featureResult&&active === 'foods' && <Foods onTalk={(prompt) => {
            const url = new URL(window.location.href);
            url.searchParams.delete('chat');
            window.history.replaceState(null, '', url);
            setChatPrompt(prompt || '帮我记录食材：');
            setActive('chat');
          }}/>}
          {!isChat && <footer>蔬食 · 开发预览<span>信息不足时，保留未知。</span></footer>}
        </main>
      </div>
    </>}
  </div>;
}
