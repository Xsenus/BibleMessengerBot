import { useState } from 'react';
import { Clock, RefreshCw, Send, TriangleAlert } from 'lucide-react';
import { Status, useData } from '../ui.jsx';
import { PageHeader, Panel, Stat, StatusBadge, TimeAgo, formatNumber } from '../components.jsx';
import { PLATFORMS, SERVICES, label } from '../labels.js';
import ReleaseStatus from '../ReleaseStatus.jsx';

// Служба считается живой, если сигнал не старше 3 минут (с запасом на расхождение часов).
const isFresh = (lastSeen) => {
  const age = Date.now() - new Date(lastSeen).getTime();
  return age >= -10000 && age < 180000;
};

export default function Overview() {
  const { data, error, loading, reload } = useData('/overview');
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = () => { reload(); setRefreshKey((value) => value + 1); };
  const totalChats = data ? data.platforms.reduce((sum, p) => sum + p.count, 0) : 0;
  const attention = data?.queue.attention || 0;
  return (
    <section>
      <PageHeader title="Состояние" description="Очередь доставки, работа служб и установленная версия."
        actions={<button className="small" onClick={refresh}><RefreshCw size={15} aria-hidden="true" />Обновить</button>} />
      <Status error={error} loading={loading} />
      {data && (
        <>
          <div className="stat-grid">
            <Stat icon={Clock} label="В очереди" value={data.queue.queued} hint="ждут отправки или повтора" />
            <Stat icon={Send} label="Отправлено за сутки" value={data.queue.sent_24h} hint="подтверждённые доставки" />
            <Stat icon={TriangleAlert} label="Требуют внимания" value={attention} tone={attention ? 'danger' : 'ok'}
              hint={attention ? 'Открыть доставки →' : 'всё в порядке'} href={attention ? '#/deliveries' : undefined} />
          </div>
          <div className="split">
            <Panel title="Службы">
              <ul className="service-list">
                {data.services.map((s) => {
                  const fresh = isFresh(s.last_seen);
                  return (
                    <li key={s.service}>
                      <span className="service-name">{label(SERVICES, s.service)}<small>{s.service}</small></span>
                      <StatusBadge tone={fresh ? 'ok' : 'error'}>{fresh ? 'работает' : 'нет сигнала'}</StatusBadge>
                      <small className="service-seen"><TimeAgo value={s.last_seen} /></small>
                    </li>
                  );
                })}
              </ul>
            </Panel>
            <Panel title="Активные чаты по платформам">
              <ul className="share-list">
                {data.platforms.map((p) => (
                  <li key={p.platform}>
                    <span>{label(PLATFORMS, p.platform)}</span>
                    <strong>{formatNumber(p.count)}</strong>
                    <span className="share-bar" aria-hidden="true"><span style={{ width: `${totalChats ? (p.count / totalChats) * 100 : 0}%` }} /></span>
                  </li>
                ))}
              </ul>
              <p className="muted panel-foot">Всего: {formatNumber(totalChats)}</p>
            </Panel>
          </div>
        </>
      )}
      <ReleaseStatus refreshKey={refreshKey} />
    </section>
  );
}
