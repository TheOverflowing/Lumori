// Preferences belong to an account; shared browser storage never supplies a global fallback.
export const generationPreferencesKey = userId => userId ? `zhixu.account.${encodeURIComponent(userId)}.generation` : null;

export function createGenerationPreferences({ storage, onChange = () => {} } = {}) {
  let userId = null, queryFusion = false, autoExplore = false, useSubagents = false, progressMode = 'detailed';
  const read = () => {
    let value;
    try { value = userId ? JSON.parse(storage?.getItem(generationPreferencesKey(userId)) || '{}') : {}; }
    catch { value = {}; }
    return {queryFusion:value?.query_fusion === true, autoExplore:value?.auto_explore === true,
      useSubagents:value?.use_subagents === true, progressMode:value?.progress_mode === 'simple' ? 'simple' : 'detailed'};
  };
  const publish = changed => onChange({ userId, queryFusion, autoExplore, useSubagents, progressMode, changed });
  const persist = () => {
    try { storage?.setItem(generationPreferencesKey(userId), JSON.stringify({query_fusion:queryFusion,
      auto_explore:autoExplore, use_subagents:useSubagents, progress_mode:progressMode})); }
    catch { /* The current account keeps its preferences until the next sign-in. */ }
  };
  return {
    get queryFusion() { return queryFusion; },
    get autoExplore() { return autoExplore; },
    get useSubagents() { return useSubagents; },
    get progressMode() { return progressMode; },
    setAccount(id) {
      if (userId === (id || null)) return;
      userId = id || null;
      ({queryFusion, autoExplore, useSubagents, progressMode} = read());
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
    setUseSubagents(enabled) {
      if (!userId || useSubagents === (enabled === true)) return;
      useSubagents = enabled === true;
      persist();
      publish(['useSubagents']);
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
      if (next.useSubagents !== useSubagents) changed.push('useSubagents');
      if (next.progressMode !== progressMode) changed.push('progressMode');
      if (!changed.length) return;
      ({queryFusion, autoExplore, useSubagents, progressMode} = next);
      publish(changed);
    },
    requestFields(material) { return userId ? {...(queryFusion ? {query_fusion:true} : {}),
      ...(autoExplore ? {auto_explore:true} : {}),
      ...(['quiz','assignment'].includes(material) ? {use_subagents:useSubagents} : {})} : {}; },
  };
}
