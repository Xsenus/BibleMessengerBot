import { useState } from 'react';
import { MessagesSquare, Search, Users } from 'lucide-react';
import { api } from '../api.js';
import { Help, PLATFORMS as PLATFORM_OPTIONS, Status, useAction, useData, useDebounced } from '../ui.jsx';
import { EmptyState, PageHeader, Panel, StatusBadge, Switch, Tabs, TimeAgo } from '../components.jsx';
import { CHAT_TYPES, PLATFORMS, label } from '../labels.js';

function SearchBox({ value, onChange, placeholder }) {
  return (
    <label className="input-with-icon search-box"><Search size={15} aria-hidden="true" />
      <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} aria-label={placeholder} /></label>
  );
}

function ChatsTab() {
  const [platform, setPlatform] = useState('');
  const [q, setQ] = useState('');
  const query = useDebounced(q);
  const { data, error, loading, reload } = useData(`/chats?platform=${platform}&q=${encodeURIComponent(query)}`);
  const { run, message, busy } = useAction(reload);
  return (
    <>
      <div className="toolbar">
        <select value={platform} aria-label="Платформа" onChange={(e) => setPlatform(e.target.value)}>
          <option value="">Все платформы</option>{PLATFORM_OPTIONS.map(([id, text]) => <option key={id} value={id}>{text}</option>)}
        </select>
        <SearchBox value={q} onChange={setQ} placeholder="Поиск по названию" />
        {data && <span className="muted">Показано: {data.length}{data.length >= 200 ? ' (первые 200)' : ''}</span>}
      </div>
      <Status error={error} loading={loading} />
      {data?.length === 0 && <EmptyState icon={MessagesSquare} title="Чатов не найдено">{q || platform ? 'Измените условия поиска.' : 'Чаты появятся, когда бота добавят в мессенджеры.'}</EmptyState>}
      {data?.length > 0 && (
        <div className="table-wrap">
          <table className="data-table table-stack">
            <thead><tr><th scope="col">Чат</th><th scope="col">Платформа</th><th scope="col">Тип</th><th scope="col">Язык и пояс</th><th scope="col">Обновлён</th><th scope="col">Рассылки</th></tr></thead>
            <tbody>{data.map((c) => {
              const name = c.title || (c.username ? `@${c.username}` : String(c.id));
              return (
                <tr key={c.id}>
                  <td data-label="Чат"><b>{name}</b><div><small><code>{c.id}</code></small></div></td>
                  <td data-label="Платформа">{label(PLATFORMS, c.platform)}</td>
                  <td data-label="Тип">{label(CHAT_TYPES, c.chat_type)}</td>
                  <td data-label="Язык и пояс">{c.ui_language} · <small>{c.timezone}</small></td>
                  <td data-label="Обновлён"><TimeAgo value={c.updated_at} /></td>
                  <td data-label="Рассылки"><Switch checked={c.is_active} disabled={busy} on="получает" off="отключён" label={`Рассылки в чат «${name}»`}
                    onChange={(value) => run(() => api(`/chats/${c.id}/active`, { method: 'PUT', body: { value } }))} /></td>
                </tr>
              );
            })}</tbody>
          </table>
        </div>
      )}
      {message && <p className="form-message" role="alert">{message}</p>}
    </>
  );
}

function UsersTab() {
  const [q, setQ] = useState('');
  const query = useDebounced(q);
  const { data, error, loading, reload } = useData(`/users?q=${encodeURIComponent(query)}`);
  const { run, message, busy } = useAction(reload);
  return (
    <>
      <div className="toolbar"><SearchBox value={q} onChange={setQ} placeholder="Поиск по имени" />
        {data && <span className="muted">Показано: {data.length}{data.length >= 200 ? ' (первые 200)' : ''}</span>}</div>
      <Status error={error} loading={loading} />
      {data?.length === 0 && <EmptyState icon={Users} title="Пользователей не найдено" />}
      {data?.length > 0 && (
        <div className="table-wrap">
          <table className="data-table table-stack">
            <thead><tr><th scope="col">Пользователь</th><th scope="col">Платформа</th><th scope="col">Роль</th><th scope="col">Доступ к боту</th></tr></thead>
            <tbody>{data.map((u) => {
              const name = u.first_name || (u.username ? `@${u.username}` : String(u.id));
              return (
                <tr key={u.id}>
                  <td data-label="Пользователь"><b>{name}</b>{u.username && u.first_name && <div><small>@{u.username}</small></div>}</td>
                  <td data-label="Платформа">{label(PLATFORMS, u.platform)}</td>
                  <td data-label="Роль">{u.is_owner ? <StatusBadge tone="pending">владелец</StatusBadge> : u.is_admin ? <StatusBadge tone="ok">администратор</StatusBadge> : <span className="muted">—</span>}</td>
                  <td data-label="Доступ к боту">{u.is_owner ? <span className="muted">всегда</span> : (
                    <Switch checked={!u.is_blocked} disabled={busy} on="активен" off="заблокирован" label={`Доступ к боту: ${name}`}
                      onChange={(allowed) => run(() => api(`/users/${u.id}/blocked`, { method: 'PUT', body: { value: !allowed } }))} />)}</td>
                </tr>
              );
            })}</tbody>
          </table>
        </div>
      )}
      {message && <p className="form-message" role="alert">{message}</p>}
    </>
  );
}

export default function Chats() {
  const [tab, setTab] = useState('chats');
  return (
    <section>
      <PageHeader title="Чаты и пользователи" description="Кому бот присылает рассылки и кто может им пользоваться." />
      <Help title="Что здесь можно сделать">
        <p><b>Чаты</b>: переключатель «Рассылки» включает или выключает все рассылки этому чату (подписки при отключении ставятся на паузу). <b>Пользователи</b>: заблокированному бот перестаёт отвечать и присылать рассылки; владельца заблокировать нельзя. Роли администраторов меняются в разделе «Администраторы».</p>
      </Help>
      <Panel>
        <Tabs label="Список" value={tab} onChange={setTab} tabs={[['chats', 'Чаты'], ['users', 'Пользователи']]} />
        {tab === 'chats' ? <ChatsTab /> : <UsersTab />}
      </Panel>
    </section>
  );
}
