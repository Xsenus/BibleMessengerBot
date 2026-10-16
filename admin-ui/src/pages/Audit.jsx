import { useState } from 'react';
import { RefreshCw, ScrollText, Search } from 'lucide-react';
import { Status, useData, when } from '../ui.jsx';
import { DataTable, EmptyState, PageHeader, Panel, StatusBadge } from '../components.jsx';

const SOURCES = { panel: 'веб-панель', bot: 'бот' };

export default function Audit() {
  const { data, error, loading, reload } = useData('/audit?limit=200');
  const [filter, setFilter] = useState('');
  const [source, setSource] = useState('');
  const needle = filter.trim().toLowerCase();
  const items = data?.items.filter((i) => (!source || i.source === source)
    && (!needle || [i.actor, i.action, i.target].some((v) => String(v ?? '').toLowerCase().includes(needle))));
  return (
    <section>
      <PageHeader title="Журнал действий" description="Кто и что менял: из веб-панели и из команд бота. Секреты (ключи, пароли, адреса прокси) в журнал не попадают."
        actions={<button className="small" onClick={reload}><RefreshCw size={15} aria-hidden="true" />Обновить</button>} />
      <Panel>
        <div className="toolbar">
          <label className="input-with-icon search-box"><Search size={15} aria-hidden="true" />
            <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Кто, действие или объект" aria-label="Фильтр журнала" /></label>
          <select value={source} aria-label="Откуда" onChange={(e) => setSource(e.target.value)}>
            <option value="">Откуда угодно</option>{Object.entries(SOURCES).map(([id, text]) => <option key={id} value={id}>{text}</option>)}
          </select>
          {data && <span className="muted">Показано {items.length} из {data.items.length} последних записей</span>}
        </div>
        <Status error={error} loading={loading} />
        <DataTable caption="Журнал действий" rows={items} stack
          empty={<EmptyState icon={ScrollText} title={data?.items.length ? 'Ничего не найдено' : 'Записей пока нет'} />}
          columns={[
            { key: 'when', header: 'Когда', render: (i) => when(i.created_at) },
            { key: 'actor', header: 'Кто', render: (i) => (i.actor === 'panel' ? 'веб-панель' : <>пользователь <code>{i.actor}</code></>) },
            { key: 'source', header: 'Откуда', render: (i) => <StatusBadge tone={i.source === 'panel' ? 'pending' : 'off'}>{SOURCES[i.source] || i.source}</StatusBadge> },
            { key: 'action', header: 'Действие', render: (i) => <code>{i.action}</code> },
            { key: 'target', header: 'Объект', render: (i) => i.target || <span className="muted">—</span> },
          ]} />
      </Panel>
    </section>
  );
}
