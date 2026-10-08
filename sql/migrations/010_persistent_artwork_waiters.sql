-- Keep the original acknowledged cards until their shared artwork is available.
ALTER TABLE illustration_requests ALTER COLUMN expires_at DROP NOT NULL;
ALTER TABLE illustration_requests ALTER COLUMN expires_at DROP DEFAULT;
ALTER TABLE illustration_requests DROP CONSTRAINT illustration_requests_state_check;
ALTER TABLE illustration_requests ADD CONSTRAINT illustration_requests_state_check
 CHECK (state IN ('waiting','queued','delivered','unavailable','expired','cancelled'));
UPDATE illustration_requests SET expires_at=NULL;
-- Only revive cards whose exact target and source were recorded. Legacy unbound
-- follow-ups and operator-cancelled deliveries must never become new sends.
UPDATE illustration_requests SET state='waiting'
 WHERE state IN ('expired','cancelled') AND telegram_message_id IS NOT NULL
 AND jsonb_array_length(source_snapshot)>0 AND delivery_id IS NULL;
UPDATE illustration_requests r SET state='delivered' FROM delivery_log d
 WHERE r.delivery_id=d.id AND r.state='queued' AND d.status='sent';
UPDATE illustration_requests r SET state='unavailable' FROM delivery_log d
 WHERE r.delivery_id=d.id AND r.state='queued' AND d.status='failed'
 AND d.error_code='rejected';
UPDATE delivery_log d SET status='retry',retry_at=now(),sending_chunk=NULL,
 error_code='persistent_edit_resumed',updated_at=now()
 FROM illustration_requests r WHERE r.delivery_id=d.id AND r.state='queued'
 AND r.telegram_message_id IS NOT NULL AND d.mode='illustration_edit'
 AND jsonb_array_length(d.chunks)=1 AND d.chunks->0->>'kind'='rich_edit'
 AND (d.chunks->0->>'message_id')::bigint=r.telegram_message_id
 AND ((d.status='cancelled' AND d.error_code IN ('configuration_changed','stale_configuration'))
 OR (d.status='failed' AND d.error_code IN ('retry','forbidden')) OR d.status='uncertain');
