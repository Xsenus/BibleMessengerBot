import { useState } from 'react';
import { ArrowDown, ArrowUp, CircleCheck, CircleX, Network as NetworkIcon, X } from 'lucide-react';
import { api } from '../api.js';
import { Help, Status, useAction, useData } from '../ui.jsx';
import { EmptyState, Field, PageHeader, Panel, Switch, TimeAgo } from '../components.jsx';

const STRATEGIES = {
  failover: 'По порядку (резервные — при сбое)',
  auto: 'Автоматически: самый быстрый и надёжный',
  round_robin: 'По кругу (распределять нагрузку)',
};
const SCHEMES = ['http', 'https', 'socks5', 'socks5h'];
const EMPTY = { name: '', scheme: 'http', host: '', port: '', user: '', password: '', url: '' };

function ProxyForm({ onCreated, secretsReady }) {
  const [f, setF] = useState(EMPTY);
  const { run, message, busy } = useAction(onCreated);
  const set = (patch) => setF((old) => ({ ...old, ...patch }));
  const submit = async (event) => {
    event.preventDefault();
    if (await run(() => api('/net/proxies', { method: 'POST', body: f.url ? { name: f.name, url: f.url } : { name: f.name, scheme: f.scheme, host: f.host, port: Number(f.port), user: f.user, password: f.password } }), 'Прокси добавлен — проверьте его кнопкой «Проверить»')) {
      setF(EMPTY);
    }
  };
  return (
    <form className="panel" onSubmit={submit}>
      <div className="panel-head"><h3>Добавить прокси</h3></div>
      {!secretsReady && <p className="callout callout-danger"><CircleX size={16} aria-hidden="true" /><span>Для хранения адресов задайте <code>SECRETS_KEY</code> или <code>ADMIN_API_KEY</code> (24+ символа) и пересоздайте службу admin.</span></p>}
      <div className="form-grid form-grid-wide">
        <Field label="Название"><input value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder="Название (например, NL-1)" required /></Field>
        <Field label="Готовый адрес" hint="Если заполнен, поля ниже не нужны.">
          <input value={f.url} onChange={(e) => set({ url: e.target.value })} placeholder="socks5://логин:пароль@host:1080" /></Field>
      </div>
      {!f.url && (
        <div className="form-grid">
          <Field label="Протокол" hint="Для Telegram — HTTP или SOCKS5. SOCKS5h разрешает имена через прокси."><select value={f.scheme} onChange={(e) => set({ scheme: e.target.value })}>{SCHEMES.map((s) => <option key={s}>{s}</option>)}</select></Field>
          <Field label="Хост"><input value={f.host} onChange={(e) => set({ host: e.target.value })} placeholder="Хост" required /></Field>
          <Field label="Порт"><input type="number" value={f.port} onChange={(e) => set({ port: e.target.value })} placeholder="Порт" min="1" max="65535" required /></Field>
          <Field label="Логин"><input value={f.user} onChange={(e) => set({ user: e.target.value })} placeholder="Логин (необязательно)" autoComplete="off" /></Field>
          <Field label="Пароль"><input type="password" value={f.password} onChange={(e) => set({ password: e.target.value })} placeholder="Пароль" autoComplete="new-password" /></Field>
        </div>
      )}
      <div className="form-actions"><button disabled={busy || !f.name}>Добавить</button>{message && <span className="form-message" role="status">{message}</span>}</div>
    </form>
  );
}

