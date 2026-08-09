/* global document, window */

import {createHash} from 'node:crypto';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import {chromium} from '@playwright/test';
import {
  isAdLikeAsset,
  normalizeEvidenceText,
  sourceProfile,
} from './source-evidence-profile.mjs';

const VIEWPORT = Object.freeze({width: 1600, height: 1000});
const CAPTURE_SIZE = Object.freeze({width: 1400, height: 820});

function parseArgs(argv) {
  const args = argv[0] === '--' ? argv.slice(1) : argv;
  const values = new Map();
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index];
    const value = args[index + 1];
    if (!key?.startsWith('--') || !value) {
      throw new Error('Usage: --source <source> --url <url> --output <png>');
    }
    values.set(key.slice(2), value);
  }
  const source = values.get('source');
  const url = values.get('url');
  const output = values.get('output');
  if (!source || !url || !output) {
    throw new Error('Usage: --source <source> --url <url> --output <png>');
  }
  return {
    source,
    url,
    output: path.resolve(output),
    html: values.has('html') ? path.resolve(values.get('html')) : null,
    fixture: values.has('fixture') ? path.resolve(values.get('fixture')) : null,
  };
}

async function firstRoot(page, selectors) {
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    if ((await locator.count()) > 0 && (await locator.isVisible().catch(() => false))) {
      return locator;
    }
  }
  throw new Error('The page did not expose a visible article root.');
}

async function extractEvidence(root, page) {
  const evidence = await root.evaluate((node) => {
    const clean = (value) => String(value ?? '').replace(/\s+/g, ' ').trim();
    const visible = (element) => {
      const style = window.getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
    };
    const title = [...node.querySelectorAll('h1')]
      .filter(visible)
      .map((element) => clean(element.textContent))
      .find((text) => text.length >= 12);
    const paragraphs = [...node.querySelectorAll('p')]
      .filter(visible)
      .map((element) => clean(element.textContent))
      .filter((text) => text.length >= 40 && !/(cookie|privacy|subscribe|\u5e7f\u544a|\u767b\u5f55)/i.test(text))
      .slice(0, 2);
    const metadata = [...node.querySelectorAll('time, [rel="author"], [class*="author"], [class*="date"]')]
      .filter(visible)
      .map((element) => clean(element.textContent || element.getAttribute('datetime')))
      .filter((text) => text.length > 0 && text.length <= 120)
      .filter((text, index, items) => items.indexOf(text) === index)
      .slice(0, 3);
    const images = [...node.querySelectorAll('img')]
      .filter(visible)
      .map((element) => {
        const anchor = element.closest('a');
        const box = element.getBoundingClientRect();
        return {
          src: element.currentSrc || element.src,
          alt: clean(element.alt),
          href: anchor?.href || '',
          context: clean(element.closest('figure, picture, div')?.textContent).slice(0, 180),
          width: Math.round(box.width || element.naturalWidth),
          height: Math.round(box.height || element.naturalHeight),
          naturalWidth: element.naturalWidth,
          naturalHeight: element.naturalHeight,
        };
      })
      .filter((image) => image.src && image.naturalWidth >= 320 && image.naturalHeight >= 180);
    const metaImage = document.querySelector('meta[property="og:image"], meta[name="twitter:image"]')?.content || null;
    return {title, paragraphs, metadata, images, metaImage};
  });
  return {
    ...evidence,
    pageTitle: normalizeEvidenceText(await page.title()),
  };
}

function selectHero(images) {
  return images
    .filter((image) => !isAdLikeAsset({
      src: image.src,
      href: image.href,
      text: `${image.alt} ${image.context}`,
      width: image.naturalWidth,
      height: image.naturalHeight,
    }))
    .sort((left, right) => {
      const leftArea = left.naturalWidth * left.naturalHeight;
      const rightArea = right.naturalWidth * right.naturalHeight;
      return rightArea - leftArea;
    })[0] ?? null;
}

