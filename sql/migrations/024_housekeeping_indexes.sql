-- Support retention pruning and operational metrics without full scans of ever-growing tables.
CREATE INDEX IF NOT EXISTS ix_delivery_log_sent_at ON delivery_log(sent_at) WHERE status = 'sent';
CREATE INDEX IF NOT EXISTS ix_delivery_log_prune ON delivery_log(updated_at) WHERE status IN ('sent', 'skipped');
CREATE INDEX IF NOT EXISTS ix_delivery_log_failed ON delivery_log(updated_at) WHERE status = 'failed';
CREATE INDEX IF NOT EXISTS ix_prayer_occurrences_delivery ON prayer_occurrences(delivery_id) WHERE delivery_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_operator_events_created ON operator_events(created_at);
CREATE INDEX IF NOT EXISTS ix_max_inbox_done ON max_inbox(updated_at) WHERE status = 'done';
