import { english } from './translations.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
export const locale = () => document.documentElement.lang === 'en' ? 'en-US' : 'zh-CN';
const copy = (key, values) => typeof key === 'object' && key !== null ? [key.key, key.values || {}] : [key, values];
export function t(key, values = {}) {
  [key, values] = copy(key, values);
  let text = locale() === 'en-US' && Object.hasOwn(english, key) ? english[key] : key;
  if (typeof text === 'object') text = text[Number(values.n) === 1 ? 'one' : 'other'];
  return text.replace(/\{(\w+)\}/g, (match, name) => values[name] ?? match);
}
// Only explicitly marked interface copy is translated. Authored material stays intact.
export function m(key, values = {}) {
  [key, values] = copy(key, values);
  return `<span data-i18n="${escape(key)}" data-i18n-values="${escape(JSON.stringify(values))}">${escape(t(key, values))}</span>`;
}
export function a(key, attribute) {
  return `data-i18n-${attribute}="${escape(key)}" ${attribute}="${escape(t(key))}"`;
}
export function setText(element, key, values = {}) {
  [key, values] = copy(key, values);
  element.dataset.i18n = key;
  element.dataset.i18nValues = JSON.stringify(values);
  element.textContent = t(key, values);
}
export const formatDate = value => {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '' : date.toLocaleString(locale(), {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
};
export function localize(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(element => {
    element.textContent = t(element.dataset.i18n, JSON.parse(element.dataset.i18nValues || '{}'));
  });
  for (const attribute of ['aria-label', 'title', 'placeholder', 'data-short-label', 'alt']) {
    root.querySelectorAll(`[data-i18n-${attribute}]`).forEach(element => element.setAttribute(attribute, t(element.getAttribute(`data-i18n-${attribute}`))));
  }
  root.querySelectorAll('[data-date]').forEach(element => element.textContent = formatDate(element.dataset.date));
}
