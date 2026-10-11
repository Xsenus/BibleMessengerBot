import { api } from '../api.js';
import { Badge, Status, useAction, useData, when } from '../ui.jsx';

const MODES = { capture: 'только сохранять', approve: 'с подтверждением', auto: 'отправлять сразу' };

function Posts() {
  const { data, error, loading, reload } = useData('/posts?status=new');
  const { run, message, busy } = useAction(reload);
  return (
    <>
      <h3>Новые посты</h3>
      <Status error={error} loading={loading} />
      {(data || []).map((p) => (
        <div className="card" key={p.id}>
          <small className="muted">{p.platform} · {p.source_title} · {when(p.posted_at)}</small>
          <p className="pre">{p.body}</p>
          <button disabled={busy} onClick={() => confirm('Отправить копию всем?') && run(() => api(`/posts/${p.id}/publish`, { method: 'POST' }), 'Поставлено в очередь')}>Отправить всем</button>{' '}
          <button className="small" disabled={busy} onClick={() => run(() => api(`/posts/${p.id}/ignore`, { method: 'POST' }))}>Пропустить</button>
        </div>
      ))}
      {data?.length === 0 && <p className="muted">Новых постов нет.</p>}
      {message && <p className="muted">{message}</p>}
    </>
  );
}

export default function Sources() {
  const { data, error, loading, reload } = useData('/sources');
  const { run, message, busy } = useAction(reload);
  const patch = (id, body) => run(() => api(`/sources/${id}`, { method: 'PATCH', body }));
  return (
    <section>
      <h2>Источники контента</h2>
      <p className="muted">Каналы, группы и сообщества, где состоит бот. Новые источники появляются выключенными. Telegram не отдаёт историю канала: посты собираются с момента, когда бот стал администратором.</p>
      <Status error={error} loading={loading} />
      <table>
        <thead><tr><th>Источник</th><th>Платформа</th><th>Режим</th><th>Состояние</th></tr></thead>
        <tbody>
          {(data || []).map((s) => (
            <tr key={s.id}>
              <td>{s.title || s.external_id}</td><td>{s.platform}</td>
              <td><select value={s.mode} disabled={busy} onChange={(e) => patch(s.id, { mode: e.target.value })}>
                {Object.entries(MODES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select></td>
              <td><button className="small" disabled={busy} onClick={() => patch(s.id, { is_enabled: !s.is_enabled })}><Badge on={s.is_enabled} /></button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {message && <p className="muted">{message}</p>}
      <Posts />
    </section>
  );
}
