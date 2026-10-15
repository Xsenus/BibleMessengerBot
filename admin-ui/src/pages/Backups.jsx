import { useState } from 'react';
import { downloadBackup } from '../api.js';
import { Status, explain, useData, when } from '../ui.jsx';

const PAGE_SIZE = 30;
const size = (bytes) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(bytes / 1024 ** 2) + ' МБ';

export default function Backups() {
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(null);
  const [message, setMessage] = useState('');
  const { data, error, loading, reload } = useData(`/backups?limit=${PAGE_SIZE}&offset=${offset}`);
  const download = async (item) => {
    setBusy(item.id); setMessage('Подготавливаем и скачиваем резервную копию…');
    try { await downloadBackup(item.id); setMessage('Файл передан браузеру для сохранения.'); }
    catch (e) { setMessage('⚠ ' + explain(e)); }
    finally { setBusy(null); }
  };
  return (
    <section>
      <h2>Резервные копии</h2>
      <p className="muted">Проверенные копии базы данных в облаке. Время показано в вашем часовом поясе ({Intl.DateTimeFormat().resolvedOptions().timeZone}).</p>
      <p>Скачивается готовый файл .dump с библейскими текстами, пользователями, расписаниями, картинками и озвучками. Архив проверяется и расшифровывается автоматически.</p>
      <button onClick={reload} disabled={loading}>Обновить список</button>
      <Status error={error} loading={loading} />
      {message && <p role="status">{message}</p>}
      {data && (<>
        {!data.download_enabled && <p className="error">Скачивание недоступно: проверьте настройки S3 и ключа шифрования на сервере.</p>}
        <p className="muted">Всего копий: {data.total}</p>
        {data.items.length === 0 ? <p>Проверенных копий пока нет. После успешного резервирования они появятся здесь.</p> : (
          <div className="backup-table"><table><thead><tr><th>Создана</th><th>Проверена в облаке</th><th>Файл</th><th>Размер</th><th></th></tr></thead>
            <tbody>{data.items.map((item) => <tr key={item.id}>
              <td>{when(item.created_at)}</td><td>{when(item.verified_at)}</td><td>{item.filename}</td><td>{size(item.size_bytes)}</td>
              <td><button aria-label={`Скачать ${item.filename}`} disabled={busy !== null || !data.download_enabled} onClick={() => download(item)}>{busy === item.id ? 'Скачивание…' : 'Скачать .dump'}</button></td>
            </tr>)}</tbody></table></div>
        )}
        {data.total > 0 && <div className="row">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>← Назад</button>
          <span>{offset + 1}–{Math.min(offset + data.items.length, data.total)} из {data.total}</span>
          <button disabled={offset + data.items.length >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Далее →</button>
        </div>}
      </>)}
    </section>
  );
}
