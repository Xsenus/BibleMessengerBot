import { useState } from 'react';
import { api } from '../api.js';
import { Help, Status, useAction, useData } from '../ui.jsx';

export default function Admins() {
  const { data, error, loading, reload } = useData('/admins');
  const { run, message, busy } = useAction(reload);
  const [code, setCode] = useState('');
  return (
    <section>
      <h2>Администраторы</h2>
      <Help title="Как назначить администратора">
        <ol><li>Нажмите «Создать приглашение» — появится одноразовый код на 24 часа.</li><li>Новый администратор отправляет боту в <b>личном чате</b> (Telegram, MAX или ВКонтакте) команду <code>/claim КОД</code>.</li><li>Готово: у него открывается меню <code>/admin</code>. Снять права может только владелец.</li></ol>
      </Help>
      <p className="muted">Администратором можно стать в Telegram, MAX или ВКонтакте: создайте код и отправьте его новому администратору — он пишет боту в личном чате <code>/claim КОД</code>.</p>
      <Status error={error} loading={loading} />
      <table><tbody>
        {(data || []).map((a) => (
          <tr key={a.telegram_user_id}>
            <td>{a.is_owner ? '👑' : '•'} {a.first_name || a.username || a.telegram_user_id}</td><td>{a.platform}</td>
            <td>{!a.is_owner && <button className="small danger" disabled={busy} onClick={() => confirm('Снять права?') && run(() => api(`/admins/${a.telegram_user_id}`, { method: 'DELETE' }))}>Снять</button>}</td>
          </tr>
        ))}
      </tbody></table>
      <button disabled={busy} onClick={() => run(async () => setCode((await api('/admins/invite', { method: 'POST' })).code), 'Код создан')}>Создать приглашение</button>
      {code && <p>Код (24 часа, одноразовый): <code>{code}</code><br />Команда: <code>/claim {code}</code></p>}
      {message && <p className="muted">{message}</p>}
    </section>
  );
}
