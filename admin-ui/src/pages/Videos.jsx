import { useState } from 'react';
import { Clapperboard, TriangleAlert } from 'lucide-react';
import { ask } from '../feedback.jsx';
import { api } from '../api.js';
import { Help, AudienceEditor, Status, describeAudience, useAction, useData } from '../ui.jsx';
import { EmptyState, Field, PageHeader, Panel, Switch, TimeAgo } from '../components.jsx';
import { PLATFORMS, label } from '../labels.js';

const INTERVALS = [10, 30, 60, 180, 720];
const intervalText = (m) => (m < 60 ? `${m} мин` : m % 60 ? `${m} мин` : `${m / 60} ч`);

function Editor({ feed, onSave, onCancel, busy }) {
  const [draft, setDraft] = useState({ title: feed.title || '', template: feed.template || '', audience: feed.audience || {} });
  return (
    <div className="inline-editor">
      <div className="form-grid">
        <Field label="Название канала в сообщениях"><input value={draft.title} maxLength={200} onChange={(e) => setDraft({ ...draft, title: e.target.value })} placeholder="Название канала для сообщений" /></Field>
        <Field label="Шаблон сообщения" hint="Подстановки: {channel}, {title}, {url}. Пусто — стандартный.">
          <input value={draft.template} maxLength={500} onChange={(e) => setDraft({ ...draft, template: e.target.value })} placeholder="🎬 {channel}: {title} {url}" />
        </Field>
      </div>
      <AudienceEditor value={draft.audience} onChange={(audience) => setDraft({ ...draft, audience })} />
      <div className="form-actions">
        <button disabled={busy} onClick={() => onSave(draft)}>Сохранить</button>
        <button className="small" onClick={onCancel}>Отмена</button>
      </div>
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
      <PageHeader title="Видеоканалы" description="Бот периодически проверяет каналы и может присылать ссылки на новые видео." />
      <Help title="Как добавить канал">
        <ul>
          <li><b>YouTube</b>: нужен ID канала вида <code>UC…</code> (24 символа). Его видно в адресе <code>youtube.com/channel/UC…</code>; если у канала адрес вида <code>@имя</code>, откройте страницу канала → «О канале» → «Поделиться» → «Копировать ID канала».</li>
          <li><b>RuTube</b>: числовой ID автора из адреса <code>rutube.ru/channel/12345/</code>.</li>
          <li><b>ВКонтакте</b>: адрес сообщества вида <code>club123</code> или число; нужен <code>VK_SERVICE_TOKEN</code> в <code>.env</code>.</li>
        </ul>
        <p>Первая проверка только запоминает уже вышедшие видео. Чтобы бот присылал ссылки, включите «Рассылку». «Настроить» задаёт название, шаблон (<code>{'{title}'}</code>, <code>{'{url}'}</code>, <code>{'{channel}'}</code>) и аудиторию. При ошибках интервал проверки растёт; причина видна под названием канала.</p>
      </Help>
      <form className="panel" onSubmit={add}>
        <div className="panel-head"><h3>Добавить канал</h3></div>
        <div className="add-row">
          <Field label="Платформа">
            <select value={form.platform} onChange={(e) => setForm({ ...form, platform: e.target.value })}>
              <option value="youtube">YouTube</option><option value="rutube">RuTube</option><option value="vk">ВКонтакте</option>
            </select>
          </Field>
          <Field label="Канал" className="grow"><input value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })} placeholder="ID канала или ссылка" required /></Field>
          <label className="checkbox-line"><input type="checkbox" checked={form.notify} onChange={(e) => setForm({ ...form, notify: e.target.checked })} /> присылать ссылки</label>
          <button disabled={busy}>Добавить</button>
        </div>
      </form>
      <Panel title={`Каналы${data ? ` (${data.length})` : ''}`}>
        <Status error={error} loading={loading} />
        {data?.length === 0 && <EmptyState icon={Clapperboard} title="Каналов пока нет">Добавьте первый канал в форме выше.</EmptyState>}
        {data?.length > 0 && (
          <div className="table-wrap">
            <table className="data-table table-stack">
              <thead><tr><th scope="col">Канал</th><th scope="col">Проверка</th><th scope="col">Рассылка</th><th scope="col">Интервал</th><th scope="col"><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>
                {data.flatMap((f) => {
                  const name = f.title || f.handle;
                  return [
                    <tr key={f.id}>
                      <td data-label="Канал" className="cell-main"><b>{name}</b><div><small>{label(PLATFORMS, f.platform)} · {describeAudience(f.audience)}</small></div>
                        {f.last_error && <div className="warn-line"><TriangleAlert size={14} aria-hidden="true" />{f.last_error}</div>}</td>
                      <td data-label="Проверка"><Switch checked={f.is_enabled} disabled={busy} label={`Проверять канал «${name}»`} onChange={(value) => patch(f.id, { is_enabled: value })} />
                        <div><small>проверен <TimeAgo value={f.last_checked_at} /></small></div></td>
                      <td data-label="Рассылка"><Switch checked={f.notify} disabled={busy} on="шлёт" off="молчит" label={`Присылать ссылки с канала «${name}»`} onChange={(value) => patch(f.id, { notify: value })} /></td>
                      <td data-label="Интервал"><select value={f.interval_minutes} disabled={busy} aria-label={`Интервал проверки «${name}»`} onChange={(e) => patch(f.id, { interval_minutes: Number(e.target.value) })}>
                        {INTERVALS.map((m) => <option key={m} value={m}>{intervalText(m)}</option>)}
                      </select></td>
                      <td className="cell-actions"><div className="actions">
                        <button className="small" aria-expanded={open === f.id} onClick={() => setOpen(open === f.id ? null : f.id)}>Настроить</button>
                        <button className="small danger" disabled={busy} onClick={async () => (await ask({ title: `Удалить канал «${name}»?`, body: 'Проверка новых видео и рассылка ссылок прекратятся.', confirmText: 'Удалить', danger: true })) && run(() => api(`/videos/${f.id}`, { method: 'DELETE' }))}>Удалить</button>
                      </div></td>
                    </tr>,
                    open === f.id && <tr key={`e${f.id}`} className="row-editor"><td colSpan="5">
                      <Editor feed={f} busy={busy} onCancel={() => setOpen(null)} onSave={async (draft) => { if (await patch(f.id, draft)) setOpen(null); }} />
                    </td></tr>,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
        {message && <p className="form-message" role="alert">{message}</p>}
      </Panel>
    </section>
  );
}
