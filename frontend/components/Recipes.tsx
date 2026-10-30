'use client';

import { useEffect, useRef, useState } from 'react';
import { Check, ChevronRight, CookingPot, LoaderCircle, Plus, RefreshCw, Search } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { recipeCategoryNames, recipeCuisineQuery, recipeViews, type RecipeView } from '../lib/recipe-display';
import { prepareRecipeCreate, recipeCreateRequest, recipeCreateResult, recipeEditInput, type RecipeCreateAttempt } from '../lib/recipe-edit';
import type { Diet } from './IngredientResult';
import RecipeCandidates from './RecipeCandidates';
import PersonalRecipeEditor from './PersonalRecipeEditor';
import RecipeDetail, { RecipeDietTags } from './RecipeDetail';
import styles from './recipes.module.css';

type List = { items: unknown[]; total?: number; page?: number; page_size?: number; has_more?: boolean; next_page?: number | null; nutrition_reference?: { text?: string; source?: string } };
const meals = [['', '全部'], ['breakfast', '早餐'], ['lunch', '午餐'], ['dinner', '晚餐'], ['afternoon_tea', '下午茶'], ['main', '正餐（待补分类）']];
const mealNames = Object.fromEntries(meals);
const diets = { pure: '纯净素', vegan: 'Vegan', lacto_ovo: '蛋奶素' };
const categories = [['', '全部'], ['chinese', '中式'], ['western', '西式']] as const;

