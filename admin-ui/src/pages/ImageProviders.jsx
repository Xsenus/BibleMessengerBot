import { useState } from 'react';
import { api } from '../api.js';
import { Badge, Help, Status, useAction, useData, when } from '../ui.jsx';

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
      {template && (
        <Help title={`Как подключить: ${template.label}`} open>
          <p>{template.free} {template.signup && <a href={template.signup} target="_blank" rel="noreferrer">Открыть сайт сервиса</a>}</p>
          <ol>{(template.howto || []).map((step, i) => <li key={i}>{step}</li>)}</ol>
          <p className="muted">Названия пунктов в кабинете сервиса могут отличаться: ориентируйтесь на смысл. Условия бесплатного тарифа уточняйте на сайте сервиса.</p>
        </Help>
      )}
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
      <Help title="Как это работает и что нужно для запуска">
        <p>Каждой картинке нужен генератор. Бот пробует их по очереди: сначала добавленные здесь (по приоритету — меньшее число раньше), потом платные из <code>.env</code>. Если генератор вернул ошибку или исчерпал дневной лимит, берётся следующий.</p>
        <ol>
          <li><b>Подготовьте сервер</b> (один раз): задайте <code>SECRETS_KEY</code> или <code>ADMIN_API_KEY</code> (24+ символа) — ими шифруются ключи; запустите локальную нейросеть описания сцен: <code>docker compose --profile prayers up -d</code>. Это не даёт тексту стиха уйти во внешний сервис: наружу идёт только описание картинки.</li>
          <li><b>Добавьте генератор</b>: выберите шаблон ниже, следуйте подсказке, вставьте ключ.</li>
          <li><b>Нажмите «Проверить»</b>: появится пробная картинка. Если видите ошибку — подсказка по коду есть в справке ниже.</li>
          <li><b>Настройте порядок и лимит</b>: «Приоритет» (меньше — раньше) и «В сутки, не более». Самые надёжные ставьте первыми, медленные (AI Horde) — последними.</li>
        </ol>
        <p>Коды ошибок: <code>auth</code> — неверный ключ или Account ID; <code>rate_limited</code> — квота исчерпана, подождите или снизьте лимит; <code>response_invalid</code>/<code>no_image</code> — неверный адрес или модель; <code>remote_timeout</code> — сервис перегружен; <code>scene_unavailable</code> — не запущена локальная нейросеть.</p>
      </Help>
      <Status error={error} loading={loading} />
      {data && (
        <>
          <div className="card">
            <h3>Готовность</h3>
            <div className="check">{data.secrets_ready ? '✅' : '❌'} Шифрование ключей {data.secrets_ready ? 'настроено' : <>не настроено: задайте <code>SECRETS_KEY</code> или <code>ADMIN_API_KEY</code> (24+ символа) и пересоздайте службу admin</>}</div>
            <div className="check">{data.planner_ready === true ? '✅' : data.planner_ready === false ? '❌' : '❔'} Локальная нейросеть описания сцен {data.planner_ready === true ? 'работает' : data.planner_ready === false ? <>не отвечает: без неё бесплатные генераторы пропускаются. Запустите <code>docker compose --profile prayers up -d</code></> : 'не проверена'}</div>
            <div className="check">{data.custom.some((c) => c.is_enabled) ? '✅' : '❌'} Включён хотя бы один добавленный генератор</div>
            <div className="check">{data.custom.some((c) => c.last_test_ok) ? '✅' : '❔'} Успешная проверка выполнена</div>
          </div>
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
          <Help title="Как включить платные генераторы">
            <p>Ключи задаются только в файле <code>.env</code> на сервере (панель их не меняет): <code>OPENAI_API_KEY</code>, <code>GEMINI_API_KEY</code>, <code>BFL_API_KEY</code>, <code>IDEOGRAM_API_KEY</code>, <code>STABILITY_API_KEY</code>. После правки пересоздайте службу: <code>docker compose up -d --no-deps --force-recreate illustrator</code>. Режим <code>ILLUSTRATION_PROVIDER=auto</code> включает перебор всех ключей, <code>manual</code> — работают только генераторы из панели.</p>
          </Help>
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
