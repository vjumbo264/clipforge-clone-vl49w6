function isInteger(value) {
  return typeof value === 'number' && Number.isFinite(value) && Math.floor(value) === value;
}

export function validateProductionPlan(document) {
  const errors = [];
  if (!document || typeof document !== 'object' || Array.isArray(document)) return ['Top level must be a JSON object.'];
  if (document.title !== undefined && (typeof document.title !== 'string' || !document.title.trim())) {
    errors.push('`title` must be a non-empty string when present.');
  }
  for (const field of ['video_duration_seconds', 'target_total_duration_seconds']) {
    if (!isInteger(document[field]) || document[field] <= 0) errors.push(`\`${field}\` must be a positive integer.`);
  }
  validateStringArray(document.hashtags, 'hashtags', { min: 5, max: 8, prefix: '#', noWhitespace: true }, errors);
  validateStringArray(document.youtube_tags, 'youtube_tags', { min: 10, max: 20, forbiddenPrefix: '#', forbiddenSubstring: ',' }, errors);

  let seriesStart = null;
  let seriesEnd = null;
  if (document.series_id !== undefined) {
    if (typeof document.series_id !== 'string' || !document.series_id.trim()) errors.push('`series_id` must be a non-empty string for a series production plan.');
    if (!isInteger(document.series_part) || document.series_part <= 0) errors.push('`series_part` must be a positive integer for a series production plan.');
    if (!isInteger(document.series_start_seconds) || document.series_start_seconds < 0) errors.push('`series_start_seconds` must be a non-negative integer for a series production plan.'); else seriesStart = document.series_start_seconds;
    if (!isInteger(document.series_end_seconds) || document.series_end_seconds < 0) errors.push('`series_end_seconds` must be a non-negative integer for a series production plan.'); else seriesEnd = document.series_end_seconds;
    if (seriesStart !== null && seriesEnd !== null && seriesEnd <= seriesStart) errors.push('`series_end_seconds` must be greater than `series_start_seconds`.');
    if (typeof document.series_final !== 'boolean') errors.push('`series_final` must be boolean for a series production plan.');
    if (typeof document.series_summary !== 'string' || !document.series_summary.trim()) errors.push('`series_summary` must be a non-empty string for a series production plan.');
  }
  if (!Array.isArray(document.cuts)) return errors.concat('`cuts` must be an array.');
  if (!document.cuts.length) return errors.concat('`cuts` is empty — at least one cut is required.');

  let previousEnd = null;
  for (let index = 0; index < document.cuts.length; index += 1) {
    const cut = document.cuts[index];
    const at = `cuts[${index}]`;
    if (!cut || typeof cut !== 'object' || Array.isArray(cut)) { errors.push(`${at} must be an object.`); continue; }
    if (!isInteger(cut.start_seconds)) errors.push(`${at}.start_seconds must be an integer.`);
    if (!isInteger(cut.end_seconds)) errors.push(`${at}.end_seconds must be an integer.`);
    const narration = typeof cut.voiceover_text === 'string' && cut.voiceover_text.trim() ? cut.voiceover_text : cut.raw_narration;
    if (typeof narration !== 'string' || !narration.trim()) errors.push(`${at}.voiceover_text must be a non-empty string (legacy raw_narration accepted).`);
    if (!isInteger(cut.start_seconds) || !isInteger(cut.end_seconds)) continue;
    if (cut.start_seconds < 0) errors.push(`${at}.start_seconds must be at least 0.`);
    if (seriesStart !== null && cut.start_seconds < seriesStart) errors.push(`${at}.start_seconds precedes series_start_seconds.`);
    if (seriesEnd !== null && cut.end_seconds > seriesEnd) errors.push(`${at}.end_seconds exceeds series_end_seconds.`);
    if (cut.end_seconds <= cut.start_seconds) errors.push(`${at}.end_seconds must be greater than start_seconds.`);
    if (isInteger(document.video_duration_seconds) && cut.end_seconds > document.video_duration_seconds) {
      errors.push(`${at}.end_seconds exceeds video_duration_seconds.`);
    }
    if (previousEnd !== null && cut.start_seconds < previousEnd) errors.push(`${at} overlaps or precedes the prior cut.`);
    previousEnd = cut.end_seconds;
  }
  return errors;
}

function validateStringArray(value, name, options, errors) {
  if (value === undefined) return;
  if (!Array.isArray(value)) { errors.push(`\`${name}\` must be an array of strings when present.`); return; }
  if (value.length < options.min || value.length > options.max) errors.push(`\`${name}\` must contain between ${options.min} and ${options.max} entries.`);
  const seen = new Set();
  value.forEach((entry, index) => {
    const at = `${name}[${index}]`;
    if (typeof entry !== 'string' || !entry.trim()) { errors.push(`${at} must be a non-empty string.`); return; }
    const cleaned = entry.trim();
    if (options.prefix && !cleaned.startsWith(options.prefix)) errors.push(`${at} must start with ${options.prefix}.`);
    if (options.noWhitespace && /\s/.test(cleaned)) errors.push(`${at} must not contain whitespace.`);
    if (options.forbiddenPrefix && cleaned.startsWith(options.forbiddenPrefix)) errors.push(`${at} must not start with ${options.forbiddenPrefix}.`);
    if (options.forbiddenSubstring && cleaned.includes(options.forbiddenSubstring)) errors.push(`${at} must not contain ${options.forbiddenSubstring}.`);
    const comparable = cleaned.toLowerCase();
    if (seen.has(comparable)) errors.push(`${at} duplicates an earlier entry.`);
    seen.add(comparable);
  });
}
