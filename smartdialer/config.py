"""Every tunable, with defaults from the brief. Stored per campaign in campaigns.settings (jsonb)."""
from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class CampaignSettings:
    # Borrower retries
    max_attempts: int = 3
    retry_base_s: float = 30.0
    retry_cap_s: float = 3600.0
    # Compliance: calling window in the campaign's timezone (RBI guidance: 08:00 to 19:00)
    window_start: str = "08:00"
    window_end: str = "19:00"
    # Pacing / permits
    pacing_tick_ms: int = 250
    leader_lease_ms: int = 3000
    permit_ttl_ms: int = 2000
    target_overflow: float = 0.01
    soon_free_discount: float = 0.5
    # Predictive answer handling
    max_answer_hold_ms: int = 2000
    # Safety Controller rules
    stale_snapshot_ms: int = 1000
    cold_start_samples: int = 50
    answer_window: int = 200
    short_window: int = 30
    drift_threshold: float = 0.15
    drift_z: float = 3.5
    drift_cooldown_s: float = 15.0
    max_overdial_ratio: float = 2.5
    absolute_inflight_cap: int = 500
    abandon_window: int = 100
    abandon_min_samples: int = 50   # below this, one abandon is >= 2% and would trip on noise
    abandon_threshold: float = 0.03
    abandon_resume: float = 0.015
    abandon_hysteresis_s: float = 60.0
    agent_drop_pct: float = 0.20
    agent_drop_window_s: float = 10.0
    agent_drop_cooldown_s: float = 30.0
    half_open_probes: int = 3
    degraded_factor: float = 0.5
    # Reconciliation (reaper)
    reconcile_pre_dial_s: float = 5.0       # pre-INITIATED longer than this: ask the provider
    reconcile_in_flight_s: float = 60.0     # INITIATED / RINGING with no news for this long
    reconcile_connected_s: float = 900.0
    reconcile_give_up_s: float = 120.0      # provider unreachable this long: fail the call

    @classmethod
    def from_json(cls, data: dict | None) -> "CampaignSettings":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})

    def to_json(self) -> dict:
        return asdict(self)


def load_settings(conn, campaign_id: int) -> CampaignSettings:
    row = conn.execute("SELECT settings FROM campaigns WHERE id = %s", (campaign_id,)).fetchone()
    return CampaignSettings.from_json(row["settings"])
