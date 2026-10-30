export type AudioInput = { deviceId: string; label: string; kind?: string };

/** Device labels are a convenience heuristic, not a hardware identity guarantee. */
export function isPhoneMicrophone(label: string): boolean {
  return /iphone|ipad|continuity|连续互通|連續互通|接续互通|接續互通|手机|手機/i.test(label);
}

export function preferredMicrophone(inputs: AudioInput[], selectedId?: string): AudioInput | null {
  const physical = inputs.filter(input => (!input.kind || input.kind === 'audioinput')
    && input.deviceId.trim() && !['default', 'communications'].includes(input.deviceId.trim().toLowerCase())
    && input.label.trim());
  // Explicit, visible user selection may include a phone; auto-selection never does.
  const selected = selectedId && physical.find(input => input.deviceId === selectedId);
  if (selected) return selected;
  const local = physical.filter(input => !isPhoneMicrophone(input.label));
  return local.find(input => /built[\s-]?in|internal[\s-]?(?:microphone|mic)|macbook|内置|內置|内建|內建/i.test(input.label))
    ?? local[0] ?? null;
}

/** Known full outro, not a broad ban on ordinary words such as 点赞 or 谢谢. */
export function suspiciousTranscript(text: string): boolean {
  const compact = text.normalize('NFKC').replace(/[\s\p{P}\p{S}\u200B-\u200D\uFEFF]/gu, '');
  return ['点赞订阅转发打赏支持明镜与点点栏目',
    '點讚訂閱轉發打賞支持明鏡與點點欄目', '點贊訂閱轉發打賞支持明鏡與點點欄目']
    .some(outro => compact.endsWith(outro));
}

/** A conservative silence gate. It does not identify speech or speaker intent. */
export class VoiceSignalMeter {
  private audible = 0;
  private peakValue = 0;

  add(samples: Float32Array, sampleRate: number): void {
    if (!samples.length || !Number.isFinite(sampleRate) || sampleRate <= 0) return;
    let sum = 0;
    for (const value of samples) {
      if (!Number.isFinite(value)) return;
      sum += value;
    }
    const mean = sum / samples.length;
    let squareSum = 0, peak = 0;
    for (const value of samples) {
      const centered = value - mean;
      squareSum += centered * centered;
      peak = Math.max(peak, Math.abs(centered));
    }
    this.peakValue = Math.max(this.peakValue, peak);
    if (Math.sqrt(squareSum / samples.length) >= 0.0015)
      this.audible += samples.length / sampleRate * 1000;
  }

  summary(): { audibleMs: number; peak: number; hasSignal: boolean } {
    return { audibleMs: this.audible, peak: this.peakValue,
      hasSignal: this.audible >= 120 && this.peakValue >= 0.004 };
  }
}
