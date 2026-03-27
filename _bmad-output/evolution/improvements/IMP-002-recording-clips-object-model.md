# Improvement: Recording Clips as Session Objects

**ID:** IMP-002
**Type:** UX + Workflow Improvement
**Priority:** High
**Status:** Identified
**Date:** 2026-03-27

---

## Opportunity

**What are we improving?**

When recording on a track, the captured material should be grouped into one recording session object (clip) so the user can manipulate it as a single unit:
- transpose
- move
- copy
- paste

Current pain: recorded notes are editable as individual events, but not managed as one cohesive recording session object.

**Primary Persona**

Musicista live/producer su tablet.

**Why does this matter now?**

- Improves user experience and versatility during composition.
- Speeds up arrangement workflow.
- Increases perceived DAW professionalism by matching expected clip-based interaction model.

Business/user impact statement (current cycle):
- Editing operations should require fewer manual micro-actions.
- Session flow should feel faster and more predictable.

---

## Data

**Available Evidence (current cycle):**
- Stakeholder request: recorded content should be grouped into one session object.
- Desired operations: transpose, move, copy, paste at clip level.
- Priority rationale: UX quality + arrangement speed.

**Analytics:**
- Not available yet (same measurement gap as previous cycle).

**User Feedback Themes:**
- Need for faster editing flow after recording.
- Need for object-level manipulation instead of note-by-note work.

**Hypothesis:**
If each recording pass generates a clip object, users will edit and arrange faster with lower cognitive load.

---

## Effort Estimate (Initial)

**Design:** Medium
**Implementation:** Medium-High (data model + UI interactions)
**Testing:** Medium (regression on existing note editor and playback)
**Total:** Medium-High

---

## Success Metrics (To Confirm in Context Step)

1. Editing speed improved (qualitative: "editing piu rapido").
2. Fewer manual actions to perform transpose/move/copy/paste on newly recorded material.
3. No regression in existing per-note editing and playback behavior.

---

## Scope Hypothesis (for next step)

Potential first slice for Kaizen:
- Clip creation at record stop (single take -> single clip object)
- Clip selection and move on timeline
- Clip transpose as a whole

Deferred:
- Advanced clip splitting/merging
- Multi-clip lane management

---

## Next Steps

1. Gather context on current recording data model and note map constraints.
2. Validate root cause of missing clip abstraction.
3. Scope one minimal scenario for first clip-based workflow.
