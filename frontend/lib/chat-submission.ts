export type IngredientAttachment = { kind: 'ingredient_photo'; job_id: string };

export type ChatAttempt = {
  owner: string;
  session: string;
  content: string;
  attachments: IngredientAttachment[];
  key: string;
};

/** A retry belongs to the same account, conversation, text and uploaded task set. */
export function prepareChatAttempt(
  previous: ChatAttempt | null,
  input: Omit<ChatAttempt, 'key'>,
  createKey: () => string,
): ChatAttempt {
  const content = input.content.trim() || (input.attachments.length ? '请核对这张配料表的三类饮食适配，并说明五辛和判断依据。' : '');
  if (!content || content.length > 4000) throw new Error('请填写不超过 4000 字的内容，或添加一张配料表。');
  const attachments = input.attachments.map(item => {
    if (item.kind !== 'ingredient_photo' || !item.job_id.trim()) throw new Error('图片任务无效，请重新核对附件。');
    return { kind: 'ingredient_photo' as const, job_id: item.job_id };
  });
  const sameAttachments = previous?.attachments.length === attachments.length &&
    attachments.every((item, index) => previous.attachments[index].kind === item.kind && previous.attachments[index].job_id === item.job_id);
  if (previous && previous.owner === input.owner && previous.session === input.session &&
      previous.content === content && sameAttachments) return previous;
  return { ...input, content, attachments, key: createKey() };
}
