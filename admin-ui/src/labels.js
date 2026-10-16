// Понятные названия для кодов из API. Неизвестный код показывается как есть.
export const PLATFORMS = { telegram: 'Telegram', max: 'MAX', vk: 'ВКонтакте', youtube: 'YouTube', rutube: 'RuTube' };

export const SERVICES = {
  bot: 'Бот Telegram', maxbot: 'Бот MAX', vkbot: 'Бот ВКонтакте', worker: 'Рассылка и очередь',
  composer: 'Объявления и молитвы', illustrator: 'Картинки', speaker: 'Озвучка',
};

export const DELIVERY_MODES = {
  sequential: 'Чтение по порядку', verse_of_day: 'Стих дня', topic_of_day: 'Тема дня', reading_plan: 'План чтения',
  morning_verse: 'Утренний стих', evening_verse: 'Вечерний стих', prayer: 'Молитва', manual: 'Вручную',
  announcement: 'Объявление', video_notice: 'Новое видео', source_post: 'Пост источника', illustration_edit: 'Картинка к чтению',
};

/** Статус доставки: подпись и тон метки. */
export const DELIVERY_STATUSES = {
  pending: ['В очереди', 'pending'], sending: ['Отправляется', 'pending'], retry: ['Повтор', 'pending'],
  sent: ['Доставлено', 'ok'], skipped: ['Пропущено', 'off'], cancelled: ['Отменено', 'off'],
  failed: ['Ошибка', 'error'], uncertain: ['Исход неизвестен', 'warning'],
};

export const RUN_STATUSES = { sending: ['Отправляется', 'pending'], done: ['Завершён', 'ok'], skipped_late: ['Пропущен: опоздал', 'off'] };

export const label = (dictionary, code) => dictionary[code] || code || '—';
