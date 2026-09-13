"""Doc↔code parity gate for data_point_folding's marker tuples.

WHY THIS EXISTS — the drift it guards already cost a wasted pipeline run. The
module docstring listed THREE foldable lead markers while the runtime
``_FOLDABLE_MARKERS`` tuple held FOUR (the extra one being a bare
``DATA-POINT``). An author who read the docstring concluded a bare
``**DATA-POINT — ...**`` sub-bullet would never be folded, wrote entries in that
shape, watched them get folded, and mis-attributed the move to a different
mechanism entirely. One full evaluation cycle went to investigating a gap that
did not exist.

WHAT IS ASSERTED, AND WHY THE TWO SIDES ARE ASYMMETRIC — this is measured
behaviour, not a style preference:

* FOLDABLE → STRICT EQUALITY. ``_classify_lead`` decides foldability with
  ``any(up.startswith(f) for f in _FOLDABLE_MARKERS)`` — a whitelist that is the
  SOLE admission gate. A marker missing from the docstring is therefore a false
  promise about real behaviour.
* PROTECTED → BEHAVIOUR, not set comparison. A subset assertion here would have
  NO TEETH: deleting any one of the five protected markers leaves
  ``_classify_lead`` returning ``(False, False)`` instead of ``(False, True)`` —
  still unfoldable — because no protected marker is a prefix-extension of any
  foldable marker. Non-whitelisted leads default to not-folded. So this file
  asserts the classification RESULT per marker plus the no-overlap invariant that
  makes the default safe. Strict set equality would also be wrong in the other
  direction: the docstring legitimately names ``### CLASS`` and
  ``META-CORRECTION``, which are protected BY the whitelist default rather than
  by ``_PROTECTED_MARKERS`` — flagging a correct docstring as drift.

DESIGN CONSTRAINTS this file must keep honouring:

* It IMPORTS the runtime tuples. It must never re-transcribe a marker list
  locally — a test that compares a docstring against its own copy of the data
  passes while production drifts.
* The docstring parser has NO skip/exemption rules. A canonical-source pointer
  therefore lives in its OWN bullet; placed inside the FOLDABLE bullet it would
  be read as an extra marker token, and the alternative (teaching the parser to
  ignore identifier-shaped tokens) would install a permanent exemption that could
  one day swallow a real marker.
* The FOLDABLE bullet is located by the phrase "FOLDABLE type are candidates".
  Rewording that phrase reds this file — deliberately: the anchor is part of the
  contract, and a loud failure beats a silently mis-selected bullet.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hooks import data_point_folding as dpf  # noqa: E402

# Tokens are written as ``double-backtick`` spans in the docstring.
_TOKEN_RE = re.compile(r"``([^`]+)``")
# The bullet that enumerates the foldable whitelist.
_FOLDABLE_ANCHOR = "FOLDABLE type are candidates"
# The bullet that promises which leads are never folded.
_PROTECTED_ANCHOR = "PROTECTED lead markers are never folded"


def _top_level_bullets(doc: str | None) -> list[str]:
    """Split a docstring into its top-level ``- `` bullets.

    Each bullet runs until the NEXT top-level ``- `` or until a dedent (a
    non-empty line at column 0 that does not start a bullet). Bounding matters:
    a fixed line window bleeds into the following bullet — measured, an
    anchor+3-lines window pulled two PROTECTED markers into the FOLDABLE set,
    which would have made the strict-equality assertion below compare the wrong
    collection.

    ``None`` is accepted because at optimize level >= 2 (``python -OO``, and any
    packaging step that sets it) the interpreter DISCARDS docstrings, so
    ``__doc__`` is None and every caller here would otherwise die with an
    AttributeError instead of a readable assertion. Measured under ``-OO``: that
    raw AttributeError surfaced from this line. Normalising it here — rather than
    at each call site — keeps the single entry point responsible, and
    ``test_protected_marker_parse_is_not_silently_empty`` then reports the real
    cause (an empty ``__doc__``).
    """
    if not doc:
        return []
    bullets: list[str] = []
    current: list[str] | None = None
    for line in doc.splitlines():
        if line.startswith("- "):
            if current is not None:
                bullets.append("\n".join(current))
            current = [line]
        elif current is not None:
            if line and not line.startswith((" ", "\t")):
                bullets.append("\n".join(current))
                current = None
            else:
                current.append(line)
    if current is not None:
        bullets.append("\n".join(current))
    return bullets


def _foldable_bullet(doc: str) -> str:
    matches = [b for b in _top_level_bullets(doc) if _FOLDABLE_ANCHOR in b]
    assert matches, (
        f"no top-level bullet contains the anchor {_FOLDABLE_ANCHOR!r} — the "
        "docstring was reworded, so this gate can no longer locate the "
        "foldable-marker enumeration. Restore the anchor phrase or update it "
        "here in the same commit."
    )
    assert len(matches) == 1, (
        f"{len(matches)} bullets contain the anchor {_FOLDABLE_ANCHOR!r}; the "
        "enumeration must live in exactly one bullet so there is a single "
        "parseable copy to compare against the runtime tuple."
    )
    return matches[0]


def test_foldable_markers_match_docstring_exactly() -> None:
    """Every runtime foldable marker is documented, and nothing extra is.

    Strict equality in both directions: a runtime marker absent from the
    docstring is a false promise (the drift that cost a pipeline run); a
    documented marker absent from the runtime tuple promises folding that never
    happens.
    """
    documented = set(_TOKEN_RE.findall(_foldable_bullet(dpf.__doc__)))
    runtime = set(dpf._FOLDABLE_MARKERS)

    # Both sides non-empty BEFORE the comparison. `set() == set()` is true, so an
    # emptied whitelist plus a de-enumerated bullet would satisfy the equality
    # below while every other assertion in this file silently degrades with it
    # (the shadow test iterates an empty tuple; the restatement detector loses its
    # signals). The protected side already guards this at its parse point; this is
    # the missing symmetric guard.
    assert runtime, (
        "`_FOLDABLE_MARKERS` is empty — nothing can ever be folded, and the "
        "equality below would pass vacuously against an equally-empty docstring."
    )
    assert documented, (
        "parsed zero marker tokens from the FOLDABLE bullet. They must be written "
        "as ``double-backtick`` spans; a reformat makes them invisible here and the "
        "equality below would then only compare two empty sets."
    )

    assert documented == runtime, (
        "docstring↔code drift in the FOLDABLE whitelist.\n"
        f"  documented only : {sorted(documented - runtime)}\n"
        f"  runtime only    : {sorted(runtime - documented)}\n"
        "The runtime tuple is canonical — `_classify_lead` gates folding on it "
        "alone, so the docstring must list exactly these markers."
    )


def _documented_protected_markers() -> list[str]:
    """The markers the docstring PROMISES are never folded.

    Deliberately sourced from the docstring, NOT from ``_PROTECTED_MARKERS``. A
    parametrisation driven by the runtime tuple is vacuous against the mutation
    that matters: deleting an entry also deletes its test case, so the suite
    reports green with one fewer assertion. Measured — removing ``REGRESSION``
    took the run from 8 passing cases to 7 passing cases, exit 0. The docstring
    is an independent statement of intent, so it survives that mutation and the
    case keeps running.

    It also lists two entries that are NOT in the tuple (``### CLASS``,
    ``META-CORRECTION``); both are genuinely unfoldable via the whitelist
    default, so asserting the weaker not-foldable property over this wider set
    is correct rather than a false block.

    NEVER RAISES. This feeds a module-level ``parametrize``, which pytest evaluates
    during COLLECTION — and a collection error is not a failing test, it aborts the
    whole invocation. Measured: an earlier version asserted here, and rewording the
    anchor produced ``Interrupted: 1 error during collection`` with ZERO of 22 tests
    running across this file AND its sibling, because CI passes the whole BVT
    manifest to one pytest process. A reworded docstring must surface as one red
    test, never as a suite that did not run. So the empty/short cases are returned
    as data and adjudicated by ``test_protected_marker_parse_is_not_silently_empty``.
    """
    matches = [
        b for b in _top_level_bullets(dpf.__doc__ or "")
        if _PROTECTED_ANCHOR in b
    ]
    if not matches:
        return []
    return _TOKEN_RE.findall(matches[0])


def test_protected_marker_parse_is_not_silently_empty() -> None:
    """The docstring parse that feeds the parametrisation actually found markers.

    This is the guard that stops an empty parse from silently disarming the
    parametrised family: an empty parametrisation reports success with fewer
    assertions and is indistinguishable from a passing one. Measured — reformatting
    the markers from ``double`` to `single` backticks took the run from 11 passing
    cases to 6, exit 0. It lives in a real test rather than at the parse point so a
    failure is one red test instead of a collection abort.
    """
    assert dpf.__doc__, (
        "`data_point_folding.__doc__` is empty — the docstring this gate compares "
        "against is gone (or was stripped by an optimize level >= 2, which discards "
        "docstrings). Nothing downstream can be verified."
    )

    bullets = [b for b in _top_level_bullets(dpf.__doc__) if _PROTECTED_ANCHOR in b]
    assert bullets, (
        f"no top-level bullet contains the anchor {_PROTECTED_ANCHOR!r} — the "
        "docstring was reworded, so the protected-marker promise can no longer be "
        "located. Restore the anchor phrase or update it here in the same commit."
    )

    markers = _documented_protected_markers()
    assert len(markers) >= len(dpf._PROTECTED_MARKERS), (
        f"parsed only {len(markers)} protected marker token(s) from the docstring "
        f"({markers}) but the runtime tuple has {len(dpf._PROTECTED_MARKERS)}. The "
        "markers must be written as ``double-backtick`` spans — a single-backtick "
        "or plain-text reformat makes them invisible here and silently empties "
        "the parametrised assertions."
    )


@pytest.mark.parametrize("marker", _documented_protected_markers())
def test_documented_protected_marker_is_never_foldable(marker: str) -> None:
    """Every marker the docstring promises protection for is unfoldable.

    Asserts the classification RESULT, so it fails on a real behaviour change
    rather than on a text mismatch. The promise being verified is the weaker,
    universally-true one — "this lead never folds" — which holds both for the
    explicit ``_PROTECTED_MARKERS`` entries and for the two the whitelist
    default covers.
    """
    is_foldable, _ = dpf._classify_lead(f"{marker} (2026-01-01) — body text.")

    assert is_foldable is False, (
        f"{marker!r} is documented as never folded, but `_classify_lead` now "
        "reports it as foldable. Either the docstring's promise is stale or a "
        "foldable marker was widened until it captured this lead."
    )


def test_explicitly_protected_markers_are_still_explicitly_protected() -> None:
    """The runtime tuple's entries classify as explicitly protected.

    Separate from the test above because it catches a different regression: an
    entry silently dropped from ``_PROTECTED_MARKERS``. That drop does not change
    folding behaviour today (the whitelist default keeps the lead unfolded), so
    nothing else in this suite notices — but it removes the early return in
    ``_classify_lead`` that makes protection independent of check order. The
    docstring's list is the reference set here, minus the two entries that were
    never tuple members.
    """
    documented = set(_documented_protected_markers())
    whitelist_default_only = {"### CLASS", "META-CORRECTION"}
    expected_explicit = documented - whitelist_default_only

    # Without this the assertion below passes over an empty set — vacuously true
    # and indistinguishable from real coverage.
    assert len(expected_explicit) == len(dpf._PROTECTED_MARKERS), (
        f"expected to check {len(dpf._PROTECTED_MARKERS)} explicitly-protected "
        f"markers but derived {len(expected_explicit)} from the docstring "
        f"({sorted(expected_explicit)}). Either the docstring dropped an entry or "
        "the whitelist-default exclusion list needs updating — do not let this "
        "assertion run over a shrunken set."
    )

    not_explicit = {
        marker
        for marker in expected_explicit
        if dpf._classify_lead(f"{marker} (2026-01-01) — body.") != (False, True)
    }

    assert not not_explicit, (
        f"these markers are documented as protected but no longer classify as "
        f"EXPLICITLY protected: {sorted(not_explicit)}. They are probably still "
        "unfoldable via the whitelist default, which is why no other test fails "
        "— but their entry in `_PROTECTED_MARKERS` is what keeps protection "
        "independent of the order the two whitelists are consulted in."
    )


def test_no_protected_marker_is_shadowed_by_a_foldable_prefix() -> None:
    """No protected marker starts with a foldable marker.

    This overlap invariant is why the whitelist default is safe. ``_classify_lead``
    checks PROTECTED first and returns early, so an overlap would not change
    today's behaviour — but it would make the two tuples order-dependent, and any
    future reordering (or a check that consults the foldable whitelist first)
    would start folding protected content. Pinning it keeps the tuples
    independent rather than accidentally-ordered.
    """
    shadowed = {
        protected: [f for f in dpf._FOLDABLE_MARKERS if protected.upper().startswith(f.upper())]
        for protected in dpf._PROTECTED_MARKERS
    }
    offenders = {k: v for k, v in shadowed.items() if v}

    assert not offenders, (
        "a protected marker is prefixed by a foldable marker, so the two tuples "
        f"are no longer independent: {offenders}. `_classify_lead` currently "
        "checks PROTECTED first and hides this, but the ordering then becomes "
        "load-bearing."
    )


def test_foldable_enumeration_has_exactly_one_parseable_copy() -> None:
    """The marker list is transcribed in exactly one place.

    A second copy elsewhere in the docstring is unreachable by the parser above,
    so it would sit stale forever while this file reports green — and that is not
    hypothetical: the docstring previously carried the phrase
    "(RECURRENCE / CONTAINMENT DATA-POINT records)" in prose, outside any bullet.

    Detecting that shape needs care. It names two markers by SHARING one
    occurrence of "DATA-POINT" across a slash, so counting whole marker names
    finds only one and a "two or more names" threshold never fires — measured:
    reinstating that exact line passed a threshold-of-two detector. The
    discriminating signal is the distinctive PREFIX of each compound marker
    (RECURRENCE / CONTAINMENT), which survives the abbreviation. Line identity is
    also wrong as an exclusion: comparing by content would exclude a VERBATIM
    duplicate of the canonical line from the scan, which is exactly one of the
    copies worth catching. Excluding by POSITION instead.
    """
    doc_lines = dpf.__doc__.splitlines()
    canonical = set(range(len(doc_lines)))
    # Exclude the canonical bullet BY POSITION, so a verbatim copy pasted
    # elsewhere is still scanned.
    bullet_lines = _foldable_bullet(dpf.__doc__).splitlines()
    for start in range(len(doc_lines) - len(bullet_lines) + 1):
        if doc_lines[start:start + len(bullet_lines)] == bullet_lines:
            canonical = set(range(start, start + len(bullet_lines)))
            break

    # Only lines carrying a ``double-backtick`` span can be transcribing the
    # whitelist. Restricting the scan this way is what keeps ordinary narrative
    # prose out of it: measured, a plain-text scan reported the module's own
    # summary line ("Data-point family folding for ...") as a restatement as soon
    # as a marker's first word was "DATA-POINT", telling the author to delete
    # legitimate prose. A transcription always quotes the marker; narrative does not.
    restatements = []
    for i, line in enumerate(doc_lines):
        if i in canonical:
            continue
        quoted = _TOKEN_RE.findall(line)
        if not quoted:
            continue
        upper_quoted = " ".join(quoted).upper()
        # Two independent signals, because either alone has a blind spot:
        #   * whole marker names — catches a line naming ONLY the bare
        #     ``DATA-POINT``. Measured: a prefix-only detector let
        #     "only bare ``DATA-POINT`` leads are folded" through with exit 0 —
        #     and the bare form is precisely the marker whose omission caused the
        #     mis-diagnosis this gate exists for.
        #   * compound PREFIXES (RECURRENCE / CONTAINMENT) — catches the
        #     slash-abbreviated shape "RECURRENCE / CONTAINMENT DATA-POINT",
        #     which names two markers while spelling neither in full.
        named_full = sum(m.upper() in upper_quoted for m in dpf._FOLDABLE_MARKERS)
        prefixes = sorted({
            m.split()[0] for m in dpf._FOLDABLE_MARKERS if len(m.split()) > 1
        })
        named_prefix = sum(p in upper_quoted for p in prefixes)
        if named_full >= 1 or named_prefix >= 2:
            restatements.append(line.strip())

    assert not restatements, (
        "the foldable-marker list is transcribed outside the canonical bullet; "
        "those copies are unreachable by this gate and will go stale silently: "
        f"{restatements}. Keep one enumeration and have other places refer to it."
    )
