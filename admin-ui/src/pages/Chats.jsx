import { useState } from 'react';
import { api } from '../api.js';
import { Help, Badge, PLATFORMS, Status, useAction, useData, when } from '../ui.jsx';

function ChatsTab() {
  const [platform, setPlatform] = useState('');
  const [q, setQ] = useState('');
  const { data, error, loading, reload } = useData(`/chats?platform=${platform}&q=${encodeURIComponent(q)}`);
  const { run, message, busy } = useAction(reload);
  return (
    <>
      <div className="row">
        <select value={platform} onChange={(e) => setPlatform(e.target.value)}>
          <option value="">Все платформы</option>{PLATFORMS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Поиск по названию" style={{ maxWidth: 240 }} />
      </div>
      <Status error={error} loading={loading} />
      <table><thead><tr><th>Чат</th><th>Платформа</th><th>Тип</th><th>Язык / пояс</th><th>Обновлён</th><th></th></tr></thead><tbody>
        {(data || []).map((c) => (
          <tr key={c.id}><td>{c.title || c.username || c.id}</td><td>{c.platform}</td><td>{c.chat_type}</td><td>{c.ui_language} · {c.timezone}</td><td>{when(c.updated_at)}</td>
            <td><button className="small" disabled={busy} onClick={() => run(() => api(`/chats/${c.id}/active`, { method: 'PUT', body: { value: !c.is_active } }))}><Badge on={c.is_active} yes="получает" no="отключён" /></button></td></tr>
        ))}
      </tbody></table>
      {message && <p className="muted">{message}</p>}
    </>
  );
}

function UsersTab() {
  const [q, setQ] = useState('');
  const { data, error, loading, reload } = useData(`/users?q=${encodeURIComponent(q)}`);
  const { run, message, busy } = useAction(reload);
  return (
    <>
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Поиск по имени" style={{ maxWidth: 240 }} />
      <Status error={error} loading={loading} />
      <table><thead><tr><th>Пользователь</th><th>Платформа</th><th>Роль</th><th></th></tr></thead><tbody>
        {(data || []).map((u) => (
          <tr key={u.id}><td>{u.first_name || u.username || u.id}</td><td>{u.platform}</td><td>{u.is_owner ? 'владелец' : u.is_admin ? 'администратор' : ''}</td>
            <td>{!u.is_owner && <button className="small" disabled={busy} onClick={() => run(() => api(`/users/${u.id}/blocked`, { method: 'PUT', body: { value: !u.is_blocked } }))}><Badge on={!u.is_blocked} yes="активен" no="заблокирован" /></button>}</td></tr>
        ))}
      </tbody></table>
      {message && <p className="muted">{message}</p>}
    </>
  );
}

export default function Chats() {
  const [tab, setTab] = useState('chats');
  return (
    <section>
      <h2>Чаты и пользователи</h2>
      <Help title="Что здесь можно сделать">
        <p><b>Чаты</b>: «получает» / «отключён» — включает или выключает все рассылки этому чату (подписки при отключении ставятся на паузу). <b>Пользователи</b>: «заблокирован» — бот перестаёт отвечать и присылать рассылки; владельца заблокировать нельзя. Роли администраторов меняются в разделе «Администраторы».</p>
      </Help>
      <div className="row"><button className={tab === 'chats' ? '' : 'small'} onClick={() => setTab('chats')}>Чаты</button>
        <button className={tab === 'users' ? '' : 'small'} onClick={() => setTab('users')}>Пользователи</button></div>
      {tab === 'chats' ? <ChatsTab /> : <UsersTab />}
    </section>
  );
}
