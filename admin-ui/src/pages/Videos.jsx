import { useState } from 'react';
import { api } from '../api.js';
import { AudienceEditor, Badge, Status, describeAudience, useAction, useData, when } from '../ui.jsx';

function Editor({ feed, onSave, busy }) {
  const [draft, setDraft] = useState({ title: feed.title || '', template: feed.template || '', audience: feed.audience || {} });
  return (
    <div className="row">
      <input value={draft.title} maxLength={200} onChange={(e) => setDraft({ ...draft, title: e.target.value })} placeholder="Название канала для сообщений" />
      <input value={draft.template} maxLength={500} onChange={(e) => setDraft({ ...draft, template: e.target.value })} placeholder="Шаблон: 🎬 {channel}: {title} {url} (пусто — стандартный)" />
      <AudienceEditor value={draft.audience} onChange={(audience) => setDraft({ ...draft, audience })} />
      <button disabled={busy} onClick={() => onSave(draft)}>Сохранить</button>
    </div>
  );
}

export default function Videos() {
  const [open, setOpen] = useState(null);
  const { data, error, loading, reload } = useData('/videos');
  const { run, message, busy } = useAction(reload);
  const [form, setForm] = useState({ platform: 'youtube', channel: '', notify: false });
  const add = async (event) => {
    event.preventDefault();
    if (await run(() => api('/videos', { method: 'POST', body: form }), 'Канал добавлен')) setForm({ ...form, channel: '' });
  };
  const patch = (id, body) => run(() => api(`/videos/${id}`, { method: 'PATCH', body }));
  return (
    <section>
      <h2>Видеоканалы</h2>
      <p className="muted">Бот периодически проверяет канал. Первая проверка только запоминает уже вышедшие видео. Для ВКонтакте нужен VK_SERVICE_TOKEN.</p>
      <Status error={error} loading={loading} />
      <table>
        <thead><tr><th>Канал</th><th>Проверка</th><th>Рассылка</th><th>Интервал</th><th></th></tr></thead>
        <tbody>
          {(data || []).flatMap((f) => [
            <tr key={f.id}>
              <td>{f.platform}<br /><small>{f.title || f.handle}</small>{f.last_error && <><br /><small className="error">⚠ {f.last_error}</small></>}</td>
              <td><button className="small" disabled={busy} onClick={() => patch(f.id, { is_enabled: !f.is_enabled })}><Badge on={f.is_enabled} /></button><br /><small>{when(f.last_checked_at)}</small></td>
              <td><button className="small" disabled={busy} onClick={() => patch(f.id, { notify: !f.notify })}><Badge on={f.notify} yes="шлёт" no="молчит" /></button></td>
              <td><select value={f.interval_minutes} disabled={busy} onChange={(e) => patch(f.id, { interval_minutes: Number(e.target.value) })}>
                {[10, 30, 60, 180, 720].map((m) => <option key={m} value={m}>{m} мин</option>)}
              </select></td>
              <td><button className="small" onClick={() => setOpen(open === f.id ? null : f.id)}>Настроить</button>{' '}
                <button className="small danger" disabled={busy} onClick={() => confirm('Удалить канал?') && run(() => api(`/videos/${f.id}`, { method: 'DELETE' }))}>Удалить</button><br />
                <small className="muted">{describeAudience(f.audience)}</small></td>
            </tr>,
            open === f.id && <tr key={`e${f.id}`}><td colSpan="5"><Editor feed={f} busy={busy} onSave={async (draft) => { if (await patch(f.id, draft)) setOpen(null); }} /></td></tr>,
          ])}
        </tbody>
      </table>
      <form className="card row" onSubmit={add}>
        <select value={form.platform} onChange={(e) => setForm({ ...form, platform: e.target.value })}>
          <option value="youtube">YouTube</option><option value="rutube">RuTube</option><option value="vk">ВКонтакте</option>
        </select>
        <input value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })} placeholder="ID канала или ссылка" required />
        <label><input type="checkbox" checked={form.notify} onChange={(e) => setForm({ ...form, notify: e.target.checked })} /> присылать ссылки</label>
        <button disabled={busy}>Добавить</button>
      </form>
      {message && <p className="muted">{message}</p>}
    </section>
  );
}
