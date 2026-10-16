import { Badge, Status, useData, when } from '../ui.jsx';
import ReleaseStatus from '../ReleaseStatus.jsx';

export default function Overview() {
  const { data, error, loading, reload } = useData('/overview');
  return (
    <section>
      <h2>Состояние <button className="small" onClick={reload}>Обновить</button></h2>
      <ReleaseStatus />
      <Status error={error} loading={loading} />
      {data && (
        <>
          <div className="grid">
            <div className="card"><h3>В очереди</h3><big>{data.queue.queued}</big></div>
            <div className="card"><h3>Отправлено за сутки</h3><big>{data.queue.sent_24h}</big></div>
            <div className="card"><h3>Требуют внимания</h3><big className={data.queue.attention ? 'error' : ''}>{data.queue.attention}</big></div>
          </div>
          <h3>Назначения по платформам</h3>
          <table><tbody>{data.platforms.map((p) => <tr key={p.platform}><td>{p.platform}</td><td>{p.count}</td></tr>)}</tbody></table>
          <h3>Службы</h3>
          <table><tbody>
            {data.services.map((s) => {
              const age = Date.now() - new Date(s.last_seen).getTime();
              const fresh = age >= -10000 && age < 180000;
              return <tr key={s.service}><td>{s.service}</td><td><Badge on={fresh} yes="работает" no="нет сигнала" /></td><td>{when(s.last_seen)}</td></tr>;
            })}
          </tbody></table>
        </>
      )}
    </section>
  );
}
