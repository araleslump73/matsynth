---
project_name: 'matsynth'
user_name: 'matteo'
date: '2026-03-27'
sections_completed: ['technology_stack', 'language_rules', 'framework_rules', 'testing_rules', 'code_quality', 'dev_workflow', 'critical_rules']
status: 'complete'
rule_count: 87
optimized_for_llm: true
---

# Project Context for AI Agents

_This file contains critical rules and patterns that AI agents must follow when implementing code in this project. Focus on unobvious details that agents might otherwise miss._

---

## Technology Stack & Versions

| Layer | Technology | Version / Constraint | Critical Notes |
|---|---|---|---|
| Runtime | Python | 3.7+ (Pi OS default) | No C-extension libs not in Pi OS repos |
| Web Framework | Flask | ≥ 3.0.0 | No ORM, no DB — flat JSON state file |
| Real-time | Flask-SocketIO | ≥ 5.3.0 | `async_mode='threading'` ONLY — never eventlet/gevent |
| MIDI Library | mido (+ python-rtmidi) | ≥ 1.3.0 | ⚠️ Missing from requirements.txt (known bug) |
| Audio Engine | FluidSynth | System daemon (not pip) | TCP shell on port 9800 via `socket` stdlib |
| Frontend | HTML5 + Bootstrap 5 + Vanilla JS + Canvas API 2D | CDN-served, no build step | No bundler — Pi serves static files only |
| Socket.IO Client | socket.io.js | CDN (must match server version) | Version mismatch breaks WebSocket |
| State | JSON flat file | Atomic write via `os.replace()` | Protected by `STATE_LOCK` (threading.Lock) |
| Target HW | Raspberry Pi Zero 2W | ARM A53 4×1GHz, 512MB RAM | CPU budget: Flask ~10%, FluidSynth ~60%, DAW ~20%, OS ~10% |
| Audio Output | ALSA via USB adapter | Buffer 64 samples (~1.5ms) | `plughw:N` device notation |
| Testing | None installed | Recommended: pytest + pytest-flask | Mock `send_fluid()` and mido ports for isolation |
| Deploy | SSH + bash scripts | systemd service | `deploy.sh` / `.ps1` / `.bat` |

## Critical Implementation Rules

### Python Backend Rules

- **Never `sleep()` in Flask main thread** — blocks all HTTP/WebSocket handling
- **Always use `STATE_LOCK`** (threading.Lock) before reading/writing `last_state.json`
- **Atomic file writes**: `write .tmp` → `fsync` → `os.replace()` — never direct `open().write()`
- **`os.path.basename()`** on every filename received from URL params — prevents path traversal
- **`subprocess.run(list)`** for shell commands — never `os.system()` with user input
- **`try/except`** around all FluidSynth/subprocess calls — daemon may be unavailable
- **Thread daemon mode** for all continuous loops (`thread.daemon = True`) — ensures clean shutdown
- **Log format**: `print(f"[ModuleName] message")` — English, consistent prefix
- **Comments and logs in English** — user-facing responses in Italian
- **No global mutable state without lock** — `sf_id` is a known unprotected global (tech debt)
- **Debounced state writer** for rapid CC changes — batches I/O to reduce SD card wear on Pi

### JavaScript Frontend Rules

- **Zero DOM reflow in animation loops** — only `transform`, `opacity`, `requestAnimationFrame`
- **Canvas API only** for timeline rendering — no SVG, no DOM manipulation in render loop
- **Off-screen canvas (tapes)** for pre-rendered track data — blit to visible canvas each frame
- **Pre-allocate arrays** to avoid GC pauses during playback
- **WebSocket payload < 1KB per tick** — no redundant data, minimal JSON
- **Debounce/throttle** on REST calls from sliders and controls
- **Channel indexing**: 0-based internally (JS + Python + MIDI), displayed as 1-16 in UI
- **No ES modules / import** — all JS is inline in HTML templates (no build step)

### Flask + SocketIO Rules

