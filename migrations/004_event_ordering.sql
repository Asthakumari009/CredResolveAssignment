-- Per-call event ordering: highest provider sequence number applied so far, and an index so the
-- processor can cheaply check "is there an older unprocessed event for this call?".
ALTER TABLE calls ADD COLUMN last_event_seq int NOT NULL DEFAULT 0;
CREATE INDEX provider_events_call_pending ON provider_events (call_id, sequence, id) WHERE processed_at IS NULL;
-- Answer-rate windows sample calls by dial time.
CREATE INDEX calls_by_dial_time ON calls (campaign_id, created_at);
