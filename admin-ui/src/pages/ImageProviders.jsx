import { useState } from 'react';
import { CircleCheck, CircleHelp, CircleX, KeyRound, RotateCcw, TriangleAlert, WandSparkles } from 'lucide-react';
import { ask } from '../feedback.jsx';
import { api } from '../api.js';
import { Help, Status, useAction, useData, when } from '../ui.jsx';
import { DataTable, EmptyState, Field, PageHeader, Panel, Stat, StatusBadge, Switch, TimeAgo, formatNumber } from '../components.jsx';

const ADAPTERS = {
  cloudflare: 'Cloudflare Workers AI', huggingface: 'Hugging Face', pollinations: 'Pollinations',
  horde: 'AI Horde', openai_compatible: 'OpenAI-совместимый API',
};
const BILLING = {
  unknown: 'Цена неизвестна — запуск запрещён',
  metered: 'Платный API / бесплатные кредиты аккаунта',
  free_only: 'Анонимный Horde / Pollinations без ключа',
};
const JOBS = { queued: 'в очереди', running: 'выполняется', done: 'готово', failed: 'ошибки', stopped: 'остановлено' };
const EMPTY = { slug: '', label: '', adapter: 'openai_compatible', base_url: '', model: '', api_key: '', account_id: '',
  width: 1216, height: 832, steps: '', priority: 500, daily_limit: 20, is_enabled: true,
  billing_mode: 'unknown', reservation_usd: '0' };