- **`async_mode='threading'`** — hardcoded, never change. eventlet/gevent break mido MIDI thread access
- **`cors_allowed_origins="*"`** — device is LAN-only, no auth, no cloud
- **SocketIO emitters from threads**: use `socketio.emit()` (not `emit()`) when calling from background threads
- **No Flask `before_request` hooks** for heavy computation — Pi CPU is the bottleneck
- **Single `daw` instance** — `MultiTrackDAW` is created once at module level in `app.py`. Never instantiate a second one — it claims exclusive MIDI port handles
- **Transport commands via WebSocket**: `emit('transport_cmd', {cmd: '...'})` — valid cmds: `play_start`, `play_stop`, `record_start`, `record_stop`, `stop_all`, `rewind`, `seek`

### FluidSynth Communication (`send_fluid()`)

- **Telnet-like protocol** — raw text commands over TCP socket (port 9800). NOT HTTP, NOT REST. Use `socket` stdlib only
- **Every call opens/closes a TCP socket** — minimize call frequency
- **Fire-and-forget** for write commands (`set`, `select`, `cc`, `unload`, `load`)
- **Read commands** (`inst`, `fonts`, `channels`) require `sleep(0.2)` + recv loop
- **Timeout: 2 seconds** — FluidSynth may hang on heavy soundfont loads
- **No lock on `send_fluid()`** — multi-step sequences (e.g. unload + load font) are NOT atomic. Avoid concurrent calls from different threads
- **`sf_id` global** tracks active soundfont ID — parsed from `fonts` command output
- **Soundfont loading is slow** (~5-10s on Pi) — never block HTTP response waiting for it

### MIDI / DAW Rules

- **Events stored in beats** (float), not seconds — allows BPM changes without data loss
- **`beat_position = elapsed_seconds / (60.0 / bpm)`**
- **Channel 9 (0-indexed) = GM drum kit** — reserved for metronome. Never `select_prog` on ch9 unless loading a drum kit
- **Metronome**: note 76 (beat 1), note 77 (other beats), 50ms duration
- **`_send_all_notes_off()`** before every start/stop to prevent stuck notes
- **MIDI port handles are exclusive** — `mido.open_input()`/`open_output()` claim the port. A second open fails or steals it
- **MIDI port discovery priority**: FANTOM > SINCO > USB > other (exclude 'MIDI THROUGH')
- **Flush MIDI buffer** before recording — discard accumulated stale messages

### Canvas Timeline Rendering

- **Two-layer rendering**: `renderTrackTape(ch)` writes to offscreen tape → `renderAllTracks()` blits tapes to visible canvases. Never draw directly on visible canvas
- **`ensureTape(ch, height)`** checks existence before creating — do not re-create unconditionally
- **`refreshDensityMap(force)`** is an async HTTP fetch that invalidates all tapes — call sparingly to avoid jank
- **16 canvases** (`track-canvas-N`, 22px height each) — one per MIDI channel
- **Density heatmap**: slot = 1/8 beat (0.125), color green→red via HSL
- **3-phase playhead scrolling**: static → follow → scroll-ahead
- **Playhead**: `lineWidth: 2`, red during rec/play, cyan during stop
- **Momentum scrolling** with friction on timeline drag
- **Selection overlay**: `rgba(255,200,50,0.25)` active channel, `.12` for other tracks

### Testing Rules

#### Two-Level Strategy: PC (Mock) + Pi (Smoke)

~80-90% of tests run **on PC** via `pytest` — the code does NOT require Pi hardware to test. Only two boundaries need mocking:

1. **`send_fluid()` (TCP to FluidSynth)** — mock `socket.socket` or patch `send_fluid` directly
2. **`mido.open_input()` / `mido.open_output()`** — mock at port level; `mido` parsing works on any OS

Everything else (Flask routes, SocketIO events, DAW state machine, JSON load/save, MIDI parsing, playlist logic, beat/tick math) is **pure Python, platform-independent**.

#### Mock Boundaries (PC Tests)

