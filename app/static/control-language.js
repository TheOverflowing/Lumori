import { t, m, a } from './i18n.js';

const nativeMessages = new WeakMap();

export function renderFilePicker() {
  return `<div class="file-field"><span id="upload-file-label">${m('选择文件')}</span><label class="localized-file-picker"><input class="localized-file-input" type="file" name="file" accept=".pdf,.txt,.md,.png,.jpg,.jpeg" required aria-labelledby="upload-file-label" aria-describedby="upload-file-name" ${a('选择文件','title')}><span class="file-picker-button" aria-hidden="true">${m('选择文件')}</span><span id="upload-file-name" data-file-name role="status">${m('未选择文件')}</span></label></div>`;
}

// Store the translation key so existing business-rule errors can change language too.
export function setLocalizedValidity(control, message) {
  nativeMessages.delete(control);
  if (message) control.dataset.localizedValidity = JSON.stringify(message);
  else delete control.dataset.localizedValidity;
  control.setCustomValidity(message ? t(message) : '');
}

export function nativeValidationMessage(control) {
  const v = control.validity;
  if (v.valueMissing) return control.type === 'file' ? '请选择文件。' : control.tagName === 'SELECT' ? '请选择一个选项。' : '请填写此字段。';
  if (v.typeMismatch) return control.type === 'email' ? '请输入有效的邮箱地址。' : '请输入有效的网址。';
  if (v.badInput) return '请输入有效的数字。';
  if (v.tooShort) return {key:'请至少输入 {n} 个字符。',values:{n:control.minLength}};
  if (v.tooLong) return {key:'请最多输入 {n} 个字符。',values:{n:control.maxLength}};
  if (v.rangeUnderflow) return {key:'请输入不小于 {n} 的数值。',values:{n:control.min}};
  if (v.rangeOverflow) return {key:'请输入不大于 {n} 的数值。',values:{n:control.max}};
  if (v.stepMismatch) return {key:'请输入符合步长 {n} 的数值。',values:{n:control.step || 1}};
  if (v.patternMismatch) return '请按要求的格式填写。';
  return '';
}

function localizeValidation(control) {
  if (!control?.validity || !control.setCustomValidity) return;
  const owned = nativeMessages.get(control);
  if (owned && control.validationMessage === owned) control.setCustomValidity('');
  nativeMessages.delete(control);
  if (control.dataset.localizedValidity) {
    control.setCustomValidity(t(JSON.parse(control.dataset.localizedValidity)));
    return;
  }
  // Leave validation owned by other features intact.
  if (control.validity.customError) return;
  const key = nativeValidationMessage(control);
  if (key) {
    const message = t(key);
    control.setCustomValidity(message);
    nativeMessages.set(control, message);
  }
}

export function refreshControlLanguage(root = document) {
  for (const input of root.querySelectorAll('.localized-file-input')) {
    const output = input.closest('.localized-file-picker').querySelector('[data-file-name]');
    if (input.files?.length) output.textContent = [...input.files].map(file => file.name).join(', ');
    else output.innerHTML = m('未选择文件');
    input.title = t('选择文件');
  }
  for (const input of root.querySelectorAll('input, select, textarea')) {
    if (nativeMessages.has(input) || input.dataset.localizedValidity) localizeValidation(input);
  }
}

export function installControlLanguage(root = document) {
  root.addEventListener('invalid', event => localizeValidation(event.target), true);
  const update = event => {
    const input = event.target;
    if (nativeMessages.has(input)) localizeValidation(input);
    if (input.matches?.('.localized-file-input')) refreshControlLanguage(input.parentElement);
  };
  root.addEventListener('input', update, true);
  root.addEventListener('change', update, true);
}
