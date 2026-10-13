import { useState } from 'react';
import { api } from '../api.js';
import { Badge, Help, Status, useAction, useData, when } from '../ui.jsx';

const STRATEGIES = {
  failover: 'По порядку (резервные — при сбое)',
  auto: 'Автоматически: самый быстрый и надёжный',
  round_robin: 'По кругу (распределять нагрузку)',
};
const SCHEMES = ['http', 'https', 'socks5', 'socks5h'];

function ProxyForm({ onCreated, secretsReady }) {
  const [f, setF] = useState({ name: '', scheme: 'http', host: '', port: '', user: '', password: '', url: '' });
  const { run, message, busy } = useAction(onCreated);
  const set = (patch) => setF((old) => ({ ...old, ...patch }));
  const submit = async (event) => {
    event.preventDefault();
    if (await run(() => api('/net/proxies', { method: 'POST', body: f.url ? { name: f.name, url: f.url } : { name: f.name, scheme: f.scheme, host: f.host, port: Number(f.port), user: f.user, password: f.password } }), 'Прокси добавлен — проверьте его кнопкой «Проверить»')) {
      setF({ name: '', scheme: 'http', host: '', port: '', user: '', password: '', url: '' });
    }
  };
  return (
    <form className="card" onSubmit={submit}>
      <h3>Добавить прокси</h3>
      {!secretsReady && <p className="error">Для хранения адресов задайте <code>SECRETS_KEY</code> или <code>ADMIN_API_KEY</code> (24+ символа) и пересоздайте службу admin.</p>}
      <div className="row">
        <input value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder="Название (например, NL-1)" required />
      </div>
      <p className="muted">Введите готовый адрес или заполните поля ниже.</p>
      <p className="muted">Для Telegram выберите HTTP или SOCKS5. HTTPS-прокси доступны для остальных сервисов. SOCKS5h разрешает имена через прокси.</p>
      <input value={f.url} onChange={(e) => set({ url: e.target.value })} placeholder="socks5://логин:пароль@host:1080" />
      {!f.url && (
        <div className="row">
          <select value={f.scheme} onChange={(e) => set({ scheme: e.target.value })}>{SCHEMES.map((s) => <option key={s}>{s}</option>)}</select>
          <input value={f.host} onChange={(e) => set({ host: e.target.value })} placeholder="Хост" required />
          <input type="number" value={f.port} onChange={(e) => set({ port: e.target.value })} placeholder="Порт" min="1" max="65535" required />
          <input value={f.user} onChange={(e) => set({ user: e.target.value })} placeholder="Логин (необязательно)" autoComplete="off" />
          <input type="password" value={f.password} onChange={(e) => set({ password: e.target.value })} placeholder="Пароль" autoComplete="new-password" />
        </div>
      )}
      <button disabled={busy || !f.name}>Добавить</button> {message && <span className="muted">{message}</span>}
    </form>
  );
}