export default function Recipes({ diet }: { diet: Diet }) {
  const [rows, setRows] = useState<RecipeView[]>([]);
  const [candidatePayload, setCandidatePayload] = useState<Record<string, unknown> | null>(null);
  const [meal, setMeal] = useState('');
  const [tag, setTag] = useState('');
  const [purpose, setPurpose] = useState('');
  const [category, setCategory] = useState<'' | 'chinese' | 'western'>('');
  const [dietOnly, setDietOnly] = useState(false);
  const [query, setQuery] = useState('');
  const [libraryQuery, setLibraryQuery] = useState('');
  const [editing, setEditing] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [pageState, setPageState] = useState({ total: 0, page: 1, hasMore: false });
  const [pageBusy, setPageBusy] = useState(false);
  const [uncertainCreates, setUncertainCreates] = useState<RecipeCreateAttempt[]>([]);
  const [createDeleted, setCreateDeleted] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [revision, setRevision] = useState(0);
  const [deleting, setDeleting] = useState<string | null>(null);
  const saving = useRef(false);
  const createAttempts = useRef<RecipeCreateAttempt[]>([]);
  const searching = useRef(false);
  const paging = useRef(false);
  const pageRequest = useRef<AbortController | null>(null);
  const searchRequest = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const cuisine = recipeCuisineQuery(category);
  const params = new URLSearchParams({ meal, tag, purpose, cuisine, diet: dietOnly ? diet : '', q: libraryQuery, page_size: '20' }).toString();
  const matches = rows.filter(row => !category || row.category === category);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; pageRequest.current?.abort(); searchRequest.current?.abort(); }; }, []);

  useEffect(() => {
    const controller = new AbortController();
    pageRequest.current?.abort();
    void Promise.resolve().then(() => { if (!controller.signal.aborted) { setPageBusy(false); setLoading(true); setError(''); } });
    void api<List>('/recipes?' + params, { signal: controller.signal }).then(data => {
      if (controller.signal.aborted) return;
      if (!Array.isArray(data.items)) throw new ApiError('INVALID_RESPONSE', '菜谱列表暂时无法读取，原结果仍保留。', 200);
      setRows(recipeViews(data.items));
      setPageState({ total: typeof data.total === 'number' ? data.total : data.items.length, page: data.page || 1, hasMore: data.has_more === true });
    }).catch(reason => { if (!controller.signal.aborted) setError(reason.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [params, revision]);

  function clearSearchResult() { setCandidatePayload(null); setNotice(''); }

  async function search() {
    if (!query.trim() || busy || loading || searching.current || saving.current) return;
    searching.current = true;
    const controller = new AbortController(); searchRequest.current?.abort(); searchRequest.current = controller;
    const submitted = query.trim();
    setBusy(true); setError(''); clearSearchResult();
    try {
      const result = await api<List & { local: boolean; notice?: string }>('/recipes/search', {
        method: 'POST', signal: controller.signal, body: JSON.stringify({ query: submitted, meal, tag, purpose, cuisine, diet: dietOnly ? diet : '' }),
      });
      if (controller.signal.aborted || !mounted.current) return;
      if (!Array.isArray(result.items)) throw new ApiError('INVALID_RESPONSE', '搜索结果暂时无法读取，请重新查询。', 200);
      setLibraryQuery(submitted);
      if (result.local) {
        const found = recipeViews(result.items); setRows(found);
        setPageState({ total: typeof result.total === 'number' ? result.total : found.length, page: result.page || 1, hasMore: result.has_more === true });
        setNotice(category && !found.some(row => row.category === category) ? '库内找到其他分类的菜谱，当前分类暂无匹配。可以切换分类查看。' : '已找到库内菜谱，展开查看做法与来源。');
      } else { setRows([]); setPageState({ total: 0, page: 1, hasMore: false }); setCandidatePayload({ items: result.items, notice: result.notice }); }
    } catch (reason) { if (!controller.signal.aborted && mounted.current) setError(reason instanceof Error ? reason.message : '暂时没有找到，请稍后重试。'); }
    finally { searching.current = false; if (mounted.current && searchRequest.current === controller) setBusy(false); }
  }

  async function loadMore() {
    if (pageBusy || busy || loading || paging.current || saving.current || searching.current || !pageState.hasMore) return;
    paging.current = true;
    const controller = new AbortController(); pageRequest.current?.abort(); pageRequest.current = controller;
    setPageBusy(true); setError('');
    try {
      const next = pageState.page + 1;
      const queryParams = new URLSearchParams(params); queryParams.set('page', String(next));
      const data = await api<List>('/recipes?' + queryParams, { signal: controller.signal });
      if (controller.signal.aborted || !mounted.current) return;
      if (!Array.isArray(data.items) || data.page !== next) throw new Error('下一页状态不一致，已保留当前菜谱。请刷新列表。');
      setRows(previous => [...new Map([...previous, ...recipeViews(data.items)].map(row => [row.scope + ':' + row.id, row])).values()]);
      setPageState({ total: typeof data.total === 'number' ? data.total : pageState.total, page: next, hasMore: data.has_more === true });
    } catch (reason) { if (!controller.signal.aborted && mounted.current) setError(reason instanceof Error ? reason.message : '下一页读取失败，当前菜谱仍保留。'); }
    finally { paging.current = false; if (!controller.signal.aborted && mounted.current) setPageBusy(false); }
  }

  async function savePersonal(form: FormData) {
    if (saving.current || searching.current) return;
    const value = (key: string) => String(form.get(key) || '').trim();
    try {
      const body = recipeEditInput({
        title: value('title'), ingredients: value('ingredients'), steps: value('steps'),
        meal: value('meal'), dessert: !!form.get('dessert'), purpose: value('purpose'),
        cuisine: value('cuisine'), nutrition_notes: value('nutrition_notes'), source_url: value('source_url'),
        origin: value('origin'), yield_amount: value('yield_amount'), yield_unit: value('yield_unit'), unsupportedTags: [],
        confirmed: form.get('confirmed') === 'on',
      });
      const attempt = prepareRecipeCreate(createAttempts.current, body);
      await sendCreate(attempt);
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : '请核对填写内容。');
    }
  }

  async function sendCreate(attempt: RecipeCreateAttempt) {
    if (saving.current || searching.current || attempt.outcome === 'deleted') return;
    saving.current = true; setBusy(true); setError(''); setNotice('');
    let sent = false;
    try {
      const request = recipeCreateRequest(attempt);
      sent = true;
      const result = await api<unknown>('/recipes', request);
      if (!mounted.current) return;
      const saved = recipeCreateResult(result, attempt.body);
      if (!saved) throw new ApiError('INVALID_RESPONSE', '尚未核实保存结果，请先重新读取列表。', 200);
      attempt.outcome = 'saved';
      setUncertainCreates(createAttempts.current.filter(item => item.outcome === 'uncertain'));
      setCreateDeleted(false);
      setEditing(false); setRevision(n => n + 1);
      setNotice(saved.changedSinceSave ? '这次保存已经完成，记录后来已有其他修改。正在刷新列表，请查看当前内容；未重复新建或覆盖。'
        : '已保存到个人菜谱。若当前筛选未显示，可切换为全部查看。');
    } catch (reason) {
      if (!mounted.current) return;
      const unknown = sent && (!(reason instanceof ApiError) || reason.status === 0 || reason.status >= 500 || reason.code === 'INVALID_RESPONSE');
      attempt.outcome = reason instanceof ApiError && reason.status === 410 ? 'deleted' : unknown ? 'uncertain' : 'pending';
      setUncertainCreates(createAttempts.current.filter(item => item.outcome === 'uncertain'));
      setCreateDeleted(attempt.outcome === 'deleted');
      setError(attempt.outcome === 'deleted' ? '这次保存的菜谱已删除，不会重新创建。填写内容仍保留，请先核对列表。'
        : unknown ? '未收到确定的保存结果，填写内容仍保留。可核对下方这次保存；修改内容会作为另一份菜谱保存，原结果仍需核对。'
          : reason instanceof Error ? reason.message : '保存未完成，填写内容仍保留。');
    }
    finally { saving.current = false; if (mounted.current) setBusy(false); }
  }

  async function removePersonal(row: RecipeView) {
    if (row.scope !== 'personal' || busy || saving.current || searching.current) return;
    saving.current = true;
    setBusy(true); setError('');
    try { await api('/recipes/' + encodeURIComponent(row.id), { method: 'DELETE' }); setRevision(n => n + 1); setDeleting(null); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '删除未完成，请稍后重试。'); }
    finally { saving.current = false; if (mounted.current) setBusy(false); }
  }

  return <>
    <div className="section-heading"><div><span className="eyebrow">03 / 营养菜谱</span><h2>今天，想做点什么？</h2><p className="muted">找到一道菜，看用量、照步骤做。</p></div><CookingPot className="section-icon"/></div>
    <section className={`panel recipe-search ${styles.search}`}>
      <form className="inline-form" onSubmit={event => { event.preventDefault(); void search(); }}><label>搜索菜谱<input value={query} disabled={busy} onChange={event => { setQuery(event.target.value); if (!event.target.value.trim()) setLibraryQuery(''); clearSearchResult(); }} maxLength={120} placeholder="输入菜名或食材，例如：鹰嘴豆"/></label><button className="primary" disabled={busy || loading || pageBusy || !query.trim()}>{busy ? <LoaderCircle size={16} className="spin"/> : <Search size={16}/>}搜索</button></form>
      <div className={styles.categories} aria-label="按中式或西式浏览">{categories.map(([id, label]) => <button key={id} type="button" aria-pressed={category === id} className={category === id ? styles.selectedCategory : ''} disabled={busy} onClick={() => { setCategory(id); clearSearchResult(); }}>{label}</button>)}</div>
      <details className={styles.filters}><summary>餐次、饮食与更多筛选<ChevronRight size={14}/></summary>
      <div className="meal-tabs" aria-label="按餐次筛选">{meals.map(([id, label]) => <button key={id} type="button" disabled={busy} aria-pressed={meal === id} className={meal === id ? 'selected' : ''} onClick={() => { setMeal(id); clearSearchResult(); }}>{label}</button>)}</div>
      <div className="recipe-filters">
        <label className="check-row"><input type="checkbox" disabled={busy} checked={tag === 'dessert'} onChange={event => { setTag(event.target.checked ? 'dessert' : ''); clearSearchResult(); }}/>甜点</label>
        <label className="check-row"><input type="checkbox" disabled={busy} checked={dietOnly} onChange={event => { setDietOnly(event.target.checked); clearSearchResult(); }}/>符合 {diets[diet]} 与我的忌口</label>
        <label>用途<select disabled={busy} value={purpose} onChange={event => { setPurpose(event.target.value); clearSearchResult(); }}><option value="">全部搭配</option><option value="everyday">日常搭配</option><option value="protein">补蛋白</option><option value="fat">补脂肪</option></select></label>
      </div>
      <p className={`small muted ${styles.filterNote}`}>三类饮食分别判断，可以重叠。甜点可以同时属于任一餐次。</p>
      </details>
    </section>
    <div className={`toolbar space-top ${styles.toolbar}`}><span className="small muted">{loading ? '正在读取菜谱…' : `已显示 ${matches.length} / ${pageState.total} 道库内菜谱`}</span><div className="button-row"><button className="text-button" disabled={busy || loading} onClick={() => setRevision(n => n + 1)}><RefreshCw size={15}/>刷新列表</button><button className="text-button" disabled={busy} onClick={() => { setEditing(!editing); if (!editing) setCreateDeleted(false); }}><Plus size={16}/>录入自己的菜谱</button></div></div>
    {editing && <section className="panel"><div className="row-between"><h3>留住一道自己的菜</h3><button className="text-button" disabled={busy} onClick={() => setEditing(false)}>收起</button></div>
      <form onSubmit={event => { event.preventDefault(); void savePersonal(new FormData(event.currentTarget)); }} onChange={event => {
        const target = event.target;
        if ((target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement) && target.name !== 'confirmed') {
          const confirmation = event.currentTarget.elements.namedItem('confirmed');
          if (confirmation instanceof HTMLInputElement) confirmation.checked = false;
          setCreateDeleted(false);
        }
      }}>
        <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
          <label>菜谱名称<input name="title" required maxLength={100}/></label>
          <div className="form-grid"><label>餐次<select name="meal" defaultValue={meal && meal !== 'main' ? meal : 'lunch'}>{meals.filter(([id]) => id && id !== 'main').map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><label>用途<select name="purpose" defaultValue={purpose || 'everyday'}><option value="everyday">日常搭配</option><option value="protein">补蛋白（个人标签）</option><option value="fat">补脂肪（个人标签）</option></select></label><label>菜谱分类<select name="cuisine" defaultValue={cuisine || 'other'}><option value="other">待补分类</option><option value="chinese">中式</option><option value="western">西式</option></select></label></div>
          <label className="check-row"><input type="checkbox" name="dessert" defaultChecked={tag === 'dessert'}/>同时标为甜点</label>
          <label>食材名称与分量<textarea name="ingredients" required maxLength={6000} rows={3} placeholder="逐项记录食材和用量，例如：豆腐 200 克、番茄 1 个"/></label>
          <label>详细做法<textarea name="steps" required maxLength={10000} rows={4} placeholder="按顺序记录准备、烹饪步骤和必要时间"/></label>
          <div className="form-grid"><label>份量数量（可选）<input name="yield_amount" type="number" min={0} max={100} step="any"/></label><label>份量单位<input name="yield_unit" maxLength={20} placeholder="例如：人份、碗、个"/></label><label>来源类型<select name="origin" required defaultValue=""><option value="">请明确来源</option><option value="user_original">我的原创（个人声明）</option><option value="adapted">参考后改编</option><option value="external">外部来源</option></select></label></div>
          <label>来源链接（可选）<input name="source_url" type="url" maxLength={2000} placeholder="https://…"/></label>
          <label>搭配笔记（可选）<textarea name="nutrition_notes" maxLength={2000} rows={2} placeholder="个人笔记会标明未经营养审核"/></label>
          <label className="check-row"><input type="checkbox" name="confirmed" required/>我已核对内容，确认保存到个人菜谱</label>
        </fieldset>
        <button className="primary" disabled={busy || createDeleted}>{busy ? <LoaderCircle size={16} className="spin"/> : <Check size={16}/>}保存菜谱</button>
      </form>
    </section>}
    {error && <p className="error" role="alert">{error}</p>}{notice && <p role="status" className="warning">{notice}</p>}
    {uncertainCreates.length > 0 && <section className="warning" aria-label="待核对的菜谱保存"><p>这些保存尚未收到确定结果。你可以继续确认保存；若已成功，会取回原记录，不会重复创建。</p>{uncertainCreates.map(attempt => <div className="row-between" key={attempt.key}><span>《{attempt.body.title}》</span><button type="button" className="text-button" disabled={busy} onClick={() => void sendCreate(attempt)}>继续确认这次保存</button></div>)}</section>}
    {editingId && <PersonalRecipeEditor key={editingId} recipeId={editingId} onClose={() => setEditingId(null)} onSaved={() => { setEditingId(null); setRevision(n => n + 1); setNotice('个人菜谱修改已保存，正在刷新列表。'); }} />}
    {loading ? <section className="panel empty" role="status"><LoaderCircle size={24} className="spin"/>正在读取当前分类…</section> : <div className="recipe-grid">{matches.map(row => <article key={row.scope + ':' + row.id} className={`panel recipe ${styles.recipe}`}>
      <span className="eyebrow">{mealNames[row.meal] || '餐次待补'} · {recipeCategoryNames[row.category]}{row.tags.includes('dessert') ? ' · 甜点' : ''}</span><h3>{row.title}</h3>
      {row.ingredients.length > 0 && <p>{row.ingredients.slice(0, 3).join('、')}{row.ingredients.length > 3 ? '…' : ''}</p>}
      <RecipeDietTags recipe={row}/>
      <details><summary>查看用量与做法<ChevronRight size={14}/></summary><RecipeDetail recipe={row} diet={diet}/></details>
      {row.scope === 'personal' && <div className="button-row"><button type="button" className="text-button" disabled={busy || (!!editingId && editingId !== row.id)} onClick={() => { setEditingId(row.id); setDeleting(null); }}>编辑这道菜谱</button>{deleting === row.id ? <><span className="small">删除这道个人菜谱？</span><button className="text-button danger" disabled={busy || editingId === row.id} onClick={() => { void removePersonal(row); }}>确认删除</button><button className="text-button" disabled={busy} onClick={() => setDeleting(null)}>保留</button></> : <button className="text-button danger" disabled={busy || editingId === row.id} onClick={() => setDeleting(row.id)}>删除</button>}</div>}
    </article>)}</div>}
    {!loading && pageState.hasMore && <div className="button-row space-top"><button type="button" className="secondary" disabled={busy || pageBusy} onClick={() => void loadMore()}>{pageBusy ? <LoaderCircle size={15} className="spin"/> : <ChevronRight size={15}/>}加载更多库内菜谱</button></div>}
    {!loading && !matches.length && <section className="panel empty"><CookingPot size={32}/><h3>{pageState.hasMore ? '当前已加载的内容中暂无匹配' : libraryQuery ? '还没有找到这道菜' : '这个分类还没有菜谱'}</h3><p>{pageState.hasMore ? '可继续加载后续菜谱，或切换分类。' : libraryQuery ? '可以换个关键词，或点击搜索查找来源。' : '先录入自己的配方，或搜索喜欢的菜名。未发布的公共内容不会出现在这里。'}</p></section>}
    {candidatePayload && <RecipeCandidates payload={candidatePayload} disabled={busy}/>}
  </>;
}
