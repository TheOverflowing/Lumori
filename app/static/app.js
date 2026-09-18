import { createSessionClient, isSessionChange, accountCourseKey } from './auth.js';
import { parsingDialog, parsingPage, parsingLabel, parserControls } from './parsing.js';
import { renderOutputFormats, renderMultimodal } from './multimodal.js';
import { watchResource } from './live-updates.js';
import { mountInputControls } from './input-controls.js';
import { t, m, a, setText, localize, formatDate } from './i18n.js';
import { ensureDifficultyDraft, renderLessonDepth, renderDifficultyControls, updateDifficultyControls, bindDifficultyPresets, difficultyValidation, difficultySummary, generationPayload, renderQuestionDifficulty, renderDifficultyAssessment, renderQuestionDifficultyEvaluation, readQuestionDifficulties, bindDifficultyEvaluation } from './difficulty.js';
/* Learning Studio: independent page renderers, native controls, one API boundary. */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const page = $('#page');
const modal = $('#modal');
let pageInputs = null, modalInputs = null;
modal.addEventListener('close', () => { modalInputs?.destroy(); modalInputs = null; });
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const icon = name => `<img class="icon" src="/static/icons/${name}.svg" alt="">`;
const titles = {home:'课程概览', documents:'课程资料', generate:'创作学习材料', contents:'内容与审核', jobs:'任务记录', content:'内容详情', learning:'学习材料'};
const labels = {ready:'索引就绪', parsed:'待建索引', draft:'待审核', approved:'已确认', queued:'排队中', running:'处理中', succeeded:'已完成', failed:'处理失败', insufficient_evidence:'资料依据不足', parse:'资料解析', figures:'图片理解', pending:'待处理', parse_failed:'解析失败', index:'资料索引', generate:'内容生成', media:'媒体生成', vision:'视觉理解模型', text:'文本模型', embedding:'嵌入模型', speech:'语音模型', image:'图像模型'};
const kinds = {lesson:'学习讲解', quiz:'测验练习', assignment:'课后作业'};
const badge = status => `<span class="badge ${esc(status)}">${m(labels[status] || status)}</span>`;
const button = (label, action, style = '', attrs = '') => `<button class="${style}" data-action="${action}" ${attrs}>${label.includes('<') ? label : m(label)}</button>`;
const heading = title => `<div class="page-heading"><h1>${m(title)}</h1></div>`;
const empty = (title, description, action = '', label = '', symbol = 'book-open') => `<div class="empty-state">${icon(symbol)}<h2>${m(title)}</h2>${description?`<p>${m(description)}</p>`:''}${action?button(label, action):''}</div>`;
const date = value => `<time data-date="${esc(value)}">${esc(formatDate(value))}</time>`;
const state = {course:'', cleanup:null, library:{course:'',filter:'all',query:''}, courses:[], revision:0, route:{page:'home'}, drafts:new Map(), content:null, learningId:new URLSearchParams(location.search).get('learn')};
let workspaceReady = false, workspaceOpened = false, authMode = 'login', sessionCheck = null;
let pendingRestore = null, restoreEmail = '', restoreAttemptUser = null;
function captureRestoreLink() {
  if (!location.hash.startsWith('#restore-workspace?')) return false;
  const params = new URLSearchParams(location.hash.split('?').slice(1).join('?'));
  pendingRestore = params.get('token') || null; restoreEmail = params.get('email') || '';
  history.replaceState(null, '', location.pathname + location.search + '#home');
  return true;
}
captureRestoreLink();
const auth = createSessionClient({onReset:clearWorkspace, onSession:receiveSession});
const api = (...args) => auth.request(...args);
function clearWorkspace(reason = '') {
  workspaceReady = false;
  state.cleanup?.(); state.cleanup = null;
  pageInputs?.destroy(); pageInputs = null;
  modalInputs?.destroy(); modalInputs = null;
  state.revision++;
  state.course = ''; state.courses = []; state.content = null; state.drafts.clear();
  state.library = {course:'',filter:'all',query:''}; state.reveal = false;
  if (workspaceOpened && reason !== 'checking') {
    state.learningId = null;
    history.replaceState(null, '', location.pathname + '#home');
  }
  document.body.classList.remove('learning');
  page.replaceChildren(); $('#toolbar-actions').replaceChildren();
  $('#course-select').replaceChildren(); $('#account-name').textContent = ''; $('#account-initial').textContent = '';
  $('#account-button').removeAttribute('title');
  if (modal.open) modal.close(); $('#modal-content').replaceChildren();
  $('#notice').hidden = true; $('#notice-text').textContent = '';
  $('.app-shell').hidden = true; $('.skip').hidden = true; $('#auth-gate').hidden = false;
  if (reason === 'expired') renderAuth('登录已过期或账号已切换，请重新登录。');
  else renderAuthLoading();
}
function renderAuthLoading() {
  $('#auth-content').innerHTML = `<h1 id="auth-title">${m('正在载入…')}</h1><p class="auth-loading" role="status">${m('正在检查登录状态…')}</p>`;
}
function renderAuth(message = '') {
  const registering = authMode === 'register';
  $('#auth-gate').hidden = false;
  $('#auth-content').innerHTML = `<h1 id="auth-title">${m(registering?'创建你的账号':'登录知序')}</h1><p class="auth-description">${m(registering?'保存课程资料，创作属于你的学习材料。':'继续你的学习与创作。')}</p>${message?`<p class="auth-message" role="status">${m(message)}</p>`:''}${pendingRestore?`<p class="auth-message">${m('有一份原有工作空间等待恢复，请使用对应邮箱登录或注册。')}${restoreEmail?`<br>${esc(restoreEmail)}`:''}</p>`:''}<form id="auth-form">${registering?`<label for="auth-name">${m('你的称呼')}<input id="auth-name" name="display_name" autocomplete="nickname" maxlength="80" required></label>`:''}<label for="auth-email">${m('邮箱')}<input id="auth-email" name="email" type="email" value="${pendingRestore?esc(restoreEmail):''}" autocomplete="username" inputmode="email" autocapitalize="none" spellcheck="false" maxlength="254" required></label><label for="auth-password">${m('密码')}<input id="auth-password" name="password" type="password" autocomplete="${registering?'new-password':'current-password'}" ${registering?'minlength="15"':''} maxlength="128" required ${registering?'aria-describedby="auth-password-hint"':''}>${registering?`<span id="auth-password-hint" class="auth-hint">${m('使用 15–128 个字符，可以使用一句容易记住的话。')}</span>`:''}</label>${registering?`<label for="auth-confirm">${m('确认密码')}<input id="auth-confirm" name="password_confirmation" type="password" autocomplete="new-password" minlength="15" maxlength="128" required></label>`:''}<button type="submit" class="auth-submit">${m(registering?'创建账号':'登录')}</button></form><div class="auth-switch"><span>${m(registering?'已有账号？':'还没有账号？')}</span><button class="link" id="auth-switch" type="button">${m(registering?'去登录':'创建账号')}</button></div>`;
  $('#auth-switch').onclick = () => { authMode = registering?'login':'register'; renderAuth(); $('#auth-email').focus(); };
  const form = $('#auth-form');
  form.oninput = () => $('.error-message',form)?.remove();
  form.onsubmit = event => {
    event.preventDefault(); if (!form.reportValidity()) return;
    const fields = new FormData(form), password = fields.get('password');
    if (registering && password !== fields.get('password_confirmation')) { inlineError(form,'两次输入的密码不一致。'); $('#auth-confirm').focus(); return; }
    const data = {email:fields.get('email').trim(), password};
    if (registering) data.display_name = fields.get('display_name').trim();
    busy($('button[type=submit]',form), async () => {
      $('#auth-switch').disabled = true;
      try { await auth.authenticate(registering?'register':'login',data); signalSessionChange(); }
      finally { if ($('#auth-switch')) $('#auth-switch').disabled = false; }
    },form);
  };
  document.title = `${t(registering?'创建你的账号':'登录知序')} · ${t('知序')}`;
}
function receiveSession(session, {changed = false, reason = ''} = {}) {
  if (!session.user) {
    if (changed || !$('#auth-form') || reason) renderAuth(reason==='expired'?'登录已过期或账号已切换，请重新登录。':'');
    if (reason === 'account_changed') queueMicrotask(() => checkSession({suspend:true}));
    return;
  }
  authMode = 'login';
  if (changed || !workspaceReady) loadWorkspace(auth.epoch);
}
async function loadWorkspace(epoch) {
  const user = auth.session.user;
  let claimError = null, claimed = false;
  try {
    if (pendingRestore && restoreAttemptUser !== user.id) {
      restoreAttemptUser = user.id;
      try { await api('/auth/claim-legacy','POST',{token:pendingRestore}); pendingRestore = null; claimed = true; }
      catch (error) {
        if (isSessionChange(error)) throw error;
        if (['invalid_recovery_token','recovery_already_used','recovery_conflict'].includes(error.code)) pendingRestore = null;
        claimError = error;
      }
    }
    try { state.course = localStorage.getItem(accountCourseKey(user.id)) || ''; } catch { state.course = ''; }
    if (!state.learningId) await loadCourses();
    if (epoch !== auth.epoch) return;
    $('#account-name').textContent = user.display_name || user.email;
    $('#account-initial').textContent = [...(user.display_name || user.email)].slice(0,1).join('').toUpperCase();
    $('#account-button').title = user.email;
    $('#auth-content').replaceChildren(); $('#auth-gate').hidden = true; $('.app-shell').hidden = false; $('.skip').hidden = false;
    workspaceReady = true; workspaceOpened = true;
    await renderRoute();
    if (epoch !== auth.epoch) return;
    if (claimed) notify('原有工作空间已恢复到当前账号。');
    if (claimError) notify(claimError.uiMessage || claimError.message);
  } catch (error) {
    if (isSessionChange(error) || epoch !== auth.epoch) return;
    $('#auth-gate').hidden = false;
    $('#auth-content').innerHTML = `<h1 id="auth-title">${m('无法连接工作空间')}</h1><p class="auth-description">${m(error.uiMessage || error.message)}</p><button id="auth-retry" class="auth-submit">${m('重试')}</button><button id="auth-signout" class="link">${m('退出登录')}</button>`;
    $('#auth-retry').onclick = () => { renderAuthLoading(); loadWorkspace(auth.epoch); };
    $('#auth-signout').onclick = () => busy($('#auth-signout'),logout);
  }
}
function signalSessionChange() {
  try { localStorage.setItem('zhixu.session-change',crypto.randomUUID()); } catch { /* Focus checks cover browsers with storage disabled. */ }
}
async function logout() { await auth.logout(); restoreAttemptUser = null; signalSessionChange(); $('#auth-email')?.focus(); }
async function checkSession({suspend = false} = {}) {
  if (suspend) auth.suspend('checking');
  if (sessionCheck) return suspend ? sessionCheck.then(() => checkSession()) : sessionCheck;
  sessionCheck = (async () => {
    try { await auth.restore(); }
    catch (error) {
      if (isSessionChange(error)) return;
      if (workspaceReady || $('#auth-form')) return;
      $('#auth-content').innerHTML = `<h1 id="auth-title">${m('无法连接工作空间')}</h1><p class="auth-description">${m('请检查网络连接，然后重试。')}</p><button id="auth-retry" class="auth-submit">${m('重试')}</button>`;
      $('#auth-retry').onclick = () => { renderAuthLoading(); checkSession(); };
    } finally { sessionCheck = null; }
  })();
  return sessionCheck;
}
function notify(message) {
  setText($('#notice-text'), message);
  $('#notice').hidden = false;
}
function inlineError(form, message) {
  let error = $('.error-message', form);
  if (!error) { error = document.createElement('div'); error.className = 'error-message'; error.setAttribute('role','alert'); form.prepend(error); }
  setText(error, message);
  error.scrollIntoView({block:'nearest'});
}
async function busy(control, task, form = null) {
  if (control?.disabled) return;
  const old = control?.innerHTML;
  if (control) { control.disabled = true; control.setAttribute('aria-busy','true'); if (form) control.innerHTML = m('正在处理…'); }
  if (form) $('.error-message', form)?.remove();
  try { await task(); }
  catch (error) { if (isSessionChange(error)) return; if (form?.isConnected) inlineError(form, error.uiMessage || error.message); else notify(error.uiMessage || error.message); }
  finally { if (control?.isConnected) { control.disabled = false; control.removeAttribute('aria-busy'); control.innerHTML = old; localize(control); } }
}
function dialog(title, html, wide = false, titleIsData = false) {
  modalInputs?.destroy();
  modal.classList.toggle('wide', wide);
  $('#modal-content').innerHTML = `<div class="dialog-top"><h2 id="modal-title">${titleIsData ? esc(title) : m(title)}</h2>${button(icon('plus'),'close-modal','plain icon-button close-button',`${a('关闭','aria-label')} ${a('关闭','title')}`)}</div>${html}`;
  if (!modal.open) modal.showModal();
  modalInputs = mountInputControls(modal);
}
function courseRequired() {
  return {html:empty('暂无课程','','new-course','新建课程','files')};
}
function go(name, id = '') {
  const hash = '#' + name + (id ? '/' + encodeURIComponent(id) : '');
  if (location.hash === hash) return renderRoute();
  location.hash = hash;
}
function selectCourse(id) {
  state.course = id;
  $('#course-select').value = id;
  try { localStorage.setItem(accountCourseKey(auth.session.user?.id), id); } catch { /* Session selection remains available. */ }
}
function replaceRows(root, html) {
  const focused = root.contains(document.activeElement) ? document.activeElement : null;
  const action = focused?.dataset.action, id = focused?.dataset.id;
  root.innerHTML = html;
  if (action) $$('[data-action]', root).find(control => control.dataset.action === action && control.dataset.id === id)?.focus({preventScroll:true});
}
function liveError(root, error) {
  root.hidden = !error;
  root.innerHTML = error ? `${m('自动更新暂不可用')} ${button('刷新状态','retry','link')}` : '';
}
async function loadCourses() {
  state.courses = await api('/courses');
  if (!state.courses.some(course => course.id === state.course)) selectCourse(state.courses[0]?.id || '');
  $('#course-select').innerHTML = state.courses.length ? state.courses.map(course => `<option value="${esc(course.id)}">${esc(course.name)}</option>`).join('') : `<option value="" data-i18n="选择或新建课程">${t('选择或新建课程')}</option>`;
  $('#course-select').value = state.course;
}
function courseUrl(resource, course = state.course) { return `/${resource}?course_id=${encodeURIComponent(course)}`; }
function renderNavigation(name) {
  const active = name === 'content' ? 'contents' : name;
  $$('nav a').forEach(link => { if (link.dataset.page === active) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current'); });
  setText($('#route-label'), titles[name] || titles.home);
  document.title = `${t(titles[name] || titles.home)} · ${t('知序')}`;
}
function refreshPreferences() {
  localize();
  for (const draft of state.drafts.values()) ensureDifficultyDraft(draft);
  const profile = $('#learner-profile'), draft = state.drafts.get(state.course);
  if (profile && draft?.learner_profile_is_default) profile.value = draft.learner_profile;
  pageInputs?.refresh();
  modalInputs?.refresh();
  renderNavigation(state.route.page);
  const dark = document.documentElement.dataset.theme === 'dark';
  const themeLabel = t(dark ? '切换到浅色模式' : '切换到深色模式');
  for (const themeToggle of $$('[data-theme-toggle]')) {
  themeToggle.innerHTML = icon(dark ? 'sun' : 'moon');
  themeToggle.setAttribute('aria-label', themeLabel);
  themeToggle.title = themeLabel;
  }
  const english = document.documentElement.lang === 'en';
  for (const languageToggle of $$('[data-language-toggle]')) {
  languageToggle.textContent = english ? '中' : 'EN';
  languageToggle.lang = english ? 'zh-CN' : 'en';
  languageToggle.setAttribute('aria-label', english ? '切换到中文' : 'Switch to English');
  languageToggle.title = english ? '切换到中文' : 'Switch to English';
  }
  if (!workspaceReady) document.title = `${t(authMode==='register'?'创建你的账号':'登录知序')} · ${t('知序')}`;
}
async function renderRoute() {
  if (!workspaceReady || !auth.session.user) return;
  state.cleanup?.(); state.cleanup = null;
  pageInputs?.destroy(); pageInputs = null;
  const revision = ++state.revision;
  const [requested = 'home', rawId = ''] = location.hash.slice(1).split('/');
  const name = state.learningId ? 'learning' : (renderers[requested] ? requested : 'home');
  let id; try { id = decodeURIComponent(rawId); } catch { id = ''; }
  state.route = {page:name, id};
  renderNavigation(name);
  $('#toolbar-actions').innerHTML = '';
  page.innerHTML = `<div class="loading" role="status">${m('正在载入…')}</div>`;
  window.scrollTo({top:0,behavior:'instant'});
  try {
    const result = await renderers[name](id);
    if (revision !== state.revision) return;
    page.innerHTML = result.html;
    $('#toolbar-actions').innerHTML = result.toolbar || '';
    state.cleanup = result.bind?.() || null;
    pageInputs = mountInputControls(page);
  } catch (error) {
    if (revision !== state.revision) return;
    page.innerHTML = empty('暂时无法载入', error.uiMessage || error.message, 'retry', '重新尝试');
  }
}
function materialRow(content, showCourse = false) {
  return `<div class="collection-row"><span class="file-icon">${icon('book-open')}</span><div class="row-main"><h3>${esc(content.title)}</h3><p>${showCourse?`${esc(content.course_name)} · `:''}${m('版本 {n}',{n:content.version})} · ${date(content.created_at)}</p></div>${badge(content.status)}<div class="row-actions">${button('打开','open-content','secondary',`data-id="${esc(content.id)}"`)}</div></div>`;
}

async function renderHome() {
  if (!state.course) return {...courseRequired(), toolbar:button('模型连接','settings','secondary')};
  const course = state.courses.find(item => item.id === state.course);
  const [docs, contents] = await Promise.all([api(courseUrl('documents')), api(courseUrl('contents'))]);
  const drafts = contents.filter(content => content.status === 'draft').length;
  return {
    toolbar:button(icon('upload')+m('导入资料'),'upload','secondary'),
    html:`<section class="overview-heading"><h1>${esc(course.name)}</h1><div class="course-stats"><div><strong>${docs.length}</strong><span>${m('资料')}</span></div><div><strong>${contents.length}</strong><span>${m('材料')}</span></div><div><strong>${drafts}</strong><span>${m('待审核')}</span></div></div></section>
      <div class="home-grid"><section class="creation-section" ${a('创作材料','aria-label')}><div class="quick-create">${[['lesson','book-open'],['quiz','layers'],['assignment','file-text']].map(([kind,symbol])=>button(`${icon(symbol)}<strong>${m(kinds[kind])}</strong><img class="choice-arrow" src="/static/icons/arrow-up-right.svg" alt="">`,'create-kind','create-choice',`data-kind="${kind}"`)).join('')}</div></section>
      <section class="home-panel materials-panel"><div class="heading-row"><h2>${m('最近材料')}</h2>${button(icon('arrow-right'),'contents','plain icon-button',`${a('查看全部材料','aria-label')} ${a('查看全部材料','title')}`)}</div>${contents.length?`<div class="collection">${contents.slice(0,4).map(content=>materialRow(content)).join('')}</div>`:`<div class="recent-empty">${icon('book-open')}<p>${m('暂无材料')}</p></div>`}</section>
      <section class="home-panel documents-panel"><div class="heading-row"><h2>${m('课程资料')}</h2>${button(icon('arrow-right'),'documents','plain icon-button',`${a('管理课程资料','aria-label')} ${a('管理课程资料','title')}`)}</div>${docs.length?`<div class="collection">${docs.slice(0,3).map(doc=>`<div class="collection-row"><span class="file-icon">${icon('file-text')}</span><div class="row-main"><h3>${esc(doc.name)}</h3><p>${m('{n} 页',{n:doc.pages})}</p></div>${badge(['pending','parse_failed'].includes(doc.status)?doc.status:doc.index_current?'ready':'parsed')}</div>`).join('')}</div>`:`<div class="recent-empty"><p>${m('暂无资料')}</p></div>`}</section></div>`
  };
}

async function renderDocuments() {
  if (!state.course) return courseRequired();
  let docs = await api(courseUrl('documents'));
  const documentsCourse=state.course;
  return {
    toolbar:button(icon('upload')+m('导入资料'),'upload'),
    html:heading('课程资料')+`<div class="collection-tools"><div class="segmented" ${a('资料状态筛选','aria-label')}><button data-filter="all" aria-pressed="true">${m('全部 {n}',{n:docs.length})}</button><button data-filter="ready" aria-pressed="false">${m('索引就绪')}</button><button data-filter="parsed" aria-pressed="false">${m('待建索引')}</button></div><label class="search"><span class="sr-only">${m('搜索文件')}</span><input id="file-search" type="search" ${a('搜索文件名称','placeholder')}></label></div><div id="files"></div>`,
    bind() {
      let filter = 'all';
      const paint = () => {
        $('[data-filter=all]').innerHTML=m('全部 {n}',{n:docs.length});
        const query = $('#file-search').value.trim().toLowerCase();
        const rows = docs.filter(doc => doc.name.toLowerCase().includes(query) && (filter==='all'||(filter==='ready')===Boolean(doc.index_current)));
        $('#files').innerHTML = rows.length ? `<div class="collection">${rows.map(doc=>`<div class="collection-row"><span class="file-icon">${icon('file-text')}</span><div class="row-main"><h3>${esc(doc.name)}</h3><p>${m('{n} 页',{n:doc.pages})} · ${m('{n} 个知识片段',{n:doc.chunks})} · ${m(parsingLabel(doc.parsing?.status))}${doc.active_jobs?.length?' · '+m('后台处理中'):''}${doc.figures?.length?' · '+m('可检索图片 {ready} / {total}',{ready:doc.figures.filter(f=>f.status==='ready').length,total:doc.figures.length}):''}</p></div>${badge(['pending','parse_failed'].includes(doc.status)?doc.status:doc.index_current?'ready':'parsed')}<div class="row-actions">${button('文字与原图','parsing','secondary',`data-id="${esc(doc.id)}"`)}${button('阅读片段','chunks','secondary',`data-id="${esc(doc.id)}" data-name="${esc(doc.name)}"`)}${button(doc.index_current?'重建索引':'建立索引','index','secondary',`data-id="${esc(doc.id)}" ${doc.status==='pending'?'disabled':''}`)}</div></div>`).join('')}</div>` : empty(docs.length?'没有匹配的资料':'暂无资料',docs.length?'试试其他名称或筛选条件。':'',docs.length?'':'upload','导入资料','files');
      };
      $('#file-search').oninput = paint;
      $$('[data-filter]').forEach(control => control.onclick = () => {filter=control.dataset.filter;$$('[data-filter]').forEach(item=>item.setAttribute('aria-pressed',String(item===control)));paint();});
      paint();
      return watchResource({load:()=>api(courseUrl('documents',documentsCourse)),initial:docs,update:next=>{docs=next;paint();}});
    }
  };
}
function draftFor(course = state.course) {
  if (!state.drafts.has(course)) state.drafts.set(course,{material:'lesson',topic:'',difficulty:'medium',language:'zh',question_type:'mixed',count:3});
  return ensureDifficultyDraft(state.drafts.get(course));
}
async function renderGenerate() {
  if (!state.course) return courseRequired();
  const course = state.course;
  const draft = draftFor(course);
  const [docs,status] = await Promise.all([api(courseUrl('documents',course)),api('/status')]);
  const ready = docs.filter(doc=>doc.index_current).length;
  const select = (id,label,values) => `<label class="select-field">${m(label)}<select id="${id}" name="${id}">${values.map(([value,text])=>`<option value="${value}" ${String(draft[id])===value?'selected':''} data-i18n="${text}">${t(text)}</option>`).join('')}</select></label>`;
  return {
    toolbar:button('模型连接','settings','secondary'),
    html:heading('创作学习材料')+`<div class="compose-layout"><form id="compose" class="compose-form"><fieldset><legend class="sr-only">${m('材料类型')}</legend><div class="type-switch">${Object.entries(kinds).map(([kind,title])=>`<label><input type="radio" name="material" value="${kind}" ${draft.material===kind?'checked':''}>${m(title)}</label>`).join('')}</div></fieldset>
      <div class="writing-field"><label for="topic">${m('学习目标')}</label><textarea id="topic" name="topic" required minlength="2" ${a('输入知识点或学习目标','placeholder')}>${esc(draft.topic)}</textarea></div>
      <div class="compose-options"><div class="field-grid">${select('language','内容语言',[['zh','中文'],['en','English']])}</div><div class="field-grid" id="question-options">${select('question_type','题目形式',[['mixed','选择题与简答题'],['mcq','选择题'],['short_answer','简答题']])}<div class="number-field"><label for="count">${m('题目数量')}</label><input type="number" name="count" id="count" min="1" max="10" step="1" required value="${draft.count}"></div></div>${renderLessonDepth(draft)}</div>
      ${renderDifficultyControls(draft)}
      ${renderOutputFormats()}<div class="submit-bar"><button type="submit">${icon('wand-sparkles')}${m('开始生成')}</button></div></form>
      <aside class="context-pane"><div class="context-title">${m('本次创作')}</div><h2 id="draft-kind"></h2><p id="draft-summary"></p><div class="context-divider"></div><div class="heading-row"><h3>${m('课程知识')}</h3><small>${m('{ready} / {total} 份就绪',{ready,total:docs.length})}</small></div>${docs.slice(0,4).map(doc=>`<div class="context-file">${icon('file-text')}<span>${esc(doc.name)}</span></div>`).join('')||`<p>${m('尚未导入课程资料。')}</p>`}${button('管理课程资料','documents','link')}<div class="inline-note">${m(ready?'索引就绪':'请先建立索引')}</div>${!status.capabilities.text.configured||!status.capabilities.embedding.configured?button('配置文本与嵌入模型','settings','link'):''}<details><summary>${m('关于内容与引用')}</summary><p>${m('模型依据检索片段生成内容，系统检查格式与引用标识。事实是否正确、引用是否支持结论，仍需要你对照原文判断。')}</p></details></aside></div>`,
    bind() {
      const form=$('#compose');
      const update=event=>{
        if (event?.target?.name === 'learner_profile') draft.learner_profile_is_default = false;
        const data=new FormData(form);
        for (const key of ['topic','material','language','question_type']) if(data.has(key)) draft[key]=data.get(key);
        if(data.has('count')) draft.count=Number(data.get('count'));
        const lesson=draft.material==='lesson';
        $('#question-options').hidden=lesson;$('#count').disabled=lesson;$('#question_type').disabled=lesson;
        updateDifficultyControls(form,draft);
        setText($('#draft-kind'), kinds[draft.material]);
        $('#draft-summary').innerHTML=`${m(draft.language==='zh'?'中文':'English')} · ${difficultySummary(draft)}${!lesson&&draft.difficulty_mode==='distribution'?` · ${m('{n} 道题',{n:draft.count||'—'})}`:''}`;
      };
      form.oninput=update;form.onchange=update;update();
      bindDifficultyPresets(form,draft,update);
      form.onsubmit=event=>{event.preventDefault();update();const error=difficultyValidation(draft);if(error){inlineError(form,error);return;}if(!form.reportValidity())return;busy($('button[type=submit]',form),async()=>{await api('/generations','POST',{...generationPayload(draft),count:draft.material==='lesson'?3:draft.count,course_id:course,request_key:crypto.randomUUID()});if(form.isConnected&&state.route.page==='generate'&&state.course===course){state.library={course:'',filter:'all',query:''};go('contents');}else notify('生成任务已创建，可在内容与审核中查看进度。');},form);};
    }
  };
}
function jobRow(job, generation = false) {
  const course = state.courses.find(item => item.id === job.course_id);
  const pending = ['queued','running'].includes(job.status);
  const title = generation ? (pending ? '生成中' : '生成未完成') : (labels[job.kind] || job.kind);
  const action = job.content_id && job.status === 'succeeded'
    ? button('打开','open-content','secondary',`data-id="${esc(job.content_id)}"`)
    : button(generation?'查看任务':'查看详情','job-details','secondary',`data-id="${esc(job.id)}"`);
  return `<div class="collection-row ${generation?'generation-row':''}"><span class="file-icon">${icon('clock-3')}</span><div class="row-main"><h3>${m(title)}</h3><p>${course?`${esc(course.name)} · `:''}${date(job.created_at)}</p>${job.error?`<p class="job-error">${m(job.error)}</p>`:''}</div>${badge(job.status)}<div class="row-actions">${action}</div></div>`;
}
async function renderContents() {
  const view = state.library;
  if (view.course && !state.courses.some(course => course.id === view.course)) view.course = '';
  const load = async () => {
    const [rows,jobs] = await Promise.all([api('/contents'),api('/jobs')]);
    return {rows,jobs};
  };
  let data = await load();
  return {
    toolbar:button(icon('plus')+m('创作材料'),'generate'),
    html:heading('内容与审核')+`<div class="collection-tools library-tools"><div class="segmented" ${a('内容状态筛选','aria-label')}><button data-content-filter="all" aria-pressed="${view.filter==='all'}">${m('全部')}</button><button data-content-filter="draft" aria-pressed="${view.filter==='draft'}"><span id="draft-filter-label"></span></button><button data-content-filter="approved" aria-pressed="${view.filter==='approved'}">${m('已确认')}</button></div><div class="library-filters"><label class="library-course"><span class="sr-only">${m('筛选课程')}</span><select id="library-course"><option value="" data-i18n="全部课程">${t('全部课程')}</option>${state.courses.map(course=>`<option value="${esc(course.id)}" ${view.course===course.id?'selected':''}>${esc(course.name)}</option>`).join('')}</select></label><label class="search"><span class="sr-only">${m('搜索学习材料')}</span><input id="content-search" type="search" value="${esc(view.query)}" ${a('搜索材料标题','placeholder')}></label></div></div><div id="library-update-error" class="live-update-error" role="status" hidden></div><div id="generation-status" aria-live="polite"></div><div id="content-list"></div>`,
    bind() {
      const paint = () => {
        const scoped = data.rows.filter(row => !view.course || row.course_id === view.course);
        setText($('#draft-filter-label'),'待审核 {n}',{n:scoped.filter(row=>row.status==='draft').length});
        const query = view.query.trim().toLowerCase();
        const selected = scoped.filter(row=>(view.filter==='all'||row.status===view.filter)&&row.title.toLowerCase().includes(query));
        const generationJobs = query || view.filter === 'approved' ? [] : data.jobs.filter(job=>job.kind==='generate'&&job.status!=='succeeded'&&(!view.course||job.course_id===view.course));
        const activeJobs = generationJobs.filter(job=>['queued','running'].includes(job.status));
        const failedJobs = generationJobs.filter(job=>!['queued','running'].includes(job.status)).slice(0,3);
        replaceRows($('#generation-status'), [...activeJobs,...failedJobs].map(job=>jobRow(job,true)).join(''));
        replaceRows($('#content-list'), selected.length?`<div class="collection">${selected.map(row=>materialRow(row,true)).join('')}</div>`:activeJobs.length?'':empty(scoped.length?'没有匹配的内容':'暂无材料',scoped.length?'调整筛选条件或搜索名称。':'','generate','开始创作'));
      };
      $('#content-search').oninput = () => {view.query=$('#content-search').value;paint();};
      $('#library-course').onchange = () => {view.course=$('#library-course').value;paint();};
      $$('[data-content-filter]').forEach(control=>control.onclick=()=>{view.filter=control.dataset.contentFilter;$$('[data-content-filter]').forEach(item=>item.setAttribute('aria-pressed',String(item===control)));paint();});
      paint();
      return watchResource({load,initial:data,update:next=>{data=next;paint();},error:cause=>liveError($('#library-update-error'),cause)});
    }
  };
}

function renderAsset(asset, showCitations=true, difficultyAssessment=null) {
  const cites=ids=>showCitations?`<div class="citations">${m('资料片段')} · ${ids.map(esc).join(' · ')}</div>`:'';
  return `<h2>${esc(asset.title)}</h2>${asset.learning_objectives?.length?`<p class="objective">${m('学习目标：')}${asset.learning_objectives.map(esc).join('；')}</p>`:''}${asset.sections.map(section=>`<h3>${esc(section.heading)}</h3><div class="prose">${esc(section.text)}</div>${cites(section.citation_ids)}`).join('')}${asset.questions.map((question,index)=>`<section class="question"><h3>${index+1}. ${esc(question.stem)}</h3>${showCitations?renderQuestionDifficulty(question,index,difficultyAssessment):''}${question.options.length?`<ol type="A">${question.options.map(option=>`<li>${esc(option)}</li>`).join('')}</ol>`:''}${cites(question.citation_ids)}${question.answer!==undefined?`<details><summary>${m('参考答案与解析')}</summary><div class="answer"><p><strong>${esc(question.answer)}</strong></p><p>${esc(question.explanation)}</p></div></details>`:''}</section>`).join('')}`;
}
function renderMedia(media, review=false, version=0) {
  return media.map(item=>`<div class="media-item"><div class="row-actions"><small>${m(item.kind==='audio'?'AI 合成语音':'AI 生成配图')}</small>${review?badge(item.status):''}</div>${item.kind==='audio'?`<audio controls preload="metadata" src="/api/media/${encodeURIComponent(item.id)}/file"></audio>${item.metadata?.transcript?`<p>${esc(item.metadata.transcript)}</p>`:''}`:`<img loading="lazy" src="/api/media/${encodeURIComponent(item.id)}/file" ${a(review?'待核对的教学辅助配图':'已审核的教学辅助配图','alt')}>`}${review?button(item.status==='approved'?'媒体已确认':'确认媒体内容','approve-media','secondary',`data-id="${esc(item.id)}" data-version="${version}" ${item.status==='approved'?'disabled':''}`):''}</div>`).join('');
}
async function renderContent(id) {
  const content=await api('/contents/'+encodeURIComponent(id));
  const cid=encodeURIComponent(id);
  return {
    toolbar:button('返回内容库','contents','secondary'),
    html:`<div class="review-bar">${badge(content.status)}<small>${m('版本 {n}',{n:content.version})}</small><a href="/api/contents/${cid}/export">${m('导出 Markdown')}</a><a href="/api/contents/${cid}/evidence">${m('导出生成依据')}</a>${content.status==='approved'?`<a href="/?learn=${cid}" target="_blank" rel="noopener">${m('打开学习页')}</a>`:''}</div><div class="review-layout"><div><article class="document-paper">${renderAsset(content.asset,true,content.difficulty_assessment)}<div class="submit-bar"><div class="row-actions">${button('编辑内容','edit-content','secondary')}${button(content.status==='approved'?'文字已确认':'确认文字','approve-content','',content.status==='approved'?'disabled':'')}</div></div><div id="edit-panel"></div></article></div>
      <aside class="review-inspector"><section class="inspector-section"><h2>${m('资料依据')}</h2>${content.sources.map((source,index)=>`<details ${index===0?'open':''}><summary>${esc(source.document_name)} · ${m('第 {n} 页',{n:source.page})}</summary><div class="source-chunk">${source.metadata?.source_kind==='figure'?`<figure class="retrieved-figure"><img loading="lazy" src="/api/documents/${encodeURIComponent(source.document_id)}/assets/${encodeURIComponent(source.metadata.source_asset_ids[0])}" alt="${esc(t('原文图像，点击放大'))}"><figcaption>${m(source.metadata.annotation?.origin==='user'?'人工编辑的图片说明':'AI 图片说明（需核对）')}</figcaption></figure>`:''}<p>${esc(source.text)}</p><small>${esc(source.id)}</small>${source.document_id?button('查看原页','parsing','link',`data-id="${esc(source.document_id)}" data-page="${source.page}"`):''}</div></details>`).join('')}</section>
      ${renderMultimodal(content, renderMedia)}
      ${renderDifficultyAssessment(content)}
      <section class="inspector-section"><h2>${m('评价当前版本')}</h2><p>${m('1 分最低，5 分最高。')}</p><form id="evaluation">${[['correctness','内容正确性'],['groundedness','资料支持度'],['difficulty_match','难度匹配度']].map(([name,label])=>`<label>${m(label)}<select name="${name}" required><option value="" data-i18n="选择评分">${t('选择评分')}</option>${[1,2,3,4,5].map(n=>`<option value="${n}" data-i18n="{n} 分" data-i18n-values='{"n":${n}}'>${t('{n} 分',{n})}</option>`).join('')}</select></label>`).join('')}${renderQuestionDifficultyEvaluation(content)}<label>${m('观察记录')}<textarea name="notes" ${a('记录值得保留或需要改进的地方','placeholder')}></textarea></label><button type="submit" class="secondary">${m('保存评价')}</button></form><p><a class="link" href="/api/evaluations/export">${m('导出评价 CSV')}</a></p><p><a class="link" href="/api/difficulty/evaluations/export">${m('导出逐题难度评价 CSV')}</a></p></section></aside></div>`,
    bind(){state.content=content;selectCourse(content.course_id);const evaluation=$('#evaluation');bindDifficultyEvaluation(evaluation);evaluation.onsubmit=event=>{event.preventDefault();const form=event.currentTarget;if(!form.reportValidity())return;busy($('button[type=submit]',form),async()=>{const fields=new FormData(form);const question_difficulties=readQuestionDifficulties(form);await api(`/contents/${cid}/evaluations`,'POST',{version:content.version,notes:fields.get('notes'),question_difficulties,...Object.fromEntries(['correctness','groundedness','difficulty_match'].map(key=>[key,Number(fields.get(key))]))});notify('当前版本的评价已保存。');},form);};}
  };
}
function editContent() {
  const content=state.content;
  const asset=structuredClone(content.asset);
  const field=(title,path,value)=>`<label>${Array.isArray(title)?m(...title):m(title)}<textarea data-path="${path}" required>${esc(value)}</textarea></label>`;
  $('#edit-panel').innerHTML=`<div class="edit-panel"><h3>${m('编辑版本 {n}',{n:content.version})}</h3><p>${m('保存为新版本后，需要重新确认。引用标识将保留，请确保修改后仍有资料支持。')}</p><form id="edit-form">${field('标题','title',asset.title)}${asset.sections.map((section,index)=>field(['小节 {n} 标题',{n:index+1}],`sections.${index}.heading`,section.heading)+field(['小节 {n} 内容',{n:index+1}],`sections.${index}.text`,section.text)).join('')}${asset.questions.map((question,index)=>field(['第 {n} 题',{n:index+1}],`questions.${index}.stem`,question.stem)+question.options.map((option,i)=>field(['选项 {letter}',{letter:String.fromCharCode(65+i)}],`questions.${index}.options.${i}`,option)).join('')+field('参考答案',`questions.${index}.answer`,question.answer)+field('答案解析',`questions.${index}.explanation`,question.explanation)).join('')}<label>${m('配图描述')}<textarea data-path="visual_prompt">${esc(asset.visual_prompt)}</textarea></label><div class="modal-actions">${button('取消编辑','cancel-edit','secondary','type="button"')}<button type="submit">${m('保存新版本')}</button></div></form></div>`;
  pageInputs?.refresh();
  $('#edit-panel').scrollIntoView({block:'start',behavior:'instant'});
  $('#edit-form').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy($('button[type=submit]',form),async()=>{$$('[data-path]',form).forEach(input=>{const keys=input.dataset.path.split('.');let target=asset;for(const key of keys.slice(0,-1))target=target[key];target[keys.at(-1)]=input.value;});await api(`/contents/${encodeURIComponent(content.id)}/review`,'POST',{version:content.version,action:'save',asset});notify('新版本已保存，请重新审核。');await renderRoute();},form);};
}
async function renderJobs() {
  const rows=await api('/jobs');
  const render = rows => rows.length?`<div class="collection">${rows.map(job=>jobRow(job)).join('')}</div>`:empty('暂无任务','','documents','前往课程资料','clock-3');
  return {
    toolbar:button('刷新状态','retry','secondary'),
    html:heading('任务记录')+`<div id="jobs-update-error" class="live-update-error" role="status" hidden></div><div id="job-list">${render(rows)}</div>`,
    bind() { return watchResource({load:()=>api('/jobs'),initial:rows,update:next=>replaceRows($('#job-list'),render(next)),error:cause=>liveError($('#jobs-update-error'),cause)}); }
  };
}

async function renderLearning() {
  document.body.classList.add('learning');
  const data=await api(`/learn/${encodeURIComponent(state.learningId)}?reveal_answers=${Boolean(state.reveal)}`);
  return {html:`<article class="document-paper">${renderAsset(data.asset,false)}${!state.reveal&&data.asset.questions.length?`<div class="submit-bar">${button('查看答案与解析','reveal')}</div>`:''}${renderMedia(data.media)}</article>`};
}
const renderers={home:renderHome,documents:renderDocuments,generate:renderGenerate,contents:renderContents,jobs:renderJobs,content:renderContent,learning:renderLearning};

const actions={
  account:()=>{const user=auth.session.user;if(!user)return;dialog('账号',`<div class="account-details"><strong>${esc(user.display_name)}</strong><p>${esc(user.email)}</p></div><p>${m('课程、资料和学习材料保存在当前账号下。')}</p><div class="modal-actions">${button('退出登录','logout','secondary')}</div>`);},
  logout,
  home:()=>go('home'), documents:()=>go('documents'), generate:()=>go('generate'), contents:()=>go('contents'), retry:renderRoute,
  'close-modal':()=>modal.close(),
  'new-course':()=>{
    dialog('新建课程',`<form id="new-course-form"><label>${m('课程名称')}<input name="name" required ${a('例如：机器学习基础','placeholder')} autocomplete="off"></label><div class="modal-actions">`+button('取消','close-modal','secondary','type="button"')+`<button type="submit">${m('创建课程')}</button></div></form>`);
    $('#new-course-form').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy($('button[type=submit]',form),async()=>{const course=await api('/courses','POST',{name:new FormData(form).get('name')});selectCourse(course.id);await loadCourses();modal.close();notify('课程已创建，可以导入资料了。');go('documents');},form);};
  },
  upload:()=>{
    if(!state.course) return actions['new-course']();
    const course=state.course;
    dialog('导入课程资料',`<p>${m('支持 PDF、TXT、Markdown、PNG 和 JPEG，单份最大 10 MB、300 页。')}</p><form id="upload-form"><label>${m('选择文件')}<input type="file" name="file" accept=".pdf,.txt,.md,.png,.jpg,.jpeg" required></label>${parserControls()}<div class="modal-actions">`+button('取消','close-modal','secondary','type="button"')+`<button type="submit">${m('导入并解析')}</button></div></form>`);
    $('#upload-form').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy($('button[type=submit]',form),async()=>{const data=new FormData(form);data.append('course_id',course);const result=await api('/documents','POST',data);modal.close();notify(result.duplicate?'这份资料已经导入。':result.job_id?'资料已保存，后台正在解析，可在任务页面查看进度。':result.parsing?.status==='good'?'资料已解析，可以查看片段并建立索引。':'资料已保留，部分页面需核对，请打开文字与原图。');go('documents');},form);};
  },
  settings:async()=>{
    const status=await api('/status');
    dialog('模型连接',`<p>${m('按需连接不同能力。填写配置后，需要实际调用验证连接。')}</p>${Object.entries(status.capabilities).map(([key,cap])=>`<div class="connection-row"><div class="row-main"><h3>${m(labels[key])}</h3><p>${cap.model ? esc(cap.model) : m('尚未指定模型')}</p></div><span class="badge ${cap.configured?'ready':''}">${m(cap.configured?'已填写配置':'未配置')}</span></div>`).join('')}<div class="inline-note">${m('在本地 .env 文件中设置服务地址、模型名称和密钥，然后重启应用。密钥不会显示在网页中。')}</div><p class="muted">${m('今日调用 {count} / {limit} · UTC',{count:status.calls_today,limit:status.daily_call_limit})}</p>`);
  },
  'create-kind':control=>{draftFor().material=control.dataset.kind;go('generate');},
  'open-content':control=>go('content',control.dataset.id),
  chunks:async control=>{const rows=await api(`/documents/${encodeURIComponent(control.dataset.id)}/chunks`);dialog(control.dataset.name,rows.map(chunk=>`<section class="source-chunk"><h3>${m('第 {n} 页',{n:chunk.page})}</h3><p>${esc(chunk.text)}</p></section>`).join(''),true,true);},
  parsing:async control=>{
    const id=control.dataset.id, report=await api(`/documents/${encodeURIComponent(id)}/parsing`);
    dialog('文字与原图对照',parsingDialog(report,id),true);
    const select=$('#parsing-page');
    if(select){select.value=String(control.dataset.page||1);select.onchange=()=>{$('#parsing-detail').innerHTML=parsingPage(report,select.value);};select.onchange();}
  },
  reparse:async control=>{const tier=$('[name=tier]',modal)?.value||'standard',images=Boolean($('[name=images]',modal)?.checked);const result=await api(`/documents/${encodeURIComponent(control.dataset.id)}/parse`,'POST',{tier,images});modal.close();notify(result.job_id?'资料已保存，后台正在解析，可在任务页面查看进度。':'资料已重新解析，请核对原图后重新建立索引。');go(result.job_id?'jobs':'documents');},
  'retry-figure':async control=>{const suffix=control.dataset.asset?'?asset_id='+encodeURIComponent(control.dataset.asset):'';await api(`/documents/${encodeURIComponent(control.dataset.id)}/figures/retry${suffix}`,'POST');modal.close();notify('图片处理任务已创建。');go('jobs');},
  'edit-figure':async control=>{const id=control.dataset.id,aid=control.dataset.asset;const figures=await api(`/documents/${encodeURIComponent(id)}/figures`),figure=figures.find(f=>f.asset_id===aid);dialog('编辑图片说明',`<form id="figure-edit"><label>${m('图片说明')}<textarea name="description" required maxlength="2400">${esc(figure?.annotation?.data.description||'')}</textarea></label><p>${m('保存后原图片索引失效，请更新图片索引。')}</p><button type="submit">${m('保存')}</button></form>`);$('#figure-edit').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy($('button[type=submit]',form),async()=>{await api(`/documents/${encodeURIComponent(id)}/figures/${encodeURIComponent(aid)}`,'PATCH',{description:new FormData(form).get('description')});modal.close();notify('保存后原图片索引失效，请更新图片索引。');go('documents');},form);};},
  index:async control=>{await api(`/documents/${encodeURIComponent(control.dataset.id)}/index`,'POST');notify('索引任务已创建。');go('jobs');},
  'job-details':async control=>{const revision=state.revision;const job=await api(`/jobs/${encodeURIComponent(control.dataset.id)}`);if(revision!==state.revision)return;if(job.course_id)selectCourse(job.course_id);if(job.status==='succeeded'&&(job.content_id||job.result?.content_id))return go('content',job.content_id||job.result.content_id);dialog('任务详情',`${badge(job.status)}<div class="inline-note">${m(job.error||job.result?.message||(job.status==='succeeded'?'处理完成，可以返回资料库查看索引状态。':'任务正在处理，请稍后刷新状态。'))}</div>`);},
  'approve-content':async()=>{const content=state.content;await api(`/contents/${encodeURIComponent(content.id)}/review`,'POST',{version:content.version,action:'approve'});notify('文字已确认，可以打开学习页或制作媒体。');await renderRoute();},
  'edit-content':editContent,
  'cancel-edit':()=>{$('#edit-panel').innerHTML='';},
  'make-media':async control=>{const content=state.content;await api(`/contents/${encodeURIComponent(content.id)}/media`,'POST',{version:content.version,kind:control.dataset.kind,request_key:crypto.randomUUID()});notify('媒体生成任务已创建。');go('jobs');},
  'approve-media':async control=>{await api(`/media/${encodeURIComponent(control.dataset.id)}/approve`,'POST',{version:Number(control.dataset.version)});notify('媒体内容已确认。');await renderRoute();},
  reveal:async()=>{state.reveal=true;await renderRoute();}
};

