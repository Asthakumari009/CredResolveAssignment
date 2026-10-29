-- EVENT_LAG reads the inbox by arrival time on every pacing tick.
CREATE INDEX provider_events_received ON provider_events (received_at);
