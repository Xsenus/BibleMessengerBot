// Разделы панели: пункт меню, заголовок и данные из моков, которые должны быть видны.
export const SECTIONS = [
  { slug: 'overview', nav: 'Состояние', heading: 'Состояние', texts: ['4821', 'Версия и автодеплой', 'scheduler'] },
  { slug: 'announcements', nav: 'Объявления', heading: 'Объявления', texts: ['Субботнее чтение', 'Новое объявление'] },
  { slug: 'sources', nav: 'Источники', heading: 'Источники контента', texts: ['Слово на каждый день', 'Новые посты'] },
  { slug: 'videos', nav: 'Видео', heading: 'Видеоканалы', texts: ['Библейские уроки', 'HTTP 503 от rutube.ru'] },
  { slug: 'image-providers', nav: 'Генераторы картинок', heading: 'Генераторы картинок', texts: ['Cloudflare Flux', 'Готовность', 'gpt-image-1'] },
  { slug: 'gallery', nav: 'Картинки', heading: 'Картинки', texts: ['Выбор генератора и стиль', 'JHN 3:11'] },
  { slug: 'chats', nav: 'Чаты и пользователи', heading: 'Чаты и пользователи', texts: ['Домашняя группа'] },
  { slug: 'deliveries', nav: 'Доставки', heading: 'Доставки, требующие решения', texts: ['#501'] },
  { slug: 'reports', nav: 'Отчёты', heading: 'Отчёты о доставке', texts: ['chat_not_found', 'Субботнее чтение'] },
  { slug: 'audit', nav: 'Журнал', heading: 'Журнал действий', texts: ['announcement.create'] },
  { slug: 'admins', nav: 'Администраторы', heading: 'Администраторы', texts: ['Мария'] },
  { slug: 'network', nav: 'Сеть и прокси', heading: 'Сеть и прокси', texts: ['api.telegram.org', 'NL-1'] },
  { slug: 'backups', nav: 'Резервные копии', heading: 'Резервные копии', texts: ['biblebot-2026-10-16.dump.age', 'В наличии'] },
  { slug: 'settings', nav: 'Настройки', heading: 'Настройки', texts: ['Раздел «Поддержка проекта»', 'показан'] },
];
