import { useState } from 'react';
import { CircleCheck, Inbox, Megaphone } from 'lucide-react';
import { Status, useData, when } from '../ui.jsx';
import { DataTable, EmptyState, PageHeader, Panel, Stat, StatusBadge, formatNumber } from '../components.jsx';
import { DELIVERY_MODES, RUN_STATUSES, label } from '../labels.js';

const PERIODS = [1, 7, 30, 90];
const percent = (part, total) => (total ? `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format((part / total) * 100)} %` : '—');

export default function Reports() {
  const [days, setDays] = useState(7);
  const { data, error, loading } = useData(`/delivery-report?days=${days}`);
  const totals = (data?.per_day || []).reduce((sum, r) => ({ total: sum.total + r.total, sent: sum.sent + r.sent, failed: sum.failed + r.failed }), { total: 0, sent: 0, failed: 0 });
  const num = (key) => (row) => formatNumber(row[key]);
  return (
    <section>
      <PageHeader title="Отчёты о доставке" description="Сколько сообщений отправлено и какие ошибки встречались."
        actions={<label className="inline-field">За период
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>{PERIODS.map((d) => <option key={d} value={d}>{d === 1 ? 'сутки' : `${d} сут.`}</option>)}</select></label>} />
      <Status error={error} loading={loading} />
      {data && (<>
        <div className="stat-grid">
          <Stat label="Всего заданий" value={totals.total} />
          <Stat label="Доставлено" value={totals.sent} hint={percent(totals.sent, totals.total)} tone="ok" />
          <Stat label="Ошибок" value={totals.failed} hint={percent(totals.failed, totals.total)} tone={totals.failed ? 'danger' : undefined} />
        </div>
        <Panel title="По дням и видам">
          <DataTable caption="По дням и видам" rowKey={(r) => r.day + r.mode} rows={data.per_day} columns={[
            { key: 'day', header: 'День', render: (r) => new Date(`${r.day}T00:00:00`).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }) },
            { key: 'mode', header: 'Вид', render: (r) => label(DELIVERY_MODES, r.mode) },
            { key: 'total', header: 'Всего', align: 'end', render: num('total') },
            { key: 'sent', header: 'Доставлено', align: 'end', render: num('sent') },
            { key: 'failed', header: 'Ошибок', align: 'end', render: (r) => <span className={r.failed ? 'error' : 'muted'}>{formatNumber(r.failed)}</span> },
          ]} empty={<EmptyState icon={Inbox} title="За период рассылок не было" />} />
        </Panel>
        <Panel title="Частые ошибки">
          <DataTable caption="Частые ошибки" rowKey={(r) => r.error_code + r.mode} rows={data.errors} columns={[
            { key: 'code', header: 'Код', render: (r) => <code>{r.error_code}</code> },
            { key: 'mode', header: 'Вид', render: (r) => label(DELIVERY_MODES, r.mode) },
            { key: 'n', header: 'Сколько', align: 'end', render: num('n') },
          ]} empty={<EmptyState icon={CircleCheck} title="Ошибок нет" />} />
        </Panel>
        <Panel title="Запуски объявлений">
          <DataTable caption="Запуски объявлений" rows={data.runs} columns={[
            { key: 'title', header: 'Объявление', render: (r) => <>{r.title || `#${r.announcement_id}`}<div><small>запуск #{r.id}</small></div></> },
            { key: 'when', header: 'Когда', render: (r) => when(r.created_at) },
            { key: 'status', header: 'Состояние', render: (r) => { const [text, tone] = RUN_STATUSES[r.status] || [r.status || '—', 'off']; return <StatusBadge tone={tone}>{text}</StatusBadge>; } },
            { key: 'recipients', header: 'Получателей', align: 'end', render: num('recipients') },
            { key: 'queued', header: 'В очереди', align: 'end', render: num('queued') },
            { key: 'sent', header: 'Доставлено', align: 'end', render: num('sent') },
            { key: 'failed', header: 'Ошибок', align: 'end', render: (r) => <span className={r.failed ? 'error' : 'muted'}>{formatNumber(r.failed)}</span> },
          ]} empty={<EmptyState icon={Megaphone} title="Объявления за период не отправлялись" />} />
        </Panel>
      </>)}
    </section>
  );
}
