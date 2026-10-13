import { useEffect, useState } from 'react';
import { api, getKey } from '../api.js';
import { Help, Status, useAction, useData, when } from '../ui.jsx';

function Thumb({ id }) {
  const [src, setSrc] = useState('');
  useEffect(() => {
    let url = '';
    let dead = false;
    fetch(`/api/admin/illustrations/${id}/thumb`, { headers: { 'X-Admin-Key': getKey() } })
      .then((r) => (r.ok ? r.blob() : null))
      .then((blob) => { if (blob && !dead) { url = URL.createObjectURL(blob); setSrc(url); } })
      .catch(() => {});
    return () => { dead = true; if (url) URL.revokeObjectURL(url); };
  }, [id]);
  return src ? <img src={src} alt="" style={{ width: '100%', borderRadius: 6 }} /> : <div className="muted">…</div>;
}

function Tuning() {
  const { data, error, loading, reload } = useData('/image-tuning');
  const { run, message, busy } = useAction(reload);
  const [style, setStyle] = useState(null);
  const current = style ?? data?.style ?? '';
  const save = (body) => run(() => api('/image-tuning', { method: 'PUT', body }));
  return (
    <div className="card">
      <h3>Выбор генератора и стиль</h3>
      <Status error={error} loading={loading} />
      {data && (<>
        <label>Порядок выбора генератора{' '}
          <select value={data.ranking} disabled={busy} onChange={(e) => save({ ranking: e.target.value })}>
            <option value="priority">По приоритету (как задано вручную)</option>
            <option value="auto">Автоматически — лучшие по статистике первыми</option>
          </select></label>
        <p className="muted">Автоматический режим учитывает долю успешных генераций, брак по надписям и скорость за 7 суток.</p>
        <label>Общий стиль картинок (добавляется к каждому заданию)
          <input value={current} maxLength={300} onChange={(e) => setStyle(e.target.value)} placeholder="например: oil painting, warm light" /></label>
        <button disabled={busy || style === null} onClick={() => save({ style: current })}>Сохранить стиль</button>
        <table><thead><tr><th>Генератор</th><th>Попыток</th><th>Успех</th><th>Брак</th><th>Время, с</th><th>Оценка</th></tr></thead><tbody>
          {Object.entries(data.stats).map(([name, s]) => (
            <tr key={name}><td>{name}</td><td>{s.attempts}</td><td>{s.success_rate ?? '—'}</td><td>{s.rejected}</td><td>{s.avg_seconds}</td><td>{s.score}</td></tr>))}
        </tbody></table>
        {message && <p className="muted">{message}</p>}
      </>)}
    </div>
  );
}

export default function Gallery() {
  const [book, setBook] = useState('');
  const { data, error, loading, reload } = useData(`/illustrations?q=${encodeURIComponent(book)}&limit=24`);
  const { run, busy } = useAction(reload);
  return (
    <section>
      <h2>Картинки</h2>
      <Help>
        <p>Для одного стиха может быть несколько версий. Закреплённая версия показывается первой. Версию с ошибкой (например, с лишней надписью) отправьте в карантин — она перестанет показываться, но останется в базе.</p>
      </Help>
      <Tuning />
      <label>Книга (код, например JHN) <input value={book} onChange={(e) => setBook(e.target.value.toUpperCase())} /></label>
      <Status error={error} loading={loading} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(160px,1fr))', gap: 12 }}>
        {(data?.items || []).map((i) => (
          <div key={i.id} className="card" style={{ opacity: i.quarantined_at ? 0.45 : 1 }}>
            <Thumb id={i.id} />
            <div className="muted">{i.book_code} {i.chapter}:{i.verse}{i.verse_end ? `–${i.verse_end}` : ''} · {when(i.generated_at)}</div>
            <button className="small" disabled={busy} onClick={() => run(() => api(`/illustrations/${i.id}/pin?pinned=${!i.pinned}`, { method: 'POST' }))}>{i.pinned ? '📌 Открепить' : 'Закрепить'}</button>{' '}
            <button className="small" disabled={busy} title="Создать ещё один вариант для этого текста" onClick={() => run(() => api(`/illustrations/${i.id}/variant`, { method: 'POST' }), 'Вариант заказан')}>+ вариант</button>{' '}
            <button className="small" disabled={busy} onClick={() => run(() => api(`/illustrations/${i.id}/quarantine?quarantined=${!i.quarantined_at}`, { method: 'POST' }))}>{i.quarantined_at ? 'Вернуть' : 'В карантин'}</button>
          </div>))}
      </div>
    </section>
  );
}
