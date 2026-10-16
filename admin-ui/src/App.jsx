import { useEffect, useState } from 'react';
import { BookOpen, CircleAlert, KeyRound } from 'lucide-react';
import { api, getKey, setKey } from './api.js';
import { explain } from './ui.jsx';
import { findSection } from './sections.js';
import { useHashRoute } from './hooks.js';
import Shell from './Shell.jsx';
import { DialogHost, Toaster } from './feedback.jsx';

function Login({ onDone, notice }) {
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
        {!error && notice && <p className="callout callout-info" role="alert"><CircleAlert size={16} aria-hidden="true" />{notice}</p>}
        {error && <p className="callout callout-danger" role="alert"><CircleAlert size={16} aria-hidden="true" />{error}</p>}
        <button className="button-block" disabled={!value.trim() || busy}>Войти</button>
      </form>
    </div>
  );
}

function Panel() {
  const [authed, setAuthed] = useState(Boolean(getKey()));
  const [notice, setNotice] = useState('');
  const route = useHashRoute();
  const current = findSection(route);
  useEffect(() => {
    document.title = authed ? `${current.label} — Библия · панель` : 'Библия — панель администратора';
  }, [authed, current]);
  useEffect(() => {
    if (!authed) return undefined;
    const expired = () => { setKey(''); setAuthed(false); setNotice('Ключ больше не подходит: возможно, его сменили на сервере. Войдите снова.'); };
    window.addEventListener('admin:unauthorized', expired);
    return () => window.removeEventListener('admin:unauthorized', expired);
  }, [authed]);
  if (!authed) return <Login notice={notice} onDone={() => { setNotice(''); setAuthed(true); }} />;
  const Page = current.page;
  return (
    <Shell current={current} onLogout={() => { setKey(''); setNotice(''); setAuthed(false); }}>
      <Page key={current.id} />
    </Shell>
  );
}

export default function App() {
  return <><Panel /><Toaster /><DialogHost /></>;
}
