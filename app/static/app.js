import { renderAsset, renderContentSources } from './content-renderer.js';
import { mountCitations } from './citations.js';
import { renderRatingPanel, bindRatingPanel, renderExportDialog, readExportOptions, renderReviewBar } from './content-review.js';
import { renderQuestionRevisionDialog, bindQuestionRevision, renderQuestionComparison } from './question-revision.js';
import { restoreGenerationDraft } from './generation-retry.js';
import { renderLibraryRow, courseLibraryRows } from './content-library.js';
import { downloadFile } from './downloads.js';
import { documentControls } from './document-controls.js';
import { renderFilePicker, installControlLanguage, refreshControlLanguage } from './control-language.js';
import { createSessionClient, isSessionChange, accountCourseKey } from './auth.js';
import { parsingDialog, parsingPage, parsingLabel, parserControls } from './parsing.js';
import { renderOutputFormats, renderMultimodal } from './multimodal.js';
import { watchResource } from './live-updates.js';
import { beginViewLoad, patchCollection } from './view-updates.js';
import { renderGenerationProgress, mountGenerationProgress, watchGenerationProgress } from './generation-progress.js';
import { mountInputControls } from './input-controls.js';
import { t, m, a, setText, localize, formatDate } from './i18n.js';
import { createGenerationFlow, renderPreparationDialog } from './clarification.js';
import { createGenerationPreferences } from './generation-preferences.js';
import { renderSwitch, renderSettings, renderModelConnections } from './settings.js';
import { generationEligibility, renderExternalSource } from './exploration.js';
import { ensureDifficultyDraft, renderLessonDepth, renderDifficultyControls, updateDifficultyControls, renderExplanationOption, updateExplanationOption, bindDifficultyPresets, difficultyValidation, difficultySummary, generationPayload, renderDifficultyAssessment } from './difficulty.js';
/* Learning Studio: independent page renderers, native controls, one API boundary. */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const page = $('#page');
const modal = $('#modal');
let pageInputs = null, modalInputs = null;
let routeLoad = null;
modal.addEventListener('close', () => { modalInputs?.destroy(); modalInputs = null; });
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const icon = name => `<img class="icon" src="/static/icons/${name}.svg" alt="">`;
const titles = {home:'课程概览', documents:'课程资料', generate:'创作学习材料', contents:'内容与审核', jobs:'任务记录', progress:'生成进度', content:'内容详情', partial:'已完成题目', learning:'学习材料'};
const labels = {ready:'索引就绪', parsed:'待建索引', draft:'待审核', approved:'已确认', queued:'排队中', running:'处理中', cancelled:'已停止', revise_question:'题目修改', succeeded:'已完成', failed:'处理失败', insufficient_evidence:'资料依据不足', parse:'资料解析', figures:'图片理解', pending:'待处理', parse_failed:'解析失败', index:'资料索引', generate:'内容生成', media:'媒体生成', vision:'视觉理解模型', text:'文本模型', embedding:'嵌入模型', speech:'语音模型', image:'图像模型'};
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
const generationPreferences = createGenerationPreferences({
  storage:{getItem:key=>localStorage.getItem(key), setItem:(key,value)=>localStorage.setItem(key,value)},
  onChange:detail=>{
    const control=$('#query-fusion');
    if(control)control.checked=detail.queryFusion;
    const explorationControl=$('#auto-explore');
    if(explorationControl)explorationControl.checked=detail.autoExplore;
    const subagentControl=$('#use-subagents');
    if(subagentControl)subagentControl.checked=detail.useSubagents;
    for(const option of $$('input[name="progress-mode"]',modal))option.checked=option.value===detail.progressMode;
    if(detail.changed.some(key=>['queryFusion','autoExplore','useSubagents'].includes(key)))window.dispatchEvent(new CustomEvent('generationpreferenceschange',{detail}));
    if(detail.changed.includes('progressMode')&&state.route.page==='progress')void renderRoute();
  },
});
function clearWorkspace(reason = '') {
  workspaceReady = false;
  generationPreferences.setAccount(null);
  state.cleanup?.(); state.cleanup = null;
  routeLoad?.cancel(); routeLoad = null;
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
  $('#auth-content').innerHTML = `<h1 id="auth-title">${m(registering?'创建你的账号':'登录知序')}</h1>${message?`<p class="auth-message" role="status">${m(message)}</p>`:''}${pendingRestore?`<p class="auth-message">${m('有一份原有工作空间等待恢复，请使用对应邮箱登录或注册。')}${restoreEmail?`<br>${esc(restoreEmail)}`:''}</p>`:''}<form id="auth-form">${registering?`<label for="auth-name">${m('你的称呼')}<input id="auth-name" name="display_name" autocomplete="nickname" maxlength="80" required></label>`:''}<label for="auth-email">${m('邮箱')}<input id="auth-email" name="email" type="email" value="${pendingRestore?esc(restoreEmail):''}" autocomplete="username" inputmode="email" autocapitalize="none" spellcheck="false" maxlength="254" required></label><label for="auth-password">${m('密码')}<input id="auth-password" name="password" type="password" autocomplete="${registering?'new-password':'current-password'}" ${registering?'minlength="15"':''} maxlength="128" required ${registering?'aria-describedby="auth-password-hint"':''}>${registering?`<span id="auth-password-hint" class="auth-hint">${m('15–128 个字符')}</span>`:''}</label>${registering?`<label for="auth-confirm">${m('确认密码')}<input id="auth-confirm" name="password_confirmation" type="password" autocomplete="new-password" minlength="15" maxlength="128" required></label>`:''}<button type="submit" class="auth-submit">${m(registering?'创建账号':'登录')}</button></form><div class="auth-switch"><span>${m(registering?'已有账号？':'还没有账号？')}</span><button class="link" id="auth-switch" type="button">${m(registering?'去登录':'创建账号')}</button></div>`;
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
  generationPreferences.setAccount(session.user.id);
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
  if (state.course !== id) state.library = {course:id,filter:'all',query:''};
  state.course = id;
  $('#course-select').value = id;
  try { localStorage.setItem(accountCourseKey(auth.session.user?.id), id); } catch { /* Session selection remains available. */ }
}
function replaceRows(root, html) {
  const focused = root.contains(document.activeElement) ? document.activeElement : null;
  const action = focused?.dataset.action, id = focused?.dataset.id;
  const documentId = focused?.dataset.document;
  patchCollection(root, html);
  if (action && !focused.isConnected) $$('[data-action]', root).find(control => control.dataset.action === action && control.dataset.id === id)?.focus({preventScroll:true});
  else if (documentId && !focused.isConnected) $$('[data-document]',root).find(control=>control.dataset.document===documentId)?.focus({preventScroll:true});
}
function liveError(root, error) {
  if (root.hidden !== !error) root.hidden = !error;
  const html = error ? `${m('自动更新暂不可用')} ${button('刷新状态','retry','link')}` : '';
  if (root.innerHTML !== html) root.innerHTML = html;
}
async function loadCourses() {
  state.courses = await api('/courses');
  if (!state.courses.some(course => course.id === state.course)) selectCourse(state.courses[0]?.id || '');
  $('#course-select').innerHTML = state.courses.length ? state.courses.map(course => `<option value="${esc(course.id)}">${esc(course.name)}</option>`).join('') : `<option value="" data-i18n="选择或新建课程">${t('选择或新建课程')}</option>`;
  $('#course-select').value = state.course;
}
function courseUrl(resource, course = state.course) { return `/${resource}?course_id=${encodeURIComponent(course)}`; }
function renderNavigation(name) {
  const active = name === 'content' ? 'contents' : ['progress','partial'].includes(name) ? 'jobs' : name;
  $$('nav a').forEach(link => { if (link.dataset.page === active) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current'); });
  setText($('#route-label'), titles[name] || titles.home);
  document.title = `${t(titles[name] || titles.home)} · ${t('知序')}`;
}
function refreshPreferences() {
  localize();
  refreshControlLanguage();
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
  languageToggle.textContent = english ? 'ZH' : 'EN';
  languageToggle.lang = 'en';
  languageToggle.setAttribute('aria-label', t(english ? '切换到中文' : '切换到英文'));
  languageToggle.title = t(english ? '切换到中文' : '切换到英文');
  }
  if (!workspaceReady) document.title = `${t(authMode==='register'?'创建你的账号':'登录知序')} · ${t('知序')}`;
}
async function renderRoute() {
  if (!workspaceReady || !auth.session.user) return;
  state.cleanup?.(); state.cleanup = null;
  pageInputs?.destroy(); pageInputs = null;
  routeLoad?.cancel();
  const revision = ++state.revision;
  const [requested = 'home', rawId = ''] = location.hash.slice(1).split('/');
  const name = state.learningId ? 'learning' : (renderers[requested] ? requested : 'home');
  let id; try { id = decodeURIComponent(rawId); } catch { id = ''; }
  state.route = {page:name, id};
  renderNavigation(name);
  $('#toolbar-actions').innerHTML = '';
  const loading = routeLoad = beginViewLoad(page, `<div class="loading" role="status">${m('正在载入…')}</div>`);
  const read = path => api(path, 'GET', undefined, {signal:loading.signal});
  try {
    const result = await renderers[name](id, read);
    if (revision !== state.revision) return;
    loading.finish();
    page.innerHTML = result.html;
    $('#toolbar-actions').innerHTML = result.toolbar || '';
    state.cleanup = result.bind?.() || null;
    pageInputs = mountInputControls(page);
    window.scrollTo({top:0,behavior:'instant'});
  } catch (error) {
    if (revision !== state.revision || loading.signal.aborted) return;
    loading.finish();
    page.innerHTML = empty('暂时无法载入', error.uiMessage || error.message, 'retry', '重新尝试');
  }
}
function materialRow(content, showCourse = false) {
  return renderLibraryRow(content,{showCourse});
}

async function renderHome(_id, read = api) {
  if (!state.course) return {...courseRequired(), toolbar:button('模型连接','settings','secondary')};
  const course = state.courses.find(item => item.id === state.course);
  const [docs, contents] = await Promise.all([read(courseUrl('documents')), read(courseUrl('contents'))]);
  const drafts = contents.filter(content => content.status === 'draft').length;
  return {
    toolbar:button(icon('upload')+m('导入资料'),'upload','secondary'),
    html:`<section class="overview-heading"><h1>${esc(course.name)}</h1><div class="course-stats"><div><strong>${docs.length}</strong><span>${m('资料')}</span></div><div><strong>${contents.length}</strong><span>${m('材料')}</span></div><div><strong>${drafts}</strong><span>${m('待审核')}</span></div></div></section>
      <div class="home-grid"><section class="creation-section" ${a('创作材料','aria-label')}><div class="quick-create">${[['lesson','book-open'],['quiz','layers'],['assignment','file-text']].map(([kind,symbol])=>button(`${icon(symbol)}<strong>${m(kinds[kind])}</strong><img class="choice-arrow" src="/static/icons/arrow-up-right.svg" alt="">`,'create-kind','create-choice',`data-kind="${kind}"`)).join('')}</div></section>
      <section class="home-panel materials-panel"><div class="heading-row"><h2>${m('最近材料')}</h2>${button(icon('arrow-right'),'contents','plain icon-button',`${a('查看全部材料','aria-label')} ${a('查看全部材料','title')}`)}</div>${contents.length?`<div class="collection">${contents.slice(0,4).map(content=>materialRow(content)).join('')}</div>`:`<div class="recent-empty">${icon('book-open')}<p>${m('暂无材料')}</p></div>`}</section>
      <section class="home-panel documents-panel"><div class="heading-row"><h2>${m('课程资料')}</h2>${button(icon('arrow-right'),'documents','plain icon-button',`${a('管理课程资料','aria-label')} ${a('管理课程资料','title')}`)}</div>${docs.length?`<div class="collection">${docs.slice(0,3).map(doc=>`<div class="collection-row"><span class="file-icon">${icon('file-text')}</span><div class="row-main"><h3 id="document-name-${esc(doc.id)}">${esc(doc.name)}</h3><p>${m('{n} 页',{n:doc.pages})}</p>${renderExternalSource(doc)}</div>${badge(['pending','parse_failed'].includes(doc.status)?doc.status:doc.index_current?'ready':'parsed')}</div>`).join('')}</div>`:`<div class="recent-empty"><p>${m('暂无资料')}</p></div>`}</section></div>`
  };
}

async function renderDocuments(_id, read = api) {
  if (!state.course) return courseRequired();
  let docs = await read(courseUrl('documents')+'&include_deleted=true');
  const documentsCourse=state.course;
  return {
    toolbar:button(icon('upload')+m('导入资料'),'upload'),
    html:heading('课程资料')+`<div class="collection-tools"><div class="segmented" ${a('资料状态筛选','aria-label')}><button data-filter="all" aria-pressed="true">${m('全部 {n}',{n:docs.length})}</button><button data-filter="ready" aria-pressed="false">${m('索引就绪')}</button><button data-filter="parsed" aria-pressed="false">${m('待建索引')}</button><button data-filter="deleted" aria-pressed="false">${m('最近删除')}</button></div><label class="search"><span class="sr-only">${m('搜索文件')}</span><input id="file-search" type="search" ${a('搜索文件名称','placeholder')}></label></div><div id="files"></div>`,
    bind() {
      let filter = 'all';
      const saves=new Map(),confirmed=new Map(docs.map(d=>[d.id,d.enabled!==false]));
      const paint = () => {
        const total=docs.filter(d=>!d.deleted_at).length;
        if ($('[data-filter=all]').dataset.total !== String(total)) { $('[data-filter=all]').dataset.total=total; $('[data-filter=all]').innerHTML=m('全部 {n}',{n:total}); }
        const query = $('#file-search').value.trim().toLowerCase();
        const rows = docs.filter(doc => doc.name.toLowerCase().includes(query) && (filter==='deleted'?Boolean(doc.deleted_at):!doc.deleted_at&&(filter==='all'||(filter==='ready')===Boolean(doc.index_current))));
        replaceRows($('#files'), rows.length ? `<div class="collection">${rows.map(doc=>`<div data-row-key="document:${esc(doc.id)}" class="collection-row document-row ${doc.enabled===false?'document-inactive':''}"><span class="file-icon">${icon('file-text')}</span><div class="row-main"><h3 id="document-name-${esc(doc.id)}">${esc(doc.name)}</h3><p>${m('{n} 页',{n:doc.pages})} · ${m('{n} 个知识片段',{n:doc.chunks})}${['needs_review','unreadable'].includes(doc.parsing?.status)?' · '+m(parsingLabel(doc.parsing.status)):''}${doc.active_jobs?.length?' · '+m('后台处理中'):''}${doc.figures?.length?' · '+m('可检索图片 {ready} / {total}',{ready:doc.figures.filter(f=>f.status==='ready').length,total:doc.figures.length}):''}</p>${renderExternalSource(doc)}</div>${badge(['pending','parse_failed'].includes(doc.status)?doc.status:doc.index_current?'ready':'parsed')}<div class="document-management">${documentControls(doc)}</div><div class="row-actions" ${doc.deleted_at?'hidden':''}>${button('文字与原图','parsing','secondary',`data-id="${esc(doc.id)}"`)}${button('阅读片段','chunks','secondary',`data-id="${esc(doc.id)}" data-name="${esc(doc.name)}"`)}${button(doc.index_current?'重建索引':'建立索引','index','secondary',`data-id="${esc(doc.id)}" ${doc.status==='pending'?'disabled':''}`)}</div></div>`).join('')}</div>` : empty(docs.length?'没有匹配的资料':'暂无资料',docs.length?'试试其他名称或筛选条件。':'',docs.length?'':'upload','导入资料','files'));
        $$('.document-enabled').forEach(input=>{input.onchange=()=>{
          const id=input.dataset.document,doc=docs.find(d=>d.id===id),enabled=input.checked;
          doc.enabled=enabled;
          input.closest('.document-row').classList.toggle('document-inactive',!enabled);
          input.setAttribute('aria-busy','true');
          const prior=saves.get(id)||Promise.resolve();
          const pending=prior.catch(()=>{}).then(()=>api(`/documents/${encodeURIComponent(id)}`,'PATCH',{enabled})).then(result=>{confirmed.set(id,result.enabled);return result;});
          saves.set(id,pending);
          pending.catch(error=>{if(saves.get(id)===pending&&input.isConnected){notify(error.message);doc.enabled=confirmed.get(id);input.checked=doc.enabled;input.closest('.document-row').classList.toggle('document-inactive',!doc.enabled);}}).finally(()=>{if(saves.get(id)===pending){saves.delete(id);input.removeAttribute('aria-busy');}});
        };});
      };
      $('#file-search').oninput = paint;
      $$('[data-filter]').forEach(control => control.onclick = () => {filter=control.dataset.filter;$$('[data-filter]').forEach(item=>item.setAttribute('aria-pressed',String(item===control)));paint();});
      paint();
      return watchResource({load:({signal})=>api(courseUrl('documents',documentsCourse)+'&include_deleted=true','GET',undefined,{signal}),initial:docs,update:next=>{if(!saves.size&&JSON.stringify(docs)!==JSON.stringify(next)){docs=next;docs.forEach(d=>confirmed.set(d.id,d.enabled!==false));paint();}}});
    }
  };
}
function draftFor(course = state.course) {
  if (!state.drafts.has(course)) state.drafts.set(course,{material:'lesson',topic:'',difficulty:'medium',language:'zh',question_type:'mixed',count:3});
  return ensureDifficultyDraft(state.drafts.get(course));
}
async function renderGenerate(_id, read = api) {
  if (!state.course) return courseRequired();
  const course = state.course;
  const draft = draftFor(course);
  const [docs,status] = await Promise.all([read(courseUrl('documents',course)),read('/status')]);
  const ready = docs.filter(doc=>doc.index_current&&doc.enabled!==false).length;
  const select = (id,label,values) => `<label class="select-field">${m(label)}<select id="${id}" name="${id}">${values.map(([value,text])=>`<option value="${value}" ${String(draft[id])===value?'selected':''} data-i18n="${text}">${t(text)}</option>`).join('')}</select></label>`;
  return {
    html:heading('创作学习材料')+`<div class="compose-layout"><form id="compose" class="compose-form"><fieldset><legend class="sr-only">${m('材料类型')}</legend><div class="type-switch">${Object.entries(kinds).map(([kind,title])=>`<label><input type="radio" name="material" value="${kind}" ${draft.material===kind?'checked':''}>${m(title)}</label>`).join('')}</div></fieldset>
      <div class="writing-field"><label for="topic">${m('学习目标')}</label><textarea id="topic" name="topic" required minlength="2" ${a('输入知识点或学习目标','placeholder')}>${esc(draft.topic)}</textarea></div>
      <div class="compose-options"><div class="field-grid">${select('language','内容语言',[['zh','中文'],['en','English']])}</div><div class="field-grid" id="question-options">${select('question_type','题目形式',[['mixed','选择题与简答题'],['mcq','选择题'],['short_answer','简答题']])}<div class="number-field"><label for="count">${m('题目数量')}</label><input type="number" name="count" id="count" min="1" max="50" step="1" required value="${draft.count}"></div></div>${renderLessonDepth(draft)}</div>
      ${renderDifficultyControls(draft)}
      ${renderExplanationOption(draft)}${renderOutputFormats()}${renderSwitch({id:'check-missing-details',label:'生成前追问',checked:true,className:'clarification-check'})}<div class="submit-bar"><button type="submit" aria-describedby="generation-eligibility">${icon('wand-sparkles')}${m('开始生成')}</button></div></form>
      <aside class="context-pane"><div class="context-title">${m('本次创作')}</div><h2 id="draft-kind"></h2><p id="draft-summary"></p><div class="context-divider"></div><div class="heading-row"><h3>${m('课程知识')}</h3><small>${m('{ready} / {total} 份就绪',{ready,total:docs.length})}</small></div>${docs.filter(doc=>doc.enabled!==false).slice(0,4).map(doc=>`<div class="context-file">${icon('file-text')}<span>${esc(doc.name)}</span></div>`).join('')||`<p>${m(docs.length?'暂无启用的资料':'尚未导入课程资料。')}</p>`}${button('管理课程资料','documents','link')}<div id="generation-eligibility" class="inline-note" role="status" hidden></div>${!status.capabilities.text.configured||!status.capabilities.embedding.configured?button('配置文本与嵌入模型','settings','link'):''}</aside></div>`,
    bind() {
      const form=$('#compose');
      const submitControl=$('button[type=submit]',form), submitLabel=submitControl.innerHTML;
      const epoch=auth.epoch, routeRevision=state.revision;
      let flow, draftSignature='', clarificationOpen=false;
      const current=()=>form.isConnected&&state.route.page==='generate'&&state.course===course&&state.revision===routeRevision&&auth.epoch===epoch;
      const eligibility=()=>generationEligibility({course,documents:docs,status,autoExplore:generationPreferences.autoExplore});
      const updateEligibility=(phase=flow?.state.phase)=>{
        const available=eligibility(), note=$('#generation-eligibility');
        submitControl.disabled=!available.allowed||['checking','submitting'].includes(phase);
        const message=available.reason||(available.exploration?(ready?'资料不足时自动查找':'将自动查找参考资料'):'');
        note.hidden=!message;
        if(message)setText(note,message);
      };
      const closeClarification=()=>{
        const ownsDialog=clarificationOpen&&Boolean($('[data-clarification-dialog]',modal));
        clarificationOpen=false;
        if(ownsDialog&&modal.open)modal.close();
      };
      const perform=async(task,errorForm=form)=>{
        try { await task(); }
        catch(error){
          if(isSessionChange(error)||!current())return;
          const target=errorForm.isConnected&&(errorForm===form||modal.open)?errorForm:form;
          inlineError(target,error.uiMessage||error.message);
        }
      };
      const showClarification=view=>{
        dialog(view.phase==='clarification_required'?'补充一点信息':'需求检查暂不可用',renderPreparationDialog(view));
        clarificationOpen=true;
        const replyForm=$('#clarification-form',modal), answer=$('#clarification-answer',replyForm);
        $('[data-clarification-cancel]',replyForm).onclick=()=>flow.cancel();
        $('[data-clarification-unknown]',replyForm)?.addEventListener('click',()=>perform(()=>flow.unknown(),replyForm));
        for(const choice of $$('[data-clarification-choice]',replyForm))choice.onclick=()=>{
          answer.value=view.options[Number(choice.dataset.clarificationChoice)];
          answer.dispatchEvent(new Event('input',{bubbles:true}));answer.focus();
        };
        replyForm.oninput=()=>{
          $('.error-message',replyForm)?.remove();
          for(const choice of $$('[data-clarification-choice]',replyForm))choice.setAttribute('aria-pressed',String(answer.value===view.options[Number(choice.dataset.clarificationChoice)]));
        };
        replyForm.onsubmit=event=>{
          event.preventDefault();
          if(view.phase==='clarification_required'){
            if(!replyForm.reportValidity())return;
            perform(()=>flow.answer(answer.value),replyForm);
          }else perform(()=>flow.skip(),replyForm);
        };
        (answer||$('button[type=submit]',replyForm)).focus();
      };
      flow=createGenerationFlow({request:api,isCurrent:current,onState:view=>{
        if(!current())return;
        const pending=['checking','submitting'].includes(view.phase);
        updateEligibility(view.phase);
        if(pending)submitControl.setAttribute('aria-busy','true');else submitControl.removeAttribute('aria-busy');
        submitControl.innerHTML=pending?m(view.phase==='checking'?'正在检查学习目标…':'正在创建生成任务…'):view.phase==='retry_generation'?m('重试生成'):submitLabel;
        localize(submitControl);
        if(['clarification_required','unavailable'].includes(view.phase)){
          if(!clarificationOpen||!$('[data-clarification-dialog]',modal))showClarification(view);
        }else if(['cancelled','expired','complete'].includes(view.phase))closeClarification();
        if(clarificationOpen&&$('[data-clarification-dialog]',modal)){
          for(const control of $$('button,textarea',modal))control.disabled=pending;
          if(pending)$('#clarification-form',modal).setAttribute('aria-busy','true');else $('#clarification-form',modal).removeAttribute('aria-busy');
        }
      },onCreated:job=>{
        state.library={course,filter:'all',query:''};go('progress',job.job_id);
      }});
      const modalClosed=()=>{if(clarificationOpen){clarificationOpen=false;flow.cancel();}};
      const modalCancelled=event=>{if(clarificationOpen&&flow.state.phase==='submitting')event.preventDefault();};
      modal.addEventListener('close',modalClosed);
      modal.addEventListener('cancel',modalCancelled);
      const update=event=>{
        if (event?.target?.name === 'learner_profile') draft.learner_profile_is_default = false;
        const data=new FormData(form);
        for (const key of ['topic','material','language','question_type']) if(data.has(key)) draft[key]=data.get(key);
        if(data.has('count')) draft.count=Number(data.get('count'));
        const lesson=draft.material==='lesson';
        $('#question-options').hidden=lesson;$('#count').disabled=lesson;$('#question_type').disabled=lesson;
        updateDifficultyControls(form,draft);
        updateExplanationOption(form,draft);
        setText($('#draft-kind'), kinds[draft.material]);
        $('#draft-summary').innerHTML=`${m(draft.language==='zh'?'中文':'English')} · ${difficultySummary(draft)}${!lesson&&draft.difficulty_mode==='distribution'?` · ${m('{n} 道题',{n:draft.count||'—'})}`:''}${!lesson&&generationPreferences.useSubagents?` · ${m('逐题子代理')}`:''}`;
        const nextSignature=JSON.stringify(generationPayload(draft));
        if((draftSignature&&draftSignature!==nextSignature)||event?.target?.id==='check-missing-details')flow.cancel();
        draftSignature=nextSignature;
      };
      form.oninput=update;form.onchange=update;update();updateEligibility();
      bindDifficultyPresets(form,draft,update);
      const appearanceUpdated=()=>update();
      const preferencesUpdated=()=>{if(flow.state.phase!=='submitting')flow.cancel();update();updateEligibility();};
      const settingsOpening=event=>{if(flow.state.phase==='submitting')event.preventDefault();else flow.cancel();};
      window.addEventListener('appearancechange',appearanceUpdated);
      window.addEventListener('generationpreferenceschange',preferencesUpdated);
      window.addEventListener('generationsettingsopen',settingsOpening);
      form.onsubmit=event=>{
        event.preventDefault();update();
        const available=eligibility();if(!available.allowed){inlineError(form,available.reason);return;}
        const error=difficultyValidation(draft);if(error){inlineError(form,error);return;}
        if(!form.reportValidity())return;
        $('.error-message',form)?.remove();
        if(flow.state.phase==='retry_generation'){perform(()=>flow.retry());return;}
        const snapshot={...generationPayload(draft),...generationPreferences.requestFields(draft.material),count:draft.material==='lesson'?3:draft.count,course_id:course,request_key:crypto.randomUUID()};
        perform(()=>flow.start(snapshot,{check:$('#check-missing-details',form).checked}));
      };
      return ()=>{
        modal.removeEventListener('close',modalClosed);modal.removeEventListener('cancel',modalCancelled);
        window.removeEventListener('appearancechange',appearanceUpdated);
        window.removeEventListener('generationpreferenceschange',preferencesUpdated);
        window.removeEventListener('generationsettingsopen',settingsOpening);
        closeClarification();flow.cancel();
      };
    }
  };
}
function jobRow(job, generation = false) {
  const course = state.courses.find(item => item.id === job.course_id);
  const title = job.title ? esc(job.title) : m(labels[job.kind] || job.kind);
  const type = job.kind === 'generate' ? (kinds[job.material] || labels.generate) : (labels[job.kind] || job.kind);
  const action = job.content_id && job.status === 'succeeded'
    ? button('打开','open-content','secondary',`data-id="${esc(job.content_id)}"`)
    : button(generation?'查看任务':'查看详情','job-details','secondary',`data-id="${esc(job.id)}"`);
  return `<div data-row-key="job:${esc(job.id)}" class="collection-row ${generation?'generation-row':''}"><span class="file-icon">${icon('clock-3')}</span><div class="row-main"><h3>${title}</h3><p>${m(type)} · ${course?`${esc(course.name)} · `:''}${date(job.created_at)}</p>${job.error?`<p class="job-error">${m(job.error)}</p>`:''}</div>${badge(job.status)}<div class="row-actions">${action}</div></div>`;
}
async function renderContents(_id, read = api) {
  if (!state.course) return courseRequired();
  const course = state.course;
  const view = state.library;
  view.course = course;
  const load = async (request = api) => {
    const [rows,jobs] = await Promise.all([request(courseUrl('contents',course)),request(courseUrl('jobs',course))]);
    return {rows,jobs};
  };
  let data = await load(read);
  return {
    toolbar:button(icon('plus')+m('创作材料'),'generate'),
    html:heading('内容与审核')+`<div class="collection-tools library-tools"><div class="segmented" ${a('内容状态筛选','aria-label')}><button data-content-filter="all" aria-pressed="${view.filter==='all'}">${m('全部')}</button><button data-content-filter="draft" aria-pressed="${view.filter==='draft'}"><span id="draft-filter-label"></span></button><button data-content-filter="approved" aria-pressed="${view.filter==='approved'}">${m('已确认')}</button></div><div class="library-filters"><label class="search"><span class="sr-only">${m('搜索学习材料')}</span><input id="content-search" type="search" value="${esc(view.query)}" ${a('搜索材料标题','placeholder')}></label></div></div><div id="library-update-error" class="live-update-error" role="status" hidden></div><div id="generation-status" aria-live="polite"></div><div id="content-list"></div>`,
    bind() {
      const paint = () => {
        const scoped = courseLibraryRows(data.rows,course);
        setText($('#draft-filter-label'),'待审核 {n}',{n:scoped.filter(row=>row.status==='draft').length});
        const query = view.query.trim().toLowerCase();
        const selected = scoped.filter(row=>(view.filter==='all'||row.status===view.filter)&&row.title.toLowerCase().includes(query));
        const generationJobs = query || view.filter === 'approved' ? [] : courseLibraryRows(data.jobs,course).filter(job=>['generate','revise_question'].includes(job.kind)&&job.status!=='succeeded');
        const activeJobs = generationJobs.filter(job=>['queued','running'].includes(job.status));
        const failedJobs = generationJobs.filter(job=>!['queued','running'].includes(job.status)).slice(0,3);
        replaceRows($('#generation-status'), [...activeJobs,...failedJobs].map(job=>jobRow(job,true)).join(''));
        replaceRows($('#content-list'), selected.length?`<div class="collection">${selected.map(row=>materialRow(row)).join('')}</div>`:activeJobs.length?'':empty(scoped.length?'没有匹配的内容':'暂无材料',scoped.length?'调整筛选条件或搜索名称。':'','generate','开始创作'));
      };
      $('#content-search').oninput = () => {view.query=$('#content-search').value;paint();};
      $$('[data-content-filter]').forEach(control=>control.onclick=()=>{view.filter=control.dataset.contentFilter;$$('[data-content-filter]').forEach(item=>item.setAttribute('aria-pressed',String(item===control)));paint();});
      paint();
      return watchResource({load:({signal})=>load(path=>api(path,'GET',undefined,{signal})),initial:data,update:next=>{data=next;paint();},error:cause=>liveError($('#library-update-error'),cause)});
    }
  };
}

function renderMedia(media, review=false, version=0) {
  return media.map(item=>{
    const src=`/api/media/${encodeURIComponent(item.id)}/file`;
    const label={audio:'AI 合成语音',image:'AI 生成配图',video:'AI 生成视频'}[item.kind] || '媒体文件';
    const player=item.kind==='audio'?`<audio controls preload="metadata" src="${src}"></audio>${item.metadata?.transcript?`<p>${esc(item.metadata.transcript)}</p>`:''}`:item.kind==='video'?`<video controls preload="metadata" src="${src}" ${a('教学视频','aria-label')}></video>`:item.kind==='image'?`<img loading="lazy" src="${src}" ${a(review?'待核对的教学辅助配图':'已审核的教学辅助配图','alt')}>`:'';
    return `<div class="media-item"><div class="row-actions"><small>${m(label)}</small>${review?badge(item.status):''}</div>${player}<div class="row-actions"><a class="button secondary" href="/api/media/${encodeURIComponent(item.id)}/download" download>${m('下载')}</a>${review?button(item.status==='approved'?'媒体已确认':'确认媒体内容','approve-media','secondary',`data-id="${esc(item.id)}" data-version="${version}" ${item.status==='approved'?'disabled':''}`):''}</div></div>`;
  }).join('');
}
async function renderContent(id, read = api) {
  const content=await read('/contents/'+encodeURIComponent(id));
  const cid=encodeURIComponent(id);
  return {
    toolbar:button('返回内容库','contents','secondary'),
    html:`${renderReviewBar(content)}<div class="review-layout"><div><article class="document-paper">${renderQuestionComparison(content)}${renderAsset(content.asset,true,content.difficulty_assessment,content.sources,{editableQuestions:true})}<div class="submit-bar"><div class="row-actions">${button('编辑内容','edit-content','secondary')}${button(content.status==='approved'?'文字已确认':'确认文字','approve-content','',content.status==='approved'?'disabled':'')}</div></div><div id="edit-panel"></div></article></div>
      <aside class="review-inspector"><section class="inspector-section"><h2>${m('资料依据')}</h2>${renderContentSources(content.sources,content.asset)}</section>
      ${renderMultimodal(content, renderMedia)}
      ${renderDifficultyAssessment(content)}
      ${renderRatingPanel(content)}</aside></div>`,
    bind(){
      state.content=content; selectCourse(content.course_id);
      const stopCitations=mountCitations(page,content.sources,content.asset);
      const stopRating=bindRatingPanel(page,content,{
        save:payload=>api(`/contents/${cid}/evaluations`,'POST',payload),
        onSaved:evaluation=>{content.evaluation=evaluation;notify('当前版本的评价已保存。');}
      });
      return ()=>{stopCitations();stopRating();};
    }
  };
}
function editContent() {
  const content=state.content, revision=state.revision;
  const asset=structuredClone(content.asset);
  const field=(title,path,value)=>`<label>${Array.isArray(title)?m(...title):m(title)}<textarea data-path="${path}" required>${esc(value)}</textarea></label>`;
  $('#edit-panel').innerHTML=`<div class="edit-panel"><h3>${m('编辑版本 {n}',{n:content.version})}</h3><form id="edit-form">${field('标题','title',asset.title)}${asset.sections.map((section,index)=>field(['小节 {n} 标题',{n:index+1}],`sections.${index}.heading`,section.heading)+field(['小节 {n} 内容',{n:index+1}],`sections.${index}.text`,section.text)).join('')}${asset.questions.map((question,index)=>field(['第 {n} 题',{n:index+1}],`questions.${index}.stem`,question.stem)+question.options.map((option,i)=>field(['选项 {letter}',{letter:String.fromCharCode(65+i)}],`questions.${index}.options.${i}`,option)).join('')+field('参考答案',`questions.${index}.answer`,question.answer)+field('答案解析',`questions.${index}.explanation`,question.explanation)).join('')}<label>${m('配图描述')}<textarea data-path="visual_prompt">${esc(asset.visual_prompt)}</textarea></label><div class="modal-actions">${button('取消编辑','cancel-edit','secondary','type="button"')}<button type="submit">${m('保存新版本')}</button></div></form></div>`;
  pageInputs?.refresh();
  $('#edit-panel').scrollIntoView({block:'start',behavior:'instant'});
  $('#edit-form').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy($('button[type=submit]',form),async()=>{$$('[data-path]',form).forEach(input=>{const keys=input.dataset.path.split('.');let target=asset;for(const key of keys.slice(0,-1))target=target[key];target[keys.at(-1)]=input.value;});await api(`/contents/${encodeURIComponent(content.id)}/review`,'POST',{version:content.version,action:'save',asset});if(revision!==state.revision)return;notify('新版本已保存，请重新审核。');await renderRoute();},form);};
}
async function renderProgress(id, read = api) {
  const revision = state.revision;
  const job = await read(`/jobs/${encodeURIComponent(id)}`);
  if (revision === state.revision && job.course_id) selectCourse(job.course_id);
  return {
    html:renderGenerationProgress(job,{mode:generationPreferences.progressMode}),
    bind() {
      const view = mountGenerationProgress($('[data-generation-progress]'),job,{mode:generationPreferences.progressMode});
      const stop = watchGenerationProgress({load:({signal})=>api(`/jobs/${encodeURIComponent(id)}`,'GET',undefined,{signal}),initial:job,update:view.update,error:view.connection});
      return ()=>{stop();view.destroy?.();};
    }
  };
}

