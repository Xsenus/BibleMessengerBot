// Базовые компоненты разделов. Стили — в styles.css (блок «Компоненты»).
import { Inbox, Upload } from 'lucide-react';

const number = new Intl.NumberFormat('ru-RU');
export const formatNumber = (value) => (typeof value === 'number' ? number.format(value) : value ?? '—');

const relative = new Intl.RelativeTimeFormat('ru', { numeric: 'auto' });
/** «2 мин назад», «вчера»; полная дата — во всплывающей подсказке. */
export function TimeAgo({ value }) {
  if (!value) return <span className="muted">—</span>;
  const date = new Date(value);
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const abs = Math.abs(seconds);
  const [amount, unit] = abs < 45 ? [0, 'second'] : abs < 3600 ? [Math.round(seconds / 60), 'minute']
    : abs < 86400 ? [Math.round(seconds / 3600), 'hour'] : [Math.round(seconds / 86400), 'day'];
  const text = amount === 0 ? 'только что' : relative.format(amount, unit);
  return <time dateTime={date.toISOString()} title={date.toLocaleString('ru-RU')}>{text}</time>;
}

/** Заголовок раздела: название, пояснение и действия справа. */
export function PageHeader({ title, description, actions }) {
  return (
    <header className="page-header">
      <div className="page-header-text">
        <h2>{title}</h2>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

/** Карточка с необязательным заголовком и действиями. */
export function Panel({ title, actions, children, className = '' }) {
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && <div className="panel-head">{title && <h3>{title}</h3>}{actions}</div>}
      {children}
    </section>
  );
}

/** Показатель: подпись, крупное число, пояснение. tone: 'danger' | 'warning' | 'ok'. */
export function Stat({ label, value, hint, tone, icon: Icon, href }) {
  const body = (
    <>
      <span className="stat-label">{Icon && <Icon size={16} aria-hidden="true" />}{label}</span>
      <span className={`stat-value ${tone ? `tone-${tone}` : ''}`}>{formatNumber(value)}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </>
  );
  return href ? <a className="stat stat-link" href={href}>{body}</a> : <div className="stat">{body}</div>;
}

/** Метка статуса. tone: ok | off | error | warning | pending. */
export const StatusBadge = ({ tone = 'off', children }) => <span className={`badge ${tone}`}>{children}</span>;

export function EmptyState({ icon: Icon = Inbox, title, children }) {
  return (
    <div className="empty-state">
      <Icon size={22} aria-hidden="true" />
      <strong>{title}</strong>
      {children && <span className="muted">{children}</span>}
    </div>
  );
}

/**
 * Таблица: columns — [{ key, header, render?, align?, className? }].
 * Пустой список показывает empty; пока данных нет — ничего. stack — на телефоне строки становятся карточками.
 */
export function DataTable({ columns, rows, rowKey = (row) => row.id, empty, caption, stack = false }) {
  if (!rows) return null;
  if (!rows.length) return empty || null;
  return (
    <div className="table-wrap">
      <table className={`data-table${stack ? ' table-stack' : ''}`}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead><tr>{columns.map((c) => <th key={c.key} className={c.align === 'end' ? 'align-end' : undefined} scope="col">{c.header}</th>)}</tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((c) => (
                <td key={c.key} data-label={stack && c.header && !c.className?.includes('cell-actions') ? c.header : undefined}
                  className={[c.align === 'end' && 'align-end', c.className].filter(Boolean).join(' ') || undefined}>
                  {c.render ? c.render(row) : row[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Поле формы с подписью и подсказкой. Для группы флажков — as="fieldset". */
export function Field({ label, hint, children, as: Tag = 'label', className = '' }) {
  const Caption = Tag === 'fieldset' ? 'legend' : 'span';
  return (
    <Tag className={`field ${className}`}>
      <Caption className="field-label">{label}</Caption>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </Tag>
  );
}

/** Флажок-«чип» для выбора нескольких вариантов. */
export function Chip({ checked, onChange, children, disabled }) {
  return (
    <label className={`chip${checked ? ' chip-on' : ''}`}>
      <input type="checkbox" checked={checked} onChange={onChange} disabled={disabled} />
      <span>{children}</span>
    </label>
  );
}

/** Кнопка выбора файла с русской подписью вместо стандартной «Choose File». */
export function FilePicker({ accept, onChange, disabled, children, icon: Icon = Upload, title }) {
  return (
    <label className={`button-like small${disabled ? ' is-disabled' : ''}`} title={title}>
      <Icon size={15} aria-hidden="true" />{children}
      <input type="file" accept={accept} className="sr-only" disabled={disabled} onChange={onChange} />
    </label>
  );
}

/** Переключатель «включено / выключено». label — что переключается (для чтения с экрана). */
export function Switch({ checked, onChange, label, on = 'вкл', off = 'выкл', disabled }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} className="switch" disabled={disabled} onClick={() => onChange(!checked)}>
      <span className="switch-track" aria-hidden="true"><span className="switch-thumb" /></span>
      <span className="switch-text">{checked ? on : off}</span>
    </button>
  );
}

/** Вкладки: tabs — [[id, подпись]]. Стрелки ←/→ переключают вкладку. */
export function Tabs({ tabs, value, onChange, label }) {
  const onKey = (event) => {
    const index = tabs.findIndex(([id]) => id === value);
    const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    if (!step) return;
    const next = tabs[(index + step + tabs.length) % tabs.length][0];
    onChange(next);
    event.currentTarget.parentElement.querySelector(`[data-tab="${next}"]`)?.focus();
  };
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map(([id, text]) => (
        <button key={id} type="button" role="tab" data-tab={id} aria-selected={id === value} tabIndex={id === value ? 0 : -1}
          className="tab" onClick={() => onChange(id)} onKeyDown={onKey}>{text}</button>
      ))}
    </div>
  );
}
