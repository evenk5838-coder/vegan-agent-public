import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/barcode.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { normalizeBarcode, barcodeIngredientBody, barcodeCameraError, stopBarcodeVideo } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));

test('GTIN8, UPC-A, EAN13 and GTIN14 check digits are accepted without dropping zeroes', () => {
  for (const code of ['96385074', '036000291452', '4006381333931', '10012345678902']) assert.equal(normalizeBarcode(code), code);
  assert.equal(normalizeBarcode(' 0360 0029 1452\n'), '036000291452');
});

test('invalid scans cannot become a public product lookup', () => {
  for (const code of ['', '4006381333932', '1234567', 'https://example.com/4006381333931', '４００６３８１３３３９３１']) assert.equal(normalizeBarcode(code), null);
});

test('product ingredients cannot be evaluated as complete until the user confirms the packaging', () => {
  assert.throws(() => barcodeIngredientBody('水、燕麦', false), /核对完整/);
  assert.throws(() => barcodeIngredientBody('', true), /补充/);
  assert.throws(() => barcodeIngredientBody('豆'.repeat(12001), true), /上限/);
  assert.deepEqual(barcodeIngredientBody(' 水、燕麦 ', true, ['可能含乳']), { text: '水、燕麦', complete: true, label_warnings: ['可能含乳'] });
});

test('permission and device failures retain a manual input alternative', () => {
  for (const name of ['NotAllowedError', 'NotFoundError', 'NotReadableError', 'UnknownError']) {
    const error = new Error('private device error'); error.name = name;
    const message = barcodeCameraError(error);
    assert.match(message, /输入.*数字/);
    assert.doesNotMatch(message, /private device error/);
  }
});

test('camera stop releases every track and detaches video, including repeated stops', () => {
  let stopped = 0;
  const video = { srcObject: { getTracks: () => [{ stop: () => stopped++ }, { stop: () => stopped++ }] } };
  stopBarcodeVideo(video);
  assert.equal(stopped, 2);
  assert.equal(video.srcObject, null);
  stopBarcodeVideo(video); stopBarcodeVideo(null);
  assert.equal(stopped, 2);
});
