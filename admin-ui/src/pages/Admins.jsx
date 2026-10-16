import { useState } from 'react';
import { Copy, Crown, ShieldCheck, UserPlus } from 'lucide-react';
import { api } from '../api.js';
import { Help, Status, useAction, useData } from '../ui.jsx';
import { EmptyState, Field, PageHeader, Panel, StatusBadge } from '../components.jsx';
import { PLATFORMS, label } from '../labels.js';

const ROLES = [
  ['viewer', 'наблюдатель', 'только просмотр'],
  ['editor', 'редактор', 'объявления, источники, видео и рассылки'],
  ['admin', 'администратор', 'то же плюс настройки'],
];

function RoleSelect({ value, onChange, disabled, ariaLabel }) {
  return (
    <select value={value} disabled={disabled} aria-label={ariaLabel} onChange={(e) => onChange(e.target.value)}>
      {ROLES.map(([id, text]) => <option key={id} value={id}>{text}</option>)}
    </select>
  );
}

function Invite() {
  const { run, message, busy } = useAction();
  const [role, setRole] = useState('admin');
  const [code, setCode] = useState('');
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(`/claim ${code}`); setCopied(true); } catch { setCopied(false); }
  };
  return (
    <Panel title="Пригласить администратора">
      <div className="add-row">
        <Field label="Роль"><RoleSelect value={role} onChange={setRole} ariaLabel="Роль нового администратора" /></Field>
        <button disabled={busy} onClick={() => run(async () => { setCopied(false); setCode((await api(`/admins/invite?role=${role}`, { method: 'POST' })).code); }, 'Код создан')}>
          <UserPlus size={15} aria-hidden="true" />Создать приглашение</button>
      </div>
      {code && (
        <div className="invite-code">
          <span className="muted">Отправьте новому администратору команду для личного чата с ботом. Код одноразовый, действует 24 часа.</span>
          <span className="invite-command"><code>/claim {code}</code>
            <button className="small" onClick={copy}><Copy size={14} aria-hidden="true" />{copied ? 'Скопировано' : 'Копировать'}</button></span>
        </div>
      )}
      {message && <p className="form-message" role="status">{message}</p>}
    </Panel>
  );
}

export default function Admins() {
  const { data, error, loading, reload } = useData('/admins');
  const { run, message, busy } = useAction(reload);
  return (
    <section>
      <PageHeader title="Администраторы" description="Кто управляет ботом из мессенджеров и какие у него права." />
      <Help title="Как назначить администратора">
        <ol><li>Нажмите «Создать приглашение» — появится одноразовый код на 24 часа.</li><li>Новый администратор отправляет боту в <b>личном чате</b> (Telegram, MAX или ВКонтакте) команду <code>/claim КОД</code>.</li><li>Готово: у него открывается меню <code>/admin</code>. Снять права может только владелец.</li></ol>
      </Help>
      <Panel title={`Администраторы${data ? ` (${data.length})` : ''}`}>
        <Status error={error} loading={loading} />
        {data?.length === 0 && <EmptyState icon={ShieldCheck} title="Администраторов пока нет">Владелец назначается командой /claim в боте.</EmptyState>}
        {data?.length > 0 && (
          <div className="table-wrap">
            <table className="data-table table-stack">
              <thead><tr><th scope="col">Имя</th><th scope="col">Платформа</th><th scope="col">Роль</th><th scope="col"><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>{data.map((a) => {
                const name = a.first_name || (a.username ? `@${a.username}` : String(a.telegram_user_id));
                return (
                  <tr key={a.telegram_user_id}>
                    <td data-label="Имя"><b>{name}</b>{a.username && a.first_name && <div><small>@{a.username}</small></div>}</td>
                    <td data-label="Платформа">{label(PLATFORMS, a.platform)}</td>
                    <td data-label="Роль">{a.is_owner ? <StatusBadge tone="pending"><Crown size={12} aria-hidden="true" />владелец</StatusBadge> : (
                      <RoleSelect value={a.admin_role} disabled={busy} ariaLabel={`Роль: ${name}`}
                        onChange={(role) => run(() => api(`/admins/${a.telegram_user_id}/role`, { method: 'PUT', body: { role } }))} />)}</td>
                    <td className="cell-actions">{!a.is_owner && <button className="small danger" disabled={busy}
                      onClick={() => confirm(`Снять права администратора с «${name}»?`) && run(() => api(`/admins/${a.telegram_user_id}`, { method: 'DELETE' }))}>Снять</button>}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
        )}
        {message && <p className="form-message" role="status">{message}</p>}
        <dl className="role-list">{ROLES.map(([id, text, rights]) => <div key={id}><dt>{text}</dt><dd>{rights}</dd></div>)}
          <div><dt>владелец</dt><dd>всё, включая список администраторов</dd></div></dl>
      </Panel>
      <Invite />
    </section>
  );
}