/** Пункт проверки готовности: ok — true/false/null (не проверено), warn — предупреждение. */
function Check({ ok, warn, children }) {
  const [Icon, tone] = warn ? [TriangleAlert, 'warning'] : ok === true ? [CircleCheck, 'ok'] : ok === false ? [CircleX, 'danger'] : [CircleHelp, 'muted'];
  return <li className={`check-item check-${tone}`}><Icon size={17} aria-hidden="true" /><span>{children}</span></li>;
}

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
      width: t.width, height: t.height, steps: t.steps || '',
      billing_mode: ['horde', 'pollinations'].includes(t.adapter) ? 'free_only' : 'unknown' });
  };
  const submit = async (event) => {
    event.preventDefault();
    const body = { ...f, steps: f.steps ? Number(f.steps) : null, width: Number(f.width), height: Number(f.height),
      priority: Number(f.priority), daily_limit: Number(f.daily_limit) };
    if (await run(() => api('/image-providers', { method: 'POST', body }), 'Генератор добавлен — проверьте его кнопкой «Проверить»')) { setF(EMPTY); setTemplateId(''); }
  };
  const keyless = ['pollinations', 'horde'].includes(f.adapter);
  return (
    <form className="panel" onSubmit={submit}>
      <div className="panel-head"><h2>Добавить генератор</h2></div>
      <Field label="Шаблон сервиса" hint="Шаблон заполнит адрес, модель и размеры; их можно поправить.">
        <select value={templateId} onChange={(e) => pick(e.target.value)}>
          <option value="">— выберите шаблон —</option>
          {templates.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
        </select>
      </Field>
      {template && (
        <Help title={`Как подключить: ${template.label}`} open>
          <p>{template.free} {template.signup && <a href={template.signup} target="_blank" rel="noreferrer">Открыть сайт сервиса ↗</a>}</p>
          <ol>{(template.howto || []).map((step, i) => <li key={i}>{step}</li>)}</ol>
          <p className="muted">Названия пунктов в кабинете сервиса могут отличаться: ориентируйтесь на смысл. Условия бесплатного тарифа уточняйте на сайте сервиса.</p>
        </Help>
      )}
      {templateId && (
        <>
          <div className="form-section">
            <div className="form-grid">
              <Field label="Короткое имя" hint="Латиница, цифры и дефис."><input value={f.slug} onChange={(e) => set({ slug: e.target.value.toLowerCase() })} placeholder="Короткое имя (латиницей)" pattern="[a-z0-9][a-z0-9\-]{1,29}" required /></Field>
              <Field label="Название"><input value={f.label} onChange={(e) => set({ label: e.target.value })} placeholder="Название" /></Field>
              <Field label="Тип API"><select value={f.adapter} onChange={(e) => set({ adapter: e.target.value })}>
                {Object.entries(ADAPTERS).map(([id, text]) => <option key={id} value={id}>{text}</option>)}
              </select></Field>
            </div>
            {(f.adapter === 'openai_compatible' || f.base_url) && <Field label="Адрес API"><input value={f.base_url} onChange={(e) => set({ base_url: e.target.value })} placeholder="Адрес API, например https://api.example.com/v1" /></Field>}
            <div className="form-grid">
              {!keyless && <Field label="Модель"><input value={f.model} onChange={(e) => set({ model: e.target.value })} placeholder="Модель" required /></Field>}
              {f.adapter === 'cloudflare' && <Field label="Account ID"><input value={f.account_id} onChange={(e) => set({ account_id: e.target.value.trim() })} placeholder="Cloudflare Account ID (32 символа)" required /></Field>}
              <Field label={keyless ? 'Ключ (необязательно)' : 'Ключ API'} hint="Хранится на сервере в зашифрованном виде.">
                <input type="password" autoComplete="new-password" value={f.api_key} onChange={(e) => set({ api_key: e.target.value })} placeholder={keyless ? 'Ключ (необязательно)' : 'Ключ API'} />
              </Field>
            </div>
            {!secretsReady && f.api_key && <p className="callout callout-danger"><CircleX size={16} aria-hidden="true" />Для хранения ключей задайте SECRETS_KEY или ADMIN_API_KEY (24+ символа) и перезапустите панель.</p>}
          </div>
          <div className="form-section">
            <h3 className="form-section-title">Оплата</h3>
            <div className="form-grid form-grid-wide">
              <Field label="Режим оплаты"><select value={f.billing_mode} onChange={(e) => set({ billing_mode: e.target.value })}>
                {Object.entries(BILLING).map(([id, text]) => <option key={id} value={id}>{text}</option>)}
              </select></Field>
              {f.billing_mode === 'metered' && <Field label="Максимальная стоимость запроса, USD"><input type="number" min="0.10" max="1000" step="0.0001" value={f.reservation_usd} onChange={(e) => set({ reservation_usd: e.target.value })} required /></Field>}
            </div>
            <p className="field-hint">Укажите верхнюю границу цены для выбранной модели и размеров. Каждый запрос, включая проверку и таймаут, резервируется в общем месячном бюджете. Бесплатный кредит платного аккаунта не гарантирует нулевую стоимость.</p>
          </div>
          <div className="form-section">
            <h3 className="form-section-title">Размер и лимиты</h3>
            <div className="form-grid">
              <Field label="Ширина"><input type="number" value={f.width} min="256" max="2048" onChange={(e) => set({ width: e.target.value })} /></Field>
              <Field label="Высота"><input type="number" value={f.height} min="256" max="2048" onChange={(e) => set({ height: e.target.value })} /></Field>
              <Field label="Шагов"><input type="number" value={f.steps} min="1" max="100" onChange={(e) => set({ steps: e.target.value })} /></Field>
              <Field label="Приоритет" hint="Меньше — раньше. Платные встроенные идут после 1000."><input type="number" value={f.priority} min="0" onChange={(e) => set({ priority: e.target.value })} /></Field>
              <Field label="В сутки, не более"><input type="number" value={f.daily_limit} min="1" onChange={(e) => set({ daily_limit: e.target.value })} /></Field>
            </div>
          </div>
          <div className="form-actions"><button disabled={busy}>Добавить</button>{message && <span className="form-message" role="alert">{message}</span>}</div>
        </>
      )}
      {!templateId && message && <p className="form-message" role="alert">{message}</p>}
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
  const field = (name, text, type = 'text') => (
    <Field label={text}><input type={type} step={name === 'reservation_usd' ? '0.0001' : undefined} value={draft[name] ?? item[name] ?? ''} onChange={(e) => setDraft({ ...draft, [name]: e.target.value })} /></Field>
  );
  const name = item.label || item.slug;
  return (
    <article className="item-card">
      <div className="item-head">
        <div className="item-title"><b>{name}</b><small>{ADAPTERS[item.adapter]} · {item.model || '—'} · {item.host}</small></div>
        <Switch checked={item.is_enabled} disabled={busy} label={`Генератор «${name}» включён`} onChange={(value) => patch({ is_enabled: value })} />
      </div>
      <div className="item-meta">
        <span><KeyRound size={13} aria-hidden="true" /> {item.needs_key ? (item.has_key ? 'ключ задан' : <span className="error">нет ключа</span>) : 'без ключа'}</span>
        <span>приоритет {item.priority}</span>
        <span>сегодня {item.daily_used} из {item.daily_limit}</span>
        {item.pricing_ready ? <span>резерв ${item.reservation_usd} за запрос</span> : <span className="error">цена неизвестна — генерация и проверка запрещены</span>}
        {item.last_test_at && <span className={item.last_test_ok ? '' : 'error'}>проверка <TimeAgo value={item.last_test_at} />: {item.last_test_ok ? 'успешно' : item.last_test_error}</span>}
      </div>
      <div className="item-actions">
        <button className="small" disabled={busy || !item.pricing_ready} onClick={test}>Проверить</button>
        <button className="small" aria-expanded={edit} onClick={() => setEdit(!edit)}>Настроить</button>
        <button className="small danger" disabled={busy} onClick={async () => (await ask({ title: `Удалить генератор «${name}»?`, body: 'Его ключ и настройки будут удалены. Статистика генераций сохранится.', confirmText: 'Удалить', danger: true })) && run(() => api(`/image-providers/${item.id}`, { method: 'DELETE' }))}>Удалить</button>
        {message && <span className="form-message" role="alert">{message}</span>}
      </div>
      {edit && (
        <div className="inline-editor">
          <div className="form-grid">
            {field('label', 'Название')}{item.adapter === 'openai_compatible' && field('base_url', 'Адрес')}{field('model', 'Модель')}
            {item.adapter === 'cloudflare' && field('account_id', 'Account ID')}
            {field('width', 'Ширина', 'number')}{field('height', 'Высота', 'number')}{field('steps', 'Шагов', 'number')}
            {field('priority', 'Приоритет', 'number')}{field('daily_limit', 'В сутки', 'number')}
            <Field label="Режим оплаты"><select value={draft.billing_mode ?? item.billing_mode} onChange={(e) => setDraft({ ...draft, billing_mode: e.target.value })}>
              {Object.entries(BILLING).map(([id, text]) => <option key={id} value={id}>{text}</option>)}
            </select></Field>
            {field('reservation_usd', 'Макс. стоимость запроса, USD', 'number')}
            <Field label="Новый ключ" hint="Пусто — не менять."><input type="password" autoComplete="new-password" placeholder="оставьте пустым, чтобы не менять" value={draft.api_key ?? ''} onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} /></Field>
          </div>
          <div className="form-actions">
            <button disabled={busy} onClick={save}>Сохранить</button>
            {item.has_key && <button className="small danger" disabled={busy} onClick={async () => (await ask({ title: 'Удалить сохранённый ключ?', body: 'Генератор не сможет работать, пока вы не зададите новый ключ.', confirmText: 'Удалить ключ', danger: true })) && patch({ clear_key: true })}>Удалить ключ</button>}
            <button className="small" onClick={() => { setEdit(false); setDraft({}); }}>Отмена</button>
          </div>
        </div>
      )}
      {preview && <img className="test-preview" alt="Проверочная картинка" src={`data:image/jpeg;base64,${preview}`} />}
    </article>
  );
}

