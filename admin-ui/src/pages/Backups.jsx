import { useEffect, useState } from 'react';
import { CircleX, DatabaseBackup, Download, RefreshCw, Trash2 } from 'lucide-react';
import { api, downloadBackup } from '../api.js';
import { ask } from '../feedback.jsx';
import { Status, explain, useData, when } from '../ui.jsx';
import { EmptyState, PageHeader, Pager, Panel, StatusBadge, formatNumber } from '../components.jsx';

const PAGE_SIZE = 30;
const size = (bytes) => {
  const factor = bytes >= 1024 ** 2 ? 1024 ** 2 : bytes >= 1024 ? 1024 : 1;
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(bytes / factor) + (factor === 1 ? ' Б' : factor === 1024 ? ' КБ' : ' МБ');
};
const STATES = { available: ['В наличии', 'ok'], missing: ['Файл отсутствует', 'error'], invalid: ['Ошибка метаданных', 'error'], unavailable: ['Не удалось проверить', 'warning'] };

export default function Backups() {
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(null);
  const [message, setMessage] = useState('');
  const [availability, setAvailability] = useState({});
  const { data: received, error, loading, reload } = useData(`/backups?limit=${PAGE_SIZE}&offset=${offset}`);
  const data = received?.offset === offset ? received : null;
  useEffect(() => {
    setAvailability({});
    if (!data) return;
    if (offset > 0 && !data.items.length) {
      setOffset(Math.floor(Math.max(0, data.total - 1) / PAGE_SIZE) * PAGE_SIZE);
      return;
    }
    if (!data.storage_enabled || !data.items.length) return;
    const controller = new AbortController();
    const ids = data.items.map(({ id }) => id);
    api('/backups/reconcile', { method: 'POST', body: { ids }, signal: controller.signal })
      .then(({ items }) => {
        if (controller.signal.aborted) return;
        setAvailability(Object.fromEntries(items.map(({ id, status }) => [id, status])));
        if (items.some(({ status }) => status === 'removed')) reload();
      })
      .catch(() => {
        if (!controller.signal.aborted) setAvailability(Object.fromEntries(ids.map((id) => [id, 'unavailable'])));
      });
    return () => controller.abort();
  }, [data, offset, reload]);
  const download = async (item) => {
    setBusy({ id: item.id, action: 'download' }); setMessage('Подготавливаем и скачиваем резервную копию…');
    try { await downloadBackup(item.id); setMessage('Файл передан браузеру для сохранения.'); }
    catch (e) { setMessage('⚠ ' + explain(e)); }
    finally { setBusy(null); }
  };
  const remove = async (item) => {
    if (!await ask({
      title: 'Удалить резервную копию?',
      body: `Файл ${item.filename} будет удалён из облачного хранилища S3 и списка.`,
      confirmText: 'Удалить копию',
      danger: true,
    })) return;
    setBusy({ id: item.id, action: 'remove' }); setMessage('Удаляем резервную копию…');
    try {
      await api(`/backups/${item.id}`, { method: 'DELETE' });
      setMessage('Резервная копия удалена.');
      reload();
    } catch (e) { setMessage('⚠ ' + explain(e)); }
    finally { setBusy(null); }
  };
  const visibleItems = data?.items.filter((item) => availability[item.id] !== 'removed') || [];
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <section>
      <PageHeader title="Резервные копии" description={`Проверенные копии базы данных в облаке. Время — в вашем часовом поясе (${zone}).`}
        actions={<button className="small" onClick={reload} disabled={loading || busy !== null}><RefreshCw size={15} aria-hidden="true" />Обновить список</button>} />
      <p className="callout callout-info"><DatabaseBackup size={16} aria-hidden="true" /><span>Перед скачиванием архив проверяется и расшифровывается в файл <code>.dump</code>. При просмотре списка записи о копиях, отсутствие которых подтвердило S3, убираются автоматически.</span></p>
      <Panel title={data ? `Копии (${formatNumber(data.total)})` : 'Копии'}>
        <Status error={error} loading={loading || (!data && !error)} />
        {message && <p className="form-message" role="alert">{message}</p>}
        {data && !data.download_enabled && <p className="callout callout-danger"><CircleX size={16} aria-hidden="true" />Скачивание недоступно: проверьте настройки S3 и ключа шифрования на сервере.</p>}
        {data?.items.length === 0 && <EmptyState icon={DatabaseBackup} title="Проверенных копий пока нет">После успешного резервирования они появятся здесь.</EmptyState>}
        {visibleItems.length > 0 && (
          <div className="table-wrap">
            <table className="data-table table-stack">
              <thead><tr><th scope="col">Создана</th><th scope="col">Проверена в облаке</th><th scope="col">Файл</th><th scope="col" className="align-end">Размер</th><th scope="col">Доступность</th><th scope="col"><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>{visibleItems.map((item) => {
                const [text, tone] = data.storage_enabled ? (STATES[availability[item.id]] || ['Проверяем…', 'pending']) : ['Не настроено', 'off'];
                return (
                  <tr key={item.id}>
                    <td data-label="Создана">{when(item.created_at)}</td>
                    <td data-label="Проверена в облаке">{when(item.verified_at)}</td>
                    <td data-label="Файл" className="cell-file"><code>{item.filename}</code></td>
                    <td data-label="Размер" className="align-end">{size(item.size_bytes)}</td>
                    <td data-label="Доступность"><StatusBadge tone={tone}>{text}</StatusBadge></td>
                    {/* Имя файла — скрытым текстом после видимого: название кнопки начинается с того, что на ней написано
                        (WCAG 2.5.3), и при этом различает строки. */}
                    <td className="cell-actions"><div className="actions">
                      <button className="small" disabled={busy !== null || availability[item.id] !== 'available' || !data.download_enabled} onClick={() => download(item)}>
                        <Download size={14} aria-hidden="true" />{busy?.id === item.id && busy.action === 'download' ? 'Скачивание…' : 'Скачать .dump'}<span className="sr-only"> {item.filename}</span>
                      </button>
                      <button className="small danger" disabled={busy !== null || availability[item.id] !== 'available' || !data.storage_enabled} onClick={() => remove(item)}>
                        <Trash2 size={14} aria-hidden="true" />{busy?.id === item.id && busy.action === 'remove' ? 'Удаляем…' : 'Удалить'}<span className="sr-only"> {item.filename}</span>
                      </button>
                    </div></td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
        )}
        {data && <Pager label="Страницы копий" noun="Копии" offset={offset} size={PAGE_SIZE} shown={data.items.length}
          hasMore={offset + data.items.length < data.total} disabled={loading || busy !== null} onChange={setOffset} prev="Новее" next="Старее" />}
      </Panel>
    </section>
  );
}
