import { useState } from 'react';
import { RefreshCw, ScrollText, Search } from 'lucide-react';
import { Status, useData, useDebounced, when } from '../ui.jsx';
import { DataTable, EmptyState, PageHeader, Pager, Panel, StatusBadge } from '../components.jsx';

const SOURCES = { panel: 'веб-панель', bot: 'бот' };
const PERIODS = [[0, 'За всё время'], [1, 'За сутки'], [7, 'За 7 дней'], [30, 'За 30 дней']];
const PAGE = 50;

/** Адрес запроса журнала: фильтры и страница считаются на сервере. */
const auditPath = ({ q = '', source = '', action = '', days = 0, offset = 0 }) =>
  `/audit?${new URLSearchParams({ limit: PAGE, offset, q, source, action, days })}`;

export default function Audit() {
  const [text, setText] = useState('');
  const [source, setSource] = useState('');
  const [action, setAction] = useState('');
  const [days, setDays] = useState(0);
  const [offset, setOffset] = useState(0);
  const q = useDebounced(text.trim());
  const { data, error, loading, reload } = useData(auditPath({ q, source, action, days, offset }));
  const { data: actions } = useData('/audit/actions');
  // Новый фильтр — снова с первой страницы.
  const filter = (set) => (value) => { set(value); setOffset(0); };
  const filtered = Boolean(q || source || action || days);
  const items = data?.items;
  return (
    <section>
      <PageHeader title="Журнал действий" description="Кто и что менял: из веб-панели и из команд бота. Секреты (ключи, пароли, адреса прокси) в журнал не попадают."
        actions={<button className="small" onClick={reload}><RefreshCw size={15} aria-hidden="true" />Обновить</button>} />
      <Panel>
        <div className="toolbar">
          <label className="input-with-icon search-box"><Search size={15} aria-hidden="true" />
            <input value={text} onChange={(e) => filter(setText)(e.target.value)} placeholder="Кто, действие или объект" aria-label="Поиск в журнале" /></label>
          <select value={source} aria-label="Откуда" onChange={(e) => filter(setSource)(e.target.value)}>
            <option value="">Откуда угодно</option>{Object.entries(SOURCES).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
          <select value={action} aria-label="Действие" onChange={(e) => filter(setAction)(e.target.value)}>
            <option value="">Любое действие</option>{(actions || []).map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
          <select value={days} aria-label="Период" onChange={(e) => filter(setDays)(Number(e.target.value))}>
            {PERIODS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        <Status error={error} loading={loading} />
        <DataTable caption="Журнал действий" rows={items} stack
          empty={<EmptyState icon={ScrollText} title={filtered || offset ? 'Ничего не найдено' : 'Записей пока нет'}>
            {filtered ? 'Измените поиск или фильтры.' : null}</EmptyState>}
          columns={[
            { key: 'when', header: 'Когда', render: (i) => when(i.created_at) },
            { key: 'actor', header: 'Кто', render: (i) => (i.actor === 'panel' ? 'веб-панель' : <>пользователь <code>{i.actor}</code></>) },
            { key: 'source', header: 'Откуда', render: (i) => <StatusBadge tone={i.source === 'panel' ? 'pending' : 'off'}>{SOURCES[i.source] || i.source}</StatusBadge> },
            { key: 'action', header: 'Действие', render: (i) => <code>{i.action}</code> },
            { key: 'target', header: 'Объект', render: (i) => i.target || <span className="muted">—</span> },
          ]} />
        {items && <Pager label="Страницы журнала" offset={offset} size={PAGE} shown={items.length} hasMore={data.has_more}
          disabled={loading} onChange={setOffset} prev="Новее" next="Старее" />}
      </Panel>
    </section>
  );
}
