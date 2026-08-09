import {describe, expect, it} from 'vitest';
import {
  isAdLikeAsset,
  normalizeEvidenceText,
  sourceProfile,
} from '../scripts/quality/source-evidence-profile.mjs';

describe('source evidence capture profiles', () => {
  it('accepts only registered source hosts over HTTPS', () => {
    expect(sourceProfile('guardian', 'https://www.theguardian.com/lifeandstyle/story').label).toBe(
      'The Guardian',
    );
    expect(sourceProfile('dazhong', 'https://m.dzplus.dzng.com/share/general/0/id').label).toBe(
      '大众新闻',
    );
    expect(() => sourceProfile('guardian', 'https://example.com/story')).toThrow(/not registered/);
    expect(() => sourceProfile('pikabu', 'http://pikabu.ru/story/id')).toThrow(/HTTPS/);
    expect(() => sourceProfile('nasa', 'https://nasa.gov/story')).toThrow(/Unsupported source/);
  });

  it('rejects advertising URLs, promotional text, and banner aspect ratios', () => {
    expect(isAdLikeAsset({src: 'https://yandex.ru/adfox/banner.png'})).toBe(true);
    expect(isAdLikeAsset({text: '大众商城促销'})).toBe(true);
    expect(isAdLikeAsset({width: 1200, height: 180})).toBe(true);
    expect(
      isAdLikeAsset({
        src: 'https://media.guim.co.uk/editorial-photo.jpg',
        width: 1200,
        height: 800,
      }),
    ).toBe(false);
  });

  it('normalizes extracted page text deterministically', () => {
    expect(normalizeEvidenceText('  one\n\t two  ')).toBe('one two');
  });
});
