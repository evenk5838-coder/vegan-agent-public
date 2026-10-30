import { restaurantData } from './restaurant-results';

export type ChatCard = { id: string; type: string; status: string; payload: Record<string, unknown> };
export type FeaturePage = 'ingredients' | 'restaurants' | 'recipes' | 'foods';
export type FeatureResult = { page: FeaturePage; cards: ChatCard[]; failed?: boolean; currentLocation?: string; restaurantView?: { cardId: string; view: 'list' | 'map'; selectedKey?: string } };
const pages: Record<string, FeaturePage> = {
  ingredient_result: 'ingredients', ingredient_history: 'ingredients',
  restaurant_list: 'restaurants', recipe_candidates: 'recipes', recipe_list: 'recipes', recipe_detail: 'recipes',
  food_draft: 'foods', food_draft_list: 'foods', food_inventory: 'foods', food_detail: 'foods',
  food_storage_advice: 'foods', reminder_status: 'foods', pending_action: 'foods', knowledge_result: 'recipes',
};
/** One destination per feature and message; full payloads stay available on the feature page. */
export function featureResults(cards: ChatCard[], citations?: unknown): FeatureResult[] {
  const groups = new Map<FeaturePage, ChatCard[]>();
  for (const card of cards) {
    const page = pages[card.type];
    if (page) groups.set(page, [...(groups.get(page) || []), card]);
  }
  if (Array.isArray(citations) && citations.length) {
    const page = groups.has('foods') ? 'foods' : groups.keys().next().value || 'recipes';
    groups.set(page, [...(groups.get(page) || []), { id: 'citations', type: 'citations', status: 'ready', payload: { items: citations } }]);
  }
  return [...groups].map(([page, cards]) => ({ page, cards }));
}

/** Only a just-submitted successful turn may request navigation; history is inert. */
export function requestedRestaurantMap(message: { role: string; status: string; turn_id: string | null; cards?: ChatCard[] | null }, submitted: ReadonlySet<string>): FeatureResult | null {
  if (message.role !== 'assistant' || !message.turn_id || !submitted.has(message.turn_id) || message.status !== 'succeeded') return null;
  const card = message.cards?.find(card => {
    const hint = card.payload?.presentation as { preferred_view?: unknown } | undefined;
    const data = restaurantData(card.payload || {});
    return card.type === 'restaurant_list' && hint?.preferred_view === 'map' && data.scopeCurrent && data.items.some(item => item.location);
  });
  return card ? { page: 'restaurants', cards: [card], restaurantView: { cardId: card.id, view: 'map' } } : null;
}
