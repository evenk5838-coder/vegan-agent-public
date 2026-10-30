import { ExternalLink } from 'lucide-react';
import { safeExternalUrl } from '../lib/api';
import { recipeVerdict, type RecipeView } from '../lib/recipe-display';
import type { Diet } from './IngredientResult';
import styles from './recipe-detail.module.css';

const dietNames = { pure: '纯净素', vegan: 'Vegan', lacto_ovo: '蛋奶素' };

/** Independent rule outcomes, never a single exclusive recipe category. */
export function RecipeDietTags({ recipe }: { recipe: RecipeView }) {
  return <div className={styles.dietTags} aria-label="三类饮食独立适配结果">{(Object.keys(dietNames) as Diet[]).map(key => {
    const verdict = recipeVerdict(recipe, key);
    const statusStyle = verdict === '不符合' ? styles.denied : verdict === '待核实' ? styles.uncertain : styles.suitable;
    return <span key={key} className={`${styles.dietTag} ${statusStyle}`}>{dietNames[key]}：{verdict}</span>;
  })}</div>;
}

/** Shared read-only tutorial view for the recipe page and conversation cards. */
export default function RecipeDetail({ recipe, diet }: { recipe: RecipeView; diet?: Diet }) {
  const source = safeExternalUrl(recipe.sourceUrl);
  const license = safeExternalUrl(recipe.licenseUrl);
  const original = recipe.originalContent;
  const result = diet && recipe.evaluation?.results && typeof recipe.evaluation.results === 'object'
    ? (recipe.evaluation.results as Record<string, unknown>)[diet] : null;
  const evidence = result && typeof result === 'object' ? result as Record<string, unknown> : {};
  const evidenceLines = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
  const denied = evidenceLines(evidence.denied), unknown = evidenceLines(evidence.unknown);
  return <div className={styles.root}>
    {(recipe.servingsText || recipe.yieldText || recipe.servings) && <p className={styles.meta}>份量：{recipe.servingsText || recipe.yieldText || `${recipe.servings} 份`}</p>}
    {recipe.ingredients.length > 0 && <section><h4>原材料与用量</h4><ul>{recipe.ingredients.map((line, index) => <li key={index}>{line}</li>)}</ul></section>}
    {recipe.steps.length > 0 && <section><h4>制作步骤</h4><ol>{recipe.steps.map((line, index) => <li key={index}>{line}</li>)}</ol></section>}
    {recipe.notes.length > 0 && <section><h4>注意事项</h4><ul>{recipe.notes.map((line, index) => <li key={index}>{line}</li>)}</ul></section>}
    {(recipe.evaluation || diet) && <RecipeDietTags recipe={recipe}/>}
    {diet && (denied.length > 0 || unknown.length > 0) && <p className={styles.meta}>{denied.length > 0 && `命中的不适配配料：${denied.join('、')}。`}{unknown.length > 0 && `仍需核对的配料：${unknown.join('、')}。`}</p>}
    {source && <a className={styles.source} href={source} target="_blank" rel="noreferrer">来源<ExternalLink size={13}/></a>}
    <details><summary className={styles.source}>补充信息与来源核对</summary>
    {(recipe.sourceAttribution || recipe.licenseName) && <p className={styles.meta}>{recipe.sourceAttribution}{recipe.sourceAttribution && recipe.licenseName ? ' · ' : ''}{license ? <a href={license} target="_blank" rel="noreferrer">{recipe.licenseName || '许可说明'}</a> : recipe.licenseName}{recipe.machineTranslationUnreviewed ? ' · 中文机器翻译' : ''}</p>}
    {recipe.languageNotice && <p className={styles.notice}>{recipe.languageNotice}</p>}
    {!recipe.tutorialComplete && <p className={styles.notice}>{recipe.tutorialMissing.length ? `信息未齐：${recipe.tutorialMissing.join('、')}。` : '这份来源的核对尚未完成。'}现有内容保留，未自动补写。</p>}
    <p className={styles.meta}>{recipe.scope === 'public' ? '公共来源 · 只读' : '个人菜谱'} · {recipe.tutorialLabel}</p>
    <p className={styles.meta}>菜系：{recipe.category === 'chinese' ? '中式' : recipe.category === 'western' ? '西式' : '待补分类，未按来源国家或语言推断'}</p>
    {!recipe.servingsText && !recipe.yieldText && !recipe.servings && <p className={styles.meta}>参考份量：未注明，未自动估算。</p>}
    {recipe.sourceCompleteUnreviewed && <p className={styles.meta}>来源结构齐备仍不代表已经逐条复核或试做。</p>}
    {recipe.qualityMessages.length > 0 && <section className={styles.notice}><h4>内容核对提示</h4><ul>{recipe.qualityMessages.map((message, index) => <li key={index}>{message}</li>)}</ul></section>}
    {recipe.notice && <p className={styles.meta}>{recipe.notice}</p>}
    {recipe.reviewSections.map((section, index) => <section key={index}><h4>{section.heading || '来源补充说明'}</h4><p className={styles.body}>{section.text}</p></section>)}
    {recipe.body && <details><summary className={styles.source}>查看完整正文与补充内容</summary><p className={styles.body}>{recipe.body}</p></details>}
    {recipe.nutritionNotes && <section className={styles.nutrition}><h4>{recipe.nutritionLabel}</h4><p>{recipe.nutritionNotes}</p>{recipe.scope === 'public' && <p className={styles.meta}>来源内容不等于个体营养评估；未提供热量或营养素测算。</p>}</section>}
    <p className={styles.meta}>饮食判断仅依据所示配料与当前规则；译文或教程齐备不代表饮食适配、营养审核或食品安全保证。</p>
    {original && <details><summary className={styles.source}>查看来源原文，核对译文</summary>
      {original.title && <h4>{original.title}</h4>}
      {original.servingsText && <p className={styles.meta}>原文份量：{original.servingsText}</p>}
      {original.ingredients.length > 0 && <section><h4>原文食材与用量</h4><ul>{original.ingredients.map((line, index) => <li key={index}>{line}</li>)}</ul></section>}
      {original.steps.length > 0 && <section><h4>原文做法</h4><ol>{original.steps.map((line, index) => <li key={index}>{line}</li>)}</ol></section>}
      {original.body && <p className={styles.body}>{original.body}</p>}
      {original.notes.length > 0 && <section><h4>原文注意事项</h4><ul>{original.notes.map((line, index) => <li key={index}>{line}</li>)}</ul></section>}
      {original.reviewSections.map((section, index) => <section key={index}><h4>{section.heading || '原文补充说明'}</h4><p className={styles.body}>{section.text}</p></section>)}
    </details>}
    {recipe.scope === 'personal' && recipe.origin === 'user_original' && <p className={styles.meta}>来源：个人原创（用户声明）。此声明不代表内容已审核或饮食适配已经确认。</p>}
    {!source && !(recipe.scope === 'personal' && recipe.origin === 'user_original') && <p className={styles.meta}>未提供可打开的来源链接。</p>}
    </details>
  </div>;
}