async function renderEvidenceCard(page, payload) {
  await page.evaluate(({captureSize, evidence}) => {
    document.documentElement.innerHTML = '<head></head><body></body>';
    document.title = `${evidence.sourceLabel}: ${evidence.title}`;
    const style = document.createElement('style');
    style.textContent = `
      * { box-sizing: border-box; }
      html, body { margin: 0; width: 100%; min-height: 100%; background: #eef2f6; }
      body { padding: 40px; color: #111827; font-family: Inter, "Noto Sans SC", "Microsoft YaHei", Arial, sans-serif; }
      body > *:not(#evidence-card), iframe, [role="dialog"], [id*="privacy" i], [class*="privacy" i],
      [id*="consent" i], [class*="consent" i] { display: none !important; }
      #evidence-card { width: ${captureSize.width}px; height: ${captureSize.height}px; overflow: hidden;
        border: 1px solid #d7dde6; border-radius: 24px; background: #fff; box-shadow: 0 20px 50px rgba(15,23,42,.14); }
      .source { height: 76px; display: flex; align-items: center; justify-content: space-between; padding: 0 38px;
        background: #0f2948; color: #fff; font-size: 25px; font-weight: 800; letter-spacing: .02em; }
      .source .kind { color: #a9c7e8; font-size: 18px; font-weight: 600; }
      .content { height: 744px; display: grid; grid-template-columns: 56% 44%; }
      .copy { padding: 44px 42px 34px 46px; overflow: hidden; }
      h1 { margin: 0 0 22px; font-family: Georgia, "Noto Serif SC", serif; font-size: 46px; line-height: 1.08; letter-spacing: -.025em; }
      .meta { margin-bottom: 26px; color: #64748b; font-size: 17px; line-height: 1.45; }
      p { margin: 0 0 18px; color: #334155; font-size: 24px; line-height: 1.5; }
      .url { margin-top: 22px; color: #64748b; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .visual { min-width: 0; height: 100%; background: linear-gradient(145deg, #dbe4ee, #eef3f7); }
      .visual img { width: 100%; height: 100%; display: block; object-fit: cover; }
      .visual.empty { display: grid; place-items: center; padding: 60px; color: #64748b; font-size: 24px; text-align: center; }
      #evidence-card.no-hero .content { grid-template-columns: 1fr; }
      #evidence-card.no-hero .visual { display: none; }
      #evidence-card.no-hero .copy { padding-right: 90px; }
    `;
    document.head.append(style);
    const card = document.createElement('section');
    card.id = 'evidence-card';
    const hero = evidence.hero;
    if (!hero) card.className = 'no-hero';
    card.innerHTML = `
      <header class="source"><span>${evidence.sourceLabel}</span><span class="kind">SOURCE EVIDENCE</span></header>
      <div class="content">
        <div class="copy">
          <h1></h1>
          <div class="meta"></div>
          <div class="paragraphs"></div>
          <div class="url"></div>
        </div>
        <div class="visual ${hero ? '' : 'empty'}"></div>
      </div>`;
    card.querySelector('h1').textContent = evidence.title;
    card.querySelector('.meta').textContent = evidence.metadata.join(' · ');
    const paragraphs = card.querySelector('.paragraphs');
    for (const value of evidence.paragraphs) {
      const paragraph = document.createElement('p');
      paragraph.textContent = value;
      paragraphs.append(paragraph);
    }
    card.querySelector('.url').textContent = evidence.url;
    const visual = card.querySelector('.visual');
    if (hero) {
      const image = document.createElement('img');
      image.src = hero.src;
      image.alt = hero.alt;
      visual.append(image);
    }
    document.body.append(card);
  }, {captureSize: CAPTURE_SIZE, evidence: payload});
  await page.locator('#evidence-card img').waitFor({state: 'visible', timeout: 15_000}).catch(() => undefined);
  await page.locator('#evidence-card').screenshot({path: payload.output});
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const profile = sourceProfile(args.source, args.url);
  await mkdir(path.dirname(args.output), {recursive: true});
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const context = await browser.newContext({
      viewport: VIEWPORT,
      userAgent: 'god-news/0.1 (+https://github.com/Iris0fTheValley/god-news)',
      locale: args.source === 'pikabu' ? 'ru-RU' : 'en-GB',
      colorScheme: 'light',
    });
    const page = await context.newPage();
    let extracted;
    if (args.fixture) {
      const fixture = JSON.parse(await readFile(args.fixture, 'utf8'));
      if (fixture.source !== args.source || fixture.url !== args.url) {
        throw new Error('The normalized source fixture does not match the requested source URL.');
      }
      const blocks = Array.isArray(fixture.blocks) ? fixture.blocks : [];
      extracted = {
        title: normalizeEvidenceText(fixture.title),
        pageTitle: normalizeEvidenceText(fixture.title),
        metadata: [fixture.author_username, fixture.published_at]
          .map(normalizeEvidenceText)
          .filter(Boolean),
        paragraphs: blocks
          .filter((block) => block.kind === 'text')
          .map((block) => normalizeEvidenceText(block.text))
          .filter(Boolean)
          .slice(0, 2),
        images: blocks
          .filter((block) => block.kind === 'image')
          .map((block) => ({
            src: block.url,
            alt: normalizeEvidenceText(block.alt_text),
            href: '',
            context: '',
            width: block.width ?? 0,
            height: block.height ?? 0,
            naturalWidth: block.width ?? 0,
            naturalHeight: block.height ?? 0,
          })),
        metaImage: null,
      };
    } else if (args.html) {
      const html = await readFile(args.html, 'utf8');
      const base = `<base href="${args.url.replaceAll('"', '&quot;')}">`;
      await page.setContent(html.replace(/<head([^>]*)>/i, `<head$1>${base}`), {
        waitUntil: 'domcontentloaded',
        timeout: 60_000,
      });
    } else {
      const response = await page.goto(args.url, {waitUntil: 'domcontentloaded', timeout: 60_000});
      if (!response || !response.ok()) {
        throw new Error(`Source page returned HTTP ${response?.status() ?? 'unknown'}.`);
      }
    }
    if (!args.fixture && /ddos-guard|access denied|captcha/i.test(await page.title())) {
      throw new Error(`Source protection page was returned instead of an article: ${await page.title()}`);
    }
    if (!extracted) {
      await page.waitForTimeout(1_500);
      const root = await firstRoot(page, profile.preferredRootSelectors);
      extracted = await extractEvidence(root, page);
    }
    const title = extracted.title || extracted.pageTitle;
    if (!title || title.length < 12) {
      throw new Error('The source page did not expose a credible article title.');
    }
    const hero = selectHero(extracted.images) ?? (
      extracted.metaImage && !isAdLikeAsset({src: extracted.metaImage})
        ? {src: extracted.metaImage, alt: '', href: '', context: ''}
        : null
    );
    const evidence = {
      source: args.source,
      sourceLabel: profile.label,
      url: args.url,
      title,
      metadata: extracted.metadata,
      paragraphs: extracted.paragraphs,
      hero,
      output: args.output,
    };
    await renderEvidenceCard(page, evidence);
    const bytes = await import('node:fs/promises').then(({readFile}) => readFile(args.output));
    const manifest = {
      schema_version: 1,
      source: args.source,
      canonical_url: args.url,
      captured_at: new Date().toISOString(),
      capture_kind: 'semantic_article_evidence',
      capture_transport: args.fixture
        ? 'normalized_source_fixture'
        : args.html
          ? 'reviewed_html_snapshot'
          : 'live_browser',
      title,
      selected_image_url: hero?.src ?? null,
      selected_image_alt: hero?.alt ?? null,
      width: CAPTURE_SIZE.width,
      height: CAPTURE_SIZE.height,
      sha256: createHash('sha256').update(bytes).digest('hex'),
      publish_eligible: false,
      rights_note: 'Internal editorial review evidence; republication rights require human review.',
    };
    await writeFile(`${args.output}.json`, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
    process.stdout.write(`${JSON.stringify(manifest, null, 2)}\n`);
  } finally {
    await browser.close();
  }
}

await main();
