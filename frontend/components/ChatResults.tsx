'use client';
import { ArrowUpRight, MapPin, ChevronRight } from 'lucide-react';
import { featureResults, type ChatCard, type FeatureResult } from '../lib/chat-presentation';
import { restaurantData, restaurantDistance, parseCoordinate } from '../lib/restaurant-results';
import FoodDraft from './FoodDraft';
import ChatAction from './ChatAction';
import RestaurantDietLabels from './RestaurantDietLabels';
import styles from './chat-results-compact.module.css';

const labels = { foods: '查看食材详情', ingredients: '查看配料结果', restaurants: '查看餐厅与地图', recipes: '查看菜谱' };
export default function ChatResults({ cards = [], citations, failed = false, currentLocation, onOpen, version = 0 }: {
  cards?: ChatCard[]; citations?: unknown; failed?: boolean; currentLocation?: string; onOpen: (result: FeatureResult) => void; version?: number;
}) {
  const groups = featureResults(cards, citations).filter(group =>
    group.cards.some(card => !['food_draft', 'pending_action'].includes(card.type))
    || (failed && group.cards.some(card => card.type === 'food_draft')));
  return <div className={styles.results}>
    {cards.filter(card => card.type === 'food_draft' && typeof card.payload?.draft_id === 'string').map(card =>
      !failed && <FoodDraft key={card.id+':'+version} draftId={String(card.payload.draft_id)} compact onDetails={() => onOpen({ page: 'foods', cards: [card] })}/>)}
    {cards.filter(card => card.type === 'pending_action').map(card =>
      <ChatAction key={card.id+':'+version} payload={card.payload} disabled={failed} compact onDetails={() => onOpen({ page: 'foods', cards: [card], failed })}/>)}
    {groups.filter(group => group.page === 'restaurants').map(group => {
      const card = group.cards.find(card => card.type === 'restaurant_list');
      if (!card) return null;
      const data = restaurantData(card.payload);
      if (!data.scopeCurrent || !data.items.length) return null;
      const open = (view: 'list' | 'map', selectedKey?: string) => onOpen({ ...group, failed, currentLocation, restaurantView: { cardId: card.id, view, selectedKey } });
      return <section key={card.id} className={styles.restaurants} aria-label="对话推荐餐厅">
        <p className={styles.restaurantCount}>已找到 {data.items.length} 家{data.pagination.hasMore ? ' · 还有更多可查' : ''}</p>
        {data.items.slice(0, 3).map(item => <button type="button" key={item.key} className={styles.restaurantChoice} onClick={() => open(item.location ? 'map' : 'list', item.key)} aria-label={`查看${item.name}${item.location ? '的地图位置' : '的详情'}`}>
          <span><strong>{item.name}</strong><small>{restaurantDistance(item, data.center, parseCoordinate(currentLocation)) || item.address}</small><RestaurantDietLabels value={item.dietLabels} compact/></span><ChevronRight size={16}/>
        </button>)}
        <div className={styles.actions}>
          {data.items.some(item => item.location) && <button type="button" className="text-button" onClick={() => open('map')}><MapPin size={15}/>看地图</button>}
          <button type="button" className="text-button" onClick={() => open('list')}>查看全部 {data.items.length} 家{data.pagination.hasMore ? ' / 继续查找' : ''}<ArrowUpRight size={14}/></button>
        </div>
      </section>;
    })}
    {groups.length > 0 && <div className={styles.links} aria-label="查看功能详情">{groups.filter(group => group.page !== 'restaurants' || !group.cards.some(card => card.type === 'restaurant_list' && restaurantData(card.payload).scopeCurrent && restaurantData(card.payload).items.length)).map(group =>
      <button key={group.page} type="button" onClick={() => onOpen({ ...group, failed, currentLocation })}>{group.page==='recipes'&&!group.cards.some(card=>card.type.startsWith('recipe_'))?'查看参考资料':labels[group.page]}<ArrowUpRight size={14}/></button>)}</div>}
  </div>;
}
