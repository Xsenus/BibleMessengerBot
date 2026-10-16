import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronRight, Info } from 'lucide-react';
import { api } from './api.js';
import { toast } from './feedback.jsx';

const MESSAGES = {
  forbidden: 'Недостаточно прав', not_found: 'Не найдено', invalid: 'Проверьте введённые данные',
  invalid_timezone: 'Неизвестный часовой пояс', send_time_required: 'Укажите время отправки',
  day_of_month_required: 'Укажите число месяца', interval_required: 'Укажите интервал (минут, от 5)',
  invalid_days: 'Неверные дни недели', owner_not_set: 'Владелец бота ещё не назначен (/claim в боте)',
  unsupported_image: 'Нужна картинка PNG, JPEG или WebP', too_large: 'Файл слишком большой',
  nothing_to_change: 'Нечего менять', no_result: 'Не найдено',
  name_taken: 'Прокси с таким названием уже есть — выберите другое',
  slug_taken: 'Генератор с таким коротким именем уже есть — выберите другое',
  secrets_key_missing: 'На сервере не задан SECRETS_KEY: ключи и пароли нельзя сохранить',
  secret_unreadable: 'Сохранённый ключ не расшифровывается (сменился SECRETS_KEY?) — введите его заново',
  variant_unavailable: 'Новый вариант этой картинки заказать нельзя: вариантов уже максимум или нет исходного задания',
  invalid_asset: 'Картинка не найдена — загрузите её заново',
  invalid_service: 'Неизвестная служба', invalid_proxy: 'Прокси не найден — обновите страницу',
  duplicate_route: 'Один прокси указан в маршрутах дважды',
  unsupported_telegram_proxy_scheme: 'Для Telegram подходит только прокси HTTP или SOCKS5, не HTTPS',
  invalid_proxy_url: 'Неверный адрес прокси', invalid_proxy_port: 'Неверный порт прокси',
  invalid_adapter: 'Неизвестный тип генератора', invalid_slug: 'Короткое имя: латиница, цифры и дефис',
  invalid_base_url: 'Укажите адрес API', 'invalid_Unsafe service address': 'Адрес API должен быть HTTPS и не во внутренней сети',
  invalid_model: 'Проверьте название модели', invalid_account_id: 'Account ID — 32 символа 0–9 и a–f',
  invalid_billing_mode: 'Неизвестный режим оплаты', invalid_pricing: 'Для этого режима оплаты укажите стоимость запроса',
  invalid_image_settings: 'Настройки генераторов на сервере повреждены — проверьте журнал службы',
  'Database unavailable': 'База данных недоступна — попробуйте позже',
};

/** Понятный текст ошибки API. Импорт объявлений сообщает номер строки: «item_3:invalid_days». */
export function explain(error) {
  const text = error.message;
  const item = /^item_(\d+):(.+)$/.exec(text || '');
  if (item) return `Объявление № ${item[1]}: ${MESSAGES[item[2]] || item[2]}`;
  return MESSAGES[text] || (error.status === 401 ? 'Неверный ключ' : text || 'Ошибка');
}

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

/** Выполнить действие и обновить данные. Успех — всплывающее уведомление; ошибка — в message, чтобы показать её
 * рядом с формой, где её видно и где её исправлять. */
export function useAction(after) {
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const run = async (fn, ok = 'Сохранено') => {
    setBusy(true); setMessage('');
    try { await fn(); toast(ok); await after?.(); return true; }
    catch (e) { if (e.status !== 401) setMessage('⚠ ' + explain(e)); return false; }
    finally { setBusy(false); }
  };
  return { run, message, busy };
}

export const Status = ({ error, loading }) => (error ? <p className="callout callout-danger" role="alert">{error}</p>
  : loading ? <p className="loading" aria-live="polite"><span className="spinner" aria-hidden="true" />Загрузка…</p> : null);
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
