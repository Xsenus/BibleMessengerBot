import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Сборка кладётся в app/web/static/panel и отдаётся FastAPI по адресу /panel/.
export default defineConfig({
  plugins: [react()],
  base: '/panel/',
  build: { outDir: '../app/web/static/panel', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8080' } },
});
