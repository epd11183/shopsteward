"""StubCapability -- the ONLY capability exercised in PR1 (M8a spec §3: PR1
ships ZERO real capabilities). Tunable knobs (max_tier, policy_verified,
precondition_ok, cost) let test_e2e_autonomy.py/test_governor.py/
test_registry.py drive every governor refusal and ladder transition without
touching Etsy or any other adapter."""

from datetime import UTC, datetime, timedelta

from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id


class StubCapability:
    def __init__(
        self,
        key: str = "stub.noop",
        max_tier: Tier = Tier.NOTIFY,
        policy_verified: bool = True,
        precondition_ok: bool = True,
        targets: dict[str, dict] | None = None,
        cost_usd: float = 0.0,
        undoable: bool = True,
    ) -> None:
        self.key = key
        self.max_tier = max_tier
        self.policy_verified = policy_verified
        self.precondition_ok = precondition_ok
        self.cost_usd = cost_usd
        self.store: dict[str, dict] = {
            t: dict(v) for t, v in (targets or {"t-1": {"on": True}}).items()
        }
        self.execute_calls: list[str] = []
        self.undo_calls: list[str] = []
        if not undoable:
            self.undo = None  # type: ignore[assignment]  -- registry() must refuse this above T2

    def propose(self, conn: object, user_id: int, cfg: object) -> list[ProposedAction]:
        # UTC, matching the DB's own strftime('...','now') created_at on
        # every event -- keeps this in sync with runner.py's day-bucketed
        # governor checks (daily cap, budget month, portfolio week).
        today_date = datetime.now(UTC).date()
        today = today_date.isoformat()
        cfg_hash = ops_config_hash(cfg)
        actions = []
        for target_id in self.store:
            inputs_hash = "fixed-inputs"
            action_id = compute_action_id(self.key, target_id, inputs_hash, cfg_hash, today)
            actions.append(
                ProposedAction(
                    action_id=action_id,
                    capability=self.key,
                    target_type="stub",
                    target_id=target_id,
                    tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
                    reason="stub proposes a no-op change for testing.",
                    inputs_hash=inputs_hash,
                    estimated_cost_usd=self.cost_usd,
                    undo_available=self.undo is not None,
                    expires_at=(
                        today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)
                    ).isoformat(),
                )
            )
        return actions

    def execute(self, conn: object, user_id: int, action: ProposedAction) -> ExecutionResult:
        self.execute_calls.append(action.target_id)
        before = dict(self.store[action.target_id])
        self.store[action.target_id]["on"] = False
        after = dict(self.store[action.target_id])
        return ExecutionResult(before=before, after=after, cost_usd=self.cost_usd, duration_ms=1)

    def undo(self, conn: object, user_id: int, action: ProposedAction) -> None:
        self.undo_calls.append(action.target_id)
        self.store[action.target_id]["on"] = True

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return self.cost_usd
