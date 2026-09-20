// Preferences belong to an account; shared browser storage never supplies a global fallback.
export const generationPreferencesKey = userId => userId ? `zhixu.account.${encodeURIComponent(userId)}.generation` : null;

export function createGenerationPreferences({ storage, onChange = () => {} } = {}) {
  let userId = null, queryFusion = false, autoExplore = false, progressMode = 'detailed';
  const read = () => {
    let value;
    try { value = userId ? JSON.parse(storage?.getItem(generationPreferencesKey(userId)) || '{}') : {}; }
    catch { value = {}; }
    return {queryFusion:value?.query_fusion === true, autoExplore:value?.auto_explore === true, progressMode:value?.progress_mode === 'simple' ? 'simple' : 'detailed'};
  };
  const publish = changed => onChange({ userId, queryFusion, autoExplore, progressMode, changed });
  const persist = () => {
    try { storage?.setItem(generationPreferencesKey(userId), JSON.stringify({query_fusion:queryFusion, auto_explore:autoExplore, progress_mode:progressMode})); }
    catch { /* The current account keeps its preferences until the next sign-in. */ }
  };
  return {
    get queryFusion() { return queryFusion; },
    get autoExplore() { return autoExplore; },
    get progressMode() { return progressMode; },
    setAccount(id) {
      if (userId === (id || null)) return;
      userId = id || null;
      ({queryFusion, autoExplore, progressMode} = read());
    },
    setQueryFusion(enabled) {
      if (!userId || queryFusion === (enabled === true)) return;
      queryFusion = enabled === true;
      persist();
      publish(['queryFusion']);
    },
    setAutoExplore(enabled) {
      if (!userId || autoExplore === (enabled === true)) return;
      autoExplore = enabled === true;
      persist();
      publish(['autoExplore']);
    },
    setProgressMode(mode) {
      if (!userId || !['simple','detailed'].includes(mode) || progressMode === mode) return;
      progressMode = mode;
      persist();
      publish(['progressMode']);
    },
    syncStorage(key) {
      if (!userId || (key !== null && key !== generationPreferencesKey(userId))) return;
      const next = read();
      const changed = [];
      if (next.queryFusion !== queryFusion) changed.push('queryFusion');
      if (next.autoExplore !== autoExplore) changed.push('autoExplore');
      if (next.progressMode !== progressMode) changed.push('progressMode');
      if (!changed.length) return;
      ({queryFusion, autoExplore, progressMode} = next);
      publish(changed);
    },
    requestFields() { return userId ? {...(queryFusion ? {query_fusion:true} : {}), ...(autoExplore ? {auto_explore:true} : {})} : {}; },
  };
}
