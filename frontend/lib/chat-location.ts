const locationPrefix = /^我的本次位置是\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*[（(]GCJ-02[）)]。?\s*/;

export function messageLocation(text: string): string | null {
  const match = text.match(locationPrefix);
  return match && Math.abs(Number(match[1])) <= 180 && Math.abs(Number(match[2])) <= 90
    ? `${match[1]},${match[2]}` : null;
}

/** Only an explicit nearby dining request starts a one-off device lookup. */
export function needsRestaurantLocation(text: string, restaurantContext = false): boolean {
  if (messageLocation(text)) return false;
  if (/(?:不要|不用|无需|别|不想|不使用|不允许|不授权).{0,6}(?:定位|位置)/.test(text)) return false;
  if (/(?:不|别|不用|不要|停止|取消)(?:再)?(?:帮我)?(?:查找|查|找|搜|推荐)/.test(text)) return false;
  const clauses = text.split(/[，。！？；\n,!?;]/).filter(clause => !/怎么|如何|为什么|教程/.test(clause));
  const requested = clauses.join('，');
  const dining = /餐厅|餐馆|饭店|饭馆|素食馆|素菜馆|蔬食|素食|吃素|吃饭|吃的/.test(requested);
  if (!dining && !restaurantContext) return false;
  // An explicit first-person reference takes precedence; named areas such as
  // “北京朝阳附近” must continue through the ordinary regional search.
  if (/(?:我(?:们)?的?(?:当前位置|位置|附近|周围|周边|身边|这里|这边))|当前位置|离我最近/.test(requested)) return true;
  return clauses.some(clause => {
    const lead = clause.replace(/^(?:(?:请|麻烦|你|帮我|帮忙|给我|我想|我|想要|想|要|可以|能不能|能|再|也|就|那就|那|的|在|用|一下|一找|看看|看一下|看|找找|找一下|找|查询|查找|查一下|查|搜一下|搜索|搜|推荐一下|推荐|一家|几家|两家|地图|上|打开|显示|展示)\s*)+/, '').trim();
    return /^(?:(?:这(?:里|边)|当前(?:位置|地点))的?)?(?:附近|周围|周边|身边)/.test(lead);
  });
}

export function withCurrentLocation(text: string, location: string): string {
  const content = `我的本次位置是 ${location}（GCJ-02）。${text.replace(locationPrefix, '').trim()}`;
  if (!messageLocation(content)) throw new Error('本次位置无效，请重新定位或填写地区。');
  if (content.length > 4000) throw new Error('内容太长，请稍作精简后再发送。');
  return content;
}

export function visibleChatText(text: string): string {
  return messageLocation(text) ? text.replace(locationPrefix, '') : text;
}

/** A short place answer after a local location failure still carries its task. */
export function regionalRestaurantRequest(answer: string, original: string): string {
  const region = answer.trim().replace(/^(?:那就|改成|换成|我在|那|就|在)/, '').replace(/[吧。！!]$/, '');
  if (!/^[\p{L}\d\s·-]{2,24}$/u.test(region) || /菜谱|食材|配料|餐厅|素食|记录|推荐|帮|请|查|找|地图|不了|算了|取消|定位|附近|周围|周边|哪里|不知道|不清楚/.test(region)) return answer;
  return `请查找${region}的素食餐厅。\n原请求：${original}\n本次按上述地区查询，不使用设备定位。`;
}
