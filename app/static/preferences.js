/* Apply saved appearance before styles paint. Storage may be unavailable. */
(() => {
  const system = matchMedia('(prefers-color-scheme: dark)');
  const read = key => { try { return localStorage.getItem(key); } catch { return null; } };
  const savedTheme = read('zhixu.theme');
  let theme = ['light', 'dark'].includes(savedTheme) ? savedTheme : null;
  let language = read('zhixu.language') === 'en' ? 'en' : 'zh';
  function apply() {
    const resolved = theme || (system.matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = resolved;
    document.documentElement.lang = language === 'en' ? 'en' : 'zh-CN';
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', resolved === 'dark' ? '#171e1b' : '#eef1ee');
  }
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch { /* Keep the session preference. */ } };
  window.uiPreferences = {
    toggleTheme() {
      theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      save('zhixu.theme', theme); apply();
    },
    toggleLanguage() {
      language = language === 'zh' ? 'en' : 'zh';
      save('zhixu.language', language); apply();
    },
  };
  system.addEventListener('change', () => { if (!theme) { apply(); window.dispatchEvent(new Event('appearancechange')); } });
  window.addEventListener('storage', event => {
    if (event.key === 'zhixu.theme') theme = ['light', 'dark'].includes(event.newValue) ? event.newValue : null;
    else if (event.key === 'zhixu.language') language = event.newValue === 'en' ? 'en' : 'zh';
    else return;
    apply(); window.dispatchEvent(new Event('appearancechange'));
  });
  apply();
})();
