import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronRight, Info } from 'lucide-react';
import { api } from './api.js';

const MESSAGES = {
  forbidden: 'Недостаточно прав', not_found: 'Не найдено', invalid: 'Проверьте введённые данные',
  invalid_timezone: 'Неизвестный часовой пояс', send_time_required: 'Укажите время отправки',
  day_of_month_required: 'Укажите число месяца', interval_required: 'Укажите интервал (минут, от 5)',
  invalid_days: 'Неверные дни недели', owner_not_set: 'Владелец бота ещё не назначен (/claim в боте)',
  unsupported_image: 'Нужна картинка PNG, JPEG или WebP', too_large: 'Файл слишком большой',
  nothing_to_change: 'Нечего менять', no_result: 'Не найдено',
};
export const explain = (error) => MESSAGES[error.message] || (error.status === 401 ? 'Неверный ключ' : error.message || 'Ошибка');

/** Загрузка списка с обновлением и показом ошибки. Ответ на устаревший запрос (например, при быстром вводе
 * в поиске) отбрасывается: на экране всегда результат последнего запроса. */
export function useData(path) {
  const [state, setState] = useState({ data: null, error: '', loading: true });
  const latest = useRef(0);
  const load = useCallback(async () => {
    const id = ++latest.current;
    try { const data = await api(path); if (id === latest.current) setState({ data, error: '', loading: false }); }
    catch (e) { if (id === latest.current) setState((s) => ({ ...s, error: explain(e), loading: false })); }
  }, [path]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => () => { latest.current += 1; }, []);
  return { ...state, reload: load };
}

/** Значение, которое обновляется только после паузы в вводе (для поиска). */
export function useDebounced(value, delay = 300) {
  const [current, setCurrent] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setCurrent(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return current;
}

/** Выполнить действие, показать результат и обновить данные. */
export function useAction(after) {
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const run = async (fn, ok = 'Готово') => {
    setBusy(true); setMessage('');
    try { await fn(); setMessage(ok); await after?.(); return true; }
    catch (e) { setMessage('⚠ ' + explain(e)); return false; }
    finally { setBusy(false); }
  };
  return { run, message, busy };
}

export const Status = ({ error, loading }) => (error ? <p className="error">{error}</p> : loading ? <p className="muted">Загрузка…</p> : null);
export const Badge = ({ on, yes = 'вкл', no = 'выкл' }) => <span className={`badge ${on ? 'ok' : 'off'}`}>{on ? yes : no}</span>;
export const when = (value) => (value ? new Date(value).toLocaleString('ru-RU') : '—');

export const PLATFORMS = [['telegram', 'Telegram'], ['max', 'MAX'], ['vk', 'ВКонтакте']];
export const CHAT_TYPES = [['private', 'личные'], ['group', 'группы'], ['channel', 'каналы']];

const toggleIn = (list, value) => (list.includes(value) ? list.filter((x) => x !== value) : [...list, value]);

const PLATFORM_NAMES = Object.fromEntries(PLATFORMS);
const CHAT_TYPE_NAMES = Object.fromEntries(CHAT_TYPES);

/** Выбор аудитории: пусто = все. */
export function AudienceEditor({ value, onChange }) {
  const audience = { platforms: [], chat_types: [], ...(value || {}) };
  return (
    <fieldset className="field audience">
      <legend className="field-label">Кому <span className="muted">(ничего не отмечено — всем)</span></legend>
      <div className="chip-group">
        {PLATFORMS.map(([id, label]) => (
          <label key={id} className={`chip${audience.platforms.includes(id) ? ' chip-on' : ''}`}><input type="checkbox" checked={audience.platforms.includes(id)}
            onChange={() => onChange({ ...audience, platforms: toggleIn(audience.platforms, id) })} /> <span>{label}</span></label>
        ))}
        <span className="chip-sep" aria-hidden="true" />
        {CHAT_TYPES.map(([id, label]) => (
          <label key={id} className={`chip${audience.chat_types.includes(id) ? ' chip-on' : ''}`}><input type="checkbox" checked={audience.chat_types.includes(id)}
            onChange={() => onChange({ ...audience, chat_types: toggleIn(audience.chat_types, id) })} /> <span>{label}</span></label>
        ))}
      </div>
    </fieldset>
  );
}

export const describeAudience = (a) => {
  const platforms = (a?.platforms || []).map((p) => PLATFORM_NAMES[p] || p).join(', ') || 'все платформы';
  const types = (a?.chat_types || []).map((t) => CHAT_TYPE_NAMES[t] || t).join(', ') || 'все чаты';
  return `${platforms} · ${types}`;
};

/** Раскрывающаяся справка: что это и как настроить. */
export function Help({ title = 'Как это настроить', children, open = false }) {
  return (
    <details className="help" open={open}>
      <summary><Info size={16} aria-hidden="true" /><span>{title}</span><ChevronRight className="help-chevron" size={16} aria-hidden="true" /></summary>
      <div className="helpbody">{children}</div>
    </details>
  );
}
