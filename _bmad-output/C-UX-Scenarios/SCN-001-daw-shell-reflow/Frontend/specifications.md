# DAW Main Shell — Update Specification (SCN-001)

## Change Summary
This update introduces a tablet-first responsive DAW shell hierarchy for the top interaction zone (transport, editing toolbar, and track shell header/first row framing), aligned to GarageBand-like professional readability while preserving existing behaviors and backend contracts.

## Before (v1)
1. Visual priority between transport/editing/track shell is not consistently legible.
2. Control density is uneven and appears generic rather than DAW-native.
3. On tablet/smartphone, spacing and grouping waste valuable horizontal/vertical space.
4. Component style language (buttons/selectors/badges) is not fully uniform.

## After (v2)
1. Top shell uses explicit hierarchy tiers:
   1. Transport primary row
   2. Editing/action secondary row
   3. Track shell header and first-row emphasis
2. Tablet is baseline breakpoint; smartphone and desktop inherit adapted variants.
3. Control groups are compact, touch-safe, and semantically grouped.
4. Shared visual tokens unify component rhythm and reduce visual noise.
5. Timeline/track area receives more effective space on mobile/tablet.

## Components

### Modified Components
1. `daw-transport-bar`
   1. Group spacing normalized
   2. Priority controls visually elevated
2. `daw-editing-toolbar`
   1. Action clusters compacted
   2. Consistent control sizing and label treatment
3. Track shell header/first row wrapper
   1. Structural spacing tuned for better scan path
   2. Better rhythm between channel/inst/action/timeline columns
4. Shared top-shell controls (`.daw-tbtn`, `.daw-tselect`, badges)
   1. Harmonized border radius, border contrast, and vertical rhythm

### New Components
None required in this cycle.

### Removed Components
None required in this cycle.

### Unchanged Components
1. DAW core command handlers and socket commands
2. Mixer deep panel logic
3. Existing piano-roll editing logic added in previous cycle

## Interaction Changes

### Before
1. User scans multiple top controls with ambiguous priority.
2. On smaller viewports, control rows consume space without clear hierarchy.

### After
1. User sees clear order of action (transport first, editing second).
2. On tablet/smartphone, controls stay usable while preserving track/timeline prominence.
3. Existing command behavior remains unchanged; only shell organization and affordance improve.

## Copy Changes
No semantic copy rewrite required in this cycle.
Only optional micro-label normalization where needed for consistency.

## Visual Changes
1. Hierarchy contrast between rows increased.
2. Spacing tokens standardized for top shell.
3. Group boundaries and control sizing made coherent.
4. Responsive compaction tuned by device class and orientation.

## Responsive Behavior

### Breakpoint Intent
1. Tablet-first baseline (`~768px to ~1199px`)
2. Smartphone compact (`<768px`) with portrait and landscape variants
3. Desktop extended (`>=1200px`) keeping consistency and breathing room

### Tablet (Priority)
1. Two-row shell with dense but readable controls
2. Touch targets remain safe
3. Timeline receives primary horizontal emphasis

### Smartphone Portrait
1. Essential controls remain in first visible row(s)
2. Secondary controls wrap predictably
3. No horizontal overflow in critical action groups

### Smartphone Landscape
1. Recover horizontal room for timeline
2. Preserve quick access to transport and key editing controls

### Desktop
1. Preserve full control availability
2. Keep same visual language and spacing system

## Acceptance Criteria
1. Stakeholder review confirms professional DAW-like shell perception (pass/fail).
2. Device QA pass on:
   1. Tablet portrait
   2. Tablet landscape
   3. Smartphone portrait
   4. Smartphone landscape
   5. Desktop standard viewport
3. Heuristic scores >= 4/5 for:
   1. Clarity
   2. Intuitiveness
   3. Visual consistency
4. No regressions in core DAW actions:
   1. play/stop/record
   2. arm/mute/solo
   3. zoom/select and top-shell action buttons
5. No backend or API changes introduced by this scenario.
