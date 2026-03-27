## What changed

### SCN-001 - Tablet-First DAW Shell Reflow
- Added semantic tier classes to transport/editing shell (`daw-shell-tier`, `daw-shell-tier-primary/secondary`)
- Added semantic group classes for responsive ordering (`transport-core-group`, `edit-main-group`, etc.)
- Added `daw-track-shell` wrapper classes for clear track table hierarchy
- Tablet-first responsive baseline (768-1199px): compact spacing and control sizing
- Smartphone portrait compaction (<767px): row reordering and overflow prevention
- Smartphone landscape recovery (max-900px + landscape): timeline room restored
- Touch-device coarse-pointer wrap override

### Goals 1-4 (previous quick-dev cycle)
- Interactive note editing: hover highlight, labels, rounded corners, sub-beat grid
- Track color stripe, ruler canvas row, increased track heights
- Transpose API (`/api/daw/transpose`) with UI buttons and keyboard shortcuts (+1/-1/+12/-12 semitones)
- Note-map enriched with note-on/note-off indices
- Removed Clear column/buttons; added Purge Track in Edit menu
- Compact inline vol/pan sliders relocated to instrument cell
- UI polish: toolbar gradients, button/select/badge consistency, quant/transpose styling

## Why

**IMP-001** - Professional DAW-like UI parity across smartphone, tablet, and desktop.
Stakeholder requirement: UI should look and feel like a professional DAW.
Benchmark: GarageBand iOS. Priority: tablet > smartphone > desktop.

Related artifacts:
- Scenario: `_bmad-output/evolution/scenarios/SCN-001-tablet-first-daw-shell-reflow.md`
- Specification: `_bmad-output/C-UX-Scenarios/SCN-001-daw-shell-reflow/Frontend/specifications.md`
- Test Report: `_bmad-output/evolution/test-reports/TR-001-SCN-001-validation.md`

## How to test

1. Deploy locally: `python app.py` (or to Pi Zero 2W via `deploy.sh`)
2. Open DAW page on a tablet - verify clear transport/editing tier hierarchy
3. Resize to smartphone portrait (<768px) - verify controls wrap and reorder predictably
4. Rotate to landscape - verify timeline regains horizontal room
5. Verify all core actions work: play/stop/record, arm/mute/solo, zoom/snap, transpose buttons, quantize

## Acceptance criteria

- [x] Shell hierarchy tiers present in markup and CSS
- [x] Tablet baseline responsive rules present
- [x] Smartphone portrait compaction rules present
- [x] Smartphone landscape recovery rules present
- [x] Desktop consistency preserved
- [x] No DAW core action regressions (static check)
- [x] No backend/API changes from SCN-001 scope
- [ ] Manual stakeholder visual sign-off (to complete in review)
- [ ] Runtime device matrix QA (to complete in review)
