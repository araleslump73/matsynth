# SCN-001 — Tablet-First DAW Shell Reflow

## Target
Improve the main DAW shell (transport + editing toolbar + track header/first row structure) to feel professional and DAW-native, with tablet-first responsive behavior and consistent adaptation to smartphone and desktop.

Why this target first:
- Highest leverage for perceived professionalism
- Directly addresses clarity and wasted-space pain points
- Can be shipped as one focused Kaizen slice without backend refactor

## Current State
Users currently experience:
1. Visual hierarchy that feels generic rather than DAW-native.
2. Inconsistent control density and spacing.
3. Weak responsive optimization on tablet/smartphone (space inefficiency).
4. Professional benchmark gap versus GarageBand iOS style language.

## Desired State
After this scenario:
1. DAW shell communicates clear hierarchy (transport primary, timeline/arrangement dominant, controls grouped logically).
2. Tablet layout is the reference baseline.
3. Smartphone adapts with compact, touch-first grouping in both portrait and landscape.
4. Desktop remains fully functional while inheriting the unified visual system.
5. Component styling is coherent across toolbar, buttons, selectors, badges, and row scaffolding.

## User Journey
### Entry Point
User opens the DAW page (new session or existing session), from tablet first.

### Current Flow
1. User lands on DAW page.
2. Sees multiple control groups with uneven visual weight.
3. On mobile/tablet, important timeline/control balance feels suboptimal.
4. User needs extra scanning effort to understand where to act first.

### Pain Points
1. Ambiguous visual priority among transport, editing, and track controls.
2. Non-uniform component language reduces confidence.
3. Limited usable timeline/control density on smaller viewports.

### Proposed Flow
1. User lands on DAW page with clear top-down hierarchy.
2. Transport and edit actions are grouped and legible at first glance.
3. Track row shell and timeline area preserve useful density on tablet/mobile.
4. User quickly identifies primary actions and starts interaction with minimal scanning.

## Success Criteria
1. Stakeholder qualitative review: "Looks and feels like a professional DAW" (pass/fail).
2. Device QA pass on:
   - Tablet portrait + landscape
   - Smartphone portrait + landscape
   - Desktop standard viewport
3. Heuristic targets:
   - Clarity score >= 4/5
   - Intuitiveness score >= 4/5
   - Visual consistency score >= 4/5
4. No regressions in existing DAW core actions (play/stop/record/arm/mute/solo/zoom/select).

## Scope
### Pages Affected
1. DAW main page shell (single view): `home/matteo/matsynth_web/templates/index.html`

### Components Touched
1. Transport toolbar group layout
2. Editing toolbar layout and responsive breakpoints
3. Track table shell header/first-row structural spacing
4. Shared visual tokens for controls (button/select/badge consistency)

### Data Changes
- None required for this scenario
- Optional telemetry deferred to future iteration

### Risk Level
**Medium**
- Primarily visual/responsive changes
- Moderate risk of interaction regressions due to layout reflow
- Mitigated via device matrix QA and regression checklist

## Non-Goals (Explicit)
1. No full piano-roll interaction redesign in this slice
2. No backend DAW engine/API changes
3. No mixer architecture rewrite

## Implementation Boundary for Next Phase [D]/[I]
1. Keep this cycle focused on DAW shell reflow and responsive hierarchy only.
2. Defer deeper feature interactions to subsequent Kaizen cycle(s).
