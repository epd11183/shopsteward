"""Planner adapter protocol for the LLM-narrated Brief (M8b slice 1, design
§5) and the intent-proposing planner (M8b slice 2, design §2). PlannerNarration/
PlannerUsage/ProposalIntent/CapabilityDescriptor/PlannerPlan are owned here
(not in `pipeline.ops`) for the same reason CopyInputs/CopyVerdict are owned
by `adapters.copy.interface`: import-linter contracts make this a one-way
relationship, `pipeline` may import `adapters.planner`, but no adapter may
import `pipeline`. That is also why `narrate()`/`plan()` take plain rendered
`str`/catalog args rather than `pipeline.ops.models` types -- the adapter
never sees the pipeline's own models, only already-rendered text/JSON.

ProposalIntent is deliberately thin -- it is NEVER an executable call and
NEVER carries an `action_id`/`inputs_hash` (design §2): the pipeline-side
validation gate (`pipeline.ops.planner.plan_proposals`) re-derives every
load-bearing number from real SQL via a capability's `materialize()` before
an intent can become a `ProposedAction`."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class PlannerUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    est_cost_usd: float


class PlannerNarration(BaseModel):
    text: str
    usage: PlannerUsage


class ProposalIntent(BaseModel):
    """One LLM-proposed intent -- MUST match a registered capability key and
    a target the deterministic grounding already blessed (design §2). `reason`
    is carried through only as commentary; the pipeline never trusts it as an
    audit reason (materialize() sets that from real data)."""

    # allow_inf_nan=False (M8b slice 3 B1 fix): a money-moving capability's
    # params can carry a price -- reject NaN/inf at parse time so a
    # malformed LLM response can never even construct an intent that later
    # slips a non-finite number past a capability's own bounds check
    # (belt-and-suspenders; capabilities validate again themselves).
    model_config = ConfigDict(allow_inf_nan=False)

    capability_key: str
    target_id: str
    # list[str] added (M8b slice 4a, `listing.seo_edit`'s tags) -- params
    # stays an open dict either way; the OpenRouter json_schema
    # (adapters/planner/openrouter.py's _INTENTS_SCHEMA) already declares
    # params as a free-form object, so this widening is Python-side only.
    params: dict[str, str | int | float | bool | list[str]] = {}
    reason: str = Field(min_length=1)


class PlannerLimits(BaseModel):
    """The configurable numeric bounds `plan()`'s prompt must tell the model
    about for the content-generating capabilities (`listing.reprice`,
    `listing.seo_edit`, `social.caption_draft`) -- plain primitives, not
    `pipeline.ops.models.OpsConfig` itself, so this stays adapter-owned (no
    adapter may import `pipeline`, mirroring `ProposalIntent`/
    `CapabilityDescriptor` above). The caller (`pipeline.ops.planner.
    plan_proposals`) builds this from the real `OpsConfig` at call time, so
    the prompt can never drift out of sync with what's actually enforced."""

    reprice_min_price_usd: float
    reprice_max_pct_change: float
    seo_edit_min_lifetime_views: int
    caption_max_len: int
    # `social.pinterest_post` Variant A (2026-08-24 design doc §2.2) -- the
    # LLM's `board_key` must be one of these keys; nothing else this
    # capability writes needs a numeric bound of its own (title/description/
    # alt_text limits are already Pinterest's own real ceilings, spelled out
    # in the prompt text directly rather than threaded through here).
    pinterest_max_title_len: int
    pinterest_max_description_len: int
    pinterest_max_alt_text_len: int
    pinterest_board_keys: list[str]


class CapabilityDescriptor(BaseModel):
    """A static catalog entry the planner is told it may choose from --
    adapter-owned, no pipeline import (design §1)."""

    key: str
    purpose: str
    params_schema: dict[str, str] = {}
    max_tier: int


class PlannerPlan(BaseModel):
    intents: list[ProposalIntent]
    usage: PlannerUsage


class PlannerAdapter(Protocol):
    def narrate(self, deterministic_brief_text: str) -> PlannerNarration: ...

    def plan(
        self, facts_json: str, catalog: list[CapabilityDescriptor], limits: PlannerLimits
    ) -> PlannerPlan: ...


class PlannerParseError(RuntimeError):
    """Raised when a planner-provider response cannot be parsed into a
    PlannerNarration/PlannerPlan (transport/parse failure -- mirrors
    CopyParseError)."""