document.addEventListener('click',event=>{const control=event.target.closest('[data-action]');if(!control||!workspaceReady)return;const action=actions[control.dataset.action];if(action){event.preventDefault();busy(control,()=>action(control));}});
document.addEventListener('pointerdown',()=>document.body.dataset.input='pointer',{capture:true,passive:true});
document.addEventListener('keydown',()=>document.body.dataset.input='keyboard',{capture:true});
$('.skip').onclick=event=>{event.preventDefault();$('#main').focus();$('#main').scrollIntoView({block:'start',behavior:'instant'});};
$('#notice .notice-close').onclick=()=>$('#notice').hidden=true;
$('#course-select').onchange=async()=>{if(!workspaceReady)return;selectCourse($('#course-select').value);state.content=null;if(state.route.page==='content')go('contents');else await renderRoute();};
window.addEventListener('hashchange',()=>{if(captureRestoreLink()){restoreAttemptUser=null;checkSession({suspend:true});return;}renderRoute();});
window.addEventListener('pagehide',()=>auth.suspend('checking'));
window.addEventListener('pageshow',event=>{if(event.persisted)checkSession({suspend:true});});
window.addEventListener('storage',event=>{if(event.key==='zhixu.session-change')checkSession({suspend:true});});
window.addEventListener('focus',()=>checkSession());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkSession();});
$$('[data-theme-toggle]').forEach(control => control.onclick = () => { window.uiPreferences.toggleTheme(); refreshPreferences(); });
$$('[data-language-toggle]').forEach(control => control.onclick = () => { window.uiPreferences.toggleLanguage(); refreshPreferences(); });
window.addEventListener('appearancechange', refreshPreferences);
refreshPreferences();
checkSession();
