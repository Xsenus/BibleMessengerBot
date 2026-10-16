import { useEffect, useState } from 'react';
import { ImageOff, Images, Pin, Search } from 'lucide-react';
import { api, getKey } from '../api.js';
import { Help, Status, useAction, useData } from '../ui.jsx';
import { DataTable, EmptyState, Field, PageHeader, Panel, StatusBadge, TimeAgo, formatNumber } from '../components.jsx';

const pct = (value) => (value === null || value === undefined ? '—' : `${Math.round(value * 100)} %`);

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
  return <div className="thumb">{src ? <img src={src} alt="" /> : <ImageOff size={20} aria-hidden="true" />}</div>;
}

function Tuning() {
  const { data, error, loading, reload } = useData('/image-tuning');
  const { run, message, busy } = useAction(reload);
  const [style, setStyle] = useState(null);
  const current = style ?? data?.style ?? '';
  const save = (body) => run(() => api('/image-tuning', { method: 'PUT', body }));
  const stats = data ? Object.entries(data.stats).map(([name, s]) => ({ name, ...s })) : null;
  return (
    <Panel title="Выбор генератора и стиль">
      <Status error={error} loading={loading} />
      {data && (<>
        <div className="form-grid form-grid-wide">
          <Field label="Порядок выбора генератора" hint="Автоматический режим учитывает долю успешных генераций, брак по надписям и скорость за 7 суток.">
            <select value={data.ranking} disabled={busy} onChange={(e) => save({ ranking: e.target.value })}>
              <option value="priority">По приоритету (как задано вручную)</option>
              <option value="auto">Автоматически — лучшие по статистике первыми</option>
            </select>
          </Field>
          <Field label="Общий стиль картинок" hint="Добавляется к каждому заданию.">
            <span className="input-row">
              <input value={current} maxLength={300} onChange={(e) => setStyle(e.target.value)} placeholder="например: oil painting, warm light" />
              <button className="small" disabled={busy || style === null} onClick={() => save({ style: current })}>Сохранить стиль</button>
            </span>
          </Field>
        </div>
        <DataTable caption="Статистика генераторов за 7 суток" rows={stats} rowKey={(s) => s.name}
          empty={<p className="muted">Статистики за 7 суток пока нет.</p>} columns={[
            { key: 'name', header: 'Генератор', render: (s) => <code>{s.name}</code> },
            { key: 'attempts', header: 'Попыток', align: 'end', render: (s) => formatNumber(s.attempts) },
            { key: 'success', header: 'Успех', align: 'end', render: (s) => pct(s.success_rate) },
            { key: 'rejected', header: 'Брак', align: 'end', render: (s) => formatNumber(s.rejected) },
            { key: 'avg', header: 'Время, с', align: 'end', render: (s) => (s.avg_seconds ?? '—') },
            { key: 'score', header: 'Оценка', align: 'end', render: (s) => (s.score ?? '—') },
          ]} />
        {message && <p className="form-message" role="alert">{message}</p>}
      </>)}
    </Panel>
  );
}

export default function Gallery() {
  const [book, setBook] = useState('');
  const { data, error, loading, reload } = useData(`/illustrations?q=${encodeURIComponent(book)}&limit=24`);
  const { run, message, busy } = useAction(reload);
  return (
    <section>
      <PageHeader title="Картинки" description="Иллюстрации к стихам: закрепление, новые варианты и карантин неудачных." />
      <Help>
        <p>Для одного стиха может быть несколько версий. Закреплённая версия показывается первой. Версию с ошибкой (например, с лишней надписью) отправьте в карантин — она перестанет показываться, но останется в базе.</p>
      </Help>
      <Tuning />
      <Panel title="Последние картинки" actions={
        <label className="input-with-icon search-box"><Search size={15} aria-hidden="true" />
          <input value={book} onChange={(e) => setBook(e.target.value.toUpperCase())} placeholder="Код книги, например JHN" aria-label="Книга (код, например JHN)" /></label>}>
        <Status error={error} loading={loading} />
        {data?.items.length === 0 && <EmptyState icon={Images} title="Картинок не найдено">{book ? 'Проверьте код книги.' : 'Они появятся после первых генераций.'}</EmptyState>}
        <div className="gallery">
          {(data?.items || []).map((i) => (
            <figure key={i.id} className={`gallery-item${i.quarantined_at ? ' is-quarantined' : ''}`}>
              <Thumb id={i.id} />
              <figcaption>
                <div className="gallery-ref"><b>{i.book_code} {i.chapter}:{i.verse}{i.verse_end ? `–${i.verse_end}` : ''}</b>
                  {i.pinned && <StatusBadge tone="pending"><Pin size={11} aria-hidden="true" />закреплена</StatusBadge>}
                  {i.quarantined_at && <StatusBadge tone="error">в карантине</StatusBadge>}</div>
                <small><TimeAgo value={i.generated_at} /></small>
              </figcaption>
              <div className="gallery-actions">
                <button className="small" disabled={busy} onClick={() => run(() => api(`/illustrations/${i.id}/pin?pinned=${!i.pinned}`, { method: 'POST' }))}>{i.pinned ? 'Открепить' : 'Закрепить'}</button>
                <button className="small" disabled={busy} title="Создать ещё один вариант для этого текста" onClick={() => run(() => api(`/illustrations/${i.id}/variant`, { method: 'POST' }), 'Вариант заказан')}>+ вариант</button>
                <button className={`small${i.quarantined_at ? '' : ' danger'}`} disabled={busy} onClick={() => run(() => api(`/illustrations/${i.id}/quarantine?quarantined=${!i.quarantined_at}`, { method: 'POST' }))}>{i.quarantined_at ? 'Вернуть' : 'В карантин'}</button>
              </div>
            </figure>
          ))}
        </div>
        {message && <p className="form-message" role="alert">{message}</p>}
      </Panel>
    </section>
  );
}
