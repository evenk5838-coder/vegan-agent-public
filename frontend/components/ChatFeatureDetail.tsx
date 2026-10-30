'use client';
import { ArrowRight, ChevronRight, CookingPot } from 'lucide-react';
import IngredientPhotoResult from './IngredientPhotoResult';
import RecipeCandidates from './RecipeCandidates';
import RecipeDetail from './RecipeDetail';
import { recipeViews } from '../lib/recipe-display';
import { chatReadTypes } from '../lib/chat-read';
import RestaurantResults from './RestaurantResults';
import ChatReadResult from './ChatReadResult';
import ChatAction from './ChatAction';
import FoodDraft from './FoodDraft';
import StorageGuidance from './StorageGuidance';
import Citations from './Citations';
import type { ChatCard as Card, FeatureResult } from '../lib/chat-presentation';
import styles from './chat.module.css';
function ResultCard({ card, failed, onNavigate, currentLocation, restaurantView }: { card: Card; failed?: boolean; onNavigate: (page: string) => void; currentLocation?: string; restaurantView?: FeatureResult['restaurantView'] }) {
  const p = card.payload || {};
  if (card.type === 'citations') return <Citations items={p.items}/>;
  if (card.type === 'ingredient_result' && p.result) return <IngredientPhotoResult payload={p} onNavigate={() => onNavigate('ingredients')}/>;
  if (card.type === 'food_draft' && typeof p.draft_id === 'string') {
    return failed ? <div className={styles.card}><h3>已整理的食材草稿</h3><p>本次对话未完成。这份草稿尚需单独核对，不能据此认为食材已保存。</p><button className="secondary" onClick={() => onNavigate('foods')}>到记食材核对<ArrowRight size={15}/></button></div> : <FoodDraft draftId={p.draft_id}/>;
  }
  if (card.type === 'restaurant_list') {
    return <RestaurantResults payload={p} currentLocation={currentLocation} initialMapOpen={restaurantView?.cardId === card.id && restaurantView.view === 'map'} initialSelectedKey={restaurantView?.cardId === card.id ? restaurantView.selectedKey : undefined}/>;
  }
  if (card.type === 'recipe_candidates') return <RecipeCandidates payload={p} disabled={failed}/>;
  if (card.type === 'recipe_list') {
    const rows = recipeViews(p.items);
    return <section className={styles.card}><h3><CookingPot size={17}/> 菜谱推荐</h3>{rows.length ? rows.map(row => <details key={row.scope + ':' + row.id}><summary>{row.title}<ChevronRight size={14}/></summary><RecipeDetail recipe={row}/></details>) : <p>{typeof p.notice === 'string' ? p.notice : '菜谱库内暂无符合条件的结果。'}</p>}<button className="text-button" onClick={() => onNavigate('recipes')}>进入选菜谱<ChevronRight size={14}/></button></section>;
  }
  if (card.type === 'food_storage_advice' && typeof p.food_id === 'string') return <StorageGuidance foodId={p.food_id}/>;
  if (card.type === 'pending_action') return <ChatAction payload={p} disabled={failed}/>;
  if (chatReadTypes.has(card.type)) return <ChatReadResult type={card.type} payload={p} disabled={failed} onNavigate={onNavigate}/>;
  return <div className={styles.card}><p>这项结果暂无法在对话中展开，请到对应功能查看。</p></div>;
}


export default function ChatFeatureDetail({ result, onNavigate, onClose }: { result: FeatureResult; onNavigate: (page: string) => void; onClose: () => void }) {
  const titles = { foods: '食材详情', ingredients: '配料结果', restaurants: '餐厅与地图', recipes: '菜谱与资料' };
  return <section aria-label="对话结果详情">
    <div className="row-between"><h2>{titles[result.page]}</h2><button type="button" className="text-button" onClick={onClose}>打开{result.page === 'foods' ? '冰箱库存' : '功能首页'}<ArrowRight size={15}/></button></div>
    {result.cards.map(card => <ResultCard key={card.id} card={card} failed={result.failed} onNavigate={onNavigate} currentLocation={result.currentLocation} restaurantView={result.restaurantView}/>) }
  </section>;
}
