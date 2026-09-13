"""Proposal Quality Feedback Tracker v2 — closes the DDD cultivation self-improvement loop.

V2 upgrades over V1:
- RejectionReason taxonomy (7 categories)
- Per-reason breakdown enables targeted channel fixes
- Auto-classification when user doesn't provide reason
- Self-correction loop: every 10 rejections → apply top-1 fix
- Anti-runaway: thresholds only increase, ceiling 0.95

Public symbols:
    - STATUS_APPROVED         — the persisted status a human approval writes
    - STATUS_REJECTED         — the persisted status a human rejection writes
    - RejectionReason         — enum of rejection categories
    - ProposalFeedbackTracker — main tracker class (compute stats, adjust thresholds)
"""
import json
import logging
import re
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── The persisted outcome vocabulary — ONE definition, consumed by BOTH sides ──
#
# These are the values a human decision writes into a proposal's JSON file, and
# the values this module counts when it measures a channel's precision. Both the
# PRODUCER (routers.cultivation's approve/reject endpoints, via
# mark_proposal_approved / mark_proposal_rejected) and the CONSUMER
# (compute_channel_stats below) import these names, so the two sides are one
# edge on a single definition rather than two independent string literals.
#
# Why that matters here specifically: this module used to count successes on the
# literal "approved", which nothing writes into a proposal file — the approve path
# writes "applied". (An unrelated "approved" is written onto the channel-approval
# DB row in channels/gateway: a different data plane that never reaches the
# proposal glob below, so the arm was dead here either way.) The success arm
# therefore read a permanent zero, precision computed as 0/(0+rejections) = 0.0,
# and get_adjusted_threshold ratcheted the channel's auto-write bar UP on a dead
# input. Renaming the vocabulary must now break loudly at one place rather than
# silently orphan the success arm again.
STATUS_APPROVED = "applied"
STATUS_REJECTED = "rejected"

# The same write-through binding for the field that TRAVELS WITH a rejection.
# Unifying the status alone left this sibling split: the writer persisted the
# reason under "reject_reason" while the breakdown below looked for
# "rejection_reason", so rejection_breakdown stayed permanently empty — and an
# empty breakdown has no dominant reason, which made get_adjusted_threshold
# unable to take its targeted branch and made check_self_correction return None
# at the exact moment a persistently-bad channel crossed the correction batch
# size. Same failure shape as the status arm, one field over.
REJECTION_REASON_KEY = "rejection_reason"
# Decisions already on disk carry whatever spelling their writer emitted, and a
# census of the live workspace found THREE. Read them all (canonical name first)
# rather than backfilling: rewriting a past human decision to normalise its key
# is a migration this does not need, and history is not ours to edit. Covering
# only SOME of the spellings is the quiet failure mode — the breakdown still
# fills, just from a minority of the evidence, so it looks like it works.
_LEGACY_REJECTION_REASON_KEYS = ("rejected_reason", "reject_reason")

# Anything outside REASON_FIX_MAP collapses here. The breakdown is PERSISTED and
# feeds a dominant-reason vote, so its keys must be a CLOSED set: real reasons
# embed a date ("Batch reject 2026-05-20: ..."), which would mint a permanent key
# per rejection campaign AND let an unclassifiable string win that vote and steer
# the threshold adjustment by accident. The count is kept — an unclassifiable
# rejection is still a signal about the channel — while the key space stays the
# known vocabulary plus this one bucket.
UNCLASSIFIED_REASON = "other"

# Threshold bounds — never too aggressive, never too permissive
THRESHOLD_FLOOR = 0.5
THRESHOLD_CEILING = 0.95
PRECISION_THRESHOLD = 0.4  # Below this precision → tighten confidence
ADJUSTMENT_STEP = 0.15  # How much to raise threshold when precision is low
SELF_CORRECTION_BATCH = 10  # Apply fix every N rejections from same channel


class RejectionReason(Enum):
    """Why a cultivation proposal was rejected.

    Each reason maps to a specific channel fix that prevents recurrence.
    """
    FALSE_POSITIVE = "false_positive"      # Detection was wrong
    STALE_CONTEXT = "stale_context"        # Info correct but already known/outdated
    WRONG_SECTION = "wrong_section"        # Right info, wrong DDD doc/section
    TOO_GRANULAR = "too_granular"          # Info too detailed for the doc level
    WRONG_PROJECT = "wrong_project"        # Routed to wrong project
    DUPLICATE = "duplicate"                # Already captured elsewhere
    JUDGMENT_NEEDED = "judgment_needed"    # Needs human rewrite, not auto-apply


# Maps rejection reason → which adjustment to apply
REASON_FIX_MAP: dict[str, str] = {
    "false_positive": "raise_confidence_threshold",
    "stale_context": "extend_dedupe_window",
    "wrong_section": "add_negative_routing_example",
    "too_granular": "increase_min_change_size",
    "wrong_project": "add_disambiguation_rule",
    "duplicate": "enable_cross_proposal_hash",
    "judgment_needed": "demote_to_suggest_only",
}


