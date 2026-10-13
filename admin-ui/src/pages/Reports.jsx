import { useState } from 'react';
import { Status, useData, when } from '../ui.jsx';

export default function Reports() {
  const [days, setDays] = useState(7);
  const { data, error, loading } = useData(`/delivery-report?days=${days}`);
  return (
    <section>
      <h2>Отчёты о доставке</h2>
      <label>За период <select value={days} onChange={(e) => setDays(Number(e.target.value))}>{[1, 7, 30, 90].map((d) => <option key={d} value={d}>{d} сут.</option>)}</select></label>
      <Status error={error} loading={loading} />
      {data && (<>
        <h3>По дням и видам</h3>
        <table><thead><tr><th>День</th><th>Вид</th><th>Всего</th><th>Доставлено</th><th>Ошибок</th></tr></thead><tbody>
          {data.per_day.map((r) => <tr key={r.day + r.mode}><td>{r.day}</td><td>{r.mode}</td><td>{r.total}</td><td>{r.sent}</td><td>{r.failed}</td></tr>)}</tbody></table>
        <h3>Частые ошибки</h3>
        {data.errors.length === 0 ? <p className="muted">Ошибок нет.</p> : (
          <table><thead><tr><th>Код</th><th>Вид</th><th>Сколько</th></tr></thead><tbody>
            {data.errors.map((r) => <tr key={r.error_code + r.mode}><td>{r.error_code}</td><td>{r.mode}</td><td>{r.n}</td></tr>)}</tbody></table>)}
        <h3>Запуски объявлений</h3>
        <table><thead><tr><th>#</th><th>Объявление</th><th>Когда</th><th>Получателей</th><th>В очереди</th><th>Доставлено</th><th>Ошибок</th></tr></thead><tbody>
          {data.runs.map((r) => <tr key={r.id}><td>{r.id}</td><td>{r.title || `#${r.announcement_id}`}</td><td>{when(r.created_at)}</td><td>{r.recipients}</td><td>{r.queued}</td><td>{r.sent}</td><td>{r.failed}</td></tr>)}</tbody></table>
      </>)}
    </section>
  );
}