async function renderPartial(id, read = api) {
  const partial = await read(`/jobs/${encodeURIComponent(id)}/partial-content`);
  return {
    toolbar:button('返回任务','job-details','secondary',`data-id="${esc(id)}"`),
    html:`<div class="partial-content-heading"><h1>${m('已完成题目')}</h1><p>${m('已完成 {done} / {total} 题',{done:partial.completed,total:partial.total})}<span aria-hidden="true"> · </span>${m('部分结果，尚未完整生成')}</p></div><article class="document-paper partial-content">${renderAsset(partial.asset,true,null,partial.sources,{originalQuestionNumbers:true})}<details class="partial-sources"><summary>${m('资料依据')}</summary>${renderContentSources(partial.sources,partial.asset)}</details></article>`,
    bind() { return mountCitations(page,partial.sources,partial.asset); }
  };
}

async function renderJobs(_id, read = api) {
  const rows=await read('/jobs');
  const render = rows => rows.length?`<div class="collection">${rows.map(job=>jobRow(job)).join('')}</div>`:empty('暂无任务','','documents','前往课程资料','clock-3');
  return {
    toolbar:button('刷新状态','retry','secondary'),
    html:heading('任务记录')+`<div id="jobs-update-error" class="live-update-error" role="status" hidden></div><div id="job-list">${render(rows)}</div>`,
    bind() { return watchResource({load:({signal})=>api('/jobs','GET',undefined,{signal}),initial:rows,update:next=>replaceRows($('#job-list'),render(next)),error:cause=>liveError($('#jobs-update-error'),cause)}); }
  };
}

