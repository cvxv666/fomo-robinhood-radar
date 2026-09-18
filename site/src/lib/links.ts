/** Every link to fomo.family, with the radar's referral on it - the same rule as fomo_agent/links.py. */
const FOMO = 'https://fomo.family';
const code = (import.meta.env.PUBLIC_FOMO_REF_CODE ?? '').trim();
const param = (import.meta.env.PUBLIC_FOMO_REF_PARAM ?? 'ref').trim();
const home = (import.meta.env.PUBLIC_FOMO_REF_URL ?? '').trim();

function ref(url: string): string {
  if (!code) return url;
  return `${url}${url.includes('?') ? '&' : '?'}${encodeURIComponent(param)}=${encodeURIComponent(code)}`;
}

export const fomoHome = () => home || ref(`${FOMO}/`);
export const fomoToken = (mint: string, chain = 'robinhood') => ref(`${FOMO}/tokens/${chain}/${mint}`);
export const fomoProfile = (handle: string) => ref(`${FOMO}/profile/${encodeURIComponent(handle)}`);
