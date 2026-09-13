"""Tests for ProposalFeedbackTracker — per-channel precision tracking + threshold adjustment.

Verifies:
- AC4: channel_stats.json tracks per-channel counts
- AC5: When precision < 40%, threshold tightens
"""
import json

# These fixtures reference the SHARED outcome constants rather than spelling the
# status themselves. They used to hard-code "approved" — a value no production
# writer has ever persisted — so the whole module stayed green over a dead code
# path while the success arm of channel precision read a permanent zero. A fixture
# that names its own vocabulary cannot detect a producer/consumer split; see
# TestProducerConsumerParity at the bottom, which drives the real writer instead.
from core.proposal_feedback import STATUS_APPROVED, STATUS_REJECTED

import pytest


class TestComputeChannelStats:
    """AC4: channel_stats.json tracks per-channel {generated, approved, rejected}."""

    def test_import_and_instantiate(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        assert hasattr(tracker, "compute_channel_stats")

    def test_empty_proposals_dir(self, tmp_path):
        from core.proposal_feedback import ProposalFeedbackTracker

        proposals_dir = tmp_path / "proposals"
        proposals_dir.mkdir()

        tracker = ProposalFeedbackTracker()
        stats = tracker.compute_channel_stats(proposals_dir)
        assert stats == {}

    def test_archived_proposals_still_counted(self, tmp_path):
        """run_419ff7d4 (Debt1-safety): after the sweep MOVES terminal proposals to
        proposals/archive/, compute_channel_stats MUST still count them (widened glob),
        else the reject-precision loop silently degrades. Stats identical whether a
        terminal proposal sits in proposals/ or proposals/archive/."""
        from core.proposal_feedback import ProposalFeedbackTracker
        proposals_dir = tmp_path / "proposals"
        (proposals_dir / "archive").mkdir(parents=True)

        # one rejected LIVE-dir, one rejected ARCHIVED — both must count.
        (proposals_dir / "proposal_live.json").write_text(json.dumps({
            "source_stage": "code_intel_feed", "status": STATUS_REJECTED,
        }))
        (proposals_dir / "archive" / "proposal_arch.json").write_text(json.dumps({
            "source_stage": "code_intel_feed", "status": STATUS_REJECTED,
        }))

        tracker = ProposalFeedbackTracker()
        stats = tracker.compute_channel_stats(proposals_dir)
        assert stats["code_intel_feed"]["rejected"] == 2, "archived proposal must still be counted"
        assert stats["code_intel_feed"]["generated"] == 2

    def test_counts_by_source_stage(self, tmp_path):
        from core.proposal_feedback import ProposalFeedbackTracker

        proposals_dir = tmp_path / "proposals"
        proposals_dir.mkdir()

        # 3 rejected from code_intel_feed, 2 approved from signal_ddd_bridge
        for i in range(3):
            (proposals_dir / f"proposal_code_{i}.json").write_text(json.dumps({
                "source_stage": "code_intel_feed",
                "status": STATUS_REJECTED,
            }))
        for i in range(2):
            (proposals_dir / f"proposal_signal_{i}.json").write_text(json.dumps({
                "source_stage": "signal_ddd_bridge",
                "status": STATUS_APPROVED,
            }))

        tracker = ProposalFeedbackTracker()
        stats = tracker.compute_channel_stats(proposals_dir)

        assert stats["code_intel_feed"]["rejected"] == 3
        assert stats["code_intel_feed"]["generated"] == 3
        assert stats["signal_ddd_bridge"]["approved"] == 2
        assert stats["signal_ddd_bridge"]["generated"] == 2

    def test_pending_counted_as_generated(self, tmp_path):
        from core.proposal_feedback import ProposalFeedbackTracker

        proposals_dir = tmp_path / "proposals"
        proposals_dir.mkdir()

        (proposals_dir / "proposal_1.json").write_text(json.dumps({
            "source_stage": "reflect_feed",
            "status": "pending",
        }))

        tracker = ProposalFeedbackTracker()
        stats = tracker.compute_channel_stats(proposals_dir)

        assert stats["reflect_feed"]["generated"] == 1
        assert stats["reflect_feed"]["approved"] == 0
        assert stats["reflect_feed"]["rejected"] == 0

    def test_persist_stats(self, tmp_path):
        from core.proposal_feedback import ProposalFeedbackTracker

        proposals_dir = tmp_path / "proposals"
        proposals_dir.mkdir()
        artifacts_dir = tmp_path / ".artifacts"
        artifacts_dir.mkdir()

        (proposals_dir / "proposal_p1.json").write_text(json.dumps({
            "source_stage": "ch1",
            "status": STATUS_APPROVED,
        }))

        tracker = ProposalFeedbackTracker()
        tracker.compute_channel_stats(proposals_dir, persist_to=artifacts_dir)

        stats_file = artifacts_dir / "channel_stats.json"
        assert stats_file.exists()
        data = json.loads(stats_file.read_text())
        assert "ch1" in data


class TestThresholdAdjustment:
    """AC5: When precision < 40%, threshold increases (reason-aware in v2)."""

    def test_high_precision_no_adjustment(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        stats = {"test_channel": {
            "generated": 10, "approved": 8, "rejected": 2,
            "rejection_breakdown": {"false_positive": 2},
        }}

        threshold = tracker.get_adjusted_threshold("test_channel", 0.7, stats)
        # 80% precision — no adjustment needed
        assert threshold == 0.7

    def test_low_precision_fp_dominant(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        # 30% precision, dominant reason = false_positive
        stats = {"test_channel": {
            "generated": 12, "approved": 3, "rejected": 7,
            "rejection_breakdown": {"false_positive": 5, "stale_context": 2},
        }}

        threshold = tracker.get_adjusted_threshold("test_channel", 0.7, stats)
        # FP-dominant → full ADJUSTMENT_STEP (0.15)
        assert threshold == pytest.approx(0.85)

    def test_low_precision_stale_dominant(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        stats = {"test_channel": {
            "generated": 12, "approved": 3, "rejected": 7,
            "rejection_breakdown": {"stale_context": 5, "false_positive": 2},
        }}

        threshold = tracker.get_adjusted_threshold("test_channel", 0.7, stats)
        # Stale-dominant → 0.7 * ADJUSTMENT_STEP = 0.105
        assert threshold == pytest.approx(0.7 + 0.15 * 0.7, abs=0.01)

    def test_threshold_caps_at_095(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        stats = {"test_channel": {
            "generated": 10, "approved": 1, "rejected": 9,
            "rejection_breakdown": {"false_positive": 9},
        }}

        threshold = tracker.get_adjusted_threshold("test_channel", 0.9, stats)
        # 0.9 + 0.15 would be 1.05, but cap at 0.95
        assert threshold == 0.95

    def test_threshold_floor_at_050(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        stats = {}

        threshold = tracker.get_adjusted_threshold("unknown", 0.3, stats)
        assert threshold == 0.5

    def test_unknown_channel_returns_base(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        stats = {"other_channel": {
            "generated": 10, "approved": 5, "rejected": 5,
            "rejection_breakdown": {},
        }}

        threshold = tracker.get_adjusted_threshold("unknown", 0.7, stats)
        assert threshold == 0.7


class TestRejectionReasonBreakdown:
    """V2: rejection reason tracking + per-reason breakdown."""

    def test_breakdown_tracked_in_stats(self, tmp_path):
        from core.proposal_feedback import ProposalFeedbackTracker

        proposals_dir = tmp_path / "proposals"
        proposals_dir.mkdir()

        (proposals_dir / "proposal_1.json").write_text(json.dumps({
            "source_stage": "code_intel_feed",
            "status": STATUS_REJECTED,
            "rejection_reason": "false_positive",
        }))
        (proposals_dir / "proposal_2.json").write_text(json.dumps({
            "source_stage": "code_intel_feed",
            "status": STATUS_REJECTED,
            "rejection_reason": "stale_context",
        }))
        (proposals_dir / "proposal_3.json").write_text(json.dumps({
            "source_stage": "code_intel_feed",
            "status": STATUS_REJECTED,
            "rejection_reason": "false_positive",
        }))

        tracker = ProposalFeedbackTracker()
        stats = tracker.compute_channel_stats(proposals_dir)

        breakdown = stats["code_intel_feed"]["rejection_breakdown"]
        assert breakdown["false_positive"] == 2
        assert breakdown["stale_context"] == 1

    def test_precision_computed(self, tmp_path):
        from core.proposal_feedback import ProposalFeedbackTracker

        proposals_dir = tmp_path / "proposals"
        proposals_dir.mkdir()

        for i in range(3):
            (proposals_dir / f"proposal_a{i}.json").write_text(json.dumps({
                "source_stage": "ch1", "status": STATUS_APPROVED,
            }))
        for i in range(7):
            (proposals_dir / f"proposal_r{i}.json").write_text(json.dumps({
                "source_stage": "ch1", "status": STATUS_REJECTED,
                "rejection_reason": "false_positive",
            }))

        tracker = ProposalFeedbackTracker()
        stats = tracker.compute_channel_stats(proposals_dir)

        assert stats["ch1"]["precision"] == pytest.approx(0.3, abs=0.01)


class TestSelfCorrection:
    """V2: self-correction triggers after N rejections."""

    def test_no_trigger_below_batch(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        stats = {"ch1": {
            "generated": 8, "approved": 2, "rejected": 6,
            "rejection_breakdown": {"false_positive": 6},
        }}

        result = tracker.check_self_correction("ch1", stats)
        assert result is None  # < 10 rejections

    def test_trigger_at_batch_threshold(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        stats = {"ch1": {
            "generated": 15, "approved": 3, "rejected": 12,
            "rejection_breakdown": {"false_positive": 8, "stale_context": 4},
        }}

        result = tracker.check_self_correction("ch1", stats)
        assert result is not None
        assert result["channel"] == "ch1"
        assert result["reason"] == "false_positive"  # dominant
        assert result["fix_type"] == "raise_confidence_threshold"

    def test_dominant_reason_maps_to_correct_fix(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        stats = {"ch1": {
            "generated": 20, "approved": 5, "rejected": 15,
            "rejection_breakdown": {"duplicate": 10, "false_positive": 5},
        }}

        result = tracker.check_self_correction("ch1", stats)
        assert result["reason"] == "duplicate"
        assert result["fix_type"] == "enable_cross_proposal_hash"


class TestAutoClassification:
    """V2: auto-classify rejection reason when user doesn't provide one."""

    def test_duplicate_when_content_in_ddd(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        proposal = {"proposed_content": "## Key Decision\nUse async everywhere"}
        ddd_content = "# TECH\n\n## Key Decision\nUse async everywhere\n\n## Stack"

        reason = tracker.auto_classify_rejection(proposal, ddd_content)
        assert reason == "duplicate"

    def test_stale_when_old(self):
        from core.proposal_feedback import ProposalFeedbackTracker
        from datetime import datetime, timedelta, timezone

        tracker = ProposalFeedbackTracker()
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        proposal = {"created_at": old_date, "confidence": 0.9}

        reason = tracker.auto_classify_rejection(proposal, "")
        assert reason == "stale_context"

    def test_false_positive_when_low_confidence(self):
        from core.proposal_feedback import ProposalFeedbackTracker

        tracker = ProposalFeedbackTracker()
        proposal = {"confidence": 0.4, "created_at": ""}

        reason = tracker.auto_classify_rejection(proposal, "different content")
        assert reason == "false_positive"

    def test_default_judgment_needed(self):
        from core.proposal_feedback import ProposalFeedbackTracker
        from datetime import datetime, timezone

        tracker = ProposalFeedbackTracker()
        recent = datetime.now(timezone.utc).isoformat()
        proposal = {"confidence": 0.9, "created_at": recent, "proposed_content": "new stuff"}

        reason = tracker.auto_classify_rejection(proposal, "completely different")
        assert reason == "judgment_needed"


class TestProducerConsumerParity:
    """AC12 — end-to-end parity: the REAL human-approval writer into the REAL reader.

    Why this class exists, and why it may not write a status literal:
    ``compute_channel_stats`` counted a channel success on the string
    ``"approved"``, which no production writer has ever persisted — the human
    approve endpoint writes ``"applied"`` (routers/cultivation.py) while the
    reject endpoint writes ``"rejected"``. The success arm therefore read a
    permanent 0, precision computed as 0/(0+rejections) = 0.0, that fell under
    ``PRECISION_THRESHOLD``, and ``get_adjusted_threshold`` ratcheted the
    channel's auto-write bar UP — a one-way ratchet fed by a dead input.

    Every pre-existing test in this module passed throughout, because their
    fixtures hand-wrote ``"approved"`` — the fixture encoded the defect it
    should have caught. So these tests are forbidden from writing a status
    string themselves: the status must come from the production writer. A
    future rename of the persisted vocabulary then breaks THIS test instead of
    silently re-orphaning the success arm.
    """

    @staticmethod
    def _seed_pending(proposals_dir, channel, ids):
        """Create pending proposals. 'pending' is the pre-decision state, not an
        outcome — the outcome statuses are written exclusively by the producer."""
        import json as _json
        for pid in ids:
            (proposals_dir / f"proposal_{pid}.json").write_text(
                _json.dumps({"id": pid, "source_stage": channel, "status": "pending"}),
                encoding="utf-8",
            )

    def test_human_approvals_are_counted_as_successes(self, tmp_path):
        """N approves + M rejects through the real writer -> precision N/(N+M)."""
        from core.proposal_feedback import ProposalFeedbackTracker
        from routers.cultivation import mark_proposal_approved, mark_proposal_rejected

        proposals_dir = tmp_path / ".artifacts" / "proposals"
        proposals_dir.mkdir(parents=True)
        self._seed_pending(proposals_dir, "reflect", ["a1", "a2", "r1"])

        # The production writer decides the persisted vocabulary, not this test.
        mark_proposal_approved(tmp_path, "a1")
        mark_proposal_approved(tmp_path, "a2")
        mark_proposal_rejected(tmp_path, "r1")

        stats = ProposalFeedbackTracker().compute_channel_stats(proposals_dir)
        assert stats["reflect"]["approved"] == 2, (
            "the reader must count the status the producer actually persists"
        )
        assert stats["reflect"]["rejected"] == 1
        assert stats["reflect"]["precision"] == round(2 / 3, 3)

    def test_good_precision_does_not_ratchet_the_threshold(self, tmp_path):
        """The compounding harm: an uncounted success arm raises the bar forever."""
        from core.proposal_feedback import ProposalFeedbackTracker
        from routers.cultivation import mark_proposal_approved, mark_proposal_rejected

        proposals_dir = tmp_path / ".artifacts" / "proposals"
        proposals_dir.mkdir(parents=True)
        self._seed_pending(proposals_dir, "reflect", ["a1", "a2", "r1"])
        mark_proposal_approved(tmp_path, "a1")
        mark_proposal_approved(tmp_path, "a2")
        mark_proposal_rejected(tmp_path, "r1")

        tracker = ProposalFeedbackTracker()
        stats = tracker.compute_channel_stats(proposals_dir)
        assert tracker.get_adjusted_threshold("reflect", 0.7, stats) == 0.7, (
            "precision 0.667 clears PRECISION_THRESHOLD -> the bar must stay at base"
        )

    def test_poor_precision_still_tightens(self, tmp_path):
        """The fix must not disarm the ratchet where it is genuinely earned."""
        from core.proposal_feedback import ProposalFeedbackTracker
        from routers.cultivation import mark_proposal_approved, mark_proposal_rejected

        proposals_dir = tmp_path / ".artifacts" / "proposals"
        proposals_dir.mkdir(parents=True)
        self._seed_pending(proposals_dir, "reflect", ["a1", "r1", "r2", "r3", "r4"])
        mark_proposal_approved(tmp_path, "a1")
        for pid in ("r1", "r2", "r3", "r4"):
            mark_proposal_rejected(tmp_path, pid)

        tracker = ProposalFeedbackTracker()
        stats = tracker.compute_channel_stats(proposals_dir)
        assert stats["reflect"]["precision"] == 0.2
        assert tracker.get_adjusted_threshold("reflect", 0.7, stats) > 0.7, (
            "precision 0.2 is below PRECISION_THRESHOLD -> the bar must rise"
        )

    def test_rejection_reason_reaches_the_breakdown(self, tmp_path):
        """AC13 — the parity contract extends to the rejection REASON, not just the status.

        The status vocabulary was unified while its sibling field was left split:
        the writer persisted the reason under one key and this reader looked for
        another, so ``rejection_breakdown`` was permanently empty. That is the
        same producer/consumer desync the class above exists to prevent, one
        field over. Consequences, both silent: ``get_adjusted_threshold`` could
        never take its targeted branch, and ``check_self_correction`` returned
        None forever because an empty breakdown yields no dominant reason — the
        self-correction loop was structurally dead at the moment it would fire.

        Like every test here, it may not name the persisted key itself; the
        reason goes in through the production writer and must come out of the
        reader's breakdown.
        """
        from core.proposal_feedback import ProposalFeedbackTracker
        from routers.cultivation import mark_proposal_rejected

        proposals_dir = tmp_path / ".artifacts" / "proposals"
        proposals_dir.mkdir(parents=True)
        self._seed_pending(proposals_dir, "code_intel_feed", ["r1", "r2"])

        mark_proposal_rejected(tmp_path, "r1", reason="false_positive")
        mark_proposal_rejected(tmp_path, "r2", reason="false_positive")

        stats = ProposalFeedbackTracker().compute_channel_stats(proposals_dir)
        breakdown = stats["code_intel_feed"]["rejection_breakdown"]
        assert breakdown == {"false_positive": 2}, (
            "the reason the writer persisted must reach the reader's breakdown; "
            f"got {breakdown!r}"
        )

    def test_free_text_reason_is_canonicalised_to_a_bounded_token(self, tmp_path):
        """The reason is free text from an HTTP query param, so the breakdown must
        not become an unbounded key space of prose.

        Real persisted values on disk look like
        ``"false positive: filename/attribute/noise, not a code symbol"`` while
        ``REASON_FIX_MAP`` is keyed on ``"false_positive"``. Matching the raw
        string would both miss the map and let one dict key grow to a sentence,
        so the reader normalises the leading clause to a bounded token. A value
        outside the known set must still bucket to something short rather than
        being dropped or stored verbatim.
        """
        from core.proposal_feedback import (
            REASON_FIX_MAP,
            ProposalFeedbackTracker,
        )
        from routers.cultivation import mark_proposal_rejected

        proposals_dir = tmp_path / ".artifacts" / "proposals"
        proposals_dir.mkdir(parents=True)
        self._seed_pending(proposals_dir, "reflect", ["r1", "r2", "r3"])

        mark_proposal_rejected(
            tmp_path, "r1",
            reason="false positive: filename/attribute/noise, not a code symbol",
        )
        mark_proposal_rejected(
            tmp_path, "r2", reason="duplicate — content already present in the doc",
        )
        mark_proposal_rejected(
            tmp_path, "r3",
            reason="not a product Non-Goal — a methodology lesson mis-routed by defer",
        )

        breakdown = ProposalFeedbackTracker().compute_channel_stats(
            proposals_dir
        )["reflect"]["rejection_breakdown"]

        assert breakdown.get("false_positive") == 1, (
            f"prose must canonicalise onto the fix-map vocabulary; got {breakdown!r}"
        )
        assert breakdown.get("duplicate") == 1, f"got {breakdown!r}"
        assert "false_positive" in REASON_FIX_MAP, (
            "the canonical form must be a key the fix map can actually resolve"
        )
        unknown = [k for k in breakdown if k not in REASON_FIX_MAP]
        assert len(unknown) == 1, f"the third reason must bucket, not vanish: {breakdown!r}"
        assert len(unknown[0]) <= 40, (
            f"an unknown reason must stay a bounded token, not stored prose: {unknown[0]!r}"
        )

    def test_every_persisted_reason_spelling_is_read(self, tmp_path):
        """AC14 — the read fallback must cover every spelling a rejection carries.

        Unifying the key at the writer fixes the FUTURE; the decisions already on
        disk carry whatever the writer of the day emitted. A census of the live
        workspace found rejections under three different spellings, and the
        fallback chain covered two — so most real rejections were still invisible
        to the breakdown, and the dominant reason was being computed from a
        minority of the evidence. A read-side chain is the whole migration here:
        rewriting a past human decision to normalise its key is not something
        this needs, and history is not ours to edit.
        """
        import json as _json
        from core.proposal_feedback import ProposalFeedbackTracker, STATUS_REJECTED

        proposals_dir = tmp_path / ".artifacts" / "proposals"
        proposals_dir.mkdir(parents=True)
        # One proposal per spelling, all genuinely rejected.
        for pid, key in (
            ("r1", "rejection_reason"),
            ("r2", "rejected_reason"),
            ("r3", "reject_reason"),
        ):
            (proposals_dir / f"proposal_{pid}.json").write_text(
                _json.dumps({
                    "id": pid, "source_stage": "reflect",
                    "status": STATUS_REJECTED, key: "duplicate",
                }),
                encoding="utf-8",
            )

        breakdown = ProposalFeedbackTracker().compute_channel_stats(
            proposals_dir
        )["reflect"]["rejection_breakdown"]
        assert breakdown == {"duplicate": 3}, (
            "every persisted spelling must reach the breakdown; a spelling the "
            f"chain misses silently shrinks the evidence: {breakdown!r}"
        )

    def test_unknown_reason_buckets_to_one_bounded_key(self, tmp_path):
        """The breakdown is PERSISTED, and its keys must be a closed set.

        Length-capping a free-text reason is not enough: real reasons embed a
        date ("Batch reject 2026-05-20: ..."), so each batch would mint its own
        permanent key and the dict would grow one entry per rejection campaign
        forever. Worse, an unrecognised token becomes the DOMINANT reason and
        selects a threshold adjustment branch by accident. So anything outside
        the fix-map vocabulary collapses to a single bucket: the count is kept
        (an unclassifiable rejection is still a signal) while the key space stays
        the known enum plus one.
        """
        import json as _json
        from core.proposal_feedback import (
            REASON_FIX_MAP,
            ProposalFeedbackTracker,
            STATUS_REJECTED,
        )

        proposals_dir = tmp_path / ".artifacts" / "proposals"
        proposals_dir.mkdir(parents=True)
        reasons = [
            "Batch reject 2026-05-20: stale queue cleanup",
            "Batch reject 2026-06-01: stale queue cleanup",
            "REJECTED by the run author after reading the target entry",
            "false_positive",
        ]
        for i, reason in enumerate(reasons):
            (proposals_dir / f"proposal_x{i}.json").write_text(
                _json.dumps({
                    "id": f"x{i}", "source_stage": "reflect",
                    "status": STATUS_REJECTED, "rejection_reason": reason,
                }),
                encoding="utf-8",
            )

        stats = ProposalFeedbackTracker().compute_channel_stats(proposals_dir)
        breakdown = stats["reflect"]["rejection_breakdown"]

        assert breakdown == {"false_positive": 1, "other": 3}, (
            "two different batch dates plus one prose reason must collapse into a "
            f"single 'other' bucket, not three permanent keys: {breakdown!r}"
        )
        unknown = set(breakdown) - set(REASON_FIX_MAP)
        assert unknown == {"other"}, (
            f"the key space must be the fix-map vocabulary plus 'other': {unknown!r}"
        )

    def test_unclassifiable_rejections_do_not_pick_a_threshold_branch(self, tmp_path):
        """A bucket must not become a dominant reason that steers the ratchet.

        get_adjusted_threshold reads the dominant reason to choose HOW MUCH to
        raise the bar: a false-positive-heavy channel gets the full step, a
        staleness-heavy one 70% of it, anything else 50%. An unclassifiable
        reason winning that vote would select a branch by accident — and because
        the reason arm was dead until now, whatever it started returning would
        silently move every affected channel's threshold. 'other' must therefore
        land on the same conservative branch as no-reason-at-all.
        """
        import json as _json
        from core.proposal_feedback import ProposalFeedbackTracker, STATUS_REJECTED

        def _threshold(reason):
            d = tmp_path / reason[:8].replace(" ", "_") / ".artifacts" / "proposals"
            d.mkdir(parents=True)
            for i in range(4):
                body = {"id": f"p{i}", "source_stage": "reflect",
                        "status": STATUS_REJECTED}
                if reason:
                    body["rejection_reason"] = reason
                (d / f"proposal_p{i}.json").write_text(
                    _json.dumps(body), encoding="utf-8")
            t = ProposalFeedbackTracker()
            return t.get_adjusted_threshold("reflect", 0.7, t.compute_channel_stats(d))

        no_reason = _threshold("")
        unknown = _threshold("Batch reject 2026-05-20: stale queue cleanup")
        assert unknown == no_reason, (
            "an unclassifiable reason must not steer the adjustment away from the "
            f"no-reason baseline: {unknown} vs {no_reason}"
        )

    def test_a_nonstring_reason_does_not_kill_every_channel(self, tmp_path):
        """A malformed reason must cost its own attribution, never the whole scan.

        The reason field comes from arbitrary persisted JSON, so a dict/int is
        reachable. A bare ``.strip()`` on one raised out of the per-channel loop,
        and the sole production caller logs at debug — so the metric died
        silently AND the self-correction call right after it never ran. That is
        the same silent-metric-death this module exists to prevent, so the scan
        must survive and still count the rejection.
        """
        from core.proposal_feedback import (
            REASON_FIX_MAP,
            UNCLASSIFIED_REASON,
            ProposalFeedbackTracker,
        )

        for i, (status, reason) in enumerate(
            [
                ("rejected", "false positive: filename noise"),
                (STATUS_APPROVED, None),
                ("rejected", {"nested": "dict"}),
                ("rejected", 5),
            ]
        ):
            record = {"source_stage": "reflect", "status": status}
            if reason is not None:
                record["rejection_reason"] = reason
            (tmp_path / f"proposal_{i}.json").write_text(json.dumps(record))

        stats = ProposalFeedbackTracker().compute_channel_stats(tmp_path)

        assert "reflect" in stats, "one malformed reason erased the whole channel"
        assert stats["reflect"]["approved"] == 1
        assert stats["reflect"]["rejected"] == 3
        breakdown = stats["reflect"]["rejection_breakdown"]
        assert breakdown.get("false_positive") == 1, (
            f"the well-formed sibling lost its attribution: {breakdown}"
        )
        assert breakdown.get(UNCLASSIFIED_REASON) == 2, (
            f"non-string rejections must still be counted, bounded: {breakdown}"
        )
        assert set(breakdown) <= set(REASON_FIX_MAP) | {UNCLASSIFIED_REASON}, (
            f"key space escaped the closed vocabulary: {breakdown}"
        )

    def test_resolvable_evidence_beats_an_unclassifiable_canonical_key(self, tmp_path):
        """When two spellings coexist, take the one that actually resolves.

        Giving the canonical key unconditional precedence threw away usable
        evidence: a file carrying an unclassifiable canonical reason plus a
        legacy reason that maps cleanly onto the fix map scored as unclassified,
        so the breakdown filled from a minority of the evidence while looking
        like it worked.
        """
        from core.proposal_feedback import (
            REJECTION_REASON_KEY,
            ProposalFeedbackTracker,
        )

        (tmp_path / "proposal_1.json").write_text(
            json.dumps(
                {
                    "source_stage": "reflect",
                    "status": "rejected",
                    REJECTION_REASON_KEY: "Batch reject 2026-05-20: campaign",
                    "reject_reason": "false_positive",
                }
            )
        )

        breakdown = ProposalFeedbackTracker().compute_channel_stats(tmp_path)[
            "reflect"
        ]["rejection_breakdown"]

        assert breakdown == {"false_positive": 1}, (
            "the resolvable spelling must win over an unclassifiable one: "
            f"{breakdown}"
        )
