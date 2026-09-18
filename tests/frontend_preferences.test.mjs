import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { t, m, a, setText, locale, formatDate, localize } from '../app/static/i18n.js';

const preferencesSource = await readFile(new URL('../app/static/preferences.js', import.meta.url), 'utf8');

function bootstrap({ saved = {}, systemDark = false, storageDenied = false } = {}) {
  const values = new Map(Object.entries(saved));
  const writes = [];
  const window = new EventTarget();
  const media = new EventTarget();
  media.matches = systemDark;
  const meta = { setAttribute(name, value) { this[name] = value; } };
  const document = {
    documentElement: { dataset: {}, lang: '' },
    querySelector(selector) { return selector === 'meta[name="theme-color"]' ? meta : null; },
  };
  const localStorage = {
    getItem(key) {
      if (storageDenied) throw new Error('Storage is disabled');
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      if (storageDenied) throw new Error('Storage is disabled');
      values.set(key, value);
      writes.push([key, value]);
    },
  };
  let appearanceChanges = 0;
  window.addEventListener('appearancechange', () => appearanceChanges++);
  runInNewContext(preferencesSource, {
    window, document, localStorage, Event,
    matchMedia(query) {
      assert.equal(query, '(prefers-color-scheme: dark)');
      return media;
    },
  });
  return {
    document, meta, values, writes,
    preferences: window.uiPreferences,
    get appearanceChanges() { return appearanceChanges; },
    setSystemDark(matches) {
      media.matches = matches;
      media.dispatchEvent(new Event('change'));
    },
    storage(key, newValue) {
      window.dispatchEvent(Object.assign(new Event('storage'), { key, newValue }));
    },
  };
}

test('saved appearance and language are applied synchronously, before the app starts', () => {
  const app = bootstrap({ saved: { 'zhixu.theme': 'dark', 'zhixu.language': 'en' } });
  assert.equal(app.document.documentElement.dataset.theme, 'dark');
  assert.equal(app.document.documentElement.lang, 'en');
  assert.equal(app.meta.content, '#171e1b');
  assert.deepEqual(app.writes, [], 'initial rendering must not overwrite preferences');
  app.setSystemDark(true);
  app.setSystemDark(false);
  assert.equal(app.document.documentElement.dataset.theme, 'dark', 'a saved choice wins over the OS');
});

test('appearance follows the OS until the user explicitly chooses a theme', () => {
  const app = bootstrap({ systemDark: true });
  assert.equal(app.document.documentElement.dataset.theme, 'dark');
  assert.equal(app.document.documentElement.lang, 'zh-CN');
  app.setSystemDark(false);
  assert.equal(app.document.documentElement.dataset.theme, 'light');
  assert.equal(app.meta.content, '#eef1ee');
  assert.equal(app.appearanceChanges, 1);
  assert.deepEqual(app.writes, [], 'an OS change must not become a saved explicit choice');

  app.preferences.toggleTheme();
  assert.equal(app.document.documentElement.dataset.theme, 'dark');
  assert.equal(app.values.get('zhixu.theme'), 'dark');
  app.setSystemDark(true);
  app.setSystemDark(false);
  assert.equal(app.document.documentElement.dataset.theme, 'dark');
  assert.equal(app.appearanceChanges, 1, 'OS events are ignored after an explicit choice');
});

test('theme and language toggles persist independently and survive a fresh load', () => {
  const app = bootstrap();
  app.preferences.toggleTheme();
  app.preferences.toggleLanguage();
  assert.equal(app.document.documentElement.dataset.theme, 'dark');
  assert.equal(app.document.documentElement.lang, 'en');
  assert.deepEqual(Object.fromEntries(app.values), { 'zhixu.theme': 'dark', 'zhixu.language': 'en' });

  const reloaded = bootstrap({ saved: Object.fromEntries(app.values) });
  assert.equal(reloaded.document.documentElement.dataset.theme, 'dark');
  assert.equal(reloaded.document.documentElement.lang, 'en');
  reloaded.preferences.toggleLanguage();
  assert.equal(reloaded.document.documentElement.lang, 'zh-CN');
  assert.equal(reloaded.document.documentElement.dataset.theme, 'dark');
  reloaded.preferences.toggleTheme();
  assert.equal(reloaded.document.documentElement.dataset.theme, 'light');
  assert.deepEqual(Object.fromEntries(reloaded.values), { 'zhixu.theme': 'light', 'zhixu.language': 'zh' });
});

