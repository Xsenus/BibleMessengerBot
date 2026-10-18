import { useState } from 'react';
import { CalendarClock, FileUp, Megaphone, X } from 'lucide-react';
import { ask, askText } from '../feedback.jsx';
import { api } from '../api.js';
import { Help, AudienceEditor, Status, describeAudience, useAction, useData, usePaged, when } from '../ui.jsx';
import { Chip, EmptyState, Field, FilePicker, PageHeader, Pager, Panel, StatusBadge, TimeAgo, formatNumber } from '../components.jsx';
import { PLATFORMS } from '../labels.js';

const KINDS = { once: 'Один раз', daily: 'Каждый день', weekly: 'По дням недели', monthly: 'Раз в месяц', interval: 'Через интервал' };
const DAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
const STATUSES = { active: ['Активно', 'ok'], paused: ['На паузе', 'warning'], finished: ['Завершено', 'off'], cancelled: ['Отменено', 'off'] };
const TIMEZONES = ['Europe/Moscow', 'Europe/Kaliningrad', 'Europe/Samara', 'Asia/Yekaterinburg', 'Asia/Omsk', 'Asia/Novosibirsk',
  'Asia/Krasnoyarsk', 'Asia/Irkutsk', 'Asia/Yakutsk', 'Asia/Vladivostok', 'Asia/Magadan', 'Asia/Kamchatka',
  'Europe/Minsk', 'Europe/Kyiv', 'Asia/Almaty', 'Asia/Tashkent', 'Asia/Tbilisi', 'Asia/Yerevan', 'UTC'];
const BODY_LIMIT = 3500;
const PAGE = 50;
const EMPTY = { title: '', body: '', kind: 'weekly', time: '10:00', timezone: 'Europe/Moscow', days: [6], dayOfMonth: 1,
  interval: 60, start: '', maxRuns: '', platforms: [], assetId: null };

/** Расписание человеческим языком: «сб в 10:00», «каждые 60 мин». */
function describeSchedule(a) {
  const time = a.send_time ? a.send_time.slice(0, 5) : '';
  const days = a.days_of_week || [];
  switch (a.kind) {
    case 'once': return a.start_at ? `Один раз, ${when(a.start_at)}` : 'Один раз';
    case 'daily': return `Каждый день в ${time}`;
    case 'weekly': return `${days.length === 7 ? 'Каждый день' : days.map((d) => DAYS[d - 1]).join(', ')} в ${time}`;
    case 'monthly': return `${a.day_of_month}-го числа в ${time}`;
    case 'interval': return `Каждые ${a.interval_minutes} мин`;
    default: return KINDS[a.kind] || a.kind;
  }
}

