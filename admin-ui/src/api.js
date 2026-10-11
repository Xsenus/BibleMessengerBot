// Тонкий клиент к /api/admin: ключ хранится только в sessionStorage вкладки и уходит заголовком X-Admin-Key.
const KEY = 'biblebot-admin-key';

export const getKey = () => {
  try { return sessionStorage.getItem(KEY) || ''; } catch { return ''; }
};
export const setKey = (value) => {
  try { value ? sessionStorage.setItem(KEY, value) : sessionStorage.removeItem(KEY); } catch { /* нет хранилища */ }
};

export class ApiError extends Error {
  constructor(status, detail) { super(detail || String(status)); this.status = status; }
}

export async function api(path, { method = 'GET', body, raw } = {}) {
  const headers = { 'X-Admin-Key': getKey() };
  let payload;
  if (raw) payload = raw;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  let response;
  try { response = await fetch(`/api/admin${path}`, { method, headers, body: payload }); }
  catch { throw new ApiError(0, 'Нет связи с сервером'); }
  if (!response.ok) {
    let detail = '';
    try { const data = await response.json(); detail = typeof data.detail === 'string' ? data.detail : 'Некорректные данные'; } catch { /* пустой ответ */ }
    throw new ApiError(response.status, detail);
  }
  return response.json();
}
