import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { english } from '../app/static/translations.js';
import { renderFilePicker, nativeValidationMessage, installControlLanguage, refreshControlLanguage, setLocalizedValidity } from '../app/static/control-language.js';
import { t, localize } from '../app/static/i18n.js';

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
test('first hover and keyboard focus localize native hints before any submission', () => {
  document.documentElement.lang='en';
  const missing=control({valueMissing:true}),number=control({rangeOverflow:true},{type:'number',max:'50'});
  const root=rootFor([missing,number]);installControlLanguage(root);
  root.listeners.pointerover({target:missing});
  root.listeners.focusin({target:number});
  assert.equal(missing.validationMessage,'Please fill out this field.');
  assert.equal(number.validationMessage,'Please enter a value less than or equal to 50.');
  assert.doesNotThrow(()=>root.listeners.pointerover({target:{tagName:'SPAN'}}));
  number.validity.rangeOverflow=false;root.listeners.input({target:number});
  assert.equal(number.validationMessage,'');
  number.validity.rangeUnderflow=true;number.min='1';root.listeners.input({target:number});
  assert.equal(number.validationMessage,'Please enter a value greater than or equal to 1.');
});
test('language refresh prepares untouched fields without replacing their values', () => {
  const input=control({typeMismatch:true},{type:'email',value:'my unfinished email'}),root=rootFor([input]);
  document.documentElement.lang='zh-CN';refreshControlLanguage(root);
  assert.equal(input.validationMessage,'请输入有效的邮箱地址。');
  document.documentElement.lang='en';refreshControlLanguage(root);
  assert.equal(input.validationMessage,'Enter a valid email address.');
  assert.equal(input.value,'my unfinished email');
});
test('temporarily disabled controls keep validation ownership across language changes', () => {
  document.documentElement.lang='zh-CN';
  const input=control({valueMissing:true}),root=rootFor([input]);
  refreshControlLanguage(root);
  const hiddenMessage=input.validationMessage;
  input.willValidate=false;input.validationMessage='';
  document.documentElement.lang='en';refreshControlLanguage(root);
  assert.equal(input.validationMessage,'');
  input.willValidate=true;input.validationMessage=hiddenMessage;
  installControlLanguage(root);root.listeners.pointerover({target:input});
  assert.equal(input.validationMessage,'Please fill out this field.');
});
test('form reset clears a localized error after restoring a valid default', async () => {
  document.documentElement.lang='en';
  const input=control({rangeOverflow:true},{max:'50'}),root=rootFor([input]);
  installControlLanguage(root);root.listeners.pointerover({target:input});
  root.listeners.reset({target:root,defaultPrevented:false});
  input.validity.rangeOverflow=false;
  await Promise.resolve();
  assert.equal(input.validationMessage,'');
  assert.equal(input.validity.customError,false);
});
test('existing interface hover titles switch language in place', () => {
  const hints=['设置','新建课程','用于生成','关闭'];
  const elements=hints.map(key=>({key,title:key,
    getAttribute:()=>key,setAttribute(name,value){this[name]=value;}}));
  const root={querySelectorAll:selector=>selector==='[data-i18n-title]'?elements:[]};
  for(const lang of ['en','zh-CN','en']) {
    document.documentElement.lang=lang;localize(root);
    for(const element of elements) assert.equal(element.title,t(element.key));
  }
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

test('question revision preparation and version conflict errors have English copy',()=>{
  const source=readFileSync('app/question_revision.py','utf8');
  for(const [,key] of source.matchAll(/raise (?:ValueError|Conflict)\('([^']*[\u3400-\u9fff][^']*)'\)/g)) {
    assert.ok(Object.hasOwn(english,key),`Question revision error needs translation: ${key}`);
    assert.doesNotMatch(english[key],/[\u3400-\u9fff]/);
  }
  for(const key of [
    '文字解析已完成；任务队列已满，可稍后继续处理图片。',
    '当前账号等待或执行中的任务已达上限，请等待完成或取消部分任务。',
    '当前账号等待或执行中的任务已达上限，请先等待完成或取消部分任务。',
    '此任务没有可查看的部分内容。',
    '任务已结束，无法取消。',
  ]) assert.ok(Object.hasOwn(english,key),key);
});
