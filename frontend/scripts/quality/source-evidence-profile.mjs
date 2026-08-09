import {URL} from 'node:url';

const SOURCE_PROFILES = Object.freeze({
  guardian: Object.freeze({
    label: 'The Guardian',
    hosts: Object.freeze(['theguardian.com']),
    preferredRootSelectors: Object.freeze(['article', 'main']),
  }),
  pikabu: Object.freeze({
    label: 'Пикабу',
    hosts: Object.freeze(['pikabu.ru']),
    preferredRootSelectors: Object.freeze([
      'article.story[data-story-id]',
      '.page-story > article.story',
      'main',
    ]),
  }),
  dazhong: Object.freeze({
    label: '大众新闻',
    hosts: Object.freeze(['dzplus.dzng.com', 'dzng.com']),
    preferredRootSelectors: Object.freeze([
      'article',
      '.article',
      '.article-content',
      'main',
      'body',
    ]),
  }),
  reddit: Object.freeze({
    label: 'Reddit',
    hosts: Object.freeze(['reddit.com']),
    preferredRootSelectors: Object.freeze(['shreddit-post', 'article', 'main']),
  }),
});

const AD_TOKENS = Object.freeze([
  'adfox',
  'doubleclick',
  'googlesyndication',
  'advert',
  'banner',
  'promo',
  'promokod',
  '广告',
  '商城',
]);

export function sourceProfile(source, rawUrl) {
  const profile = SOURCE_PROFILES[source];
  if (!profile) {
    throw new Error(`Unsupported source: ${source}`);
  }
  const url = new URL(rawUrl);
  if (url.protocol !== 'https:') {
    throw new Error('Evidence capture requires HTTPS.');
  }
  const hostname = url.hostname.toLowerCase();
  const allowed = profile.hosts.some(
    (host) => hostname === host || hostname.endsWith(`.${host}`),
  );
  if (!allowed) {
    throw new Error(`URL host ${hostname} is not registered for ${source}.`);
  }
  return profile;
}

export function isAdLikeAsset({src = '', href = '', text = '', width = 0, height = 0}) {
  const haystack = `${src} ${href} ${text}`.toLowerCase();
  if (AD_TOKENS.some((token) => haystack.includes(token))) {
    return true;
  }
  return width >= 400 && height > 0 && width / height >= 4.2;
}

export function normalizeEvidenceText(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

export {SOURCE_PROFILES};
