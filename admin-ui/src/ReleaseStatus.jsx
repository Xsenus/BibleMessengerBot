import { useEffect, useState } from 'react';
import { api } from './api.js';
import { explain, when } from './ui.jsx';

const SERVER_STATES = {
  running: ['Выполняется', 'pending'], succeeded: ['Успешно', 'ok'],
  failed: ['Ошибка', 'error'], rolled_back: ['Выполнен откат', 'error'],
  manual_intervention: ['Требуется вмешательство', 'error'], skipped: ['Пропущен', 'off'],
};
const STAGES = { checking: 'Проверка', backup: 'Резервная копия', updating: 'Обновление служб',
  verifying: 'Проверка результата', completed: 'Завершено', rollback: 'Откат', stopped: 'Службы остановлены' };

function Label({ value }) {
  const [text, style] = value || ['Нет данных', 'off'];
  return <span className={`badge ${style}`}>{text}</span>;
}

function Commit({ value, url }) {
  return value ? <a href={url} target="_blank" rel="noreferrer"><code title={value}>{value.slice(0, 8)}</code></a> : <span className="muted">Коммит неизвестен</span>;
}

function workflowState(run) {
  if (!run) return null;
  if (run.status !== 'completed') return ['Выполняется / ожидает', 'pending'];
  return ({ success: ['Успешно', 'ok'], failure: ['Ошибка', 'error'], timed_out: ['Превышено время', 'error'],
    cancelled: ['Отменён', 'off'], skipped: ['Пропущен', 'off'], action_required: ['Требуется действие', 'error'],
    startup_failure: ['Ошибка запуска', 'error'] })[run.conclusion] || ['Результат неизвестен', 'off'];
}

export default function ReleaseStatus() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let busy = false;
    const load = async () => {
      if (busy) return;
      busy = true;
      try {
        const next = await api('/release', { signal: controller.signal });
        if (!controller.signal.aborted) { setData(next); setError(''); }
      } catch (e) { if (!controller.signal.aborted) setError(explain(e)); }
      finally { busy = false; }
    };
    load();
    const timer = setInterval(load, 30000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [refresh]);
  const latest = data?.server.last_attempt;
  const current = data?.server.current;
  const run = data?.github.run;
  return (
    <div className="card release-status" aria-label="Версия и автодеплой">
      <div className="row release-heading"><h3>Версия и автодеплой</h3>
        <button className="small" onClick={() => setRefresh((x) => x + 1)}>Обновить статус</button></div>
      {error && <p className="error">Не удалось обновить статус: {error}. Показанные ниже данные могут устареть.</p>}
      {!data && !error && <p className="muted">Загрузка версии и статуса…</p>}
      {data && <>
        <div className="release-grid">
          <div><h4>Запущенная сборка</h4><p><strong>BibleMessengerBot {data.build.version || '—'}</strong></p>
            <p><Commit value={data.build.commit} url={data.build.commit_url} /></p>
            <p className="muted">Собрана: {when(data.build.built_at)}</p></div>
          <div><h4>Последняя установка на VPS</h4><p><Label value={data.server.stale ? ['Нет подтверждения завершения', 'error'] : latest && SERVER_STATES[latest.status]} /></p>
            {latest ? <><p><Commit value={latest.commit} url={latest.commit_url} /></p>
              <p>Начало: {when(latest.started_at)}<br />Завершение: {when(latest.finished_at)}</p>
              {latest.status === 'running' && <p className="muted">{data.server.stale ? 'Квитанция не обновлялась более 90 минут. Проверьте запуск и сервер.' : STAGES[latest.stage] || 'Выполняется'}</p>}
            </> : <p className="muted">Квитанция установки ещё не получена.</p>}</div>
          <div><h4>Последний запуск GitHub Actions</h4><p><Label value={data.github.available ? workflowState(run) : run ? ['Данные устарели', 'off'] : null} /></p>
            {run && <><p><a href={run.url} target="_blank" rel="noreferrer">Открыть запуск ↗</a>{run.attempt && ` · попытка ${run.attempt}`}</p>
              <p>Коммит: <code title={run.commit}>{run.commit.slice(0, 8)}</code></p>
              <p className="muted">Обновлён: {when(run.updated_at)}</p></>}
            {!data.github.available && <p className="error">GitHub временно недоступен.{data.github.stale ? ` Последние полученные данные: ${when(data.github.checked_at)}.` : ''}</p>}
            {data.github.available && !run && <p className="muted">Запусков пока нет.</p>}</div>
        </div>
        {current && current.commit !== data.build.commit && <p className="release-notice">Последняя подтверждённая установка относится к другому коммиту. Проверка этой сборки ещё не подтверждена.</p>}
        <div className="release-bots"><table><thead><tr><th>Бот</th><th>Проверка после установки</th><th>Текущий сигнал</th></tr></thead><tbody>
          {data.bots.map((bot) => <tr key={bot.service}><td>{bot.label}</td>
            <td><Label value={bot.expected === false ? ['Не запускался', 'off'] : bot.verified ? ['Проверен', 'ok'] : ['Не подтверждена', 'off']} /></td>
            <td>{bot.expected === false ? <span className="muted">—</span> : <><Label value={error || bot.live === null ? ['Нет данных', 'off'] : bot.live ? ['Свежий сигнал', 'ok'] : ['Нет свежего сигнала', 'error']} /><br /><small>{when(bot.last_seen)}</small></>}</td></tr>)}
        </tbody></table></div>
        <p className="muted release-footnote">Состояние обновляется каждые 30 секунд, данные GitHub — не чаще раза в 2 минуты. Время: {Intl.DateTimeFormat().resolvedOptions().timeZone}. Сигнал подтверждает работу процесса, но не проверяет доставку сообщений.</p>
      </>}
    </div>
  );
}