test('blocked browser storage still allows session-only appearance and language changes', () => {
  let app;
  assert.doesNotThrow(() => { app = bootstrap({ storageDenied: true, systemDark: true }); });
  assert.doesNotThrow(() => {
    app.preferences.toggleTheme();
    app.preferences.toggleLanguage();
  });
  assert.equal(app.document.documentElement.dataset.theme, 'light');
  assert.equal(app.document.documentElement.lang, 'en');
  app.setSystemDark(false);
  app.setSystemDark(true);
  assert.equal(app.document.documentElement.dataset.theme, 'light', 'the session choice still wins when it cannot be saved');
});

test('unknown stored values safely fall back to system appearance and Chinese', () => {
  for (const systemDark of [false, true]) {
    const app = bootstrap({ saved: { 'zhixu.theme': 'sepia', 'zhixu.language': '<script>' }, systemDark });
    assert.equal(app.document.documentElement.dataset.theme, systemDark ? 'dark' : 'light');
    assert.equal(app.document.documentElement.lang, 'zh-CN');
    app.setSystemDark(!systemDark);
    assert.equal(app.document.documentElement.dataset.theme, systemDark ? 'light' : 'dark');
  }
});

test('other tabs can update preferences; unrelated storage events do not affect the UI', () => {
  const app = bootstrap({ saved: { 'zhixu.theme': 'light' } });
  app.storage('zhixu.theme', 'dark');
  assert.equal(app.document.documentElement.dataset.theme, 'dark');
  app.storage('zhixu.language', 'en');
  assert.equal(app.document.documentElement.lang, 'en');
  assert.equal(app.appearanceChanges, 2);
  app.storage('other.application.preference', 'light');
  assert.equal(app.document.documentElement.dataset.theme, 'dark');
  assert.equal(app.document.documentElement.lang, 'en');
  assert.equal(app.appearanceChanges, 2);
  assert.deepEqual(app.writes, [], 'sync must not cause another storage write');
});

test('removing a saved theme in another tab restores live system appearance', () => {
  const app = bootstrap({ saved: { 'zhixu.theme': 'dark', 'zhixu.language': 'en' }, systemDark: false });
  app.storage('zhixu.theme', null);
  assert.equal(app.document.documentElement.dataset.theme, 'light');
  app.setSystemDark(true);
  assert.equal(app.document.documentElement.dataset.theme, 'dark');
  app.storage('zhixu.language', null);
  assert.equal(app.document.documentElement.lang, 'zh-CN');
  app.storage('zhixu.theme', 'unsupported');
  app.setSystemDark(false);
  assert.equal(app.document.documentElement.dataset.theme, 'light');
});

function withLanguage(language, callback) {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'document');
  globalThis.document = { documentElement: { lang: language } };
  try { return callback(); }
  finally {
    if (original) Object.defineProperty(globalThis, 'document', original);
    else delete globalThis.document;
  }
}

test('interface labels switch between Chinese and English without rewriting unknown content', () => {
  withLanguage('zh-CN', () => {
    assert.equal(locale(), 'zh-CN');
    assert.equal(t('课程概览'), '课程概览');
  });
  withLanguage('en', () => {
    assert.equal(locale(), 'en-US');
    assert.equal(t('课程概览'), 'Overview');
    assert.equal(t('RAG 基础示例'), 'RAG 基础示例');
  });
});

test('named parameters preserve zero, leave missing values visible and do not recursively replace content', () => {
  withLanguage('en', () => {
    assert.equal(t('{pages} 页 · {chunks} 个知识片段', { pages: 4, chunks: 0 }), '4 pages · 0 excerpts');
    assert.equal(t('版本 {n}', { n: 2 }), 'Version 2');
    assert.equal(t('版本 {n}'), 'Version {n}');
    assert.equal(t('版本 {n}', { n: '{other}', other: 'changed' }), 'Version {other}');
    assert.equal(t('请求未完成（{status}），请检查输入后重试。', { status: 503 }), 'Request failed (503). Check your input and try again.');
  });
  withLanguage('zh-CN', () => assert.equal(t('第 {n} 页', { n: 0 }), '第 0 页'));
});

test('message descriptors retain error details when an existing notification changes language', () => {
  const message = { key: '请求未完成（{status}），请检查输入后重试。', values: { status: 503 } };
  const notification = { dataset: {}, textContent: '' };
  withLanguage('en', () => {
    assert.equal(t(message), 'Request failed (503). Check your input and try again.');
    const markup = m(message);
    assert.ok(markup.includes('data-i18n="请求未完成（{status}），请检查输入后重试。"'));
    assert.ok(markup.includes('data-i18n-values="{&quot;status&quot;:503}"'));
    assert.ok(markup.endsWith('>Request failed (503). Check your input and try again.</span>'));
    setText(notification, message);
    assert.equal(notification.textContent, 'Request failed (503). Check your input and try again.');
  });
  withLanguage('zh-CN', () => {
    localize({ querySelectorAll: selector => selector === '[data-i18n]' ? [notification] : [] });
    assert.equal(notification.textContent, '请求未完成（503），请检查输入后重试。');
    assert.equal(t(message), '请求未完成（503），请检查输入后重试。');
    assert.equal(notification.dataset.i18n, message.key);
    assert.deepEqual(JSON.parse(notification.dataset.i18nValues), { status: 503 });
  });
});

