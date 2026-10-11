import { useCallback, useEffect, useState } from 'react';
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

/** Загрузка списка с обновлением и показом ошибки. */
export function useData(path) {
  const [state, setState] = useState({ data: null, error: '', loading: true });
  const load = useCallback(async () => {
    try { setState({ data: await api(path), error: '', loading: false }); }
    catch (e) { setState((s) => ({ ...s, error: explain(e), loading: false })); }
  }, [path]);
  useEffect(() => { load(); }, [load]);
  return { ...state, reload: load };
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
