// Restore editable inputs only. A retry must receive a new request identity and
// a fresh preparation flow; saved execution/audit fields never become form data.
const levels = ['easy','medium','hard'];
const oneOf = (value, allowed, fallback) => allowed.includes(value) ? value : fallback;
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const countValue = value => Number.isInteger(value) && value >= 1 && value <= 50;
const allocatedCount = value => Number.isInteger(value) && value >= 0 && value <= 50 ? value : '';

export function restoreGenerationDraft(request) {
  const source = record(request) ? request : {};
  const material = oneOf(source.material,['lesson','quiz','assignment'],'lesson');
  const difficulty = oneOf(source.difficulty,levels,'medium');
  const count = countValue(source.count) ? source.count : 3;
  const distribution = material !== 'lesson' && record(source.difficulty_distribution)
    ? source.difficulty_distribution : null;
  const profile = typeof source.learner_profile === 'string' && source.learner_profile.trim()
    ? source.learner_profile : null;
  return {
    material,
    topic:typeof source.topic === 'string' ? source.topic : '',
    difficulty,
    language:oneOf(source.language,['zh','en'],'zh'),
    question_type:oneOf(source.question_type,['mcq','short_answer','mixed'],'mixed'),
    count,
    difficulty_mode:distribution ? 'distribution' : 'uniform',
    difficulty_counts:Object.fromEntries(levels.map(level=>[
      level,distribution ? allocatedCount(distribution[level]) : level === difficulty ? count : 0,
    ])),
    lesson_difficulty:difficulty,
    learner_profile_is_default:profile === null,
    ...(profile !== null ? {learner_profile:profile} : {}),
    document_ids:Array.isArray(source.document_ids)
      ? [...new Set(source.document_ids.filter(value=>typeof value === 'string' && value.trim()))] : [],
    query_fusion:source.query_fusion === true,
    auto_explore:source.auto_explore === true,
    include_explanations:source.include_explanations === true,
  };
}
