export type NotificationTask = {
  id: string;
  status: string;
  due: string;
  result?: { accepted_by_service?: number; device_display?: string; message?: string; error?: { message?: string } } | null;
};

export function notificationTaskState(task: NotificationTask) {
  const labels: Record<string, string> = {
    pending: '已排期，等待处理', queued: '已排期，等待处理', running: '正在提交推送',
    waiting_configuration: '提醒已建，通知服务未配置', waiting_subscription: '提醒已建，等待设备订阅',
    accepted: '推送服务已接受，设备显示待核对', failed: '推送未完成', cancelled: '提醒已取消',
  };
  return {
    label: labels[task.status] || '任务状态待核对',
    accepted: task.status === 'accepted',
    // There is no device receipt/ack endpoint in the current API.
    deviceDisplay: '待真机核对',
    error: task.result?.error?.message || task.result?.message || '',
  };
}

export function notificationLevels(tasks: NotificationTask[] | null, subscriptionSaved: boolean) {
  const accepted = tasks?.filter(task => notificationTaskState(task).accepted).length || 0;
  return [
    { title: '提醒任务', detail: !tasks ? '尚未取回提醒任务；请刷新状态后核对。' : tasks.length ? `最近 ${tasks.length} 项提醒任务已记录，逐项状态见下方。` : '还没有提醒任务；请在食材中设置提醒时间。' },
    { title: '设备订阅', detail: subscriptionSaved ? '此设备的通知订阅已保存到当前账号。' : '尚未确认此设备订阅已保存到当前账号；浏览器允许通知仍需完成订阅。' },
    { title: '服务接受', detail: !tasks ? '尚未取回服务接受状态，请刷新后核对。' : accepted ? `最近任务中有 ${accepted} 项已获推送服务接受，仍需核对手机是否显示。` : '目前没有查到服务已接受的提醒任务。' },
    { title: '真机显示', detail: '当前没有设备送达回执；需在真实手机上核对通知是否出现。' },
  ];
}

export function samePushKey(existing: ArrayBuffer | null | undefined, configured: Uint8Array): boolean {
  if (!existing) return false;
  const bytes = new Uint8Array(existing);
  return bytes.length === configured.length && bytes.every((value, index) => value === configured[index]);
}

export function pushApplicationKey(value: string): Uint8Array<ArrayBuffer> {
  const trimmed = value.trim();
  if (!/^[A-Za-z0-9_-]+={0,2}$/.test(trimmed)) throw new Error('通知配置格式不正确，请稍后刷新配置。');
  const padded = trimmed.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - trimmed.length % 4) % 4);
  let decoded: string;
  try { decoded = atob(padded); } catch { throw new Error('通知配置格式不正确，请稍后刷新配置。'); }
  const bytes = Uint8Array.from(decoded, char => char.charCodeAt(0));
  if (bytes.length !== 65 || bytes[0] !== 4) throw new Error('通知配置格式不正确，请稍后刷新配置。');
  return bytes;
}