- `@patch('app.send_fluid')` — verify FluidSynth commands without TCP
- `@patch('mido.open_input')` / `@patch('mido.open_output')` — return `MagicMock` with `.receive()` iterator
- `@patch('app._start_update_thread')` — prevent daemon thread from starting in test process
- Use `tmp_path` (pytest fixture) for `last_state.json` — never touch real state file
- Use `monkeypatch.setenv` for path-dependent logic — avoid hardcoded `/home/matteo/` in tests
- Flask endpoints: `app.test_client()` — fully functional on PC
- SocketIO events: `socketio.test_client(app)` — fully functional on PC

#### Pi Smoke Tests (Integration)

Run on Pi only, via SSH script or manual check:

- FluidSynth daemon responds on port 9800
- `send_fluid('fonts')` returns valid font list
- SoundFont load completes within 15s
- MIDI port discovered (FANTOM > SINCO > USB)
- End-to-end: WebSocket `play_start` → notes audible
- Latency spot-check: tap-to-sound < 10ms

#### Test Priority (by risk)

1. **Security** — path traversal on filename params, subprocess injection
2. **State integrity** — `STATE_LOCK` races, atomic JSON write, `os.replace()` failure
3. **Transport state machine** — play/stop/record transitions, `_send_all_notes_off()` on every stop
4. **MIDI timing** — beat↔tick↔seconds conversions, BPM change preserves event positions
5. **API contracts** — Flask routes return expected status codes, SocketIO events emit correct payloads

#### conftest.py Fixtures

- `fake_fluid` — patches `send_fluid`, returns configurable responses per command
- `fake_midi_port` — patches `mido.open_*`, provides `.feed()` method to inject MIDI messages
- `daw_instance` — creates `MultiTrackDAW` with mocked ports + tmp state file
- `flask_app` — `app.test_client()` + `socketio.test_client()` wired to `daw_instance`

### Code Quality & Style Rules

#### File Structure

- **`app.py` is a monolith (~2000+ lines) — by design**. All backend logic lives here. Do NOT split into modules unless explicitly requested — the singleton `daw`, `STATE_LOCK`, and `send_fluid()` share module-level scope
- **No `__init__.py`** — this is not an installable package. Never create package structures
- **Templates are self-contained** — each `.html` in `/templates/` includes inline `<script>` and `<style>`. Do NOT extract JS/CSS into separate files
- **No build step** — no bundler, no transpiler, no minifier. What you write is what the Pi serves

#### Naming Conventions

- Python: `snake_case` functions/variables, `UPPER_SNAKE` constants, `PascalCase` classes only (`MultiTrackDAW`)
- JavaScript: `camelCase` functions/variables, `UPPER_SNAKE` for constants
- HTML IDs: `kebab-case` (`track-canvas-0`, `btn-play`)
- Channels: 0-indexed everywhere in code, displayed as 1-16 in UI

#### Code Hygiene

- **Constants inline, not in a separate file** — define `UPPER_SNAKE` at top of the file where they're used (e.g. `METRONOME_NOTE_ACCENT = 76` in `app.py`)
- **Import order**: stdlib → third-party → (no local modules)
- **No unused imports** — 512MB RAM, every import costs
- **No `logging` stdlib** — project uses `print(f"[Module] msg")`. Do not introduce `logging.getLogger()` unless explicitly requested
- **No type hints** — codebase has none. Adding them selectively creates inconsistency
- **Patch targets always `app.X`** — since everything lives in `app.py`, mock patches must target `app.send_fluid`, `app.mido`, etc. Never patch a module path that doesn't exist

### Development Workflow Rules

#### Development Cycle

- **Edit on PC → deploy via SSH → test on Pi via browser** — there is no hot reload or `flask run --debug` in production
- **No Git on Pi** — deploy is push-based from dev machine via `deploy.sh` / `.ps1` / `.bat`
- **No CI/CD pipeline** — testing and deploy are manual. AI agents must self-validate with `get_errors` after every change
- **Branch model**: `main` = stable, `feature/*` = active development (current: `feature/daw_enanched`)

#### Deploy Process

- `deploy.sh` copies files via SCP and restarts the `systemd` service
- **Boot order on Pi**: FluidSynth daemon (`startfluid.sh`) → Flask app (systemd) → browser connects
- FluidSynth is an **independent daemon** — not managed by Flask. `startfluid.sh` contains audio driver, buffer size, gain, and soundfont path config
- If you change FluidSynth parameters, edit `startfluid.sh` — not `app.py`

