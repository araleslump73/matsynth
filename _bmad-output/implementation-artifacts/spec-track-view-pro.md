---
status: implemented
goal: Track View Pro — DAW-Grade Visual Upgrade
spec_file: spec-track-view-pro.md
created: 2025-01-24
---

# Track View Pro — DAW-Grade Visual Upgrade

## 1. Intent

**As** a MatSynth user, **I want** the track view to look and feel like a commercial DAW (Reaper / Ableton / Logic style), **so that** the interface appears professional and conveys musical information at a glance.

**Scope**: Visual-only improvements to the existing track table. No new editing interactions (deferred to Goal 2). No backend changes.

## 2. Boundaries & Constraints

- **Frontend only** — no Python / Flask changes
- **Files**: `templates/index.html` (JS), `static/style.css`
- **Pi Zero 2W safe**: no extra RAF loops, no heavy DOM reflow; all rendering already uses Canvas + offscreen tapes
- **Existing features preserved**: compact/medium/large toggle, inline vol/pan sliders in large mode, selection overlay, loop overlay, playhead, grid, momentum scroll, click-to-seek, instrument labels
- **No new API endpoints**
- **Backward compatible**: track size cycling (dbl-click CH cell) keeps working

## 3. I/O Matrix

| File | Read / Write | What changes |
|------|-------------|-------------|
| `templates/index.html` | R+W | JS: ruler rendering, note rendering enhancements, TRACK_SIZES bump, color stripe in renderAllTracks |
| `static/style.css` | R+W | CSS: ruler row styles, adjusted canvas heights, track color stripe |

## 4. Code Map

| Symbol | File | Line(s) | Role |
|--------|------|---------|------|
| `TRACK_SIZES` | index.html | 649 | Canvas height per mode |
| `renderTrackTape(ch)` | index.html | 2400-2456 | Draws note rects on offscreen tape |
| `renderAllTracks()` | index.html | 2532-2693 | Blits tapes, grid, playhead, overlays |
| `initTrackSizeSystem()` | index.html | 1126-1172 | Sets up size classes + inline controls |
| `getTrackColor(ch)` | index.html | 3220 | Returns TRACK_PALETTE color |
| `.daw-tracks-panel` | style.css | 443-455 | Panel flex layout |
| `.track-compact/medium/large` | style.css | 1720-1775 | Size mode CSS |

## 5. Tasks & Acceptance

### T1 — Bar/Beat Ruler Row
Add a sticky ruler `<canvas>` above the track table that shows bar numbers and beat ticks, synchronized with startTime / zoom / scroll. Rendered inside `renderAllTracks()` loop using the same `tapeStartTime` and `trackView.zoomLevel`.

**AC**: Bar numbers (1, 2, 3…) displayed above measure lines. Beat sub-ticks visible. Ruler scrolls with tracks. Ruler height ~24px. Dark background, white text (Orbitron or monospace).

### T2 — Track Color Stripe
In `renderAllTracks()`, draw a 4px vertical stripe on the left edge of each visible canvas, using `getTrackColor(ch)`.

**AC**: Each track has a colored left-edge stripe matching its palette color. Stripe visible in all three size modes. Muted tracks show dimmed stripe (50% alpha).

### T3 — Increased Default Track Heights + Remove Clear Column
- Change `TRACK_SIZES` from `{compact: 32, medium: 48, large: 80}` to `{compact: 36, medium: 64, large: 110}`.
- **Remove the "Clear" column** from the track table entirely (TH + all 16 TD cells with clear buttons). The timeline column gains that ~7% width.
- Add a **"Purge Track"** item to the Edit dropdown menu (`dropdown-edit`), after the "Duplicate Track" entry. It calls `dawClearTrack(activeControlChannel)` on the currently selected track. Styled with `daw-dropdown-danger`.
- Adjust CSS `.track-compact`, `.track-medium`, `.track-large` heights accordingly.

**AC**: Clear button no longer in track rows. Timeline column wider. "Purge Track" in Edit menu works on active track. Notes more readable due to increased height.

### T4 — Inline Controls Inside Canvas (Overlay)
Rework `track-inline-controls` to render as a **semitransparent overlay at the bottom of the canvas cell** (position: absolute, bottom: 0), not as a separate div below it. Visible in **medium + large** modes (not just large). In compact mode, controls remain hidden.

Currently CSS `.track-large .track-inline-controls { display: flex; }`. Change to:
- `.track-medium .track-inline-controls, .track-large .track-inline-controls { display: flex; }`
- `.track-inline-controls { position: absolute; bottom: 0; left: 0; right: 0; }`
- `.track-timeline-cell { position: relative; }` (already has `padding: 0`)
- Background: `rgba(10, 10, 25, 0.65)` with `backdrop-filter: blur(2px)` for readability over notes.
- Canvas height: no longer subtract 22px for large mode — controls overlay on top of canvas content.

Update `applyTrackSize()`: remove the `mode === 'large' ? TRACK_SIZES[mode] - 22 : TRACK_SIZES[mode]` logic — always use full `TRACK_SIZES[mode]` since controls are now overlay.

**AC**: Vol/Pan controls visible in medium and large modes as overlay at canvas bottom. Controls don't push canvas smaller. Compact mode hides controls. Controls readable over note content thanks to backdrop blur.

### T5 — Enhanced Note Rectangles
In `renderTrackTape(ch)`: add 2px rounded corners (`roundRect`), a subtle top highlight (1px lighter line), and — when note width > 20px at current tapeScale — render the MIDI note name (e.g. "C4") inside the rectangle.

**AC**: Notes have slightly rounded corners. Highlight line on top edge. Note name text visible when zoomed in. Text does not overflow the rectangle. Performance not degraded at 16 tracks.

### T6 — Professional Sub-Beat Grid
In `renderAllTracks()` grid section: when `trackView.zoomLevel > 40`, render sub-beat lines (8ths or 16ths) with very faint opacity (0.08). Measure lines remain at 0.45 opacity.

**AC**: At high zoom, sub-divisions visible. At normal/low zoom, only beat and measure lines shown. No visual clutter at low zoom levels.

## 6. Verification

- [ ] All 6 tasks pass their AC
- [ ] dbl-click CH cell still cycles compact → medium → large
- [ ] Select mode + selection overlay still works
- [ ] Playhead follows correctly in all 3 phases
- [ ] Loop overlay renders correctly
- [ ] Inline vol/pan controls visible in medium + large modes (overlay, not below)
- [ ] Compact mode hides inline controls
- [ ] "Purge Track" in Edit menu clears the active track
- [ ] No "Clear" button in track rows
- [ ] No console errors
- [ ] `get_errors` on both files returns clean