async function renderLearning(_id, read = api) {
  document.body.classList.add('learning');
  const data=await read(`/learn/${encodeURIComponent(state.learningId)}?reveal_answers=${Boolean(state.reveal)}`);
  return {html:`<article class="document-paper">${renderAsset(data.asset,false)}${!state.reveal&&data.asset.questions.length?`<div class="submit-bar">${button('查看答案与解析','reveal')}</div>`:''}${renderMedia(data.media)}</article>`};
}
const renderers={home:renderHome,documents:renderDocuments,generate:renderGenerate,contents:renderContents,jobs:renderJobs,progress:renderProgress,content:renderContent,partial:renderPartial,learning:renderLearning};

const actions={
  account:()=>{const user=auth.session.user;if(!user)return;dialog('账号',`<div class="account-details"><strong>${esc(user.display_name)}</strong><p>${esc(user.email)}</p></div><div class="modal-actions">${button('退出登录','logout','secondary')}</div>`);},
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
    dialog('导入课程资料',`<form id="upload-form">${renderFilePicker()}<p class="muted">PDF · TXT · Markdown · PNG · JPEG<br>${m('每份最大 10 MB · 300 页')}</p>${parserControls()}<div class="modal-actions">`+button('取消','close-modal','secondary','type="button"')+`<button type="submit">${m('导入并解析')}</button></div></form>`);
    $('#upload-form').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy($('button[type=submit]',form),async()=>{const data=new FormData(form);data.append('course_id',course);const result=await api('/documents','POST',data);modal.close();notify(result.deleted_at?'文件已在最近删除中，请恢复后使用。':result.duplicate?'这份资料已经导入。':result.job_id?'资料已保存，后台正在解析，可在任务页面查看进度。':result.parsing?.status==='good'?'资料已解析，可以查看片段并建立索引。':'资料已保留，部分页面需核对，请打开文字与原图。');go('documents');},form);};
  },
  settings:async()=>{
    if(!window.dispatchEvent(new Event('generationsettingsopen',{cancelable:true})))return;
    const epoch=auth.epoch;
    dialog('设置',renderSettings(generationPreferences.queryFusion,generationPreferences.progressMode,
      generationPreferences.autoExplore,generationPreferences.useSubagents));
    const panel=$('[data-settings-panel]',modal), control=$('#query-fusion',panel);
    control.onchange=()=>{if(epoch===auth.epoch)generationPreferences.setQueryFusion(control.checked);};
    const explorationControl=$('#auto-explore',panel);
    explorationControl.onchange=()=>{if(epoch===auth.epoch)generationPreferences.setAutoExplore(explorationControl.checked);};
    const subagentControl=$('#use-subagents',panel);
    subagentControl.onchange=()=>{if(epoch===auth.epoch)generationPreferences.setUseSubagents(subagentControl.checked);};
    for(const option of $$('input[name="progress-mode"]',panel))option.onchange=()=>{if(epoch===auth.epoch&&option.checked)generationPreferences.setProgressMode(option.value);};
    try{
      const status=await api('/status');
      if(epoch===auth.epoch&&panel.isConnected)$('[data-model-connections]',panel).innerHTML=renderModelConnections(status.capabilities,status.auto_exploration);
    }catch(error){
      if(isSessionChange(error)||epoch!==auth.epoch||!panel.isConnected)return;
      $('[data-model-connections]',panel).innerHTML=`<p class="settings-connection-error" role="status">${m('暂时无法载入模型连接')}</p>`;
    }
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
  'edit-figure':async control=>{const id=control.dataset.id,aid=control.dataset.asset;const figures=await api(`/documents/${encodeURIComponent(id)}/figures`),figure=figures.find(f=>f.asset_id===aid);dialog('编辑图片说明',`<form id="figure-edit"><label>${m('图片说明')}<textarea name="description" required maxlength="2400">${esc(figure?.annotation?.data.description||'')}</textarea></label><button type="submit">${m('保存')}</button></form>`);$('#figure-edit').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy($('button[type=submit]',form),async()=>{await api(`/documents/${encodeURIComponent(id)}/figures/${encodeURIComponent(aid)}`,'PATCH',{description:new FormData(form).get('description')});modal.close();notify('保存后原图片索引失效，请更新图片索引。');go('documents');},form);};},
  'delete-document':async control=>{await api(`/documents/${encodeURIComponent(control.dataset.id)}`,'DELETE');notify('文件已移到最近删除，可随时恢复。');await renderRoute();},
  'restore-document':async control=>{await api(`/documents/${encodeURIComponent(control.dataset.id)}/restore`,'POST');notify('文件已恢复，开启“用于生成”即可继续使用原索引。');await renderRoute();},
  index:async control=>{await api(`/documents/${encodeURIComponent(control.dataset.id)}/index`,'POST');notify('索引任务已创建。');go('jobs');},
  'job-details':async control=>{const revision=state.revision;const job=await api(`/jobs/${encodeURIComponent(control.dataset.id)}`);if(revision!==state.revision)return;if(job.course_id)selectCourse(job.course_id);if(['generate','revise_question','media'].includes(job.kind))return go('progress',job.id);if(job.status==='succeeded'&&(job.content_id||job.result?.content_id))return go('content',job.content_id||job.result.content_id);dialog('任务详情',`${badge(job.status)}${job.progress?`<p>${m('已完成 {done} / {total} 题',{done:job.progress.completed,total:job.progress.total})}</p>`:''}<div class="inline-note">${m(job.error||job.result?.message||(job.status==='succeeded'?'处理完成，可以返回资料库查看索引状态。':'任务正在处理，请稍后刷新状态。'))}</div>${job.status==='failed'&&job.progress?.resumable?`<div class="modal-actions">${button('继续生成','resume-generation','secondary',`data-id="${esc(job.id)}"`)}</div>`:''}`);},
  'restore-generation':async control=>{
    const revision=state.revision;
    const saved=await api(`/jobs/${encodeURIComponent(control.dataset.id)}/generation-request`);
    if(revision!==state.revision)return;
    const {query_fusion,auto_explore,...draft}=restoreGenerationDraft(saved.request);
    generationPreferences.setQueryFusion(Boolean(query_fusion));
    generationPreferences.setAutoExplore(Boolean(auto_explore));
    generationPreferences.setUseSubagents(saved.request.use_subagents === true);
    state.drafts.set(saved.request.course_id,draft);
    selectCourse(saved.request.course_id);
    modal.close();
    notify('已恢复上次的生成条件，可以修改后再次生成。');
    go('generate');
  },
  'resume-generation':async control=>{const revision=state.revision;await api(`/jobs/${encodeURIComponent(control.dataset.id)}/resume`,'POST');if(revision!==state.revision)return;modal.close();notify('已继续生成，保留已完成的题目。中断的请求可能已计费。');go('progress',control.dataset.id);},
  'open-partial':control=>go('partial',control.dataset.id),
  'cancel-generation':async control=>{
    const revision=state.revision;
    await api(`/jobs/${encodeURIComponent(control.dataset.id)}/cancel`,'POST',{});
    if(revision!==state.revision)return;
    await renderRoute();
  },
  'revise-question':control=>{
    const content=state.content, revision=state.revision;
    if(!content)return;
    const slotId=control.dataset.slotId;
    dialog('修改此题',renderQuestionRevisionDialog(content,slotId));
    const form=$('#question-revision-form',modal), close=$('.close-button',modal);
    let pending=false;
    const preventClose=event=>{if(pending)event.preventDefault();};
    modal.addEventListener('cancel',preventClose);
    const dispose=bindQuestionRevision(form,{
      version:content.version,
      isActive:()=>revision===state.revision && modal.open,
      submit:payload=>api(`/contents/${encodeURIComponent(content.id)}/questions/${encodeURIComponent(slotId)}/revise`,'POST',payload),
      pendingChanged:value=>{pending=value;if(close?.isConnected)close.disabled=value;},
      onCreated:job=>{modal.close();go('progress',job.job_id);}
    });
    modal.addEventListener('close',()=>{dispose();modal.removeEventListener('cancel',preventClose);},{once:true});
    $('[name="instruction"]',form).focus();
  },
  'approve-content':async()=>{const content=state.content,revision=state.revision;await api(`/contents/${encodeURIComponent(content.id)}/review`,'POST',{version:content.version,action:'approve'});if(revision!==state.revision)return;notify('文字已确认，可以打开学习页或制作媒体。');await renderRoute();},
  'export-content':()=>{
    const content=state.content;
    if(!content)return;
    dialog('导出内容',renderExportDialog(content));
    const form=$('#content-export-form',modal), controller=new AbortController();
    modal.addEventListener('close',()=>controller.abort(),{once:true});
    form.onsubmit=event=>{
      event.preventDefault();
      if(!form.reportValidity())return;
      busy($('button[type=submit]',form),async()=>{
        const options=readExportOptions(form);
        const params=new URLSearchParams({...options,version:content.version});
        const downloaded=await downloadFile(api,`/contents/${encodeURIComponent(content.id)}/export?${params}`,{signal:controller.signal});
        if(downloaded&&form.isConnected)modal.close();
      },form);
    };
  },
  'edit-content':editContent,
  'cancel-edit':()=>{$('#edit-panel').innerHTML='';},
  'make-media':async control=>{const revision=state.revision,content=state.content;const job=await api(`/contents/${encodeURIComponent(content.id)}/media`,'POST',{version:content.version,kind:control.dataset.kind,request_key:crypto.randomUUID()});if(revision===state.revision)go('progress',job.job_id);},
  'approve-media':async control=>{const revision=state.revision;await api(`/media/${encodeURIComponent(control.dataset.id)}/approve`,'POST',{version:Number(control.dataset.version)});if(revision!==state.revision)return;notify('媒体内容已确认。');await renderRoute();},
  reveal:async()=>{state.reveal=true;await renderRoute();}
};

