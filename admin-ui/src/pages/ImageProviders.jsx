import { useState } from 'react';
import { api } from '../api.js';
import { Badge, Status, useAction, useData, when } from '../ui.jsx';

const ADAPTERS = {
  cloudflare: 'Cloudflare Workers AI', huggingface: 'Hugging Face', pollinations: 'Pollinations',
  horde: 'AI Horde', openai_compatible: 'OpenAI-совместимый API',
};
const EMPTY = { slug: '', label: '', adapter: 'openai_compatible', base_url: '', model: '', api_key: '', account_id: '',
  width: 1216, height: 832, steps: '', priority: 500, daily_limit: 20, is_enabled: true };

function AddForm({ templates, onCreated, secretsReady }) {
  const [templateId, setTemplateId] = useState('');
  const [f, setF] = useState(EMPTY);
  const { run, message, busy } = useAction(onCreated);
  const template = templates.find((t) => t.id === templateId);
  const set = (patch) => setF((old) => ({ ...old, ...patch }));
  const pick = (id) => {
    setTemplateId(id);
    const t = templates.find((x) => x.id === id);
    if (!t) { setF(EMPTY); return; }
    setF({ ...EMPTY, slug: t.id.slice(0, 29), label: t.label, adapter: t.adapter, base_url: t.base_url || '', model: t.model || '',
      width: t.width, height: t.height, steps: t.steps || '' });
  };
  const submit = async (event) => {
    event.preventDefault();
    const body = { ...f, steps: f.steps ? Number(f.steps) : null, width: Number(f.width), height: Number(f.height),
      priority: Number(f.priority), daily_limit: Number(f.daily_limit) };
    if (await run(() => api('/image-providers', { method: 'POST', body }), 'Генератор добавлен — проверьте его кнопкой «Проверить»')) { setF(EMPTY); setTemplateId(''); }
  };
  return (
    <form className="card" onSubmit={submit}>
      <h3>Добавить генератор</h3>
      <select value={templateId} onChange={(e) => pick(e.target.value)}>
        <option value="">— выберите шаблон —</option>
        {templates.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
      </select>
      {template && <p className="muted">{template.free} {template.signup && <a href={template.signup} target="_blank" rel="noreferrer">Получить ключ</a>}</p>}
      {templateId && (
        <>
          <div className="row">
            <input value={f.slug} onChange={(e) => set({ slug: e.target.value.toLowerCase() })} placeholder="Короткое имя (латиницей)" pattern="[a-z0-9][a-z0-9\-]{1,29}" required />
            <input value={f.label} onChange={(e) => set({ label: e.target.value })} placeholder="Название" />
            <select value={f.adapter} onChange={(e) => set({ adapter: e.target.value })}>
              {Object.entries(ADAPTERS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
            </select>
          </div>
          {(f.adapter === 'openai_compatible' || f.base_url) && <input value={f.base_url} onChange={(e) => set({ base_url: e.target.value })} placeholder="Адрес API, например https://api.example.com/v1" />}
          {f.adapter !== 'pollinations' && f.adapter !== 'horde' && <input value={f.model} onChange={(e) => set({ model: e.target.value })} placeholder="Модель" required />}
          {f.adapter === 'cloudflare' && <input value={f.account_id} onChange={(e) => set({ account_id: e.target.value.trim() })} placeholder="Cloudflare Account ID (32 символа)" required />}
          <input type="password" autoComplete="new-password" value={f.api_key} onChange={(e) => set({ api_key: e.target.value })}
            placeholder={['pollinations', 'horde'].includes(f.adapter) ? 'Ключ (необязательно)' : 'Ключ API'} />
          {!secretsReady && f.api_key && <p className="error">Для хранения ключей задайте SECRETS_KEY или ADMIN_API_KEY (24+ символа) и перезапустите панель.</p>}
          <div className="row">
            <label>Ширина <input type="number" value={f.width} min="256" max="2048" onChange={(e) => set({ width: e.target.value })} /></label>
            <label>Высота <input type="number" value={f.height} min="256" max="2048" onChange={(e) => set({ height: e.target.value })} /></label>
            <label>Шагов <input type="number" value={f.steps} min="1" max="100" onChange={(e) => set({ steps: e.target.value })} /></label>
            <label title="Меньше — раньше. Платные встроенные идут после 1000">Приоритет <input type="number" value={f.priority} min="0" onChange={(e) => set({ priority: e.target.value })} /></label>
            <label>В сутки, не более <input type="number" value={f.daily_limit} min="1" onChange={(e) => set({ daily_limit: e.target.value })} /></label>
          </div>
          <button disabled={busy}>Добавить</button>
        </>
      )}
      {message && <p className="muted">{message}</p>}
    </form>
  );
}

function Custom({ item, reload }) {
  const { run, message, busy } = useAction(reload);
  const [preview, setPreview] = useState(null);
  const [edit, setEdit] = useState(false);
  const [draft, setDraft] = useState({});
  const patch = (body) => run(() => api(`/image-providers/${item.id}`, { method: 'PATCH', body }));
  const test = async () => {
    setPreview(null);
    await run(async () => {
      const result = await api(`/image-providers/${item.id}/test`, { method: 'POST' });
      if (!result.ok) throw new Error(`Ошибка: ${result.error}`);
      setPreview(result.preview);
    }, 'Работает');
  };
  const save = async () => {
    const body = {};
    for (const [k, v] of Object.entries(draft)) body[k] = ['width', 'height', 'steps', 'priority', 'daily_limit'].includes(k) ? (v === '' ? null : Number(v)) : v;
    if (await patch(body)) { setEdit(false); setDraft({}); }
  };
  const field = (name, label, type = 'text') => (
    <label>{label} <input type={type} value={draft[name] ?? item[name] ?? ''} onChange={(e) => setDraft({ ...draft, [name]: e.target.value })} /></label>
  );
  return (
    <div className="card">
      <div className="row">
        <b>{item.label || item.slug}</b> <small className="muted">{ADAPTERS[item.adapter]} · {item.model || '—'} · {item.host}</small>
        <Badge on={item.is_enabled} />
        <small>{item.needs_key ? (item.has_key ? '🔑 ключ задан' : '⚠ нет ключа') : 'без ключа'}</small>
      </div>
      <div className="row">
        <small>приоритет {item.priority} · сегодня {item.daily_used}/{item.daily_limit}</small>
        {item.last_test_at && <small className={item.last_test_ok ? '' : 'error'}>проверка {when(item.last_test_at)}: {item.last_test_ok ? 'успешно' : item.last_test_error}</small>}
      </div>
      <div className="actions">
        <button className="small" disabled={busy} onClick={test}>Проверить</button>
        <button className="small" disabled={busy} onClick={() => patch({ is_enabled: !item.is_enabled })}>{item.is_enabled ? 'Выключить' : 'Включить'}</button>
        <button className="small" onClick={() => setEdit(!edit)}>Настроить</button>
        <button className="small danger" disabled={busy} onClick={() => confirm('Удалить генератор?') && run(() => api(`/image-providers/${item.id}`, { method: 'DELETE' }))}>Удалить</button>
      </div>
      {edit && (
        <div className="row">
          {field('label', 'Название')}{item.adapter === 'openai_compatible' && field('base_url', 'Адрес')}{field('model', 'Модель')}
          {item.adapter === 'cloudflare' && field('account_id', 'Account ID')}
          {field('width', 'Ширина', 'number')}{field('height', 'Высота', 'number')}{field('steps', 'Шагов', 'number')}
          {field('priority', 'Приоритет', 'number')}{field('daily_limit', 'В сутки', 'number')}
          <label>Новый ключ <input type="password" autoComplete="new-password" placeholder="оставьте пустым, чтобы не менять" value={draft.api_key ?? ''} onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} /></label>
          {item.has_key && <button className="small danger" disabled={busy} onClick={() => patch({ clear_key: true })}>Удалить ключ</button>}
          <button disabled={busy} onClick={save}>Сохранить</button>
        </div>
      )}
      {preview && <img alt="Проверочная картинка" src={`data:image/jpeg;base64,${preview}`} style={{ maxWidth: 240, borderRadius: 8 }} />}
      {message && <p className="muted">{message}</p>}
    </div>
  );
}

export default function ImageProviders() {
  const { data, error, loading, reload } = useData('/image-providers');
  const { run, message, busy } = useAction(reload);
  return (
    <section>
      <h2>Генераторы картинок</h2>
      <p className="muted">Бот перебирает генераторы по приоритету: сначала добавленные здесь (бесплатные), затем платные из настроек сервера. Если один недоступен или исчерпал дневной лимит, берётся следующий. Изменения подхватываются без перезапуска.</p>
      <Status error={error} loading={loading} />
      {data && (
        <>
          <div className="grid">
            <div className="card"><h3>Режим</h3><big>{data.mode}</big><small className="muted">ILLUSTRATION_PROVIDER</small></div>
            <div className="card"><h3>Очередь</h3><small>{Object.entries(data.jobs).map(([k, v]) => `${k}: ${v}`).join(' · ') || 'пусто'}</small><br />
              <button className="small" disabled={busy} onClick={() => run(async () => { const r = await api('/image-queue/retry', { method: 'POST' }); return r; }, 'Задания возвращены в очередь')}>Повторить остановленные</button></div>
          </div>
          <h3>Добавленные ({data.custom.length})</h3>
          {data.custom.map((item) => <Custom key={item.id} item={item} reload={reload} />)}
          {data.custom.length === 0 && <p className="muted">Пока нет. Добавьте бесплатный генератор ниже.</p>}
          <AddForm templates={data.templates} onCreated={reload} secretsReady={data.secrets_ready} />
          <h3>Платные (из настроек сервера)</h3>
          <table><thead><tr><th>Сервис</th><th>Модель</th><th>Ключ</th><th>Сегодня</th><th>Пауза</th></tr></thead><tbody>
            {data.builtin.map((b) => (
              <tr key={b.name}><td>{b.name}</td><td>{b.model}</td><td><Badge on={b.configured} yes="задан" no="нет" /></td>
                <td>{b.daily_used}/{b.daily_limit}</td><td>{b.blocked_until ? `${when(b.blocked_until)} (${b.error_code})` : '—'}</td></tr>
            ))}
          </tbody></table>
          <p className="muted">Ключи платных сервисов задаются в <code>.env</code> (OPENAI_API_KEY, GEMINI_API_KEY, …). Месячный бюджет: ${data.monthly_budget_usd}.</p>
        </>
      )}
      {message && <p className="muted">{message}</p>}
    </section>
  );
}
