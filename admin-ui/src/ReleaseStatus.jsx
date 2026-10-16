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

function Failure({ item }) {
  const problem = item.failure;
  if (problem) return <><span>{problem.message}</span><details><summary>Подробности ошибки</summary>
    <p>Этап: {STAGES[problem.stage] || 'Неизвестен'}{problem.service && ` · служба ${problem.service}`}</p>
    <p>Код: <code>{problem.code}</code>{problem.exit_code !== null && ` · код выхода ${problem.exit_code}`}</p>
    {item.status === 'rolled_back' && <p>Предыдущая версия восстановлена.</p>}
    {item.status === 'failed' && item.stage === 'rollback' && <p>Откат не подтверждён: требуется проверка оператора.</p>}
    {item.status === 'manual_intervention' && <p>Автоматический откат не выполнен: требуется проверка оператора.</p>}
  </details></>;
  if (item.stale) return <span className="error">Нет подтверждения завершения более 90 минут.</span>;
  if (['failed', 'rolled_back', 'manual_intervention'].includes(item.status)) return <span>Причина не сохранялась этой версией автодеплоя. Проверьте соответствующий журнал GitHub Actions.</span>;
  return <span className="muted">{item.status === 'running' ? STAGES[item.stage] || 'Выполняется' : item.legacy ? 'Подтверждено архивным маркером установки.' : '—'}</span>;
}

function WorkflowReason({ run }) {
  if (run.status !== 'completed') return <span className="muted">Ожидает запуска или выполняется.</span>;
  const message = { failure: 'GitHub Actions завершился с ошибкой. Подробности — в журнале этой попытки.',
    timed_out: 'GitHub Actions прервал выполнение по лимиту времени.', cancelled: 'Запуск отменён в GitHub Actions.',
    startup_failure: 'GitHub Actions не смог запустить задачу.', action_required: 'GitHub Actions ожидает действия оператора.',
    skipped: 'Задача пропущена условиями workflow.', stale: 'GitHub пометил запуск как устаревший.' }[run.conclusion];
  return <span className={message ? '' : 'muted'}>{message || '—'}</span>;
}

function Changes({ item }) {
  const changes = item.changes;
  if (!changes) return <span className="muted">{item.status === 'running' ? 'После завершения установки' : 'Список не сохранялся'}</span>;
  if (changes.state === 'unavailable') return <div className="muted">{({ previous_unknown: 'Предыдущий коммит не сохранён',
    range_unavailable: 'Не удалось прочитать диапазон версий', description_unavailable: 'Краткое описание недоступно' })[changes.reason] || 'Нет данных'}
    {changes.compare_url && <p><a href={changes.compare_url} target="_blank" rel="noreferrer">Сравнение версий ↗</a></p>}</div>;
  return <details className="deployment-changes"><summary>Изменения{changes.items.length ? ` · ${changes.items.length}${changes.more ? '+' : ''}` : ''}</summary>
    {item.status !== 'succeeded' && <p className="release-notice">Изменения запрошенной сборки. Их установка не подтверждена.</p>}
    {changes.state === 'unchanged' ? <p>Новых изменений кода нет: повторная установка той же версии.</p> : <ul>{changes.items.map((text, index) => <li key={index}>{text}</li>)}</ul>}
    {changes.more && <p className="muted">Остальные изменения доступны по ссылке сравнения.</p>}
    {changes.compare_url && <a href={changes.compare_url} target="_blank" rel="noreferrer">Полное сравнение версий ↗</a>}
  </details>;
}

function DeployHistory({ server, github, stale }) {
  const [limit, setLimit] = useState(5);
  const records = server.history || [];
  const runs = github.history || [];
  return <section className="deploy-history" aria-label="История автодеплоев">
    <h3>История автодеплоев</h3>
    {stale && <p className="error">История могла устареть: не удалось обновить данные.</p>}
    <h4>Установки на VPS</h4>
    {!records.length ? <p className="muted">Сохранённых установок пока нет.</p> : <>
      <div className="deploy-history-table"><table><thead><tr><th>Начало / завершение</th><th>Коммит</th><th>Результат</th><th>Причина / этап</th><th>Изменения</th></tr></thead><tbody>
        {records.slice(0, limit).map((item) => <tr key={item.id}>
          <td>{when(item.started_at)}<br /><small className="muted">{when(item.finished_at)}</small></td>
          <td><Commit value={item.commit} url={item.commit_url} /></td>
          <td><Label value={item.stale ? ['Нет подтверждения', 'error'] : SERVER_STATES[item.status]} /></td>
          <td><Failure item={item} /></td>
          <td><Changes item={item} /></td>
        </tr>)}
      </tbody></table></div>
      {records.length > limit && <button className="small" onClick={() => setLimit((value) => value + 10)}>Показать ещё</button>}
    </>}
    <p className="muted release-footnote">Сохраняются последние 50 попыток установки, включая ошибки и откаты.</p>
    <details className="github-history"><summary>Запуски GitHub Actions ({runs.length})</summary>
      <p className="muted">Здесь видны и ошибки до подключения к VPS. Результат GitHub Actions и подтверждение установки на сервере показаны отдельно.</p>
      {!github.available && <p className="error">GitHub временно недоступен.{runs.length > 0 && ' Показаны ранее полученные данные.'}</p>}
      {github.available && github.history_complete === false && <p className="muted">Часть предыдущих повторных попыток пока недоступна. Их можно открыть в GitHub.</p>}
      {!runs.length ? <p className="muted">Нет данных о запусках.</p> : <div className="deploy-history-table"><table><thead><tr><th>Начало / обновлён</th><th>Коммит</th><th>Результат</th><th>Причина / журнал</th></tr></thead><tbody>
        {runs.map((run) => <tr key={`${run.id}-${run.attempt}`}>
          <td>{when(run.started_at)}<br /><small className="muted">{when(run.updated_at)}</small></td>
          <td><Commit value={run.commit} url={`https://github.com/Xsenus/BibleMessengerBot/commit/${run.commit}`} /></td>
          <td><Label value={workflowState(run)} /></td>
          <td><WorkflowReason run={run} /><p><a href={run.attempt_url || run.url} target="_blank" rel="noreferrer">Журнал{run.attempt ? ` · попытка ${run.attempt}` : ''} ↗</a></p></td>
        </tr>)}
      </tbody></table></div>}
    </details>
  </section>;
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
              {latest.failure && <p className="error">{latest.failure.message}</p>}
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
        <DeployHistory server={data.server} github={data.github} stale={Boolean(error)} />
      </>}
    </div>
  );
}
