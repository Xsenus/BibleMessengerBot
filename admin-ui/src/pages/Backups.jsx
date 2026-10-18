import { useEffect, useState } from 'react';
import { CircleX, DatabaseBackup, Download, RefreshCw } from 'lucide-react';
import { api, downloadBackup } from '../api.js';
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
    if (!data?.download_enabled) return;
    const controller = new AbortController();
    let next = 0;
    const worker = async () => {
      while (!controller.signal.aborted) {
        const item = data.items[next++];
        if (!item) return;
        let status = 'unavailable';
        try { status = (await api(`/backups/${item.id}/availability`, { signal: controller.signal })).status; }
        catch { /* Сетевая ошибка не означает удаление копии. */ }
        if (!controller.signal.aborted) setAvailability((old) => ({ ...old, [item.id]: status }));
      }
    };
    Promise.all(Array.from({ length: Math.min(3, data.items.length) }, worker));
    return () => controller.abort();
  }, [data]);
  const download = async (item) => {
    setBusy(item.id); setMessage('Подготавливаем и скачиваем резервную копию…');
    try { await downloadBackup(item.id); setMessage('Файл передан браузеру для сохранения.'); }
    catch (e) { setMessage('⚠ ' + explain(e)); }
    finally { setBusy(null); }
  };
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <section>
      <PageHeader title="Резервные копии" description={`Проверенные копии базы данных в облаке. Время — в вашем часовом поясе (${zone}).`}
        actions={<button className="small" onClick={reload} disabled={loading}><RefreshCw size={15} aria-hidden="true" />Обновить список</button>} />
      <p className="callout callout-info"><DatabaseBackup size={16} aria-hidden="true" /><span>Скачивается готовый файл <code>.dump</code> с библейскими текстами, пользователями, расписаниями, картинками и озвучками: архив проверяется и расшифровывается автоматически. Наличие файлов в облаке проверяется отдельно; отсутствующие копии остаются в истории, но скачать их нельзя.</span></p>
      <Panel title={data ? `Копии (${formatNumber(data.total)})` : 'Копии'}>
        <Status error={error} loading={loading || (!data && !error)} />
        {message && <p className="form-message" role="alert">{message}</p>}
        {data && !data.download_enabled && <p className="callout callout-danger"><CircleX size={16} aria-hidden="true" />Скачивание недоступно: проверьте настройки S3 и ключа шифрования на сервере.</p>}
        {data?.items.length === 0 && <EmptyState icon={DatabaseBackup} title="Проверенных копий пока нет">После успешного резервирования они появятся здесь.</EmptyState>}
        {data?.items.length > 0 && (
          <div className="table-wrap">
            <table className="data-table table-stack">
              <thead><tr><th scope="col">Создана</th><th scope="col">Проверена в облаке</th><th scope="col">Файл</th><th scope="col" className="align-end">Размер</th><th scope="col">Доступность</th><th scope="col"><span className="sr-only">Скачать</span></th></tr></thead>
              <tbody>{data.items.map((item) => {
                const [text, tone] = data.download_enabled ? (STATES[availability[item.id]] || ['Проверяем…', 'pending']) : ['Не настроено', 'off'];
                return (
                  <tr key={item.id}>
                    <td data-label="Создана">{when(item.created_at)}</td>
                    <td data-label="Проверена в облаке">{when(item.verified_at)}</td>
                    <td data-label="Файл" className="cell-file"><code>{item.filename}</code></td>
                    <td data-label="Размер" className="align-end">{size(item.size_bytes)}</td>
                    <td data-label="Доступность"><StatusBadge tone={tone}>{text}</StatusBadge></td>
                    {/* Имя файла — скрытым текстом после видимого: название кнопки начинается с того, что на ней написано
                        (WCAG 2.5.3), и при этом различает строки. */}
                    <td className="cell-actions"><button className="small" disabled={busy !== null || availability[item.id] !== 'available' || !data.download_enabled} onClick={() => download(item)}>
                      <Download size={14} aria-hidden="true" />{busy === item.id ? 'Скачивание…' : 'Скачать .dump'}<span className="sr-only"> {item.filename}</span></button></td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
        )}
        {data && <Pager label="Страницы копий" noun="Копии" offset={offset} size={PAGE_SIZE} shown={data.items.length}
          hasMore={offset + data.items.length < data.total} disabled={loading} onChange={setOffset} prev="Новее" next="Старее" />}
      </Panel>
    </section>
  );
}
