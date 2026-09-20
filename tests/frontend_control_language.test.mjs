import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { english } from '../app/static/translations.js';
import { renderFilePicker, nativeValidationMessage, installControlLanguage, refreshControlLanguage, setLocalizedValidity } from '../app/static/control-language.js';
import { t } from '../app/static/i18n.js';

globalThis.document = {documentElement:{lang:'en'}};
function control(flags = {}, extra = {}) {
  return {dataset:{},type:'text',tagName:'INPUT',validity:{...flags},validationMessage:'',
    setCustomValidity(message) { this.validationMessage = message; this.validity.customError = Boolean(message); },
    matches:()=>false,...extra};
}
function rootFor(inputs) {
  const listeners = {};
  return {listeners,addEventListener:(event,callback)=>listeners[event]=callback,
    querySelectorAll:selector=>selector === '.localized-file-input' ? [] : inputs};
}
test('native validation uses English and clears after valid editing', () => {
  document.documentElement.lang='en';
  const input=control({valueMissing:true}),root=rootFor([input]);
  installControlLanguage(root);
  root.listeners.invalid({target:input});
  assert.equal(input.validationMessage,'Please fill out this field.');
  document.documentElement.lang='zh-CN';refreshControlLanguage(root);
  assert.equal(input.validationMessage,'请填写此字段。');
  input.validity.valueMissing=false;root.listeners.input({target:input});
  assert.equal(input.validationMessage,'');
  assert.equal(input.validity.customError,false);
});
test('business validation switches language without losing its rule or overriding unrelated errors', () => {
  const input=control(),root=rootFor([input]);
  setLocalizedValidity(input,{key:'还需分配 {n} 题。',values:{n:2}});
  document.documentElement.lang='en';refreshControlLanguage(root);
  assert.equal(input.validationMessage,t('还需分配 {n} 题。',{n:2}));
  setLocalizedValidity(input,null);assert.equal(input.validationMessage,'');
  input.setCustomValidity('External validation');installControlLanguage(root);
  root.listeners.invalid({target:input});assert.equal(input.validationMessage,'External validation');
});
test('native file, email and numeric errors retain specific constraints in English', () => {
  document.documentElement.lang='en';
  for (const [flags,extra,expected] of [
    [{valueMissing:true},{type:'file'},'Please choose a file.'],
    [{typeMismatch:true},{type:'email'},'Enter a valid email address.'],
    [{rangeUnderflow:true},{min:'0'},'Please enter a value greater than or equal to 0.'],
    [{rangeOverflow:true},{max:'10'},'Please enter a value less than or equal to 10.'],
    [{stepMismatch:true},{step:'1'},'Please enter a value in increments of 1.'],
  ]) assert.equal(t(nativeValidationMessage(control(flags,extra))),expected);
});
test('file selection and its authored filename survive language changes', () => {
  const output={textContent:'',innerHTML:''}, files=[{name:'课程 <notes>.md'}];
  const input={files,title:'',closest:()=>({querySelector:()=>output})};
  const root={querySelectorAll:selector=>selector==='.localized-file-input'?[input]:[]};
  for(const lang of ['en','zh-CN','en']) {
    document.documentElement.lang=lang;refreshControlLanguage(root);
    assert.equal(input.files,files);assert.equal(output.textContent,files[0].name);
    assert.equal(input.title,t('选择文件'));
  }
  input.files=[];refreshControlLanguage(root);
  assert.match(output.innerHTML,/No file selected/);
  assert.match(renderFilePicker(),/Choose file/);
});
test('all Chinese interface string literals and static translation markers have English entries', () => {
  for(const file of readdirSync('app/static').filter(x=>x.endsWith('.js')&&x!=='translations.js')) {
    const source=readFileSync(`app/static/${file}`,'utf8');
    for(const match of source.matchAll(/(['"])([^'"\n<>]*[\u3400-\u9fff][^'"\n<>]*)\1/g)) {
      assert.ok(Object.hasOwn(english,match[2]),`${file}: missing English for ${match[2]}`);
    }
  }
  const html=readFileSync('app/static/index.html','utf8');
  for(const [,key] of html.matchAll(/data-i18n(?:-[\w-]+)?="([^"]+)"/g)) if(/[\u3400-\u9fff]/.test(key)) assert.ok(Object.hasOwn(english,key),key);
});