#### Path & Environment

- **Pi filesystem**: `/home/matteo/matsynth_web/` — paths in code are hardcoded to this. Never use Windows-style paths or assume relative paths resolve the same way
- **`last_state.json`** is runtime state — never commit to git. Verify it's in `.gitignore`
- **`requirements.txt` is incomplete** — `mido` and `python-rtmidi` are missing (known bug). A fresh `pip install -r requirements.txt` alone won't produce a working system

#### Adding Dependencies

- **No C compilation on Pi** — 512MB RAM is not enough for `pip install` of packages with C extensions. Only add dependencies that are pure Python or available in Pi OS apt repos
- **Verify on Pi OS**: `apt list` or `pip install --dry-run` before adding to requirements
- **Update `requirements.txt`** when adding any new pip dependency — even if currently incomplete

#### Documentation

- **Update `CONTEXT.md`** after architectural changes (new endpoints, new threads, new state fields) — not for minor fixes
- **Update `project-context.md`** if a rule in this file becomes obsolete or a new unobvious constraint is discovered

### Critical Don't-Miss Rules

_These are silent-failure traps. Violating them produces no errors but breaks the system in ways that are hard to diagnose._

#### Backend Traps

- ⛔ **Never instantiate a second `MultiTrackDAW()`** — it claims exclusive MIDI port handles. The second instance fails silently or steals the port from the first
- ⛔ **`send_fluid()` is not thread-safe** — multi-step sequences (unload → load → select) are NOT atomic. Concurrent calls from different threads produce interleaved commands with unpredictable results
- ⛔ **Never call `send_fluid()` during playback from a new endpoint** — commands interleave with the playback thread's MIDI output. Check `daw.playing` / `daw.recording` first; reject with 409 or queue
- ⛔ **`sf_id` is a global with no lock** — known tech debt. Do not add logic that depends on `sf_id` being stable across async operations
- ⛔ **`sf_id` changes on soundfont reload** — any cached reference to the old ID will send `select` to a non-existent font → silent failure (no notes, no error)
- ⛔ **Soundfont loading blocks FluidSynth for 5-10s** — never load synchronously in a Flask route. Use a background thread and notify completion via SocketIO
- ⛔ **`_send_all_notes_off()` before every transport start/stop** — omitting this = infinite stuck notes that survive until FluidSynth restarts
- ⛔ **Channel 9 (0-indexed) = GM drums** — never `select_prog` on ch9 unless loading a drum kit. Doing so replaces the drum map with a melodic instrument
- ⛔ **`mido.Message` is immutable** — `msg.note = 60` returns a new object. If you don't capture the return, the modification is silently lost

#### Frontend Traps

- ⛔ **Always `cancelAnimationFrame` before starting a new loop** — without this, start/stop cycles accumulate parallel animation loops, destroying client CPU
- ⛔ **Never `socket.emit()` inside `requestAnimationFrame`** — that's 60 emits/second. The Pi cannot handle it. Emit only in event handlers
- ⛔ **Remove event listeners before re-adding** — `addEventListener` without prior `removeEventListener` duplicates handlers on each re-render

#### Cross-Cutting Traps

- ⛔ **`os.path.basename()` on EVERY filename from URL** — no exceptions. Path traversal is the #1 security risk
- ⛔ **Socket.IO client/server version must match** — if you upgrade `flask-socketio`, update the CDN `<script>` tag in templates too. Mismatch breaks WebSocket silently (falls back to polling, then fails)
- ⛔ **Never extract inline JS/CSS from templates into separate files** — breaks the no-build-step architecture and the Pi's static file serving

---

## Usage Guidelines

**For AI Agents:**

- Read this file before implementing any code in this project
- Follow ALL rules exactly as documented
- When in doubt, prefer the more restrictive option
- Update this file if new patterns or constraints emerge

**For Humans:**

- Keep this file lean and focused on agent needs
- Update when technology stack changes
- Review periodically for outdated rules
- Remove rules that become obvious over time

Last Updated: 2026-03-27
