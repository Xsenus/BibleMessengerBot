import { api } from '../api.js';
import { useState } from 'react';
import { AudienceEditor, Badge, Status, describeAudience, useAction, useData, when } from '../ui.jsx';

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

function Editor({ source, onSave, busy }) {
  const [draft, setDraft] = useState({ prefix: source.prefix || '', min_length: source.min_length, audience: source.audience || {} });
  return (
    <div className="row">
      <input value={draft.prefix} maxLength={200} onChange={(e) => setDraft({ ...draft, prefix: e.target.value })} placeholder="Приставка перед текстом копии" />
      <label>Мин. длина <input type="number" min="1" value={draft.min_length} onChange={(e) => setDraft({ ...draft, min_length: Number(e.target.value) })} /></label>
      <AudienceEditor value={draft.audience} onChange={(audience) => setDraft({ ...draft, audience })} />
      <button disabled={busy} onClick={() => onSave(draft)}>Сохранить</button>
    </div>
  );
}

export default function Sources() {
  const [open, setOpen] = useState(null);
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
          {(data || []).flatMap((s) => [
            <tr key={s.id}>
              <td>{s.title || s.external_id}</td><td>{s.platform}</td>
              <td><select value={s.mode} disabled={busy} onChange={(e) => patch(s.id, { mode: e.target.value })}>
                {Object.entries(MODES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select></td>
              <td><button className="small" disabled={busy} onClick={() => patch(s.id, { is_enabled: !s.is_enabled })}><Badge on={s.is_enabled} /></button>{' '}
                <button className="small" onClick={() => setOpen(open === s.id ? null : s.id)}>Настроить</button><br /><small className="muted">{describeAudience(s.audience)}</small></td>
            </tr>,
            open === s.id && <tr key={`e${s.id}`}><td colSpan="4"><Editor source={s} busy={busy} onSave={async (draft) => { if (await patch(s.id, draft)) setOpen(null); }} /></td></tr>,
          ])}
        </tbody>
      </table>
      {message && <p className="muted">{message}</p>}
      <Posts />
    </section>
  );
}
