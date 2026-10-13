import { Status, useData, when } from '../ui.jsx';

export default function Audit() {
  const { data, error, loading, reload } = useData('/audit?limit=200');
  return (
    <section>
      <h2>Журнал действий</h2>
      <p className="muted">Кто и что менял: из веб-панели и из команд бота. Секреты (ключи, пароли, адреса прокси) в журнал не попадают.</p>
      <button className="small" onClick={reload}>Обновить</button>
      <Status error={error} loading={loading} />
      <table><thead><tr><th>Когда</th><th>Кто</th><th>Откуда</th><th>Действие</th><th>Объект</th></tr></thead><tbody>
        {(data?.items || []).map((i) => (
          <tr key={i.id}><td>{when(i.created_at)}</td><td>{i.actor}</td><td>{i.source}</td><td>{i.action}</td><td>{i.target}</td></tr>))}
      </tbody></table>
    </section>
  );
}
