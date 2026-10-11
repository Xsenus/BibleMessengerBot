import { useState } from 'react';
import { api } from '../api.js';
import { Badge, Status, useAction, useData, when } from '../ui.jsx';

export default function Videos() {
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
          {(data || []).map((f) => (
            <tr key={f.id}>
              <td>{f.platform}<br /><small>{f.title || f.handle}</small>{f.last_error && <><br /><small className="error">⚠ {f.last_error}</small></>}</td>
              <td><button className="small" disabled={busy} onClick={() => patch(f.id, { is_enabled: !f.is_enabled })}><Badge on={f.is_enabled} /></button><br /><small>{when(f.last_checked_at)}</small></td>
              <td><button className="small" disabled={busy} onClick={() => patch(f.id, { notify: !f.notify })}><Badge on={f.notify} yes="шлёт" no="молчит" /></button></td>
              <td><select value={f.interval_minutes} disabled={busy} onChange={(e) => patch(f.id, { interval_minutes: Number(e.target.value) })}>
                {[10, 30, 60, 180, 720].map((m) => <option key={m} value={m}>{m} мин</option>)}
              </select></td>
              <td><button className="small danger" disabled={busy} onClick={() => confirm('Удалить канал?') && run(() => api(`/videos/${f.id}`, { method: 'DELETE' }))}>Удалить</button></td>
            </tr>
          ))}
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