function Form({ onCreated }) {
  const [f, setF] = useState(EMPTY);
  const [preview, setPreview] = useState(null);
  const templates = useData('/announcement-templates');
  const { run, message, busy } = useAction(onCreated);
  const set = (patch) => setF((old) => ({ ...old, ...patch }));
  const toggle = (list, value) => (list.includes(value) ? list.filter((x) => x !== value) : [...list, value]);
  const repeats = f.kind !== 'once';
  const upload = async (event) => {
    const file = event.target.files[0];
    event.target.value = '';
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
    if (await run(() => api('/announcements', { method: 'POST', body }), 'Объявление создано')) { setF(EMPTY); setPreview(null); }
  };
  const saveTemplate = async () => {
    const name = await askText({ title: 'Сохранить как шаблон', label: 'Название шаблона', confirmText: 'Сохранить', initial: f.title });
    if (name) run(async () => { await api('/announcement-templates', { method: 'POST', body: { name, title: f.title, body: f.body } }); await templates.reload(); }, 'Шаблон сохранён');
  };
  const showPreview = () => run(async () => setPreview(await api('/announcements/preview', {
    method: 'POST', body: { title: f.title, body: f.body, audience: { platforms: f.platforms }, asset_id: f.assetId } })), 'Предпросмотр готов');

  return (
    <div className="compose">
      <form className="panel compose-form" onSubmit={submit}>
        <div className="panel-head"><h2>Новое объявление</h2>
          <select className="select-sm" aria-label="Шаблон" value="" onChange={(e) => { const t = (templates.data || []).find((x) => String(x.id) === e.target.value); if (t) set({ title: t.title, body: t.body }); }}>
            <option value="">Из шаблона…</option>{(templates.data || []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </div>

        <div className="form-section">
          <Field label="Заголовок" hint="Необязательно. Показывается жирным над текстом.">
            <input value={f.title} onChange={(e) => set({ title: e.target.value })} placeholder="Заголовок (необязательно)" maxLength={200} />
          </Field>
          <Field label="Текст" hint={`${f.body.length} / ${BODY_LIMIT}`}>
            <textarea value={f.body} onChange={(e) => set({ body: e.target.value })} placeholder="Текст" rows={6} maxLength={BODY_LIMIT} required />
          </Field>
        </div>

        <div className="form-section">
          <h3 className="form-section-title"><CalendarClock size={16} aria-hidden="true" />Когда отправлять</h3>
          <div className="form-grid">
            <Field label="Как часто">
              <select value={f.kind} onChange={(e) => set({ kind: e.target.value })}>{Object.entries(KINDS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
            </Field>
            {f.kind === 'once' && <Field label="Дата и время" hint="Пусто — отправить сразу после создания."><input type="datetime-local" value={f.start} onChange={(e) => set({ start: e.target.value })} /></Field>}
            {['daily', 'weekly', 'monthly'].includes(f.kind) && <Field label="Время"><input type="time" value={f.time} onChange={(e) => set({ time: e.target.value })} required /></Field>}
            {f.kind === 'monthly' && <Field label="Число месяца"><input type="number" min="1" max="31" value={f.dayOfMonth} onChange={(e) => set({ dayOfMonth: e.target.value })} /></Field>}
            {f.kind === 'interval' && <Field label="Интервал, минут" hint="Не меньше 5."><input type="number" min="5" value={f.interval} onChange={(e) => set({ interval: e.target.value })} /></Field>}
            {repeats && <Field label="Часовой пояс"><input value={f.timezone} list="announcement-timezones" onChange={(e) => set({ timezone: e.target.value })} placeholder="Europe/Moscow" /></Field>}
            {repeats && <Field label="Сколько раз отправить" hint="Пусто — без ограничения."><input type="number" min="1" value={f.maxRuns} onChange={(e) => set({ maxRuns: e.target.value })} placeholder="без ограничения" /></Field>}
          </div>
          <datalist id="announcement-timezones">{TIMEZONES.map((tz) => <option key={tz} value={tz} />)}</datalist>
          {f.kind === 'weekly' && (
            <Field as="fieldset" label="Дни недели" hint="Ничего не отмечено — каждый день.">
              <div className="chip-group">{DAYS.map((d, i) => <Chip key={d} checked={f.days.includes(i + 1)} onChange={() => set({ days: toggle(f.days, i + 1) })}>{d}</Chip>)}</div>
            </Field>
          )}
        </div>

        <div className="form-section">
          <Field as="fieldset" label={<>Кому <span className="muted">(ничего не отмечено — всем)</span></>}>
            <div className="chip-group">{Object.entries(PLATFORMS).filter(([id]) => ['telegram', 'max', 'vk'].includes(id)).map(([id, text]) => (
              <Chip key={id} checked={f.platforms.includes(id)} onChange={() => set({ platforms: toggle(f.platforms, id) })}>{text}</Chip>))}</div>
          </Field>
          <Field label="Картинка" hint="PNG, JPEG или WebP.">
            {f.assetId
              ? <span className="file-chosen"><StatusBadge tone="ok">Загружена #{f.assetId}</StatusBadge>
                <button type="button" className="small" onClick={() => set({ assetId: null })}><X size={14} aria-hidden="true" />Убрать</button></span>
              : <FilePicker accept="image/png,image/jpeg,image/webp" onChange={upload} disabled={busy}>Выбрать картинку</FilePicker>}
          </Field>
        </div>

        <div className="form-actions">
          <button disabled={busy || !f.body.trim()}>Создать</button>
          <button type="button" className="small" disabled={busy || !f.body.trim()} onClick={showPreview}>Предпросмотр</button>
          <button type="button" className="small" disabled={busy || !f.body.trim()} onClick={saveTemplate}>Сохранить как шаблон</button>
          {message && <span className="form-message" role="alert">{message}</span>}
        </div>
      </form>

      <aside className="panel compose-preview" aria-label="Предпросмотр">
        <div className="panel-head"><h2>Предпросмотр</h2></div>
        {!preview ? <p className="muted">Нажмите «Предпросмотр»: здесь появится число получателей и то, как текст разобьётся на сообщения в каждой платформе.</p> : (<>
          <p className="preview-total">Получателей: <strong>{formatNumber(preview.total)}</strong></p>
          {Object.entries(preview.platforms).map(([name, p]) => (
            <div key={name} className="preview-platform">
              <div className="preview-platform-head"><b>{PLATFORMS[name] || name}</b><span className="muted">{formatNumber(p.recipients)} получ.</span></div>
              {p.parts.map((part, i) => <pre key={i} className="preview-bubble">{part}</pre>)}
            </div>
          ))}
        </>)}
      </aside>
    </div>
  );
}

function Editor({ item, onSave, onCancel, busy }) {
  const [draft, setDraft] = useState({ title: item.title, body: item.body, audience: item.audience || {} });
  return (
    <div className="inline-editor">
      <Field label="Заголовок"><input value={draft.title} maxLength={200} onChange={(e) => setDraft({ ...draft, title: e.target.value })} placeholder="Заголовок" /></Field>
      <Field label="Текст" hint={`${draft.body.length} / ${BODY_LIMIT}`}><textarea rows={4} maxLength={BODY_LIMIT} value={draft.body} onChange={(e) => setDraft({ ...draft, body: e.target.value })} /></Field>
      <AudienceEditor value={draft.audience} onChange={(audience) => setDraft({ ...draft, audience })} />
      <div className="form-actions">
        <button disabled={busy || !draft.body.trim()} onClick={() => onSave(draft)}>Сохранить</button>
        {item.asset_id && <button className="small" disabled={busy} onClick={() => onSave({ clear_asset: true })}>Убрать картинку</button>}
        <button className="small" onClick={onCancel}>Отмена</button>
      </div>
    </div>
  );
}

function ImportButton({ run, busy }) {
  const onFile = (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (file) run(async () => { const items = JSON.parse(await file.text()); return api('/announcements/import', { method: 'POST', body: items }); }, 'Импортировано');
  };
  return (
    <FilePicker accept="application/json" disabled={busy} onChange={onFile} icon={FileUp}
      title="JSON — список объявлений в том же формате, что у API; принимается всё или ничего">Импорт JSON</FilePicker>
  );
}

export default function Announcements() {
  const [open, setOpen] = useState(null);
  const { rows: data, offset, hasMore, setOffset, error, loading, reload } = usePaged((from, limit) => `/announcements?limit=${limit}&offset=${from}`, '', PAGE);
  const { run, message, busy } = useAction(reload);
  const status = (id, value) => run(() => api(`/announcements/${id}/status`, { method: 'POST', body: { status: value } }));
  return (
    <section>
      <PageHeader title="Объявления" description="Разовые и регулярные сообщения всем чатам или выбранным платформам."
        actions={<ImportButton run={run} busy={busy} />} />
      <Help title="Как пользоваться">
        <ol>
          <li>Выберите <b>когда</b> отправлять: «Один раз» (пусто — сразу), «Каждый день», «По дням недели» (например, суббота 10:00), «Раз в месяц» или «Через интервал» (не реже 5 минут).</li>
          <li>Для повторов укажите время и <b>часовой пояс</b> (например, <code>Europe/Moscow</code>). «Сколько раз отправить» — всего запусков; пусто — без предела.</li>
          <li>Отметьте платформы, которым отправлять; если ничего не отмечено — получат все. Можно приложить картинку.</li>
          <li>В списке ниже: «Сейчас» — отправить немедленно, «Пауза», «Править» (текст и аудитория), «Отмена». Если служба была остановлена, опоздавший больше чем на час запуск пропускается, а не высылается пачкой.</li>
        </ol>
      </Help>
      {/* Новое объявление — в начале списка: показываем первую страницу. */}
      <Form onCreated={() => (offset ? setOffset(0) : reload())} />
      <Panel title={`Объявления${data && !offset && !hasMore ? ` (${data.length})` : ''}`}>
        <Status error={error} loading={loading} />
        {data?.length === 0 && !offset && <EmptyState icon={Megaphone} title="Объявлений пока нет">Создайте первое в форме выше.</EmptyState>}
        {data?.length > 0 && (
          <div className="table-wrap">
            <table className="data-table table-stack">
              <thead><tr><th scope="col">Объявление</th><th scope="col">Расписание</th><th scope="col">Следующий запуск</th><th scope="col">Действия</th></tr></thead>
              <tbody>
                {data.flatMap((a) => {
                  const [statusText, tone] = STATUSES[a.status] || [a.status, 'off'];
                  const editable = ['active', 'paused'].includes(a.status);
                  return [
                    <tr key={a.id}>
                      <td className="cell-main" data-label="Объявление"><b>{a.title || `#${a.id}`}</b><div className="clamp-2 muted">{a.body}</div><small>{describeAudience(a.audience)}</small></td>
                      <td data-label="Расписание">{describeSchedule(a)}<div className="cell-meta"><StatusBadge tone={tone}>{statusText}</StatusBadge>
                        <small>запусков {a.runs_done}{a.max_runs ? ` из ${a.max_runs}` : ''}</small></div>
                        {a.kind !== 'once' && <small>{a.timezone}</small>}</td>
                      <td data-label="Следующий запуск">{a.next_run_at ? <>{when(a.next_run_at)}<div><small><TimeAgo value={a.next_run_at} /></small></div></> : <span className="muted">—</span>}</td>
                      <td className="cell-actions"><div className="actions">
                        {editable && <button className="small" onClick={() => setOpen(open === a.id ? null : a.id)} aria-expanded={open === a.id}>Править</button>}
                        <button className="small" disabled={busy} onClick={async () => (await ask({ title: `Отправить «${a.title || `#${a.id}`}» сейчас?`, body: `Получатели: ${describeAudience(a.audience)}. Расписание не изменится.`, confirmText: 'Отправить сейчас' })) && run(() => api(`/announcements/${a.id}/run`, { method: 'POST' }), 'Отправлено')}>Сейчас</button>
                        {a.status === 'active' && <button className="small" disabled={busy} onClick={() => status(a.id, 'paused')}>Пауза</button>}
                        {a.status === 'paused' && <button className="small" disabled={busy} onClick={() => status(a.id, 'active')}>Возобновить</button>}
                        {editable && <button className="small danger" disabled={busy} onClick={async () => (await ask({ title: `Отменить «${a.title || `#${a.id}`}»?`, body: 'Новые запуски не будут выполняться. Вернуть отменённое объявление нельзя.', confirmText: 'Отменить объявление', danger: true })) && status(a.id, 'cancelled')}>Отмена</button>}
                      </div></td>
                    </tr>,
                    open === a.id && <tr key={`e${a.id}`} className="row-editor"><td colSpan="4">
                      <Editor item={a} busy={busy} onCancel={() => setOpen(null)} onSave={async (draft) => { if (await run(() => api(`/announcements/${a.id}`, { method: 'PATCH', body: draft }))) setOpen(null); }} />
                    </td></tr>,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
        {data && <Pager label="Страницы объявлений" noun="Объявления" offset={offset} size={PAGE} shown={data.length} hasMore={hasMore}
          disabled={loading} onChange={setOffset} prev="Новее" next="Старее" />}
        {message && <p className="form-message" role="alert">{message}</p>}
      </Panel>
    </section>
  );
}
