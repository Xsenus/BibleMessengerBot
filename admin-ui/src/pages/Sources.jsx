import { useState } from 'react';
import { Newspaper, Rss } from 'lucide-react';
import { api } from '../api.js';
import { Help, AudienceEditor, Status, describeAudience, useAction, useData } from '../ui.jsx';
import { EmptyState, Field, PageHeader, Panel, Switch, TimeAgo } from '../components.jsx';
import { PLATFORMS, label } from '../labels.js';

const MODES = { capture: 'только сохранять', approve: 'с подтверждением', auto: 'отправлять сразу' };

function Posts() {
  const { data, error, loading, reload } = useData('/posts?status=new');
  const { run, message, busy } = useAction(reload);
  return (
    <Panel title={`Новые посты${data ? ` (${data.length})` : ''}`}>
      <Status error={error} loading={loading} />
      {data?.length === 0 && <EmptyState icon={Newspaper} title="Новых постов нет">Посты появятся здесь, когда выйдут в подключённых источниках.</EmptyState>}
      <div className="item-list">
        {(data || []).map((p) => (
          <article className="item-card" key={p.id}>
            <div className="item-meta"><b>{p.source_title}</b><span>{label(PLATFORMS, p.platform)}</span><TimeAgo value={p.posted_at} /></div>
            <p className="item-body">{p.body}</p>
            <div className="item-actions">
              <button disabled={busy} onClick={() => confirm('Отправить копию поста всем получателям источника?') && run(() => api(`/posts/${p.id}/publish`, { method: 'POST' }), 'Поставлено в очередь')}>Отправить всем</button>
              <button className="small" disabled={busy} onClick={() => run(() => api(`/posts/${p.id}/ignore`, { method: 'POST' }))}>Пропустить</button>
            </div>
          </article>
        ))}
      </div>
      {message && <p className="form-message" role="status">{message}</p>}
    </Panel>
  );
}

function Editor({ source, onSave, onCancel, busy }) {
  const [draft, setDraft] = useState({ prefix: source.prefix || '', min_length: source.min_length, audience: source.audience || {} });
  return (
    <div className="inline-editor">
      <div className="form-grid">
        <Field label="Приставка перед текстом" hint="Например, «Из канала …». Пусто — без приставки.">
          <input value={draft.prefix} maxLength={200} onChange={(e) => setDraft({ ...draft, prefix: e.target.value })} placeholder="Приставка перед текстом копии" />
        </Field>
        <Field label="Минимальная длина поста" hint="Более короткие посты не копируются.">
          <input type="number" min="1" value={draft.min_length} onChange={(e) => setDraft({ ...draft, min_length: Number(e.target.value) })} />
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

export default function Sources() {
  const [open, setOpen] = useState(null);
  const { data, error, loading, reload } = useData('/sources');
  const { run, message, busy } = useAction(reload);
  const patch = (id, body) => run(() => api(`/sources/${id}`, { method: 'PATCH', body }));
  return (
    <section>
      <PageHeader title="Источники контента" description="Каналы, группы и сообщества, где состоит бот. Новые посты из них можно пересылать подписчикам." />
      <Help title="Как это работает">
        <ol>
          <li>Добавьте бота в канал (администратором) или группу — источник появится в списке <b>выключенным</b>.</li>
          <li>Включите его и выберите режим: «только сохранять» (посты копятся, отправляете вручную), «с подтверждением» (администраторам приходит уведомление) или «отправлять сразу».</li>
          <li>«Настроить» задаёт приставку перед копией, минимальную длину поста и аудиторию (кому отправлять копии). Копия не возвращается в чат-источник.</li>
          <li>Новые посты — в блоке ниже: «Отправить всем» или «Пропустить». Telegram не показывает боту старые посты канала: собираются только вышедшие после того, как бот стал администратором.</li>
        </ol>
      </Help>
      <Panel title={`Источники${data ? ` (${data.length})` : ''}`}>
        <Status error={error} loading={loading} />
        {data?.length === 0 && <EmptyState icon={Rss} title="Источников пока нет">Добавьте бота в канал или группу — источник появится здесь.</EmptyState>}
        {data?.length > 0 && (
          <div className="table-wrap">
            <table className="data-table table-stack">
              <thead><tr><th scope="col">Источник</th><th scope="col">Режим</th><th scope="col">Работает</th><th scope="col">Кому</th><th scope="col"><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>
                {data.flatMap((s) => [
                  <tr key={s.id}>
                    <td data-label="Источник"><b>{s.title || s.external_id}</b><div><small>{label(PLATFORMS, s.platform)}</small></div></td>
                    <td data-label="Режим"><select value={s.mode} disabled={busy} aria-label={`Режим: ${s.title || s.external_id}`} onChange={(e) => patch(s.id, { mode: e.target.value })}>
                      {Object.entries(MODES).map(([value, text]) => <option key={value} value={value}>{text}</option>)}
                    </select></td>
                    <td data-label="Работает"><Switch checked={s.is_enabled} disabled={busy} label={`Источник «${s.title || s.external_id}» включён`} onChange={(value) => patch(s.id, { is_enabled: value })} /></td>
                    <td data-label="Кому"><small>{describeAudience(s.audience)}</small></td>
                    <td className="cell-actions"><button className="small" aria-expanded={open === s.id} onClick={() => setOpen(open === s.id ? null : s.id)}>Настроить</button></td>
                  </tr>,
                  open === s.id && <tr key={`e${s.id}`} className="row-editor"><td colSpan="5">
                    <Editor source={s} busy={busy} onCancel={() => setOpen(null)} onSave={async (draft) => { if (await patch(s.id, draft)) setOpen(null); }} />
                  </td></tr>,
                ])}
              </tbody>
            </table>
          </div>
        )}
        {message && <p className="form-message" role="status">{message}</p>}
      </Panel>
      <Posts />
    </section>
  );
}
