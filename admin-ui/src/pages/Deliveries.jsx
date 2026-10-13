import { api } from '../api.js';
import { Status, useAction, useData, when } from '../ui.jsx';

const ACTIONS = [['sent', 'Дошло'], ['retry-duplicate-risk', 'Повторить'], ['cancel', 'Отменить']];

export default function Deliveries() {
  const { data, error, loading, reload } = useData('/deliveries/attention');
  const { run, message, busy } = useAction(reload);
  return (
    <section>
      <h2>Доставки, требующие решения</h2>
      <p className="muted">«Неопределённый» исход — бот не уверен, дошло ли сообщение. «Повторить» может создать дубль; «Дошло» закрывает задание как отправленное.</p>
      <Status error={error} loading={loading} />
      {data?.length === 0 && <p className="muted">Всё в порядке — решений не требуется.</p>}
      <table><tbody>
        {(data || []).map((d) => (
          <tr key={d.id}><td>#{d.id}</td><td>чат {d.chat_id}</td><td>{d.mode}</td><td>{d.status}{d.error_code ? ` · ${d.error_code}` : ''}</td><td>{when(d.updated_at)}</td>
            <td className="actions">{ACTIONS.map(([action, label]) => (
              <button key={action} className="small" disabled={busy} onClick={() => confirm(`${label}?`) && run(() => api(`/deliveries/${d.id}/resolve`, { method: 'POST', body: { action } }))}>{label}</button>))}</td></tr>
        ))}
      </tbody></table>
      {message && <p className="muted">{message}</p>}
    </section>
  );
}
