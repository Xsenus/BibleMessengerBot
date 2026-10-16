import { CircleCheck } from 'lucide-react';
import { api } from '../api.js';
import { Help, Status, useAction, useData } from '../ui.jsx';
import { DataTable, EmptyState, PageHeader, StatusBadge, TimeAgo } from '../components.jsx';
import { DELIVERY_MODES, DELIVERY_STATUSES, label } from '../labels.js';

const ACTIONS = [
  ['sent', 'Дошло', 'Отметить задание как доставленное?'],
  ['retry-duplicate-risk', 'Повторить', 'Отправить ещё раз? Если первое сообщение всё-таки дошло, получатель увидит дубль.'],
  ['cancel', 'Отменить', 'Отменить задание? Сообщение не будет отправлено.'],
];

export default function Deliveries() {
  const { data, error, loading, reload } = useData('/deliveries/attention');
  const { run, message, busy } = useAction(reload);
  const columns = [
    { key: 'id', header: 'Задание', render: (d) => <code>#{d.id}</code> },
    { key: 'chat', header: 'Чат', render: (d) => <code>{d.chat_id}</code> },
    { key: 'mode', header: 'Вид', render: (d) => label(DELIVERY_MODES, d.mode) },
    { key: 'status', header: 'Состояние', render: (d) => {
      const [text, tone] = DELIVERY_STATUSES[d.status] || [d.status, 'off'];
      return <><StatusBadge tone={tone}>{text}</StatusBadge>{d.error_code && <div><small>код: <code>{d.error_code}</code></small></div>}</>;
    } },
    { key: 'attempts', header: 'Попыток', align: 'end', render: (d) => d.attempt_count ?? '—' },
    { key: 'updated', header: 'Обновлено', render: (d) => <TimeAgo value={d.updated_at} /> },
    { key: 'actions', header: 'Решение', className: 'cell-actions', render: (d) => (
      <div className="actions">{ACTIONS.map(([action, text, question]) => (
        <button key={action} className={`small${action === 'cancel' ? ' danger' : ''}`} disabled={busy}
          onClick={() => confirm(question) && run(() => api(`/deliveries/${d.id}/resolve`, { method: 'POST', body: { action } }))}>{text}</button>
      ))}</div>
    ) },
  ];
  return (
    <section>
      <PageHeader title="Доставки, требующие решения" description="Сообщения, которые не удалось отправить или исход которых неизвестен." />
      <Help title="Что выбрать">
        <ul>
          <li><b>Дошло</b> — вы убедились, что сообщение получено: задание закрывается как отправленное.</li>
          <li><b>Повторить</b> — отправить ещё раз. При неизвестном исходе это может создать дубль.</li>
          <li><b>Отменить</b> — больше не пытаться отправить.</li>
        </ul>
      </Help>
      <Status error={error} loading={loading} />
      <DataTable columns={columns} rows={data} caption="Доставки, требующие решения" stack
        empty={<EmptyState icon={CircleCheck} title="Всё в порядке">Решений не требуется.</EmptyState>} />
      {message && <p className="muted" role="status">{message}</p>}
    </section>
  );
}
