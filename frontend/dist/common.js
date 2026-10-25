import { socialLinks } from './site-config.js?v=public-records-5';
const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
const query = new URLSearchParams(location.search);
const subscribers = new Set();
let manuallyPaused = false;
let toastTimer;
export const motion = { reduced: preference.matches || query.get('motion') === 'reduce', paused: false };
function updateMotion() {
  motion.reduced = preference.matches || query.get('motion') === 'reduce';
  motion.paused = manuallyPaused || motion.reduced;
  document.documentElement.classList.toggle('motion-enabled', !motion.reduced);
  document.documentElement.classList.toggle('motion-reduced', motion.reduced);
  document.documentElement.classList.toggle('motion-paused', motion.paused);
  const button = document.getElementById('motion-toggle');
  if (button) {
    button.disabled = motion.reduced;
    button.setAttribute('aria-pressed', String(motion.paused));
    button.setAttribute('aria-label', motion.reduced ? 'Motion reduced by preference' : motion.paused ? 'Resume motion' : 'Pause motion');
    button.querySelector('span').textContent = motion.reduced ? 'Motion reduced' : motion.paused ? 'Resume motion' : 'Pause motion';
    button.querySelector('use').setAttribute('href', motion.paused ? '#i-play' : '#i-pause');
  }
  subscribers.forEach(fn => fn(motion));
}
export function onMotionChange(fn) { subscribers.add(fn); fn(motion); return () => subscribers.delete(fn); }
export function announce(message) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3500);
}
document.getElementById('motion-toggle')?.addEventListener('click', () => { manuallyPaused = !manuallyPaused; updateMotion(); });
preference.addEventListener('change', updateMotion);
const menuButton = document.getElementById('menu-toggle');
const menu = document.getElementById('mobile-menu');
function setMenu(open) {
  if (!menu || !menuButton) return;
  menu.hidden = !open;
  menuButton.setAttribute('aria-expanded', String(open));
  menuButton.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
}
menuButton?.addEventListener('click', () => setMenu(menu.hidden));
menu?.querySelectorAll('a').forEach(link => link.addEventListener('click', () => setMenu(false)));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && menu && !menu.hidden) { setMenu(false); menuButton.focus(); }
});
window.matchMedia('(min-width: 641px)').addEventListener('change', event => { if (event.matches) setMenu(false); });
updateMotion();

// All three routes share the same public social-link configuration.
for (const link of document.querySelectorAll('[data-social-link]')) {
  const configured = socialLinks[link.dataset.socialLink];
  const value = typeof configured === 'string' ? configured.trim() : '';
  if (!value) continue;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:') continue;
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.removeAttribute('aria-disabled');
    link.removeAttribute('tabindex');
    link.removeAttribute('title');
  } catch {
    // Invalid or incomplete URLs retain the safe, unlinked placeholder.
  }
}
