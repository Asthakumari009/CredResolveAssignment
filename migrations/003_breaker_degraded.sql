-- DEGRADED: provider still works but with an elevated failure rate. The Safety Controller reduces.
-- opened_at now means "in this breaker state since".
ALTER TABLE provider_health DROP CONSTRAINT provider_health_breaker_state_check;
ALTER TABLE provider_health ADD CONSTRAINT provider_health_breaker_state_check
    CHECK (breaker_state IN ('CLOSED','DEGRADED','OPEN','HALF_OPEN'));