document.addEventListener('click',event=>{const control=event.target.closest('[data-action]');if(!control||!workspaceReady)return;const action=actions[control.dataset.action];if(action){event.preventDefault();busy(control,()=>action(control));}});
const inputMode = mode => { if (document.body.dataset.input !== mode) document.body.dataset.input = mode; };
document.addEventListener('pointerdown',()=>inputMode('pointer'),{capture:true,passive:true});
document.addEventListener('keydown',()=>inputMode('keyboard'),{capture:true});
$('.skip').onclick=event=>{event.preventDefault();$('#main').focus();$('#main').scrollIntoView({block:'start',behavior:'instant'});};
$('#notice .notice-close').onclick=()=>$('#notice').hidden=true;
$('#course-select').onchange=async()=>{if(!workspaceReady)return;selectCourse($('#course-select').value);state.content=null;if(state.route.page==='content')go('contents');else if(['progress','partial'].includes(state.route.page))go('jobs');else await renderRoute();};
window.addEventListener('hashchange',()=>{if(captureRestoreLink()){restoreAttemptUser=null;checkSession({suspend:true});return;}renderRoute();});
window.addEventListener('pagehide',()=>auth.suspend('checking'));
window.addEventListener('pageshow',event=>{if(event.persisted)checkSession({suspend:true});});
window.addEventListener('storage',event=>{if(event.key==='zhixu.session-change')checkSession({suspend:true});else generationPreferences.syncStorage(event.key);});
window.addEventListener('focus',()=>checkSession());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkSession();});
$$('[data-theme-toggle]').forEach(control => control.onclick = () => { window.uiPreferences.toggleTheme(); refreshPreferences(); });
$$('[data-language-toggle]').forEach(control => control.onclick = () => { window.uiPreferences.toggleLanguage(); refreshPreferences(); });
window.addEventListener('appearancechange', refreshPreferences);
installControlLanguage();
refreshPreferences();
checkSession();
