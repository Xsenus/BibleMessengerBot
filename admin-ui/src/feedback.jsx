// Обратная связь: всплывающие уведомления и окна подтверждения/ввода вместо confirm()/prompt() браузера.
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { CircleAlert, CircleCheck, TriangleAlert, X } from 'lucide-react';

/** Маленькое хранилище с подпиской: состояние живёт вне React-дерева, чтобы звать toast() и ask() откуда угодно. */
function createStore(initial) {
  let state = initial;
  const listeners = new Set();
  return {
    get: () => state,
    set: (next) => { state = typeof next === 'function' ? next(state) : next; listeners.forEach((l) => l()); },
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
  };
}

// ---------- Уведомления ----------
const toasts = createStore([]);
let toastId = 0;

/** Показать уведомление. tone: 'ok' | 'error'. Ошибки висят дольше. */
export function toast(text, tone = 'ok') {
  const id = ++toastId;
  toasts.set((list) => [...list.slice(-3), { id, text, tone }]);
  setTimeout(() => dismissToast(id), tone === 'error' ? 8000 : 4000);
}
const dismissToast = (id) => toasts.set((list) => list.filter((t) => t.id !== id));

export function Toaster() {
  const list = useSyncExternalStore(toasts.subscribe, toasts.get);
  return (
    <div className="toaster" role="status" aria-live="polite">
      {list.map((t) => (
        <div key={t.id} className={`toast toast-${t.tone}`}>
          {t.tone === 'error' ? <CircleAlert size={17} aria-hidden="true" /> : <CircleCheck size={17} aria-hidden="true" />}
          <span>{t.text}</span>
          <button type="button" className="icon-button" aria-label="Закрыть уведомление" onClick={() => dismissToast(t.id)}><X size={15} /></button>
        </div>
      ))}
    </div>
  );
}

// ---------- Окна подтверждения и ввода ----------
const dialogs = createStore(null);

/**
 * Спросить подтверждение. Возвращает true/false.
 * options: { title, body, confirmText = 'Подтвердить', danger = false }.
 */
export function ask(options) {
  return new Promise((resolve) => dialogs.set({ ...options, kind: 'confirm', resolve }));
}

/** Запросить строку. Возвращает текст или null при отмене. options: { title, label, confirmText, initial }. */
export function askText(options) {
  return new Promise((resolve) => dialogs.set({ ...options, kind: 'prompt', resolve }));
}

export function DialogHost() {
  const current = useSyncExternalStore(dialogs.subscribe, dialogs.get);
  const ref = useRef(null);
  const [value, setValue] = useState('');
  useEffect(() => {
    const dialog = ref.current;
    if (!current || !dialog) return;
    setValue(current.initial || '');
    if (!dialog.open) dialog.showModal();
  }, [current]);
  if (!current) return null;
  const close = (result) => {
    ref.current?.close();
    dialogs.set(null);
    current.resolve(result);
  };
  const cancelResult = current.kind === 'prompt' ? null : false;
  const submit = (event) => {
    event.preventDefault();
    if (current.kind === 'prompt') { if (value.trim()) close(value.trim()); } else close(true);
  };
  return (
    <dialog ref={ref} className="dialog" aria-labelledby="dialog-title" onCancel={(e) => { e.preventDefault(); close(cancelResult); }}>
      <form method="dialog" onSubmit={submit}>
        <div className="dialog-head">
          {current.danger && <TriangleAlert size={20} className="dialog-icon" aria-hidden="true" />}
          <h3 id="dialog-title">{current.title}</h3>
        </div>
        {current.body && <p className="dialog-body">{current.body}</p>}
        {current.kind === 'prompt' && (
          <label className="field"><span className="field-label">{current.label}</span>
            <input autoFocus value={value} maxLength={120} onChange={(e) => setValue(e.target.value)} /></label>
        )}
        <div className="dialog-actions">
          <button type="button" className="small" data-dialog="cancel" onClick={() => close(cancelResult)}>Отмена</button>
          <button type="submit" className={current.danger ? 'button-danger' : ''} data-dialog="confirm" autoFocus={current.kind !== 'prompt'}
            disabled={current.kind === 'prompt' && !value.trim()}>{current.confirmText || 'Подтвердить'}</button>
        </div>
      </form>
    </dialog>
  );
}