function ProxyRow({ proxy, services, reload }) {
  const { run, message, busy } = useAction(reload);
  const [service, setService] = useState('telegram');
  const [result, setResult] = useState(null);
  const test = () => run(async () => {
    const r = await api(`/net/proxies/${proxy.id}/test`, { method: 'POST', body: { service } });
    setResult(r);
  }, 'Проверено');
  return (
    <tr>
      <td data-label="Прокси"><b>{proxy.name}</b><div><small><code>{proxy.scheme}://{proxy.host}</code></small></div></td>
      <td data-label="Работает"><Switch checked={proxy.is_enabled} disabled={busy} label={`Прокси «${proxy.name}» включён`}
        onChange={(value) => run(() => api(`/net/proxies/${proxy.id}`, { method: 'PATCH', body: { is_enabled: value } }))} />
        {proxy.last_test_at && <div><small className={proxy.last_test_ok ? '' : 'error'}>проверка <TimeAgo value={proxy.last_test_at} />: {proxy.last_test_ok ? 'ok' : proxy.last_test_error}</small></div>}</td>
      <td className="cell-actions"><div className="actions">
        <select className="select-sm" value={service} aria-label={`Проверить «${proxy.name}» на сервисе`} onChange={(e) => setService(e.target.value)}>{services.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}</select>
        <button className="small" disabled={busy} onClick={test}>Проверить</button>
        <button className="small danger" disabled={busy} onClick={() => confirm(`Удалить прокси «${proxy.name}»? Он пропадёт из всех маршрутов.`) && run(() => api(`/net/proxies/${proxy.id}`, { method: 'DELETE' }))}>Удалить</button>
      </div>
        {result && <div className={`probe ${result.ok ? 'probe-ok' : 'probe-fail'}`}>{result.ok ? <CircleCheck size={14} aria-hidden="true" /> : <CircleX size={14} aria-hidden="true" />}
          {result.ok ? `работает (${result.ms} мс, HTTP ${result.status})` : result.error}</div>}
        {!result && message && <small className="muted">{message}</small>}
      </td>
    </tr>
  );
}

