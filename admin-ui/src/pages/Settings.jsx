import { api } from '../api.js';
import { Status, useAction, useData } from '../ui.jsx';
import { PageHeader, Panel, Switch } from '../components.jsx';

export default function Settings() {
  const { data, error, loading, reload } = useData('/settings');
  const { run, message, busy } = useAction(reload);
  return (
    <section>
      <PageHeader title="Настройки" description="Параметры, которые меняются без перезапуска бота." />
      <Status error={error} loading={loading} />
      {data && (
        <Panel title="Раздел «Поддержка проекта»">
          <div className="setting-row">
            <div>
              <p>Кнопка «Поддержать», команды /donate и /paysupport в мессенджерах.</p>
              <p className="muted">Бот применит изменение не позднее чем через 30 секунд, меню в мессенджерах обновится в течение часа. Уже начатые платежи и возвраты обрабатываются всегда.
                Источник значения: {data.support_source === 'panel' ? 'эта панель' : <code>SUPPORT_SECTION_ENABLED</code>}.</p>
            </div>
            <Switch checked={data.support_section} disabled={busy} on="показан" off="скрыт" label="Раздел «Поддержка проекта» показан"
              onChange={(value) => run(() => api('/settings', { method: 'PUT', body: { support_section: value } }))} />
          </div>
          {message && <p className="form-message" role="status">{message}</p>}
        </Panel>
      )}
      <Panel title="Что настраивается где">
        <ul className="plain-list">
          <li>Объявления, источники, видеоканалы, администраторы, генераторы картинок, чаты — в этой панели и в боте (<code>/admin</code>).</li>
          <li>Токены ботов, ключи платных сервисов, бюджеты, хранение данных — в <code>.env</code> на сервере (после изменения перезапустите службу).</li>
        </ul>
      </Panel>
    </section>
  );
}
