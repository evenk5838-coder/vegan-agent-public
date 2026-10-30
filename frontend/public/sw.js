self.addEventListener('push', event => {
  let message = { title: '蔬食提醒', body: '你有食材提醒，请打开蔬食查看。', tag: 'shushi' };
  try { message = { ...message, ...event.data.json() }; } catch { /* 无有效正文时使用通用提醒。 */ }
  event.waitUntil(self.registration.showNotification(message.title, { body: message.body, tag: message.tag, icon: '/icon.svg', data: { url: '/' } }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.matchAll({ type: 'window' }).then(windows => {
    for (const item of windows) if (item.url.startsWith(self.location.origin)) return item.focus();
    return clients.openWindow('/');
  }));
});
// 不缓存账号、API或照片，避免退出后离线读到私有数据。
