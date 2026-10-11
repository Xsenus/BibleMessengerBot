import { useState } from 'react';
import { api } from '../api.js';
import { Status, useAction, useData, when } from '../ui.jsx';

const KINDS = { once: 'Один раз', daily: 'Каждый день', weekly: 'По дням недели', monthly: 'Раз в месяц', interval: 'Через интервал' };
const DAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
const PLATFORMS = [['telegram', 'Telegram'], ['max', 'MAX'], ['vk', 'ВКонтакте']];
const EMPTY = { title: '', body: '', kind: 'weekly', time: '10:00', timezone: 'Europe/Moscow', days: [6], dayOfMonth: 1,
  interval: 60, start: '', maxRuns: '', platforms: [], assetId: null };

function Form({ onCreated }) {
  const [f, setF] = useState(EMPTY);
  const { run, message, busy } = useAction(onCreated);
  const set = (patch) => setF((old) => ({ ...old, ...patch }));
  const toggle = (list, value) => (list.includes(value) ? list.filter((x) => x !== value) : [...list, value]);
  const upload = async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    await run(async () => set({ assetId: (await api('/media', { method: 'POST', raw: file })).asset_id }), 'Картинка загружена');
  };
  const submit = async (event) => {
    event.preventDefault();
    const body = {
      title: f.title, body: f.body, kind: f.kind, timezone: f.timezone, asset_id: f.assetId,
      audience: { platforms: f.platforms },
      max_runs: f.maxRuns ? Number(f.maxRuns) : null,
      start_at: f.kind === 'once' && f.start ? new Date(f.start).toISOString() : null,
    };
    if (['daily', 'weekly', 'monthly'].includes(f.kind)) body.send_time = f.time.length === 5 ? `${f.time}:00` : f.time;
    if (f.kind === 'weekly') body.days_of_week = f.days.length ? f.days : [1, 2, 3, 4, 5, 6, 7];
    if (f.kind === 'monthly') body.day_of_month = Number(f.dayOfMonth);
    if (f.kind === 'interval') body.interval_minutes = Number(f.interval);
    if (await run(() => api('/announcements', { method: 'POST', body }), 'Объявление создано')) setF(EMPTY);
  };
  return (
    <form className="card" onSubmit={submit}>
      <h3>Новое объявление</h3>
      <input value={f.title} onChange={(e) => set({ title: e.target.value })} placeholder="Заголовок (необязательно)" maxLength={200} />
      <textarea value={f.body} onChange={(e) => set({ body: e.target.value })} placeholder="Текст" rows={5} maxLength={3500} required />
      <div className="row">
        <select value={f.kind} onChange={(e) => set({ kind: e.target.value })}>{Object.entries(KINDS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        {f.kind === 'once' && <input type="datetime-local" value={f.start} onChange={(e) => set({ start: e.target.value })} title="Пусто — отправить сразу" />}
        {['daily', 'weekly', 'monthly'].includes(f.kind) && <input type="time" value={f.time} onChange={(e) => set({ time: e.target.value })} required />}
        {f.kind === 'monthly' && <input type="number" min="1" max="31" value={f.dayOfMonth} onChange={(e) => set({ dayOfMonth: e.target.value })} title="Число месяца" />}
        {f.kind === 'interval' && <input type="number" min="5" value={f.interval} onChange={(e) => set({ interval: e.target.value })} title="Минут" />}
        {f.kind !== 'once' && <input value={f.timezone} onChange={(e) => set({ timezone: e.target.value })} placeholder="Часовой пояс" />}
        {f.kind !== 'once' && <input type="number" min="1" value={f.maxRuns} onChange={(e) => set({ maxRuns: e.target.value })} placeholder="Повторов (пусто — без предела)" />}
      </div>
      {f.kind === 'weekly' && <div className="row">{DAYS.map((d, i) => (
        <label key={d}><input type="checkbox" checked={f.days.includes(i + 1)} onChange={() => set({ days: toggle(f.days, i + 1) })} /> {d}</label>))}</div>}
      <div className="row"><span className="muted">Кому (пусто — всем):</span>{PLATFORMS.map(([id, label]) => (
        <label key={id}><input type="checkbox" checked={f.platforms.includes(id)} onChange={() => set({ platforms: toggle(f.platforms, id) })} /> {label}</label>))}</div>
      <div className="row"><label>Картинка: <input type="file" accept="image/png,image/jpeg,image/webp" onChange={upload} /></label>{f.assetId && <span>✔ #{f.assetId}</span>}</div>
      <button disabled={busy || !f.body.trim()}>Создать</button> {message && <span className="muted">{message}</span>}
    </form>
  );
}

export default function Announcements() {
  const { data, error, loading, reload } = useData('/announcements');
  const { run, message, busy } = useAction(reload);
  const status = (id, value) => run(() => api(`/announcements/${id}/status`, { method: 'POST', body: { status: value } }));
  return (
    <section>
      <h2>Объявления</h2>
      <Form onCreated={reload} />
      <Status error={error} loading={loading} />
      <table>
        <thead><tr><th>Объявление</th><th>Расписание</th><th>Следующий запуск</th><th></th></tr></thead>
        <tbody>
          {(data || []).map((a) => (
            <tr key={a.id}>
              <td><b>{a.title || `#${a.id}`}</b><br /><small className="pre">{a.body.slice(0, 120)}</small></td>
              <td>{KINDS[a.kind]}{a.send_time ? ` ${a.send_time.slice(0, 5)}` : ''}<br /><small>{a.status} · запусков {a.runs_done}{a.max_runs ? `/${a.max_runs}` : ''}</small></td>
              <td>{when(a.next_run_at)}</td>
              <td className="actions">
                <button className="small" disabled={busy} onClick={() => confirm('Отправить сейчас всем?') && run(() => api(`/announcements/${a.id}/run`, { method: 'POST' }), 'Отправлено')}>Сейчас</button>
                {a.status === 'active' && <button className="small" disabled={busy} onClick={() => status(a.id, 'paused')}>Пауза</button>}
                {a.status === 'paused' && <button className="small" disabled={busy} onClick={() => status(a.id, 'active')}>Возобновить</button>}
                {['active', 'paused'].includes(a.status) && <button className="small danger" disabled={busy} onClick={() => confirm('Отменить?') && status(a.id, 'cancelled')}>Отмена</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {message && <p className="muted">{message}</p>}
    </section>
  );
}