def canonical_rejection_reason(raw: object) -> str:
    """Map a human-written rejection reason onto a bounded token.

    The reason arrives as free text (an HTTP query param on the reject endpoint),
    so real persisted values read like ``"false positive: filename/attribute/
    noise, not a code symbol"`` while ``REASON_FIX_MAP`` is keyed on
    ``"false_positive"``. Two things go wrong if the raw string is used as the
    breakdown key: the fix map never resolves, and the key space of a persisted
    dict grows one sentence at a time.

    So the leading clause — everything before the first ``:``, em-dash or
    spaced hyphen, which is where a writer states the CATEGORY before explaining
    it — is normalised to a snake_case token, and a token outside
    ``REASON_FIX_MAP`` collapses to ``UNCLASSIFIED_REASON``. Length-capping alone
    was not enough: measured on the live workspace, the dominant real reason is
    ``"Batch reject 2026-05-20: ..."`` — a date, so each campaign would mint its
    own permanent key, and being unrecognised it would still WIN the
    dominant-reason vote and select a threshold-adjustment branch nobody
    intended. Collapsing keeps the count (an unclassifiable rejection is still a
    signal) while making the key space closed.

    ``raw`` is typed ``object`` on purpose: it comes from arbitrary persisted
    JSON, so a dict/int/list is reachable — and a bare ``.strip()`` on one used
    to raise out of the whole per-channel scan, killing stats for EVERY channel
    (the sole caller logs at debug and moves on, so the metric died silently and
    the self-correction loop right after it never ran). A non-string reason is
    unclassifiable but still a rejection that happened, so it collapses to the
    bounded bucket rather than being dropped or crashing.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return UNCLASSIFIED_REASON
    if not raw.strip():
        return ""
    head = re.split(r"[:—–]|\s-\s", raw, maxsplit=1)[0]
    token = re.sub(r"[^a-z0-9]+", "_", head.strip().lower()).strip("_")
    if not token:
        return UNCLASSIFIED_REASON
    return token if token in REASON_FIX_MAP else UNCLASSIFIED_REASON


class ProposalFeedbackTracker:
    """Tracks per-channel proposal precision with rejection reason breakdown.

    Reads proposal JSON files from .artifacts/proposals/, groups by source_stage,
    and computes {generated, approved, rejected, rejection_breakdown} per channel.
    When precision drops below threshold, applies targeted fixes based on the
    dominant rejection reason.
    """

    def compute_channel_stats(
        self,
        proposals_dir: Path,
        persist_to: Path | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Compute per-channel stats with rejection reason breakdown.

        Args:
            proposals_dir: Directory containing proposal_*.json files
            persist_to: If provided, write channel_stats.json here

        Returns:
            Dict mapping channel name → {generated, approved, rejected,
            rejection_breakdown, precision, adjustments_applied}
        """
        stats: dict[str, dict[str, Any]] = {}

        if not proposals_dir.is_dir():
            return stats

        # Scan BOTH the live dir AND archive/ (run_419ff7d4): the stale-proposal
        # reclaim sweep MOVES terminal proposals to proposals/archive/, so the
        # reject-precision counter must keep counting them or precision silently
        # degrades after every sweep. Archived proposals are terminal, so they only
        # add to the historical approved/rejected tallies — never to live "pending".
        archive_dir = proposals_dir / "archive"
        proposal_files = list(proposals_dir.glob("proposal_*.json"))
        if archive_dir.is_dir():
            proposal_files += list(archive_dir.glob("proposal_*.json"))

        for proposal_file in proposal_files:
            try:
                data = json.loads(proposal_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            source = data.get("source_stage", "unknown")
            status = data.get("status", "pending")

            if source not in stats:
                stats[source] = {
                    "generated": 0,
                    "approved": 0,
                    "rejected": 0,
                    "rejection_breakdown": {},
                }

            stats[source]["generated"] += 1
            if status == STATUS_APPROVED:
                stats[source]["approved"] += 1
            elif status == STATUS_REJECTED:
                stats[source]["rejected"] += 1
                # Track rejection reason. Read the shared key, falling back to
                # the pre-unification spelling so decisions already on disk keep
                # counting; canonicalise so the breakdown keys stay bounded and
                # resolvable against REASON_FIX_MAP.
                # Prefer the canonical key, but a file can carry two spellings at
                # once (a legacy writer plus a re-decision), and giving the
                # canonical key UNCONDITIONAL precedence discarded resolvable
                # evidence for an unclassifiable bucket. So take the first
                # spelling that canonicalises to a FIX-MAP token, and fall back
                # to canonical-key-first only when none of them resolves.
                candidates = [data.get(REJECTION_REASON_KEY)] + [
                    data.get(k) for k in _LEGACY_REJECTION_REASON_KEYS
                ]
                candidates = [c for c in candidates if c]
                reason = next(
                    (
                        r
                        for r in (canonical_rejection_reason(c) for c in candidates)
                        if r in REASON_FIX_MAP
                    ),
                    canonical_rejection_reason(candidates[0]) if candidates else "",
                )
                if reason:
                    breakdown = stats[source]["rejection_breakdown"]
                    breakdown[reason] = breakdown.get(reason, 0) + 1

        # Compute precision for each channel
        for channel_data in stats.values():
            approved = channel_data.get("approved", 0)
            rejected = channel_data.get("rejected", 0)
            decided = approved + rejected
            channel_data["precision"] = (
                round(approved / decided, 3) if decided > 0 else 1.0
            )

        # Persist if requested
        if persist_to and stats:
            stats_file = persist_to / "channel_stats.json"
            try:
                stats_file.write_text(
                    json.dumps(stats, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("proposal_feedback: failed to persist stats: %s", exc)

        return stats

    def get_adjusted_threshold(
        self,
        channel: str,
        base_threshold: float,
        stats: dict[str, dict[str, Any]],
    ) -> float:
        """Get confidence threshold for a channel, adjusted by precision.

        V2: adjustment is based on the DOMINANT rejection reason, not just
        overall precision. This enables targeted fixes rather than blanket
        threshold increases.

        Anti-runaway: threshold can only increase (never decrease automatically).
        Always bounded by [THRESHOLD_FLOOR, THRESHOLD_CEILING].

        Returns:
            Adjusted threshold (float between 0.5 and 0.95)
        """
        # Enforce floor on base
        threshold = max(base_threshold, THRESHOLD_FLOOR)

        channel_data = stats.get(channel)
        if not channel_data:
            return threshold

        # Compute precision over decided proposals only (not pending)
        approved = channel_data.get("approved", 0)
        rejected = channel_data.get("rejected", 0)
        decided = approved + rejected
        if decided == 0:
            return threshold

        precision = approved / decided

        if precision < PRECISION_THRESHOLD:
            # V2: check dominant reason for targeted adjustment
            breakdown = channel_data.get("rejection_breakdown", {})
            dominant_reason = self._get_dominant_reason(breakdown)

            if dominant_reason == "false_positive":
                # FP-heavy → stronger threshold increase
                threshold += ADJUSTMENT_STEP
            elif dominant_reason in ("stale_context", "duplicate"):
                # Staleness/duplicate → moderate increase
                threshold += ADJUSTMENT_STEP * 0.7
            else:
                # Other reasons → standard increase
                threshold += ADJUSTMENT_STEP * 0.5

        # Anti-runaway: enforce ceiling
        return min(threshold, THRESHOLD_CEILING)

    def check_self_correction(
        self,
        channel: str,
        stats: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Check if a channel has accumulated enough rejections to trigger a fix.

        Returns a fix recommendation if rejections >= SELF_CORRECTION_BATCH,
        or None if not yet triggered.

        Returns:
            {reason, fix_type, channel, rejection_count} or None
        """
        channel_data = stats.get(channel)
        if not channel_data:
            return None

        rejected = channel_data.get("rejected", 0)
        if rejected < SELF_CORRECTION_BATCH:
            return None

        breakdown = channel_data.get("rejection_breakdown", {})
        dominant_reason = self._get_dominant_reason(breakdown)
        if not dominant_reason:
            return None

        fix_type = REASON_FIX_MAP.get(dominant_reason, "unknown")

        return {
            "channel": channel,
            "reason": dominant_reason,
            "fix_type": fix_type,
            "rejection_count": rejected,
            "breakdown": breakdown,
        }

    def auto_classify_rejection(
        self,
        proposal_data: dict[str, Any],
        ddd_content: str | None = None,
    ) -> str:
        """Auto-classify a rejection reason when user doesn't provide one.

        Classification rules (in priority order):
        1. Proposal content already in DDD → duplicate
        2. Source data >7d old → stale_context
        3. Confidence < 0.6 → false_positive
        4. Default → judgment_needed

        Args:
            proposal_data: The proposal JSON data
            ddd_content: Current content of the target DDD doc (for duplicate check)

        Returns:
            RejectionReason value string
        """
        # Rule 1: duplicate check
        if ddd_content:
            proposed = proposal_data.get("proposed_content", "")
            if proposed and proposed.strip() in ddd_content:
                return RejectionReason.DUPLICATE.value

        # Rule 2: stale context
        created_at = proposal_data.get("created_at", "")
        if created_at:
            try:
                from datetime import timezone
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                # Normalize to UTC for comparison (PE-5: avoids naive/aware mismatch)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - created > timedelta(days=7):
                    return RejectionReason.STALE_CONTEXT.value
            except (ValueError, TypeError):
                pass

        # Rule 3: low confidence
        confidence = proposal_data.get("confidence", 1.0)
        if confidence < 0.6:
            return RejectionReason.FALSE_POSITIVE.value

        # Rule 4: default
        return RejectionReason.JUDGMENT_NEEDED.value

    @staticmethod
    def _get_dominant_reason(breakdown: dict[str, int]) -> str:
        """Get the most frequent rejection reason from breakdown dict."""
        if not breakdown:
            return ""
        return max(breakdown, key=lambda k: breakdown[k])
