import { api } from '../api.js';
import { Badge, Status, useAction, useData } from '../ui.jsx';

export default function Settings() {
  const { data, error, loading, reload } = useData('/settings');
  const { run, message, busy } = useAction(reload);
  return (
    <section>
      <h2>Настройки</h2>
      <Status error={error} loading={loading} />
      {data && (
        <div className="card">
          <h3>Раздел «Поддержка проекта»</h3>
          <p className="muted">Кнопка «Поддержать», команды /donate и /paysupport. Бот применит изменение не позднее чем через 30 секунд, меню в мессенджерах обновится в течение часа. Уже начатые платежи и возвраты обрабатываются всегда.</p>
          <p>Сейчас: <Badge on={data.support_section} yes="показан" no="скрыт" /> <small className="muted">(источник: {data.support_source === 'panel' ? 'панель' : 'SUPPORT_SECTION_ENABLED'})</small></p>
          <button disabled={busy} onClick={() => run(() => api('/settings', { method: 'PUT', body: { support_section: !data.support_section } }))}>
            {data.support_section ? 'Скрыть раздел' : 'Показать раздел'}
          </button>
        </div>
      )}
      <div className="card">
        <h3>Что настраивается где</h3>
        <ul>
          <li>Объявления, источники, видеоканалы, администраторы, генераторы картинок, чаты — в этой панели и в боте (<code>/admin</code>).</li>
          <li>Токены ботов, ключи платных сервисов, бюджеты, хранение данных — в <code>.env</code> на сервере (после изменения перезапустите службу).</li>
        </ul>
      </div>
      {message && <p className="muted">{message}</p>}
    </section>
  );
}
