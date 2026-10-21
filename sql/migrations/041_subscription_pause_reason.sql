-- Почему подписка выключена системой. 'bot_unavailable' — бота остановили, заблокировали или удалили из чата:
-- такие подписки включаются сами, когда бота снова запускают. NULL — включена или выключена вручную
-- (/pause, панель, сбой доставки) и сама не включается.
ALTER TABLE subscriptions ADD COLUMN paused_reason text CHECK (paused_reason IN ('bot_unavailable'));
