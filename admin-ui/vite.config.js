import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Сборка кладётся в app/web/static/panel и отдаётся FastAPI по адресу /panel/.
export default defineConfig({
  plugins: [react()],
  base: '/panel/',
  // Браузеры — как по умолчанию в Vite 6 («modules»). Vite 8 по умолчанию требует Safari 16.4+ и пишет медиазапросы
  // как (width<=720px): на iOS 15–16.3 мобильная вёрстка перестала бы включаться.
  build: { outDir: '../app/web/static/panel', emptyOutDir: true, target: ['es2020', 'edge88', 'firefox78', 'chrome87', 'safari14'] },
  server: { proxy: { '/api': 'http://127.0.0.1:8080' } },
});