function ProxyRow({ proxy, services, reload }) {
  const { run, message, busy } = useAction(reload);
  const [service, setService] = useState('telegram');
  const [result, setResult] = useState('');
  const test = () => run(async () => {
    const r = await api(`/net/proxies/${proxy.id}/test`, { method: 'POST', body: { service } });
    setResult(r.ok ? `✅ работает (${r.ms} мс, HTTP ${r.status})` : `❌ ${r.error}`);
  }, 'Проверено');
  return (
    <tr>
      <td><b>{proxy.name}</b><br /><small>{proxy.scheme}://{proxy.host}</small></td>
      <td><Badge on={proxy.is_enabled} />{proxy.last_test_at && <><br /><small className={proxy.last_test_ok ? '' : 'error'}>{when(proxy.last_test_at)}: {proxy.last_test_ok ? 'ok' : proxy.last_test_error}</small></>}</td>
      <td className="actions">
        <select value={service} onChange={(e) => setService(e.target.value)}>{services.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}</select>
        <button className="small" disabled={busy} onClick={test}>Проверить</button>
        <button className="small" disabled={busy} onClick={() => run(() => api(`/net/proxies/${proxy.id}`, { method: 'PATCH', body: { is_enabled: !proxy.is_enabled } }))}>{proxy.is_enabled ? 'Выключить' : 'Включить'}</button>
        <button className="small danger" disabled={busy} onClick={() => confirm('Удалить прокси? Он пропадёт из всех маршрутов.') && run(() => api(`/net/proxies/${proxy.id}`, { method: 'DELETE' }))}>Удалить</button>
        {(result || message) && <small className="muted">{result || message}</small>}
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
  const health = (r) => r.health ? `успехов ${r.health.successes}, ошибок ${r.health.failures}${r.health.latency_ms ? `, ${Math.round(r.health.latency_ms)} мс` : ''}${r.health.open_seconds ? ` · пауза ${r.health.open_seconds} с` : ''}` : 'нет данных';
  return (
    <div className="card">
      <div className="row"><b>{service.label}</b><small className="muted">{service.target}</small>{!service.configured && <small className="muted">(по умолчанию — напрямую)</small>}</div>
      <div className="row">
        <select value={current.strategy} onChange={(e) => edit({ strategy: e.target.value })}>{Object.entries(STRATEGIES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <label>Попыток на запрос <input type="number" min="1" max="10" value={current.attempts} onChange={(e) => edit({ attempts: e.target.value })} /></label>
      </div>
      <ol>
        {current.routes.map((r, i) => (
          <li key={String(r.proxy_id)}>
            <label><input type="checkbox" checked={r.enabled} onChange={() => { const routes = [...current.routes]; routes[i] = { ...r, enabled: !r.enabled }; edit({ routes }); }} /> {r.name}</label>{' '}
            <small className="muted">{!draft && service.routes[i] ? health(service.routes[i]) : ''}</small>{' '}
            <button className="small" onClick={() => move(i, -1)}>↑</button> <button className="small" onClick={() => move(i, 1)}>↓</button>{' '}
            <button className="small danger" onClick={() => edit({ routes: current.routes.filter((_, j) => j !== i) })}>✕</button>
          </li>
        ))}
      </ol>
      <div className="row">
        {addable.length > 0 && (
          <select value="" onChange={(e) => { const p = addable.find((a) => String(a.id) === e.target.value); if (p) edit({ routes: [...current.routes, { proxy_id: p.id, enabled: true, name: p.name }] }); }}>
            <option value="">+ добавить маршрут…</option>{addable.map((p) => <option key={String(p.id)} value={String(p.id)}>{p.name}</option>)}
          </select>
        )}
        <button disabled={busy || !draft} onClick={save}>Сохранить</button>
        <button className="small" disabled={busy} onClick={check}>Проверить маршруты</button>
        {draft && <button className="small" onClick={() => setDraft(null)}>Отмена</button>}
        {message && <small className="muted">{message}</small>}
      </div>
      {probe && <ul>{probe.map((p) => <li key={p.route}>{p.ok ? '✅' : '❌'} {p.label}: {p.ok ? `${p.ms} мс (HTTP ${p.status})` : p.error}</li>)}</ul>}
    </div>
  );
}

export default function Network() {
  const { data, error, loading, reload } = useData('/net');
  return (
    <section>
      <h2>Сеть и прокси</h2>
      <Help title="Как это работает и как настроить">
        <p>Для каждого внешнего сервиса (Telegram, MAX, ВКонтакте, YouTube, RuTube, генераторы картинок…) можно задать свой список путей: напрямую и через один или несколько прокси. Бот сам выбирает рабочий путь для каждого запроса и переключается, если путь перестал работать.</p>
        <ol>
          <li><b>Добавьте прокси</b> ниже (HTTP/HTTPS или SOCKS5, с логином и паролем или без) и нажмите «Проверить» — выберите сервис, до которого нужно достучаться.</li>
          <li><b>Назначьте прокси сервисам</b>: в карточке сервиса добавьте маршруты (↑↓ меняют порядок) и выберите стратегию.</li>
          <li><b>Стратегии</b>: «по порядку» — всегда первый рабочий, остальные резервные; «автоматически» — самый быстрый и надёжный по недавним запросам; «по кругу» — распределять нагрузку.</li>
          <li><b>Автоматическое переключение</b>: после 3 неудач подряд путь на время отключается (30 с, затем дольше, до 15 мин) и потом получает пробный запрос. Если все пути сломаны, пробуется тот, что раньше вернётся. Отправка сообщений не дублируется: после таймаута ответа запрос не повторяется другим путём, а при ошибке соединения — повторяется.</li>
          <li>Изменения подхватываются службами в течение 30 секунд, перезапуск не нужен. Адреса прокси хранятся зашифрованными.</li>
        </ol>
        <p className="muted">Без настроек все сервисы работают напрямую (с учётом переменной окружения HTTPS_PROXY, если она задана на сервере).</p>
      </Help>
      <Status error={error} loading={loading} />
      {data && (
        <>
          <h3>Сервисы</h3>
          {data.services.map((s) => <Service key={s.key} service={s} proxies={data.proxies.filter((p) => p.is_enabled)} reload={reload} />)}
          <h3>Прокси ({data.proxies.length})</h3>
          {data.proxies.length > 0 && <table><thead><tr><th>Прокси</th><th>Состояние</th><th></th></tr></thead><tbody>{data.proxies.map((p) => <ProxyRow key={p.id} proxy={p} services={data.services} reload={reload} />)}</tbody></table>}
          <ProxyForm onCreated={reload} secretsReady={data.secrets_ready} />
        </>
      )}
    </section>
  );
}