export default function ImageProviders() {
  const { data, error, loading, reload } = useData('/image-providers');
  const { run, message, busy } = useAction(reload);
  const jobs = data ? Object.entries(data.jobs) : [];
  return (
    <section>
      <PageHeader title="Генераторы картинок" description="Бот пробует генераторы по приоритету или по статистике. Всем платным запросам, включая проверки, нужен резерв в общем месячном бюджете; при неизвестной цене запуск запрещён." />
      <Help title="Как это работает и что нужно для запуска">
        <p>У бесплатных уровней аккаунтов возможны платные превышения квоты: для таких сервисов задайте максимальную цену запроса с запасом и ограничение расходов в кабинете провайдера. Нулевой резерв разрешён только анонимным Horde/Pollinations на стандартном адресе без ключа. При смене модели или размера обновите верхнюю границу стоимости.</p>
        <p>Каждой картинке нужен генератор. Бот пробует их по очереди: сначала добавленные здесь (по приоритету — меньшее число раньше), потом платные из <code>.env</code>. Если генератор вернул ошибку или исчерпал дневной лимит, берётся следующий.</p>
        <ol>
          <li><b>Подготовьте сервер</b> (один раз): задайте <code>SECRETS_KEY</code> или <code>ADMIN_API_KEY</code> (24+ символа) — ими шифруются ключи; запустите локальную нейросеть описания сцен: <code>docker compose --profile prayers up -d</code>. Это не даёт тексту стиха уйти во внешний сервис: наружу идёт только описание картинки.</li>
          <li><b>Добавьте генератор</b>: выберите шаблон ниже, следуйте подсказке, вставьте ключ.</li>
          <li><b>Нажмите «Проверить»</b>: появится пробная картинка. Если видите ошибку — подсказка по коду есть ниже.</li>
          <li><b>Настройте порядок и лимит</b>: «Приоритет» (меньше — раньше) и «В сутки, не более». Самые надёжные ставьте первыми, медленные (AI Horde) — последними.</li>
        </ol>
        <p>Коды ошибок: <code>auth</code> — неверный ключ или Account ID; <code>rate_limited</code> — квота исчерпана, подождите или снизьте лимит; <code>response_invalid</code>/<code>no_image</code> — неверный адрес или модель; <code>remote_timeout</code> — сервис перегружен; <code>scene_unavailable</code> — не запущена локальная нейросеть.</p>
      </Help>
      <Status error={error} loading={loading} />
      {data && (
        <>
          <div className="split">
            <Panel title="Готовность">
              <ul className="check-list">
                <Check ok={data.secrets_ready}>Шифрование ключей {data.secrets_ready ? 'настроено' : <>не настроено: задайте <code>SECRETS_KEY</code> или <code>ADMIN_API_KEY</code> (24+ символа) и пересоздайте службу admin</>}</Check>
                {data.secrets_ready && !data.secrets_dedicated && <Check warn>Ключи шифруются ключом <code>ADMIN_API_KEY</code>: при его смене все сохранённые ключи генераторов придётся ввести заново. Задайте отдельный <code>SECRETS_KEY</code> в <code>.env</code> (и больше не меняйте его).</Check>}
                <Check ok={data.planner_ready}>Локальная нейросеть описания сцен {data.planner_ready === true ? 'работает' : data.planner_ready === false ? <>не отвечает: без неё бесплатные генераторы пропускаются. Запустите <code>docker compose --profile prayers up -d</code></> : 'не проверена'}</Check>
                <Check ok={data.custom.some((c) => c.is_enabled)}>Включён хотя бы один добавленный генератор</Check>
                <Check ok={data.custom.some((c) => c.last_test_ok) || null}>Успешная проверка выполнена</Check>
              </ul>
            </Panel>
            <div className="stat-stack">
              <Stat label="Режим" value={data.mode} hint="ILLUSTRATION_PROVIDER" />
              <Stat label="Месячный бюджет" value={`$${data.monthly_budget_usd}`} hint="на платные запросы" />
              <div className="stat">
                <span className="stat-label">Очередь заданий</span>
                <span className="job-list">{jobs.length ? jobs.map(([k, v]) => <span key={k}><b>{formatNumber(v)}</b> {JOBS[k] || k}</span>) : <span className="muted">пусто</span>}</span>
                <button className="small" disabled={busy} onClick={() => run(() => api('/image-queue/retry', { method: 'POST' }), 'Задания возвращены в очередь')}><RotateCcw size={14} aria-hidden="true" />Повторить остановленные</button>
                {message && <span className="form-message" role="alert">{message}</span>}
              </div>
            </div>
          </div>
          <Panel title={`Добавленные (${data.custom.length})`}>
            {data.custom.length === 0 ? <EmptyState icon={WandSparkles} title="Пока нет">Добавьте бесплатный генератор в форме ниже.</EmptyState>
              : <div className="item-list">{data.custom.map((item) => <Custom key={item.id} item={item} reload={reload} />)}</div>}
          </Panel>
          <AddForm templates={data.templates} onCreated={reload} secretsReady={data.secrets_ready} />
          <Panel title="Платные (из настроек сервера)">
            <Help title="Как включить платные генераторы">
              <p>Ключи задаются только в файле <code>.env</code> на сервере (панель их не меняет): <code>OPENAI_API_KEY</code>, <code>GEMINI_API_KEY</code>, <code>BFL_API_KEY</code>, <code>IDEOGRAM_API_KEY</code>, <code>STABILITY_API_KEY</code>. После правки пересоздайте службу: <code>docker compose up -d --no-deps --force-recreate illustrator</code>. Режим <code>ILLUSTRATION_PROVIDER=auto</code> включает перебор всех ключей, <code>manual</code> — работают только генераторы из панели.</p>
            </Help>
            <DataTable caption="Платные генераторы" rows={data.builtin} rowKey={(b) => b.name} stack columns={[
              { key: 'name', header: 'Сервис', render: (b) => <code>{b.name}</code> },
              { key: 'model', header: 'Модель' },
              { key: 'key', header: 'Ключ', render: (b) => <StatusBadge tone={b.configured ? 'ok' : 'off'}>{b.configured ? 'задан' : 'нет'}</StatusBadge> },
              { key: 'today', header: 'Сегодня', render: (b) => `${b.daily_used} из ${b.daily_limit}` },
              { key: 'pause', header: 'Пауза', render: (b) => (b.blocked_until ? <>{when(b.blocked_until)} <code>{b.error_code}</code></> : '—') },
            ]} />
          </Panel>
        </>
      )}
    </section>
  );
}