function Service({ service, proxies, reload }) {
  const [draft, setDraft] = useState(null);
  const [probe, setProbe] = useState(null);
  const { run, message, busy } = useAction(reload);
  const current = draft || { strategy: service.strategy, attempts: service.attempts, routes: service.routes.map((r) => ({ proxy_id: r.proxy_id, enabled: r.enabled, name: r.name })) };
  const edit = (patch) => setDraft({ ...current, ...patch });
  const move = (i, d) => { const routes = [...current.routes]; const j = i + d; if (j < 0 || j >= routes.length) return; [routes[i], routes[j]] = [routes[j], routes[i]]; edit({ routes }); };
  const used = new Set(current.routes.map((r) => r.proxy_id));
  const addable = [...(used.has(null) ? [] : [{ id: null, name: 'Напрямую' }]), ...proxies.filter((p) => !used.has(p.id))];
  const save = async () => { if (await run(() => api(`/net/services/${service.key}`, { method: 'PUT', body: { strategy: current.strategy, attempts: Number(current.attempts), routes: current.routes.map((r) => ({ proxy_id: r.proxy_id, enabled: r.enabled })) } }), 'Сохранено')) setDraft(null); };
  const check = () => run(async () => setProbe(await api(`/net/services/${service.key}/probe`, { method: 'POST' })), 'Проверено');
  const health = (r) => (r.health ? `успехов ${r.health.successes}, ошибок ${r.health.failures}${r.health.latency_ms ? `, ${Math.round(r.health.latency_ms)} мс` : ''}${r.health.open_seconds ? ` · пауза ${r.health.open_seconds} с` : ''}` : 'нет данных');
  return (
    <section className="item-card" aria-label={service.label}>
      <div className="item-head">
        <div className="item-title"><b>{service.label}</b><small>{service.target}{!service.configured && ' · по умолчанию — напрямую'}</small></div>
        {draft && <span className="badge pending">есть несохранённые изменения</span>}
      </div>
      <div className="form-grid form-grid-wide">
        <Field label="Стратегия"><select value={current.strategy} onChange={(e) => edit({ strategy: e.target.value })}>{Object.entries(STRATEGIES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
        <Field label="Попыток на запрос"><input type="number" min="1" max="10" value={current.attempts} onChange={(e) => edit({ attempts: e.target.value })} /></Field>
      </div>
      {current.routes.length > 0 && (
        <ol className="route-list">
          {current.routes.map((r, i) => (
            <li key={String(r.proxy_id)}>
              <span className="route-index">{i + 1}</span>
              <label className="checkbox-line"><input type="checkbox" checked={r.enabled} onChange={() => { const routes = [...current.routes]; routes[i] = { ...r, enabled: !r.enabled }; edit({ routes }); }} /> {r.name}</label>
              <small className="muted route-health">{!draft && service.routes[i] ? health(service.routes[i]) : ''}</small>
              <span className="route-buttons">
                <button className="icon-button" type="button" aria-label={`Переместить «${r.name}» выше`} disabled={i === 0} onClick={() => move(i, -1)}><ArrowUp size={15} /></button>
                <button className="icon-button" type="button" aria-label={`Переместить «${r.name}» ниже`} disabled={i === current.routes.length - 1} onClick={() => move(i, 1)}><ArrowDown size={15} /></button>
                <button className="icon-button danger" type="button" aria-label={`Убрать маршрут «${r.name}»`} onClick={() => edit({ routes: current.routes.filter((_, j) => j !== i) })}><X size={15} /></button>
              </span>
            </li>
          ))}
        </ol>
      )}
      <div className="item-actions">
        {addable.length > 0 && (
          <select className="select-sm" value="" aria-label="Добавить маршрут" onChange={(e) => { const p = addable.find((a) => String(a.id) === e.target.value); if (p) edit({ routes: [...current.routes, { proxy_id: p.id, enabled: true, name: p.name }] }); }}>
            <option value="">+ добавить маршрут…</option>{addable.map((p) => <option key={String(p.id)} value={String(p.id)}>{p.name}</option>)}
          </select>
        )}
        <button disabled={busy || !draft} onClick={save}>Сохранить</button>
        {draft && <button className="small" onClick={() => setDraft(null)}>Отмена</button>}
        <button className="small" disabled={busy} onClick={check}>Проверить маршруты</button>
        {message && <small className="muted">{message}</small>}
      </div>
      {probe && <ul className="probe-list">{probe.map((p) => <li key={p.route} className={`probe ${p.ok ? 'probe-ok' : 'probe-fail'}`}>
        {p.ok ? <CircleCheck size={14} aria-hidden="true" /> : <CircleX size={14} aria-hidden="true" />}{p.label}: {p.ok ? `${p.ms} мс (HTTP ${p.status})` : p.error}</li>)}</ul>}
    </section>
  );
}

export default function Network() {
  const { data, error, loading, reload } = useData('/net');
  return (
    <section>
      <PageHeader title="Сеть и прокси" description="Через какие пути бот обращается к мессенджерам и внешним сервисам." />
      <Help title="Как это работает и как настроить">
        <p>Для каждого внешнего сервиса (Telegram, MAX, ВКонтакте, YouTube, RuTube, генераторы картинок…) можно задать свой список путей: напрямую и через один или несколько прокси. Бот сам выбирает рабочий путь для каждого запроса и переключается, если путь перестал работать.</p>
        <ol>
          <li><b>Добавьте прокси</b> ниже (HTTP/HTTPS или SOCKS5, с логином и паролем или без) и нажмите «Проверить» — выберите сервис, до которого нужно достучаться.</li>
          <li><b>Назначьте прокси сервисам</b>: в карточке сервиса добавьте маршруты (стрелки меняют порядок) и выберите стратегию.</li>
          <li><b>Стратегии</b>: «по порядку» — всегда первый рабочий, остальные резервные; «автоматически» — самый быстрый и надёжный по недавним запросам; «по кругу» — распределять нагрузку.</li>
          <li><b>Автоматическое переключение</b>: после 3 неудач подряд путь на время отключается (30 с, затем дольше, до 15 мин) и потом получает пробный запрос. Если все пути сломаны, пробуется тот, что раньше вернётся. Отправка сообщений не дублируется: после таймаута ответа запрос не повторяется другим путём, а при ошибке соединения — повторяется.</li>
          <li>Изменения подхватываются службами в течение 30 секунд, перезапуск не нужен. Адреса прокси хранятся зашифрованными.</li>
        </ol>
        <p className="muted">Без настроек все сервисы работают напрямую (с учётом переменной окружения HTTPS_PROXY, если она задана на сервере).</p>
      </Help>
      <Status error={error} loading={loading} />
      {data && (
        <>
          <Panel title="Сервисы">
            <div className="item-list">{data.services.map((s) => <Service key={s.key} service={s} proxies={data.proxies.filter((p) => p.is_enabled)} reload={reload} />)}</div>
          </Panel>
          <Panel title={`Прокси (${data.proxies.length})`}>
            {data.proxies.length === 0 ? <EmptyState icon={NetworkIcon} title="Прокси пока нет">Без прокси все сервисы работают напрямую.</EmptyState> : (
              <div className="table-wrap">
                <table className="data-table table-stack">
                  <thead><tr><th scope="col">Прокси</th><th scope="col">Работает</th><th scope="col">Проверка</th></tr></thead>
                  <tbody>{data.proxies.map((p) => <ProxyRow key={p.id} proxy={p} services={data.services} reload={reload} />)}</tbody>
                </table>
              </div>
            )}
          </Panel>
          <ProxyForm onCreated={reload} secretsReady={data.secrets_ready} />
        </>
      )}
    </section>
  );
}