test('English page counts distinguish one page from zero or multiple pages', () => {
  withLanguage('en', () => {
    assert.equal(t('{n} 页', { n: 0 }), '0 pages');
    assert.equal(t('{n} 页', { n: 1 }), '1 page');
    assert.equal(t('{n} 页', { n: 2 }), '2 pages');
  });
  withLanguage('zh-CN', () => {
    assert.equal(t('{n} 页', { n: 0 }), '0 页');
    assert.equal(t('{n} 页', { n: 1 }), '1 页');
    assert.equal(t('{n} 页', { n: 2 }), '2 页');
  });
});

test('unknown copy matching Object prototype properties renders literally', () => {
  withLanguage('en', () => {
    for (const key of ['constructor', 'toString', '__proto__']) {
      assert.equal(t(key), key);
      assert.equal(m(key), `<span data-i18n="${key}" data-i18n-values="{}">${key}</span>`);
    }
  });
});

test('marked interface text escapes dynamic content and its stored translation parameters', () => {
  withLanguage('en', () => {
    const markup = m('版本 {n}', { n: '<img src="x" onerror="alert(1)">&\'' });
    assert.ok(markup.startsWith('<span data-i18n="版本 {n}" data-i18n-values="'));
    assert.match(markup, /Version &lt;img src=&quot;x&quot; onerror=&quot;alert\(1\)&quot;&gt;&amp;&#39;<\/span>$/);
    assert.equal((markup.match(/</g) || []).length, 2, 'dynamic text must not create elements');
    const stored = markup.match(/data-i18n-values="([^"]*)"/)[1];
    assert.ok(stored.includes('&quot;'));
    assert.ok(stored.includes('&lt;img'));
    assert.ok(!stored.includes('<'));
    assert.ok(!stored.includes('"'));
  });
});

test('accessible labels and placeholders are localized and safely quoted', () => {
  withLanguage('en', () => {
    assert.equal(a('关闭提示', 'aria-label'), 'data-i18n-aria-label="关闭提示" aria-label="Dismiss notification"');
    assert.equal(a('输入知识点或学习目标', 'placeholder'), 'data-i18n-placeholder="输入知识点或学习目标" placeholder="Enter a topic or learning objective"');
    assert.equal(a('含"引号"&<标记>', 'title'), 'data-i18n-title="含&quot;引号&quot;&amp;&lt;标记&gt;" title="含&quot;引号&quot;&amp;&lt;标记&gt;"');
  });
});

test('dates follow the interface locale and invalid dates render no misleading value', () => {
  const value = '2026-09-12T09:05:00';
  withLanguage('en', () => {
    assert.match(formatDate(value), /Sep 12.*09:05.*AM/);
    assert.equal(formatDate('not a date'), '');
  });
  withLanguage('zh-CN', () => {
    assert.match(formatDate(value), /9月12日.*09:05/);
    assert.equal(formatDate('not a date'), '');
  });
});

test('live localization edits marked copy in place and preserves user input and unmarked content', () => {
  withLanguage('en', () => {
    const marked = { dataset: { i18n: '版本 {n}', i18nValues: '{"n":3}' }, textContent: '版本 3' };
    const authored = { textContent: '课程概览', value: '尚未保存的学习目标' };
    const input = {
      value: 'Keep my unsaved input',
      attributes: { 'data-i18n-placeholder': '输入知识点或学习目标', placeholder: '输入知识点或学习目标' },
      getAttribute(name) { return this.attributes[name]; },
      setAttribute(name, value) { this.attributes[name] = value; },
    };
    localize({
      querySelectorAll(selector) {
        assert.match(selector, /^\[data-(?:i18n(?:-[\w-]+)?|date)\]$/, 'localization must select explicit UI markers only');
        if (selector === '[data-i18n]') return [marked];
        if (selector === '[data-i18n-placeholder]') return [input];
        return [];
      },
    });
    assert.equal(marked.textContent, 'Version 3');
    assert.equal(input.attributes.placeholder, 'Enter a topic or learning objective');
    assert.equal(input.value, 'Keep my unsaved input');
    assert.equal(authored.textContent, '课程概览');
    assert.equal(authored.value, '尚未保存的学习目标');
  });
});
