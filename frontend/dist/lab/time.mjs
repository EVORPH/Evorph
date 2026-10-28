/** A fixed UTC display. No browser locale, device time zone or preference lookup. */
export function formatUTC(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new TypeError('An ISO timestamp with an explicit offset is required.');
  }
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new TypeError('Invalid timestamp.');
  return date.toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
}
