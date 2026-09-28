// Persist editable fields only; provider preferences, evidence and API payloads
// are not drafts. Restoring a form must not change its hidden source bindings.
const levels = ['easy','medium','hard'];
export function generationDraftData(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const result = {};
  for (const [key,choices] of Object.entries({material:['lesson','quiz','assignment'],language:['zh','en'],
    question_type:['mixed','mcq','short_answer'],difficulty:levels,lesson_difficulty:levels,
    difficulty_mode:['uniform','distribution']})) {
    if (choices.includes(value[key])) result[key] = value[key];
  }
  for (const key of ['topic','learner_profile']) {
    if (typeof value[key] === 'string') result[key] = value[key];
  }
  for (const key of ['learner_profile_is_default','include_explanations','check_missing_details']) {
    if (typeof value[key] === 'boolean') result[key] = value[key];
  }
  if (Number.isFinite(value.count)) result.count = value.count;
  if (value.difficulty_counts && typeof value.difficulty_counts === 'object') {
    result.difficulty_counts = Object.fromEntries(levels.map(level => {
      const count = value.difficulty_counts[level];
      return [level, count === '' ? '' : Number.isFinite(Number(count)) && ['number','string'].includes(typeof count) ? Number(count) : 0];
    }));
  }
  // Let the current interface language supply the default learner description.
  if (result.learner_profile_is_default) delete result.learner_profile;
  return result;
}

export function editableAssetValues(asset) {
  const fields = {title:String(asset.title ?? '')};
  (asset.sections || []).forEach((section,index) => {
    fields[`sections.${index}.heading`] = String(section.heading ?? '');
    fields[`sections.${index}.text`] = String(section.text ?? '');
  });
  (asset.questions || []).forEach((question,index) => {
    for (const key of ['stem','answer','explanation']) fields[`questions.${index}.${key}`] = String(question[key] ?? '');
    (question.options || []).forEach((option,i) => { fields[`questions.${index}.options.${i}`] = String(option ?? ''); });
  });
  fields.visual_prompt = String(asset.visual_prompt ?? '');
  return fields;
}

export function restoreEditValues(asset, saved) {
  const fields = editableAssetValues(asset);
  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return fields;
  for (const key of Object.keys(fields)) {
    if (Object.hasOwn(saved,key) && typeof saved[key] === 'string') fields[key] = saved[key];
  }
  return fields;
}

export function applyEditValues(asset, values) {
  const result = structuredClone(asset);
  for (const [path,value] of Object.entries(restoreEditValues(asset,values))) {
    const keys = path.split('.');
    let target = result;
    for (const key of keys.slice(0,-1)) target = target[key];
    target[keys.at(-1)] = value;
  }
  return result;
}

export function clearSubmittedEditDraft(store, scope, submitted) {
  const current = store.load(scope)?.data;
  if (!current || Object.keys(current).length !== Object.keys(submitted).length ||
    Object.keys(submitted).some(key => current[key] !== submitted[key])) return false;
  store.remove(scope);
  return true;
}
