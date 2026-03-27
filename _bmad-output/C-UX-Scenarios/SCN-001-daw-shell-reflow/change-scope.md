# Change Scope — SCN-001 DAW Shell Reflow

## One Thing to Change (Kaizen Focus)
Introduce a tablet-first DAW shell hierarchy and responsive control density for the main DAW page, so users can understand and operate transport/editing/track shell faster across tablet and smartphone.

---

## What's Changing

### Screens/Views Affected
1. DAW main shell only (single view)
   - `home/matteo/matsynth_web/templates/index.html`
   - `home/matteo/matsynth_web/static/style.css`

### Change Types
1. Visual hierarchy refinement
2. Responsive layout reflow (tablet-first)
3. Component spacing and density normalization
4. Control grouping consistency

### Specific Change List
1. Re-balance top DAW shell hierarchy (transport > editing > track shell).
2. Normalize control size classes and spacing tokens in top shell.
3. Improve responsive breakpoints for tablet and smartphone portrait/landscape.
4. Increase effective timeline/track usable area on mobile/tablet by reducing waste in control rows.
5. Keep existing interactions intact while improving visual affordance and scanability.

---

## What's Staying the Same

1. Backend architecture (Flask + SocketIO + DAW recorder): unchanged.
2. Existing DAW core behaviors (play/stop/record/arm/mute/solo/zoom/select): unchanged.
3. Existing page route and primary single-page DAW structure: unchanged.
4. Existing technical stack and deployment process: unchanged.
5. Existing mixer deep behavior and piano-roll interaction architecture: unchanged for this cycle.

---

## Root-Cause Fit Check

Does each proposed change directly solve root cause (unclear hierarchy + non-uniform visuals + mobile/tablet space inefficiency)?

1. Hierarchy rebalance -> Yes
2. Spacing/token normalization -> Yes
3. Responsive reflow -> Yes
4. Usable area recovery -> Yes
5. Interaction-preserving affordance improvements -> Yes

No out-of-scope redesign items included.
