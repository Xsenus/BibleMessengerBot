import { useState } from 'react';
import { api, getKey, setKey } from './api.js';
import { explain } from './ui.jsx';
import Overview from './pages/Overview.jsx';
import Announcements from './pages/Announcements.jsx';
import Sources from './pages/Sources.jsx';
import Videos from './pages/Videos.jsx';
import Admins from './pages/Admins.jsx';
import ImageProviders from './pages/ImageProviders.jsx';
import Chats from './pages/Chats.jsx';
import Deliveries from './pages/Deliveries.jsx';
import Settings from './pages/Settings.jsx';

const PAGES = [
  ['overview', 'Состояние', Overview], ['announcements', 'Объявления', Announcements],
  ['sources', 'Источники', Sources], ['videos', 'Видео', Videos], ['images', 'Генераторы картинок', ImageProviders],
  ['chats', 'Чаты и пользователи', Chats], ['deliveries', 'Доставки', Deliveries], ['admins', 'Администраторы', Admins],
  ['settings', 'Настройки', Settings],
];

function Login({ onDone }) {
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const submit = async (event) => {
    event.preventDefault();
    setKey(value.trim());
    try { await api('/overview'); onDone(); }
    catch (e) { setKey(''); setError(explain(e)); }
  };
  return (
    <form className="card login" onSubmit={submit}>
      <h1>Панель администратора</h1>
      <p className="muted">Введите ключ администратора (ADMIN_API_KEY). Он хранится только в этой вкладке.</p>
      <input type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} placeholder="Ключ" />
      <button disabled={!value.trim()}>Войти</button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getKey()));
  const [page, setPage] = useState('overview');
  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  const Current = PAGES.find(([id]) => id === page)[2];
  return (
    <div className="layout">
      <nav>
        <strong>📖 Библия</strong>
        {PAGES.map(([id, label]) => (
          <button key={id} className={id === page ? 'active' : ''} onClick={() => setPage(id)}>{label}</button>
        ))}
        <button className="link" onClick={() => { setKey(''); setAuthed(false); }}>Выйти</button>
      </nav>
      <main><Current /></main>
    </div>
  );
}
