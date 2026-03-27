# Context Synthesis: IMP-001 DAW Professional UI Multi-Device

## What We Know

1. No quantitative analytics is currently available; baseline must start from qualitative evidence.
2. Current UI is perceived as unclear, not intuitive, and visually non-uniform.
3. Space usage is inefficient on smartphone/tablet, reducing practical usability.
4. Strategic benchmark is GarageBand iOS, with required adaptation to this codebase constraints.
5. Device priority is tablet first, then smartphone, then desktop; orientation must support both portrait and landscape on mobile/tablet.
6. Historical design intent favored simplicity, which now under-serves pro-DAW expectations.

## Root Cause

The interface prioritizes generic simplicity over DAW-native information hierarchy and responsive density strategy. As a result, users do not get immediate spatial/functional cues, especially on smaller viewports.

## Hypothesis

If we introduce a DAW-structured responsive layout system (tablet-first), unify visual language, and optimize control density per viewport/orientation, the UI will be perceived as more professional and become easier to use on tablet/smartphone without sacrificing desktop operability.

## Validation Plan

1. Stakeholder heuristic review against a DAW parity checklist.
2. Scenario-based usability pass on 3 device classes (tablet, smartphone, desktop) and mobile orientations.
3. Before/after qualitative scoring on clarity, intuitiveness, and visual consistency.
4. Add minimal telemetry in next cycle to quantify behavior improvements.

## Recommended Scope Focus for Next Step [S]

For the next Kaizen slice, scope one focused scenario:
- "Tablet-first arrangement + transport + track controls responsive reflow"

This keeps scope small while maximizing perceived quality and cross-device usability gains.
