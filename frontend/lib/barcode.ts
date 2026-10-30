export type BarcodeProduct = {
  code: string; product_name: string; product_name_zh: string; brands: string;
  quantity: string; ingredients_text: string; ingredients_text_zh: string;
  last_modified_t: number | null;
};
export type BarcodeLookup = {
  code: string; status: 'found' | 'no_ingredients' | 'not_found' | 'unavailable';
  product: BarcodeProduct | null; ingredients_text: string; label_warnings: string[];
  complete: false; requires_confirmation: true;
  source: { name: string; url: string }; message: string;
};

export function normalizeBarcode(value: string): string | null {
  const code = value.replace(/\s+/g, '');
  if (![8, 12, 13, 14].includes(code.length) || !/^[0-9]+$/.test(code)) return null;
  const total = [...code.slice(0, -1)].reverse().reduce((sum, digit, index) => sum + Number(digit) * (index % 2 === 0 ? 3 : 1), 0);
  return Number(code.at(-1)) === (10 - total % 10) % 10 ? code : null;
}

export function barcodeIngredientBody(text: string, confirmed: boolean, warnings: string[] = []) {
  if (!text.trim()) throw new Error('请先补充包装上的配料。');
  if (text.trim().length > 12000) throw new Error('配料文字超过本次核对上限，请重新核对来源或改拍配料表。');
  if (!confirmed) throw new Error('请先对照包装核对完整配料。');
  return { text: text.trim(), complete: true, label_warnings: warnings.filter(value => typeof value === 'string' && value.trim()) };
}

export function barcodeCameraError(reason: unknown): string {
  const name = reason instanceof Error ? reason.name : '';
  if (['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(name)) return '没有获得相机权限。可以在浏览器设置中允许相机，或直接输入条码数字。';
  if (name === 'NotFoundError') return '没有找到可用相机，可以选择条码图片或输入数字。';
  if (name === 'NotReadableError') return '相机暂时无法使用，请关闭其他占用相机的应用，或直接输入数字。';
  return '相机暂时无法读取条码，可以选择清晰的条码图片或输入数字。';
}

/** Release every local camera track even if the decoder control was never returned. */
export function stopBarcodeVideo(video: Pick<HTMLVideoElement, 'srcObject'> | null) {
  const stream = video?.srcObject;
  if (stream && 'getTracks' in stream) stream.getTracks().forEach(track => track.stop());
  if (video) video.srcObject = null;
}
