import { useEffect, useRef, useState } from 'react';
import { BookOpen, LogOut, Menu, Monitor, Moon, Sun, X } from 'lucide-react';
import { GROUPS } from './sections.js';
import { api } from './api.js';
import { useTheme } from './hooks.js';

const THEME_LABELS = { system: ['Тема: как в системе', Monitor], light: ['Тема: светлая', Sun], dark: ['Тема: тёмная', Moon] };

function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark" aria-hidden="true"><BookOpen size={18} strokeWidth={2} /></span>
      <span className="brand-text"><strong>Библия</strong><small>панель администратора</small></span>
    </div>
  );
}

/** Число доставок, требующих решения, для метки в меню. Обновляется раз в минуту; ошибки не мешают работе. */
function useAttention() {
  const [count, setCount] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () => api('/overview').then((data) => { if (alive) setCount(data.queue.attention || 0); }).catch(() => {});
    load();
    const timer = setInterval(load, 60000);
    return () => { alive = false; clearInterval(timer); };
  }, []);
  return count;
}

/** Каркас панели: боковое меню с группами (на телефоне — выдвижное), шапка и область раздела. */
export default function Shell({ current, onLogout, children }) {
  const [open, setOpen] = useState(false);
  const [theme, nextTheme] = useTheme();
  const menuButton = useRef(null);
  const sidebar = useRef(null);
  const [themeLabel, ThemeIcon] = THEME_LABELS[theme];
  const attention = useAttention();
  const badges = { deliveries: attention };

  useEffect(() => { setOpen(false); window.scrollTo(0, 0); }, [current.id]);
  useEffect(() => {
    if (!open) return undefined;
    sidebar.current?.querySelector('[aria-current="page"]')?.focus();
    const onKey = (event) => { if (event.key === 'Escape') { setOpen(false); menuButton.current?.focus(); } };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open]);

  return (
    <div className="app" data-menu-open={open || undefined}>
      <header className="topbar">
        <button ref={menuButton} type="button" className="icon-button" aria-label="Открыть меню" aria-expanded={open}
          aria-controls="sidebar" onClick={() => setOpen(true)}><Menu size={20} /></button>
        <span className="topbar-title">{current.label}</span>
      </header>
      <div className="backdrop" hidden={!open} onClick={() => setOpen(false)} />
      <aside id="sidebar" ref={sidebar} className="sidebar">
        <div className="sidebar-head">
          <Brand />
          <button type="button" className="icon-button sidebar-close" aria-label="Закрыть меню" onClick={() => setOpen(false)}><X size={20} /></button>
        </div>
        <nav aria-label="Разделы">
          {GROUPS.map((group) => (
            <div className="nav-group" key={group.label}>
              <div className="nav-group-label">{group.label}</div>
              {group.items.map(({ id, label, icon: Icon }) => (
                <a key={id} href={`#/${id}`} className="nav-item" aria-current={id === current.id ? 'page' : undefined}
                  onClick={() => setOpen(false)}>
                  <Icon size={17} strokeWidth={1.9} aria-hidden="true" /><span>{label}</span>
                  {badges[id] > 0 && <span className="nav-count" title="Требуют решения">{badges[id]}<span className="sr-only"> требуют решения</span></span>}
                </a>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <button type="button" className="nav-item" onClick={nextTheme} title="Переключить тему">
            <ThemeIcon size={17} strokeWidth={1.9} aria-hidden="true" /><span>{themeLabel}</span>
          </button>
          <button type="button" className="nav-item" onClick={onLogout}>
            <LogOut size={17} strokeWidth={1.9} aria-hidden="true" /><span>Выйти</span>
          </button>
        </div>
      </aside>
      <main className="content" id="content">{children}</main>
    </div>
  );
}
