import { useCallback, useEffect, useState } from 'react';

const readHash = () => window.location.hash.replace(/^#\/?/, '').split(/[?/]/)[0];

/** Текущий раздел из адреса вида #/announcements; кнопки браузера «Назад/Вперёд» работают. */
export function useHashRoute() {
  const [id, setId] = useState(readHash);
  useEffect(() => {
    const onChange = () => setId(readHash());
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return id;
}

const THEME_KEY = 'biblebot-admin-theme';
export const THEMES = ['system', 'light', 'dark'];

/** Тема оформления: системная, светлая или тёмная. Выбор хранится только в этом браузере. */
export function useTheme() {
  const [theme, setTheme] = useState(() => {
    try { return THEMES.includes(localStorage.getItem(THEME_KEY)) ? localStorage.getItem(THEME_KEY) : 'system'; }
    catch { return 'system'; }
  });
  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'system') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* нет хранилища */ }
  }, [theme]);
  const next = useCallback(() => setTheme((value) => THEMES[(THEMES.indexOf(value) + 1) % THEMES.length]), []);
  return [theme, next];
}
