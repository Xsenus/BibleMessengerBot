import { useEffect, useState } from 'react';
import { BookOpen, CircleAlert, KeyRound } from 'lucide-react';
import { api, getKey, setKey } from './api.js';
import { explain } from './ui.jsx';
import { findSection } from './sections.js';
import { useHashRoute } from './hooks.js';
import Shell from './Shell.jsx';

function Login({ onDone }) {
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (event) => {
    event.preventDefault();
    setKey(value.trim());
    setBusy(true);
    try { await api('/overview'); onDone(); }
    catch (e) { setKey(''); setError(explain(e)); }
    finally { setBusy(false); }
  };
  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <span className="brand-mark brand-mark-lg" aria-hidden="true"><BookOpen size={22} /></span>
        <h1>Панель администратора</h1>
        <p className="muted">Введите ключ администратора (<code>ADMIN_API_KEY</code>). Он хранится только в этой вкладке.</p>
        <label className="field">
          <span className="field-label">Ключ</span>
          <span className="input-with-icon">
            <KeyRound size={16} aria-hidden="true" />
            <input type="password" autoComplete="off" autoFocus value={value} onChange={(e) => setValue(e.target.value)} placeholder="Ключ" />
          </span>
        </label>
        {error && <p className="callout callout-danger" role="alert"><CircleAlert size={16} aria-hidden="true" />{error}</p>}
        <button className="button-block" disabled={!value.trim() || busy}>Войти</button>
      </form>
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getKey()));
  const route = useHashRoute();
  const current = findSection(route);
  useEffect(() => {
    document.title = authed ? `${current.label} — Библия · панель` : 'Библия — панель администратора';
  }, [authed, current]);
  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  const Page = current.page;
  return (
    <Shell current={current} onLogout={() => { setKey(''); setAuthed(false); }}>
      <Page key={current.id} />
    </Shell>
  );
}
