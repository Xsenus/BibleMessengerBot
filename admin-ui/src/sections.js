// Разделы панели и группы меню. Адрес раздела — #/<id>.
import {
  Activity, ChartColumn, Clapperboard, DatabaseBackup, Image, Inbox, Megaphone, MessagesSquare,
  Network as NetworkIcon, Rss, ScrollText, Settings as SettingsIcon, ShieldCheck, WandSparkles,
} from 'lucide-react';
import Overview from './pages/Overview.jsx';
import Announcements from './pages/Announcements.jsx';
import Sources from './pages/Sources.jsx';
import Videos from './pages/Videos.jsx';
import Admins from './pages/Admins.jsx';
import ImageProviders from './pages/ImageProviders.jsx';
import Chats from './pages/Chats.jsx';
import Deliveries from './pages/Deliveries.jsx';
import Settings from './pages/Settings.jsx';
import Gallery from './pages/Gallery.jsx';
import Audit from './pages/Audit.jsx';
import Reports from './pages/Reports.jsx';
import Network from './pages/Network.jsx';
import Backups from './pages/Backups.jsx';

export const GROUPS = [
  { label: 'Обзор', items: [
    { id: 'overview', label: 'Состояние', icon: Activity, page: Overview },
    { id: 'deliveries', label: 'Доставки', icon: Inbox, page: Deliveries },
    { id: 'reports', label: 'Отчёты', icon: ChartColumn, page: Reports },
  ] },
  { label: 'Контент', items: [
    { id: 'announcements', label: 'Объявления', icon: Megaphone, page: Announcements },
    { id: 'sources', label: 'Источники', icon: Rss, page: Sources },
    { id: 'videos', label: 'Видео', icon: Clapperboard, page: Videos },
  ] },
  { label: 'Картинки', items: [
    { id: 'gallery', label: 'Картинки', icon: Image, page: Gallery },
    { id: 'images', label: 'Генераторы картинок', icon: WandSparkles, page: ImageProviders },
  ] },
  { label: 'Аудитория', items: [
    { id: 'chats', label: 'Чаты и пользователи', icon: MessagesSquare, page: Chats },
    { id: 'admins', label: 'Администраторы', icon: ShieldCheck, page: Admins },
  ] },
  { label: 'Система', items: [
    { id: 'network', label: 'Сеть и прокси', icon: NetworkIcon, page: Network },
    { id: 'backups', label: 'Резервные копии', icon: DatabaseBackup, page: Backups },
    { id: 'audit', label: 'Журнал', icon: ScrollText, page: Audit },
    { id: 'settings', label: 'Настройки', icon: SettingsIcon, page: Settings },
  ] },
];

export const SECTIONS = GROUPS.flatMap((group) => group.items.map((item) => ({ ...item, group: group.label })));
export const DEFAULT_SECTION = SECTIONS[0];
export const findSection = (id) => SECTIONS.find((section) => section.id === id) || DEFAULT_SECTION;
