/* MatSynth - legacy_daw.js
 * JavaScript inline di index.html spostato qui dalla fase ui-shell SENZA cambi di comportamento.
 * Unici adattamenti (cerca "ui-shell"): (1) usa il socket unico di api.js invece di un proprio io();
 * (2) specchia i CC nello stato unico (state.updatePartCC); (3) il loop di disegno canvas si ferma
 * quando la vista Studio e' nascosta; (4) in fondo, il ponte verso stato unico e Libreria.
 * Script classico (non modulo): le funzioni restano globali perche' il markup usa onclick="dawPlay()" ecc.
 * Verra' smontato da ui-studio (timeline/tracce/trasporto) e ui-editor (piano roll/eventi). */
        let instrumentData = [];
        let activeControlChannel = 0; // The channel on which CC sliders act
        let channelCC = {};  // Per-channel CC values: channelCC[ch] = {7: vol, 10: pan, 71: res, ...}
        const SOUND_CCS = [73, 75, 72, 74, 71]; // ATK, DEC, REL, CUT, RES

        // ═══════════════ TRACK SIZE SYSTEM ═══════════════
        const TRACK_SIZES = { compact: 36, medium: 64, large: 110 };
        const TRACK_SIZE_ORDER = ['medium', 'large', 'compact'];
        const trackSizes = new Array(16).fill('medium');

        // Mixer Initialization
        async function setupMixer() {
            try {
                const res = await fetch('/get_instruments');
                instrumentData = await res.json();

                for (let i = 0; i < 16; i++) {
                    const dropdown = document.getElementById(`select-ch-${i}`);
                    if (dropdown) {
                        populateDropdown(dropdown);
                    }
                }
                
                // Carica lo stato salvato dai file
                await loadSavedChannelState();
                
                // Poi sincronizza con lo stato REALE di FluidSynth
                await syncWithSynth();
                
                // Highlight first channel by default
                setControlChannel(0);
            } catch(e) { console.error("Setup Error:", e); }
        }

        function populateDropdown(dropdown) {
            dropdown.innerHTML = '<option value="">-- Select Sound --</option>';
            instrumentData.forEach(inst => {
                if (!inst.includes('-')) return;
                const parts = inst.split('-');
                const bank = parts[0].trim();
                const prog = parts[1].trim().split(' ')[0];
                
                let opt = document.createElement('option');
                opt.value = `${bank},${prog}`;
                opt.innerText = inst;
                dropdown.appendChild(opt);
            });
        }

        async function loadSavedChannelState() {
            try {
                // Carica lo stato salvato (che include le selezioni dei canali)
                const resState = await fetch('/get_state');
                const state = await resState.json();
                
                console.log("Ripristino stato canali salvati:", state.channels);
                
                // Applica le selezioni salvate ai dropdown
                if (state.channels) {
                    const ccNameMap = {volume: 7, attack: 73, decay: 75, release: 72, cutoff: 74, resonance: 71};
                    for (let chan in state.channels) {
                        const channelData = state.channels[chan];
                        const bank = channelData.bank;
                        const prog = channelData.program;
                        const value = `${bank},${prog}`;
                        
                        const dropdown = document.getElementById(`select-ch-${chan}`);
                        if (dropdown) {
                            // Cerca l'opzione corrispondente nel dropdown
                            for (let option of dropdown.options) {
                                if (option.value === value) {
                                    dropdown.value = value;
                                    console.log(`✓ Ripristinato canale ${chan}: ${option.text}`);
                                    break;
                                }
                            }
                        }
                        // Populate channelCC from saved state
                        const ch = parseInt(chan);
                        if (!channelCC[ch]) channelCC[ch] = {};
                        for (let [name, cc] of Object.entries(ccNameMap)) {
                            if (channelData[name] !== undefined) {
                                channelCC[ch][cc] = parseInt(channelData[name]);
                            }
                        }
                        // Also store pan if present
                        if (channelData.pan !== undefined) channelCC[ch][10] = parseInt(channelData.pan);
                    }
                }
            } catch (e) {
                console.error("Errore caricamento stato canali:", e);
            }
        }

        // Change Channel for Sliders
        function setControlChannel(chan) {
            activeControlChannel = chan;
            document.getElementById('target-ch-display').innerText = chan + 1;
            // Highlight the active track row with selection class
            for (let i = 0; i < 16; i++) {
                const row = document.getElementById(`daw-track-${i}`);
                if (row) {
                    row.classList.toggle('daw-track-selected', i === chan);
                }
            }
            // Sync Channel Controls sliders FROM inline track sliders
            const iv = document.getElementById(`track-vol-${chan}`);
            const ip = document.getElementById(`track-pan-${chan}`);
            const mainVol = document.getElementById('vol-slider');
            const mainPan = document.getElementById('pan-slider');
            if (iv && mainVol) {
                mainVol.value = iv.value;
                const vl = document.getElementById('val-7');
                if (vl) vl.textContent = iv.value;
            }
            if (ip && mainPan) {
                mainPan.value = ip.value;
                updatePanLabel(ip.value);
            }
            // Sync SOUND tab sliders (ADSR + filter) from channelCC map
            const ccData = channelCC[chan] || {};
            SOUND_CCS.forEach(cc => {
                const val = ccData[cc] !== undefined ? ccData[cc] : 64;
                const slider = document.getElementById(`cc-slider-${cc}`);
                const badge = document.getElementById(`val-${cc}`);
                if (slider) slider.value = val;
                if (badge) badge.textContent = val;
            });
            // U2: Auto-switch to SOUND tab if active track is in large mode
            const hint = document.getElementById('tab-mix-inline-hint');
            if (trackSizes[chan] === 'large') {
                switchMixerTab('tab-sound');
                if (hint) hint.style.display = '';
            } else {
                if (hint) hint.style.display = 'none';
            }
        }

        // Actions
        async function changeInst(chan, val) {
            if (!val) return;
            const [bank, prog] = val.split(',');
            setControlChannel(chan);
            await fetch(`/select_prog/${chan}/${bank}/${prog}`);
            
            // Quick channel reset via FluidSynth command
            // await fetch(`/reset_channel/${chan}`);
            
            // Reset UI sliders to default values
            resetUISliders();

            // Update compact-mode instrument label
            updateTrackInstLabel(chan);
        }
        
        // NOTE: With native vertical sliders (-webkit-appearance: slider-vertical), 
        // we cannot easily modify the background gradient as with horizontal ones.
        // The updateSliderBackground function is removed because it has no effect on native vertical sliders.
        
        function resetUISliders() {
            // Reset only the UI to default values
            
            // Volume (CC7) - default 100
            const volumeSlider = document.querySelector('input[oninput*="updateCC(7"]');
            if (volumeSlider) { 
                volumeSlider.value = 100; 
                document.getElementById('val-7').innerText = "100";
            }
            updateCC(7, 100);

            // Pan (CC10) - default 64 (center)
            const panSlider = document.getElementById('pan-slider');
            if (panSlider) {
                panSlider.value = 64;
                updatePanLabel(64);
            }
            updateCC(10, 64);
            
            // Attack (CC73) - default 64
            const attackSlider = document.querySelector('input[oninput*="updateCC(73"]');
            if (attackSlider) { 
                attackSlider.value = 64; 
                document.getElementById('val-73').innerText = "64";
            }
            updateCC(73, 64);
            
            // Decay (CC75) - default 64
            const decaySlider = document.querySelector('input[oninput*="updateCC(75"]');
            if (decaySlider) { 
                decaySlider.value = 64; 
                document.getElementById('val-75').innerText = "64";
            }
            updateCC(75, 64);
            
            // Release (CC72) - default 64
            const releaseSlider = document.querySelector('input[oninput*="updateCC(72"]');
            if (releaseSlider) { 
                releaseSlider.value = 64; 
                document.getElementById('val-72').innerText = "64";
            }
            updateCC(72, 64);

            // Cutoff (CC74) - default 64
            const cutoffSlider = document.querySelector('input[oninput*="updateCC(74"]');
            if (cutoffSlider) { 
                cutoffSlider.value = 64; 
                document.getElementById('val-74').innerText = "64";
            }
            updateCC(74, 64);

            // Resonance (CC71) - default 0
            const resoSlider = document.querySelector('input[oninput*="updateCC(71"]');
            if (resoSlider) { 
                resoSlider.value = 64; 
                document.getElementById('val-71').innerText = "64";
            }
            updateCC(71, 64);
        }

        function updateCC(cc, val) {
            // Store value in per-channel map
            if (!channelCC[activeControlChannel]) channelCC[activeControlChannel] = {};
            channelCC[activeControlChannel][cc] = parseInt(val);
            // ui-shell: specchio nello stato unico, cosi' il pannello della vista Suona vede lo stesso valore.
            // origin 'legacy' evita che il ponte in fondo al file riapplichi il valore a questi stessi slider.
            window.MatSynth.state.updatePartCC(activeControlChannel, cc, parseInt(val), { origin: 'legacy' });

            if (dawSocket && dawSocket.connected) {
                dawSocket.emit('cc_update', {chan: activeControlChannel, cc: cc, val: val});
            } else {
                fetch(`/cc/${activeControlChannel}/${cc}/${val}`);
            }
            // Sync inline track sliders (bidirectional)
            if (cc === 7) {
                const iv = document.getElementById(`track-vol-${activeControlChannel}`);
                if (iv) { iv.value = val; document.getElementById(`track-vol-val-${activeControlChannel}`).textContent = val; }
            } else if (cc === 10) {
                const ip = document.getElementById(`track-pan-${activeControlChannel}`);
                if (ip) { ip.value = val; updateInlinePanLabel(activeControlChannel, val); }
            }
        }

        function updatePanLabel(val) {
            val = parseInt(val);
            const label = document.getElementById('val-10');
            if (!label) return;
            if (val < 60) label.innerText = 'L' + (64 - val);
            else if (val > 68) label.innerText = 'R' + (val - 64);
            else label.innerText = 'C';
        }

        function setEff(type, val) {
            if (dawSocket && dawSocket.connected) {
                dawSocket.emit('effect_update', {type: type, val: val});
            } else {
                fetch(`/set_effect/${type}/${val}`);
            }
        }

function updateGain(val) {
    // Update the visible number next to the label
    document.getElementById('gain-value').innerText = parseFloat(val).toFixed(1);
    
    // Send command to server
    // Note: 'gain' doesn't need special prefixes because the route 
    // in app.py already adds 'synth.'
    setEff('gain', val);
}

 
async function syncWithSynth() {
    const syncBtn = document.querySelector('.daw-menu-sync');
    if (syncBtn) {
        syncBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        syncBtn.disabled = true;
    }

    try {
        const res = await fetch('/refresh_status');
        const data = await res.json();
        const lines = data.raw.split('\n');

        lines.forEach(line => {
            // Extract the channel number and instrument name
            // Example: "chan 0, Grand Piano" -> ch: 0, name: "Grand Piano"
            const parts = line.split(',');
            if (parts.length >= 2) {
                const chan = parts[0].replace('chan', '').trim();
                const instrumentName = parts[1].trim();

                const dropdown = document.getElementById(`select-ch-${chan}`);
                if (dropdown) {
                    // Search among options for the one containing the instrument name
                    for (let option of dropdown.options) {
                        if (option.text.includes(instrumentName)) {
                            dropdown.value = option.value;
                            break;
                        }
                    }
                }
            }
        });
    } catch (e) {
        console.error("Sync Error:", e);
    } finally {
        if (syncBtn) {
            syncBtn.innerHTML = '<i class="fas fa-sync-alt"></i>';
            syncBtn.disabled = false;
        }
    }
}

async function loadInitialState() {
    try {
        const resState = await fetch('/get_state');
        const state = await resState.json();

        console.log("State received from server:", state);

        // Update the Gain (Volume)
        if (state.gain) {
            const val = parseFloat(state.gain);
            const badge = document.getElementById('gain-value');
            if (badge) badge.innerText = val.toFixed(1);
            const slider = document.querySelector('input[oninput*="updateGain"]');
            if (slider) slider.value = val;
        }
    } catch (e) {
        console.error("Initial state sync error:", e);
    }
}


// Call the function on load
loadInitialState();


// ==========================================
// DAW MULTI-TRACK RECORDER JAVASCRIPT
// ==========================================

let dawState = null;
let dawSocket = null;  // WebSocket connection
let armInProgress = {};  // Track which ARM buttons are being processed
let muteInProgress = {};  // Track which MUTE buttons are being processed
let soloInProgress = {};  // Track which SOLO buttons are being processed
let trackView = {
    startTime: 0,
    zoomLevel: 10,
    noteMap: {},               // note data per track for piano-roll
    meta: { totalBeats: 0, beatsPerMeasure: 4 },
    liveActivity: {},
    lastTrackCounts: {},
    rafId: null,
    tapes: {},                // offscreen “virtual strip” per track
    tapeScale: 40,            // px per beat on the offscreen tape
    totalBars: 200,           // logical tape length
    lastBeatsPerMeasure: 4,
    drag: {
        active: false,
        lastX: 0,
        lastT: 0,
        velocity: 0,
        momentumId: null
    }
};
let densityRefreshTimer = null;
let recordStartPosition = null;  // saved position before recording
let selectionInBeat = null;   // mark-in beat position
let selectionOutBeat = null;  // mark-out beat position
let selectMode = false;       // drag-to-select mode toggle

let hiddenTracks = new Set();

// Clip drag state for drag-on-canvas clip moving
let clipDrag = null; // { ch, clipId, startBeat, endBeat, offsetBeat, ghostBeat }
// Clip resize state for right-edge resize
let clipResize = null; // { ch, clipId, startBeat, origEndBeat, ghostEndBeat, changed }

// ═══════════════ PIANO ROLL EDITOR STATE ═══════════════
const pianoRoll = {
    visible: false,
    scrollNote: 84,       // top visible MIDI note
    rowHeight: 12,        // px per semitone row
    lastTapTime: 0,       // for double-tap detection on canvas
    lastTapCh: -1,
    bodyHeight: 280,      // user-resizable panel height (px)
    tool: 'select',       // 'select' | 'draw' | 'erase'
    defaultVelocity: 100, // velocity for newly drawn notes
    defaultLength: 1,     // default note length in beats for draw tool
};
const PR_DOUBLETAP_MS = 400;
const PR_BLACK_KEYS = new Set([1, 3, 6, 8, 10]);
const PR_NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];

// Piano Roll interactive editor drag/selection state
let prEditor = {
    drag: null,       // { mode:'move'|'resize-right'|'draw'|'box'|'vel', ch, startX, startY, originals, ... }
    ghostNote: null,  // { note, startBeat, endBeat } preview while drawing
    boxSel: null,     // { x0, y0, x1, y1 } box selection rectangle (canvas coords)
    velDrag: null,    // { ch, noteId, startY, origVel } velocity lane drag
};

function hitTestClip(ch, canvas, localX) {
    if (!dawState || !dawState.clips) return null;
    const clips = dawState.clips[ch] || [];
    if (!clips.length) return null;
    const tapeStartTime = trackView.startTime;
    for (let i = clips.length - 1; i >= 0; i--) {
        const clip = clips[i];
        const cx0 = (clip.start_beat - tapeStartTime) * trackView.zoomLevel;
        const cx1 = (clip.end_beat - tapeStartTime) * trackView.zoomLevel;
        if (localX >= cx0 && localX <= cx1) return clip;
    }
    return null;
}

const CLIP_EDGE_PX = 8; // px tolerance for right-edge hit detection
function hitTestClipEdge(ch, canvas, localX) {
    if (!dawState || !dawState.clips) return null;
    const clips = dawState.clips[ch] || [];
    if (!clips.length) return null;
    const tapeStartTime = trackView.startTime;
    for (let i = clips.length - 1; i >= 0; i--) {
        const clip = clips[i];
        const cx1 = (clip.end_beat - tapeStartTime) * trackView.zoomLevel;
        if (Math.abs(localX - cx1) <= CLIP_EDGE_PX) return clip;
    }
    return null;
}

async function commitClipDrag() {
    if (!clipDrag || !clipDrag.changed) { clipDrag = null; return; }
    const targetBeat = Math.max(0, snapToGrid(clipDrag.ghostBeat));
    try {
        const res = await fetch('/api/daw/clip/move', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                channel: clipDrag.ch,
                clip_id: clipDrag.clipId,
                target_start_beat: targetBeat
            })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showStatus(`Clip moved to beat ${targetBeat.toFixed(1)}`, 'success');
            await refreshDensityMap(true);
            await dawLoadInitialState();
        } else {
            showStatus(data.message || 'Clip move failed', 'danger');
        }
    } catch (err) {
        console.error('Clip drag commit error:', err);
    }
    clipDrag = null;
}

async function commitClipResize() {
    if (!clipResize || !clipResize.changed) { clipResize = null; return; }
    const newEnd = Math.max(clipResize.startBeat + 0.125, snapToGrid(clipResize.ghostEndBeat));
    try {
        const res = await fetch('/api/daw/clip/resize', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                channel: clipResize.ch,
                clip_id: clipResize.clipId,
                new_end_beat: newEnd
            })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showStatus(`Clip resized to beat ${newEnd.toFixed(1)}`, 'success');
            await refreshDensityMap(true);
            await dawLoadInitialState();
        } else {
            showStatus(data.message || 'Clip resize failed', 'danger');
        }
    } catch (err) {
        console.error('Clip resize commit error:', err);
    }
    clipResize = null;
}

function getLastClip(channel) {
    if (!dawState || !dawState.clips) return null;
    const list = dawState.clips[channel] || [];
    if (!list.length) return null;
    return list[list.length - 1];
}

async function dawMoveLastClip(deltaBeat) {
    const ch = activeControlChannel;
    const clip = getLastClip(ch);
    if (!clip) {
        showStatus('No clip on active track', 'warning');
        return;
    }
    if (!dawState || dawState.is_playing || dawState.is_recording) {
        showStatus('Stop playback/recording before moving clip', 'warning');
        return;
    }
    try {
        const res = await fetch('/api/daw/clip/move', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                channel: ch,
                clip_id: clip.id,
                target_start_beat: Math.max(0, (clip.start_beat || 0) + deltaBeat)
            })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showStatus(`Clip moved ${deltaBeat > 0 ? 'right' : 'left'} by ${Math.abs(deltaBeat)} beat`, 'success');
            await refreshDensityMap(true);
            await dawLoadInitialState();
        } else {
            showStatus(data.message || 'Clip move failed', 'danger');
        }
    } catch (e) {
        console.error('Move clip error:', e);
    }
}

async function dawDeleteLastClip() {
    const ch = activeControlChannel;
    const clip = getLastClip(ch);
    if (!clip) {
        showStatus('No clip on active track', 'warning');
        return;
    }
    if (!confirm(`Delete last clip on CH ${ch + 1}?`)) return;
    try {
        const res = await fetch('/api/daw/clip/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: ch, clip_id: clip.id })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showStatus('Clip deleted', 'success');
            await refreshDensityMap(true);
            await dawLoadInitialState();
        } else {
            showStatus(data.message || 'Clip delete failed', 'danger');
        }
    } catch (e) {
        console.error('Delete clip error:', e);
    }
}

async function dawTransposeLastClip(semitones) {
    const ch = activeControlChannel;
    const clip = getLastClip(ch);
    if (!clip) {
        showStatus('No clip on active track', 'warning');
        return;
    }
    if (!dawState || dawState.is_playing || dawState.is_recording) {
        showStatus('Stop playback/recording before transpose', 'warning');
        return;
    }
    try {
        const res = await fetch('/api/daw/clip/transpose', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: ch, clip_id: clip.id, semitones })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showStatus(`Clip transpose ${semitones > 0 ? '+' : ''}${semitones} · ${data.modified} events`, 'success');
            await refreshDensityMap(true);
        } else {
            showStatus(data.message || 'Clip transpose failed', 'danger');
        }
    } catch (e) {
        console.error('Transpose clip error:', e);
    }
}

async function dawCutClipAtPlayhead() {
    const ch = activeControlChannel;
    if (!dawState || dawState.is_playing || dawState.is_recording) {
        showStatus('Stop playback/recording first', 'warning');
        return;
    }
    const clips = (dawState.clips && dawState.clips[ch]) || [];
    if (!clips.length) {
        showStatus('No clips on active track', 'warning');
        return;
    }
    // Find the clip that contains the playhead
    const playBeat = dawState.current_beat || 0;
    const clip = clips.find(c => playBeat > c.start_beat + 0.01 && playBeat < c.end_beat - 0.01);
    if (!clip) {
        showStatus('Playhead is not inside any clip', 'warning');
        return;
    }
    try {
        const res = await fetch('/api/daw/clip/split', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: ch, clip_id: clip.id, split_beat: playBeat })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showStatus(`Clip split at beat ${playBeat.toFixed(1)}`, 'success');
            await refreshDensityMap(true);
            await dawLoadInitialState();
        } else {
            showStatus(data.message || 'Clip split failed', 'danger');
        }
    } catch (e) {
        console.error('Cut clip error:', e);
    }
}

async function dawMergeAllClips() {
    const ch = activeControlChannel;
    if (!dawState || dawState.is_playing || dawState.is_recording) {
        showStatus('Stop playback/recording first', 'warning');
        return;
    }
    const clips = (dawState.clips && dawState.clips[ch]) || [];
    if (clips.length < 2) {
        showStatus('Need at least 2 clips to merge', 'warning');
        return;
    }
    const clipIds = clips.map(c => c.id);
    try {
        const res = await fetch('/api/daw/clip/merge', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: ch, clip_ids: clipIds })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showStatus(`${clips.length} clips merged`, 'success');
            await refreshDensityMap(true);
            await dawLoadInitialState();
        } else {
            showStatus(data.message || 'Clip merge failed', 'danger');
        }
    } catch (e) {
        console.error('Merge clips error:', e);
    }
}

// Interactive piano roll editor state
let noteEditor = {
    selected: new Set(),
    drag: null,
    box: null,
    preview: { note: null, at: 0 }
};

// Long-press handler for CH cells: tap = select, hold 600ms = hide
let chPressTimer = null;
let chLongPressTriggered = false;

function getChannelFromCanvas(canvas) {
    if (!canvas || !canvas.id) return -1;
    const m = canvas.id.match(/^track-canvas-(\d+)$/);
    return m ? parseInt(m[1], 10) : -1;
}

function noteEntryId(ch, n) {
    const onIdx = (n.length > 4) ? n[4] : -1;
    const offIdx = (n.length > 5) ? n[5] : -1;
    if (onIdx >= 0 || offIdx >= 0) {
        return `${ch}:${onIdx}:${offIdx}`;
    }
    return `${ch}:n:${n[0]}:${n[2]}:${n[3]}`;
}

function getTrackNoteMetrics(trackData, canvasHeight) {
    const minNote = trackData?.min_note ?? 48;
    const maxNote = trackData?.max_note ?? 84;
    const noteRange = Math.max(1, maxNote - minNote);
    const padding = 2;
    const drawHeight = canvasHeight - padding * 2;
    const noteH = Math.max(1, Math.min(drawHeight / (noteRange + 1), canvasHeight * 0.18));
    return { minNote, maxNote, noteRange, padding, drawHeight, noteH };
}

function previewNote(channel, note, velocity = 96) {
    const now = performance.now();
    if ((now - noteEditor.preview.at) < 180) return;
    if (noteEditor.preview.note === note && (now - noteEditor.preview.at) < 300) return;
    noteEditor.preview.note = note;
    noteEditor.preview.at = now;
    fetch('/api/daw/preview_note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, note, velocity, length_ms: 120 })
    }).catch(() => {});
}

function findCurrentNoteById(ch, id) {
    const trackData = trackView.noteMap[ch];
    if (!trackData || !trackData.notes) return null;
    for (const n of trackData.notes) {
        if (noteEntryId(ch, n) === id) return n;
    }
    return null;
}

function setupChCellHandlers() {
    document.querySelectorAll('.ch-cell').forEach(cell => {
        const chan = parseInt(cell.dataset.ch);

        function startPress(e) {
            if (e.type === 'mousedown' && e.button !== 0) return;
            e.preventDefault();
            chLongPressTriggered = false;
            cell.classList.add('ch-holding');

            chPressTimer = setTimeout(() => {
                chLongPressTriggered = true;
                cell.classList.remove('ch-holding');
                dawHideTrack(chan);
            }, 600);
        }

        function endPress() {
            clearTimeout(chPressTimer);
            cell.classList.remove('ch-holding');
            if (!chLongPressTriggered) {
                setControlChannel(chan);
            }
        }

        function cancelPress() {
            clearTimeout(chPressTimer);
            cell.classList.remove('ch-holding');
            chLongPressTriggered = false;
        }

        cell.addEventListener('touchstart', startPress, { passive: false });
        cell.addEventListener('touchend', endPress);
        cell.addEventListener('touchcancel', cancelPress);
        cell.addEventListener('mousedown', startPress);
        cell.addEventListener('mouseup', endPress);
        cell.addEventListener('mouseleave', cancelPress);
        cell.addEventListener('contextmenu', e => { e.preventDefault(); dawCycleTrackColor(chan); });
        cell.addEventListener('dblclick', e => { e.preventDefault(); dawCycleTrackSize(chan); });
    });

    // Click anywhere on a track row to select that channel
    for (let ch = 0; ch < 16; ch++) {
        const row = document.getElementById(`daw-track-${ch}`);
        if (!row) continue;
        row.addEventListener('click', function(e) {
            const tag = e.target.tagName;
            if (tag === 'BUTTON' || tag === 'SELECT' || tag === 'INPUT' || tag === 'OPTION'
                || e.target.closest('button') || e.target.closest('select') || e.target.closest('.ch-cell')) return;
            setControlChannel(ch);
        });
    }
}

function dawHideTrack(ch) {
    const row = document.getElementById(`daw-track-${ch}`);
    if (!row) return;
    row.style.display = 'none';
    const cr = document.getElementById(`track-controls-${ch}`);
    if (cr) cr.style.display = 'none';
    hiddenTracks.add(ch);
    dawUpdateHiddenBar();
}

function dawShowTrack(ch) {
    const row = document.getElementById(`daw-track-${ch}`);
    if (!row) return;
    row.style.display = '';
    const cr = document.getElementById(`track-controls-${ch}`);
    if (cr) cr.style.display = '';
    hiddenTracks.delete(ch);
    dawUpdateHiddenBar();
}

function dawShowAllTracks() {
    hiddenTracks.forEach(ch => {
        const row = document.getElementById(`daw-track-${ch}`);
        if (row) row.style.display = '';
        const cr = document.getElementById(`track-controls-${ch}`);
        if (cr) cr.style.display = '';
    });
    hiddenTracks.clear();
    dawUpdateHiddenBar();
}

function dawUpdateHiddenBar() {
    const bar = document.getElementById('daw-hidden-bar');
    const badges = document.getElementById('daw-hidden-badges');
    if (hiddenTracks.size === 0) {
        bar.style.display = 'none';
        return;
    }
    bar.style.display = '';
    badges.innerHTML = '';
    [...hiddenTracks].sort((a, b) => a - b).forEach(ch => {
        const color = ch === 9 ? '#ff9800' : '#0dcaf0';
        const badge = document.createElement('span');
        badge.className = 'badge';
        badge.style.cssText = `cursor:pointer; background:${color}; color:#000; font-size:0.7rem; padding:2px 6px;`;
        badge.textContent = ch + 1;
        badge.title = `Show CH ${ch + 1}`;
        badge.onclick = () => dawShowTrack(ch);
        badges.appendChild(badge);
    });
}

// ═══════════════ TRACK SIZE INIT & FUNCTIONS ═══════════════

function initTrackSizeSystem() {
    for (let ch = 0; ch < 16; ch++) {
        const row = document.getElementById(`daw-track-${ch}`);
        if (!row) continue;

        // Add default class
        row.classList.add('track-medium');

        // Add instrument label span (hidden in medium, visible in compact)
        const instSelect = document.getElementById(`select-ch-${ch}`);
        if (instSelect) {
            const label = document.createElement('span');
            label.className = 'track-inst-label';
            label.id = `track-inst-label-${ch}`;
            label.textContent = '--';
            instSelect.parentElement.appendChild(label);
        }

        // Embed controls inside the instrument cell (visible in medium+large via CSS)
        const instTd = instSelect ? instSelect.parentElement : null;
        const canvas = document.getElementById(`track-canvas-${ch}`);
        if (canvas) {
            const timelineTd = canvas.parentElement;
            timelineTd.classList.add('track-timeline-cell');
        }
        if (instTd) {
            const controls = document.createElement('div');
            controls.className = 'track-inline-controls';
            controls.id = `track-controls-${ch}`;
            controls.innerHTML = `
                <div class="tic-row">
                    <span class="tic-icon" style="color:#00ff88;"><i class="fas fa-volume-up"></i></span>
                    <input type="range" id="track-vol-${ch}" class="track-inline-slider" min="0" max="127" value="100">
                    <span id="track-vol-val-${ch}" class="tic-val">100</span>
                </div>
                <div class="tic-row">
                    <span class="tic-icon" style="color:#00b4ff;"><i class="fas fa-arrows-alt-h"></i></span>
                    <input type="range" id="track-pan-${ch}" class="track-inline-slider" min="0" max="127" value="64">
                    <span id="track-pan-val-${ch}" class="tic-val">C</span>
                </div>
            `;
            instTd.appendChild(controls);
        }

        // Setup inline slider events (closure over ch)
        setupInlineSliders(ch);
    }
}

function setupInlineSliders(ch) {
    const volSlider = document.getElementById(`track-vol-${ch}`);
    const panSlider = document.getElementById(`track-pan-${ch}`);
    if (!volSlider || !panSlider) return;

    volSlider.addEventListener('input', function() {
        const val = parseInt(this.value);
        document.getElementById(`track-vol-val-${ch}`).textContent = val;
        // Send CC7 to this specific channel
        if (dawSocket && dawSocket.connected) {
            dawSocket.emit('cc_update', {chan: ch, cc: 7, val: val});
        } else {
            fetch(`/cc/${ch}/7/${val}`);
        }
        // Sync with Channel Controls if this is the active channel
        if (ch === activeControlChannel) {
            const mainVol = document.querySelector('#vol-slider');
            if (mainVol) { mainVol.value = val; }
            const valLabel = document.getElementById('val-7');
            if (valLabel) valLabel.textContent = val;
        }
    });

    panSlider.addEventListener('input', function() {
        const val = parseInt(this.value);
        updateInlinePanLabel(ch, val);
        // Send CC10 to this specific channel
        if (dawSocket && dawSocket.connected) {
            dawSocket.emit('cc_update', {chan: ch, cc: 10, val: val});
        } else {
            fetch(`/cc/${ch}/10/${val}`);
        }
        // Sync with Channel Controls if this is the active channel
        if (ch === activeControlChannel) {
            const mainPan = document.getElementById('pan-slider');
            if (mainPan) { mainPan.value = val; }
            updatePanLabel(val);
        }
    });
}

function updateInlinePanLabel(ch, val) {
    val = parseInt(val);
    const label = document.getElementById(`track-pan-val-${ch}`);
    if (!label) return;
    if (val < 60) label.textContent = 'L' + (64 - val);
    else if (val > 68) label.textContent = 'R' + (val - 64);
    else label.textContent = 'C';
}

function dawCycleTrackSize(ch) {
    const current = trackSizes[ch];
    const idx = TRACK_SIZE_ORDER.indexOf(current);
    const next = TRACK_SIZE_ORDER[(idx + 1) % TRACK_SIZE_ORDER.length];
    trackSizes[ch] = next;
    applyTrackSize(ch);
}

function applyTrackSize(ch) {
    const mode = trackSizes[ch];
    const row = document.getElementById(`daw-track-${ch}`);
    const canvas = document.getElementById(`track-canvas-${ch}`);
    if (!row || !canvas) return;

    // Remove old classes, add new
    row.classList.remove('track-compact', 'track-medium', 'track-large');
    row.classList.add(`track-${mode}`);

    // Canvas height (full height — inline controls are overlay)
    canvas.height = TRACK_SIZES[mode];

    // U2: Auto-switch mixer tab when active track changes size
    if (ch === activeControlChannel) {
        const hint = document.getElementById('tab-mix-inline-hint');
        if (mode === 'large') {
            switchMixerTab('tab-sound');
            if (hint) hint.style.display = '';
        } else {
            switchMixerTab('tab-mix');
            if (hint) hint.style.display = 'none';
        }
    }

    // Update instrument label (compact shows text instead of dropdown)
    updateTrackInstLabel(ch);

    // Size indicator dot on CH cell
    const chCell = row.querySelector('.ch-cell');
    if (chCell) {
        let dot = chCell.querySelector('.ch-size-dot');
        if (mode === 'medium') {
            if (dot) dot.remove();
        } else {
            if (!dot) {
                dot = document.createElement('span');
                dot.className = 'ch-size-dot';
                chCell.appendChild(dot);
            }
            dot.className = `ch-size-dot ${mode}`;
        }
        // Update tooltip
        chCell.title = `Tap: select · Hold: hide · DblClick: resize (${mode})`;
    }

    // Force tape re-render
    trackView.tapes[ch] = null;
    renderTrackTape(ch);
    renderAllTracks();
}

function updateTrackInstLabel(ch) {
    const label = document.getElementById(`track-inst-label-${ch}`);
    const select = document.getElementById(`select-ch-${ch}`);
    if (!label || !select) return;
    const opt = select.options[select.selectedIndex];
    label.textContent = (opt && opt.value) ? opt.text.substring(0, 25) : '--';
}

// ═══════════════════════ MIDI EVENT VIEWER ═══════════════════
let evViewerOpen = false;
let evCurrentChannel = -1;  // -1 = all
let evEvents = [];           // cached decoded events
let evHighlightBeat = -1;    // current playback beat for highlight

function dawOpenEventViewer() {
    evViewerOpen = true;
    document.getElementById('midi-event-viewer-modal').style.display = 'flex';
    dawBuildChannelFilter();
    dawEventViewerFilter();
}

function dawCloseEventViewer(event) {
    if (event && event.target !== event.currentTarget) return;
    evViewerOpen = false;
    document.getElementById('midi-event-viewer-modal').style.display = 'none';
}

function dawBuildChannelFilter() {
    const sel = document.getElementById('ev-channel-filter');
    const val = sel.value;
    sel.innerHTML = '<option value="-1">All</option>';
    if (dawState && dawState.has_data) {
        for (let ch = 0; ch < 16; ch++) {
            if (dawState.has_data[ch]) {
                const name = (dawState.track_names && dawState.track_names[ch]) || ('Track ' + (ch + 1));
                const opt = document.createElement('option');
                opt.value = ch;
                opt.textContent = 'Ch ' + ch + ' — ' + name;
                sel.appendChild(opt);
            }
        }
    }
    sel.value = val;
}

async function dawEventViewerFilter() {
    const sel = document.getElementById('ev-channel-filter');
    evCurrentChannel = parseInt(sel.value);
    await dawLoadEvents();
}

async function dawLoadEvents() {
    const ch = evCurrentChannel;
    try {
        const res = await fetch('/api/daw/track/' + ch + '/events');
        const data = await res.json();
        if (data.status === 'ok') {
            evEvents = data.events || [];
            dawRenderEventTable();
        }
    } catch (e) {
        console.error('[EventViewer] Load error:', e);
    }
}

function dawRenderEventTable() {
    const tbody = document.getElementById('ev-table-body');
    const countEl = document.getElementById('ev-event-count');
    countEl.textContent = evEvents.length + ' events';

    if (!evEvents.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted py-3">No events</td></tr>';
        return;
    }

    const isRec = dawState && dawState.is_recording;
    let html = '';
    for (let i = 0; i < evEvents.length; i++) {
        const ev = evEvents[i];
        const isHighlighted = (evHighlightBeat >= 0 && Math.abs(ev.beat - evHighlightBeat) < 0.05);
        const cls = isHighlighted ? ' style="background:rgba(0,255,136,0.18);"' : '';
        html += '<tr id="ev-row-' + i + '"' + cls + '>';
        html += '<td>' + ev.beat.toFixed(2) + '</td>';
        html += '<td>' + dawEvTypeBadge(ev.type) + '</td>';
        html += '<td>' + (ev.note || '—') + '</td>';
        html += '<td>' + (ev.vel !== undefined ? ev.vel : '—') + '</td>';
        html += '<td>' + ev.ch + '</td>';
        html += '<td>' + (ev.dur !== null && ev.dur !== undefined ? ev.dur.toFixed(2) : '—') + '</td>';
        html += '<td>' + (ev.cc_num !== undefined ? ev.cc_num : '—') + '</td>';
        html += '<td>' + (ev.cc_val !== undefined ? ev.cc_val : (ev.pitch !== undefined ? ev.pitch : (ev.program !== undefined ? ev.program : '—'))) + '</td>';
        html += '<td>';
        if (!isRec) {
            html += '<button class="btn btn-outline-warning btn-sm px-1 py-0" style="font-size:0.68rem;" onclick="dawEditEventInline(' + ev.ch + ',' + ev.idx + ',' + i + ')" title="Edit"><i class="fas fa-pen"></i></button> ';
            html += '<button class="btn btn-outline-danger btn-sm px-1 py-0" style="font-size:0.68rem;" onclick="dawDeleteEvent(' + ev.ch + ',' + ev.idx + ')" title="Delete"><i class="fas fa-trash"></i></button>';
        }
        html += '</td>';
        html += '</tr>';
    }
    tbody.innerHTML = html;
}

function dawEvTypeBadge(type) {
    const colors = {
        'note_on': '#00ff88',
        'note_off': '#666',
        'control_change': '#0dcaf0',
        'program_change': '#ffc107',
        'pitchwheel': '#b388ff'
    };
    const short = {
        'note_on': 'NoteOn',
        'note_off': 'NoteOff',
        'control_change': 'CC',
        'program_change': 'PC',
        'pitchwheel': 'Pitch'
    };
    const c = colors[type] || '#aaa';
    const t = short[type] || type;
    return '<span style="color:' + c + '; font-weight:600;">' + t + '</span>';
}

function dawEditEventInline(ch, trackIdx, viewIdx) {
    const ev = evEvents[viewIdx];
    if (!ev) return;
    const tbody = document.getElementById('ev-table-body');
    const row = tbody.rows[viewIdx];
    if (!row) return;
    const inputStyle = 'font-size:0.72rem; padding:1px 4px; background:#2a2a3c; color:#fff; border:1px solid #0dcaf0;';
    const mkInput = function(id, val, w, min, max, step) {
        return '<input type="number"' + (step ? ' step="' + step + '"' : '') + ' min="' + min + '" max="' + max + '" value="' + val + '" class="form-control form-control-sm" style="width:' + w + 'px; ' + inputStyle + '" id="' + id + '">';
    };
    // Beat — always editable
    row.cells[0].innerHTML = mkInput('ev-edit-beat-' + viewIdx, ev.beat, 60, 0, 9999, 0.01);
    // Note — editable for note_on, note_off
    if ((ev.type === 'note_on' || ev.type === 'note_off') && ev.note_num !== undefined) {
        row.cells[2].innerHTML = mkInput('ev-edit-note-' + viewIdx, ev.note_num, 45, 0, 127);
    }
    // Velocity — editable for note_on, note_off
    if (ev.vel !== undefined && (ev.type === 'note_on' || ev.type === 'note_off')) {
        row.cells[3].innerHTML = mkInput('ev-edit-vel-' + viewIdx, ev.vel, 45, 0, 127);
    }
    // CC number — editable for control_change
    if (ev.type === 'control_change' && ev.cc_num !== undefined) {
        row.cells[6].innerHTML = mkInput('ev-edit-ccnum-' + viewIdx, ev.cc_num, 45, 0, 127);
    }
    // CC value — editable for control_change
    if (ev.type === 'control_change' && ev.cc_val !== undefined) {
        row.cells[7].innerHTML = mkInput('ev-edit-ccval-' + viewIdx, ev.cc_val, 45, 0, 127);
    }
    // Pitch — editable for pitchwheel
    if (ev.type === 'pitchwheel' && ev.pitch !== undefined) {
        row.cells[7].innerHTML = mkInput('ev-edit-pitch-' + viewIdx, ev.pitch, 55, -8192, 8191);
    }
    // Program — editable for program_change
    if (ev.type === 'program_change' && ev.program !== undefined) {
        row.cells[7].innerHTML = mkInput('ev-edit-prog-' + viewIdx, ev.program, 45, 0, 127);
    }
    row.cells[8].innerHTML = '<button class="btn btn-outline-success btn-sm px-1 py-0" style="font-size:0.68rem;" onclick="dawSaveEventEdit(' + ch + ',' + trackIdx + ',' + viewIdx + ')"><i class="fas fa-check"></i></button> <button class="btn btn-outline-secondary btn-sm px-1 py-0" style="font-size:0.68rem;" onclick="dawLoadEvents()"><i class="fas fa-times"></i></button>';
}

async function dawSaveEventEdit(ch, trackIdx, viewIdx) {
    const beatInput = document.getElementById('ev-edit-beat-' + viewIdx);
    const velInput = document.getElementById('ev-edit-vel-' + viewIdx);
    const noteInput = document.getElementById('ev-edit-note-' + viewIdx);
    const ccnumInput = document.getElementById('ev-edit-ccnum-' + viewIdx);
    const ccvalInput = document.getElementById('ev-edit-ccval-' + viewIdx);
    const pitchInput = document.getElementById('ev-edit-pitch-' + viewIdx);
    const progInput = document.getElementById('ev-edit-prog-' + viewIdx);
    const updates = {};
    if (beatInput) updates.beat = parseFloat(beatInput.value);
    if (velInput && velInput.value !== '') updates.velocity = parseInt(velInput.value);
    if (noteInput && noteInput.value !== '') updates.note = parseInt(noteInput.value);
    if (ccnumInput && ccnumInput.value !== '') updates.cc_num = parseInt(ccnumInput.value);
    if (ccvalInput && ccvalInput.value !== '') updates.cc_value = parseInt(ccvalInput.value);
    if (pitchInput && pitchInput.value !== '') updates.pitch = parseInt(pitchInput.value);
    if (progInput && progInput.value !== '') updates.program = parseInt(progInput.value);
    try {
        const res = await fetch('/api/daw/track/' + ch + '/events/' + trackIdx, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(updates)
        });
        const data = await res.json();
        if (data.status === 'ok') {
            await dawLoadEvents();
            scheduleDensityRefresh();
        } else {
            alert(data.message || 'Edit failed');
        }
    } catch (e) {
        console.error('[EventViewer] Edit error:', e);
    }
}

async function dawDeleteEvent(ch, trackIdx) {
    if (!confirm('Delete this event?')) return;
    try {
        const res = await fetch('/api/daw/track/' + ch + '/events/' + trackIdx, {
            method: 'DELETE'
        });
        const data = await res.json();
        if (data.status === 'ok') {
            await dawLoadEvents();
            scheduleDensityRefresh();
        } else {
            alert(data.message || 'Delete failed');
        }
    } catch (e) {
        console.error('[EventViewer] Delete error:', e);
    }
}

// Open MIDI Manager Modal
function dawOpenMidiManager() {
    document.getElementById('midi-manager-modal').style.display = 'flex';
    dawRefreshMidiList();
}

// Close MIDI Manager Modal
function dawCloseMidiManager(event) {
    if (event && event.target !== event.currentTarget) return;
    document.getElementById('midi-manager-modal').style.display = 'none';
}

// Quick Save (from main button)
// Current session tracking
let currentMidiSessionName = null;

function dawSetSessionName(name) {
    currentMidiSessionName = name || null;
    // Update title bar session label
    const titleLabel = document.getElementById('daw-session-title');
    if (titleLabel) {
        titleLabel.textContent = currentMidiSessionName || 'Nuovo progetto';
        titleLabel.style.color = currentMidiSessionName ? '#00d4ff' : '#555';
        titleLabel.style.fontStyle = currentMidiSessionName ? 'normal' : 'italic';
    }
    const btn = document.getElementById('daw-save-btn');
    if (btn) {
        btn.title = currentMidiSessionName
            ? `Salva → "${currentMidiSessionName}"`
            : 'Salva (chiede il nome)';
    }
}

function dawNewProject() {
    if (!confirm('Start a new project? Unsaved changes will be lost.')) return;
    dawClearAll();
}

// ── Menu helpers ──────────────────────────────────────────────
function toggleMenu(menuId) {
    const allMenus = document.querySelectorAll('.daw-menu-item');
    allMenus.forEach(m => {
        if (m.id !== menuId) m.classList.remove('open');
    });
    document.getElementById(menuId).classList.toggle('open');
}

function closeMenus() {
    document.querySelectorAll('.daw-menu-item').forEach(m => m.classList.remove('open'));
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('.daw-menu-item')) closeMenus();
});

// ── Panel toggle ──────────────────────────────────────────────
function togglePanel(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    panel.classList.toggle('collapsed');
}

// Switch between MIX and SOUND tabs in Channel Controls panel
function switchMixerTab(tabId) {
    document.querySelectorAll('#panel-mixer-body .daw-panel-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabId);
    });
    document.querySelectorAll('#panel-mixer-body .daw-tab-pane').forEach(pane => {
        pane.classList.toggle('active', pane.id === tabId);
    });
}

// SAVE: direct if name known, ask if new project
function dawQuickSaveMidi() {
    if (currentMidiSessionName) {
        dawDoSave(currentMidiSessionName);
    } else {
        const name = prompt('Nome per la sessione MIDI:');
        if (!name || !name.trim()) return;
        dawDoSave(name.trim());
    }
}

// SAVE AS: always ask for a new name
function dawSaveAsMidi() {
    const suggestion = currentMidiSessionName || '';
    const name = prompt('Salva con nome:', suggestion);
    if (!name || !name.trim()) return;
    dawDoSave(name.trim());
}

// Save from modal
function dawSaveMidi() {
    const nameInput = document.getElementById('midi-save-name');
    const name = nameInput.value.trim();
    if (!name) { alert('Inserisci un nome per la sessione'); return; }
    dawDoSave(name);
    nameInput.value = '';
}

// Core save logic
function dawDoSave(name) {
    fetch('/api/daw/midi/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
    })
    .then(r => r.json())
    .then(d => {
        if (d.status !== 'ok') throw new Error(d.message);
        dawSetSessionName(name);
        dawRefreshMidiList();
    })
    .catch(err => alert('Save error: ' + err.message));
}

// Refresh file list in modal
function dawRefreshMidiList() {
    const panel = document.getElementById('midi-file-list');
    if (!panel) return;
    panel.innerHTML = '<div class="text-center text-muted small py-3"><i class="fas fa-spinner fa-spin"></i></div>';
    fetch('/api/daw/midi/list')
        .then(r => r.json())
        .then(files => {
            if (!files.length) {
                panel.innerHTML = '<div class="text-center text-muted small py-3"><i class="fas fa-inbox"></i> Nessun file MIDI salvato</div>';
                return;
            }
            let html = '';
            files.forEach(f => {
                const sizeKB = (f.size / 1024).toFixed(1);
                html += `<div class="d-flex align-items-center py-2 px-2" style="border-bottom:1px solid rgba(255,255,255,0.06); transition:background 0.2s;" onmouseover="this.style.background='rgba(0,212,255,0.05)'" onmouseout="this.style.background='transparent'">
                    <div style="flex:1; min-width:0;">
                        <div class="text-truncate" style="color:#00d4ff; font-size:0.9rem; font-weight:500;"><i class="fas fa-music me-1"></i>${f.name}</div>
                        <div style="font-size:0.72rem; color:#777;">${f.modified} &middot; ${sizeKB} KB</div>
                    </div>
                    <div class="d-flex gap-1 ms-2" style="flex-shrink:0;">
                        <button class="btn btn-outline-success btn-sm px-2 py-1" onclick="dawLoadMidiFile('${f.filename}')" title="Carica nelle tracce">
                            <i class="fas fa-play" style="font-size:0.75rem;"></i>
                        </button>
                        <button class="btn btn-outline-light btn-sm px-2 py-1" onclick="dawDownloadMidiFile('${f.filename}')" title="Scarica sul PC">
                            <i class="fas fa-download" style="font-size:0.75rem;"></i>
                        </button>
                        <button class="btn btn-outline-danger btn-sm px-2 py-1" onclick="dawDeleteMidiFile('${f.filename}','${f.name}')" title="Elimina">
                            <i class="fas fa-trash" style="font-size:0.75rem;"></i>
                        </button>
                    </div>
                </div>`;
            });
            panel.innerHTML = html;
        })
        .catch(() => { panel.innerHTML = '<div class="text-center text-danger small">Errore caricamento</div>'; });
}

// Load a saved MIDI file into DAW tracks
function dawLoadMidiFile(filename) {
    if (!confirm('Caricare questo file? Le tracce attuali verranno sostituite.')) return;
    fetch(`/api/daw/midi/load/${encodeURIComponent(filename)}`)
        .then(r => r.json())
        .then(d => {
            if (d.status !== 'ok') throw new Error(d.message);
            // Derive session name from filename (strip .mid extension)
            const sessionName = filename.replace(/\.midi?$/i, '');
            dawSetSessionName(sessionName);
            dawCloseMidiManager();
            dawLoadInitialState();
            if (typeof loadInitialState === 'function') loadInitialState();
        })
        .catch(err => alert('Load error: ' + err.message));
}

// Download MIDI file to PC
function dawDownloadMidiFile(filename) {
    const a = document.createElement('a');
    a.href = `/api/daw/midi/download/${encodeURIComponent(filename)}`;
    a.download = filename;
    a.click();
}

// Delete a saved MIDI file
function dawDeleteMidiFile(filename, name) {
    if (!confirm(`Eliminare "${name}"?`)) return;
    fetch(`/api/daw/midi/delete/${encodeURIComponent(filename)}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(d => {
            if (d.status !== 'ok') throw new Error(d.message);
            dawRefreshMidiList();
        })
        .catch(err => alert('Delete error: ' + err.message));
}

// Upload MIDI from PC
function dawUploadMidi(input) {
    const file = input.files[0];
    if (!file) return;
    input.value = '';
    const formData = new FormData();
    formData.append('file', file);
    fetch('/api/daw/midi/upload', { method: 'POST', body: formData })
        .then(r => r.json().then(d => ({ ok: r.ok, data: d })))
        .then(({ ok, data }) => {
            if (!ok) throw new Error(data.message || 'Upload failed');
            dawLoadInitialState();
            if (typeof loadInitialState === 'function') loadInitialState();
            dawRefreshMidiList();
        })
        .catch(err => alert('Upload error: ' + err.message));
}

// Initialize DAW tracks
function initDAW() {
    attachCanvasDragHandlers();
    
    // Inizializza WebSocket per aggiornamenti real-time
    dawInitWebSocket();
    
    // Carica lo stato iniziale
    dawLoadInitialState();
}

// Inizializza connessione WebSocket
function dawInitWebSocket() {
    // ui-shell: un solo io() per pagina, creato da api.js e condiviso con la shell (niente secondo socket).
    dawSocket = window.MatSynth.api.socket();
    
    dawSocket.on('connect', () => {
        console.log('[WebSocket] Connesso al server');
    });
    
    dawSocket.on('disconnect', () => {
        console.log('[WebSocket] Disconnesso dal server');
    });
    
    // Ascolta aggiornamenti di stato in real-time
    dawSocket.on('daw_state_update', (data) => {
        console.log('[WebSocket] State update ricevuto');
        const wasRecording = dawState && dawState.is_recording;
        const wasPlaying = dawState && dawState.is_playing;
        dawState = data;
        // Restore position after recording stops
        if (wasRecording && !dawState.is_recording && recordStartPosition !== null) {
            dawState.timeline_position = recordStartPosition;
            recordStartPosition = null;
            // Sync restored position to server
            dawSocket.emit('transport_cmd', {cmd: 'seek', position: dawState.timeline_position});
        }
        dawUpdateUI();
        // Auto-refresh event viewer when recording stops (get durations) or playback stops
        if (evViewerOpen && ((wasRecording && !dawState.is_recording) || (wasPlaying && !dawState.is_playing))) {
            evHighlightBeat = -1;
            dawBuildChannelFilter();
            dawLoadEvents();
        }
    });

    dawSocket.on('daw_tick', (tick) => {
        dawHandleTick(tick);
    });

    dawSocket.on('record_activity', (data) => {
        if (!data || !Array.isArray(data.channels)) return;
        data.channels.forEach(({ channel, channel_id, count }) => {
            const ch = (channel !== undefined ? channel : channel_id);
            if (ch === undefined) return;
            trackView.liveActivity[ch] = count || 0;
            renderTrackTape(ch);
        });
    });

    // Live event append during recording
    dawSocket.on('midi_event_new', (ev) => {
        if (!evViewerOpen) return;
        // Only append if channel matches filter (or filter is "All")
        if (evCurrentChannel >= 0 && ev.ch !== evCurrentChannel) return;
        evEvents.push(ev);
        // Append row incrementally instead of full re-render
        const tbody = document.getElementById('ev-table-body');
        if (evEvents.length === 1) {
            tbody.innerHTML = '';  // clear "No events" placeholder
        }
        const countEl = document.getElementById('ev-event-count');
        countEl.textContent = evEvents.length + ' events';
        const i = evEvents.length - 1;
        const tr = document.createElement('tr');
        tr.id = 'ev-row-' + i;
        tr.innerHTML =
            '<td>' + ev.beat.toFixed(2) + '</td>' +
            '<td>' + dawEvTypeBadge(ev.type) + '</td>' +
            '<td>' + (ev.note || '—') + '</td>' +
            '<td>' + (ev.vel !== undefined ? ev.vel : '—') + '</td>' +
            '<td>' + ev.ch + '</td>' +
            '<td>—</td>' +
            '<td>' + (ev.cc_num !== undefined ? ev.cc_num : '—') + '</td>' +
            '<td>' + (ev.cc_val !== undefined ? ev.cc_val : '—') + '</td>' +
            '<td></td>';
        tbody.appendChild(tr);
        // Auto-scroll to bottom
        const wrap = document.getElementById('ev-table-wrap');
        wrap.scrollTop = wrap.scrollHeight;
    });

    // Playback beat highlight
    dawSocket.on('midi_playback_beat', (data) => {
        if (!evViewerOpen || !evEvents.length) return;
        evHighlightBeat = data.beat;
        // Find closest event to current beat
        let closest = -1;
        let minDist = Infinity;
        for (let i = 0; i < evEvents.length; i++) {
            const d = Math.abs(evEvents[i].beat - data.beat);
            if (d < minDist) { minDist = d; closest = i; }
            if (evEvents[i].beat > data.beat) break;  // events sorted, no need to continue
        }
        // Remove previous highlight
        const tbody = document.getElementById('ev-table-body');
        const prev = tbody.querySelector('tr.ev-highlight');
        if (prev) { prev.classList.remove('ev-highlight'); prev.style.background = ''; }
        // Apply highlight + scroll
        if (closest >= 0 && minDist < 0.5) {
            const row = document.getElementById('ev-row-' + closest);
            if (row) {
                row.classList.add('ev-highlight');
                row.style.background = 'rgba(0,255,136,0.18)';
                row.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
        }
    });
}

// Carica lo stato iniziale (una sola volta)
async function dawLoadInitialState() {
    try {
        const res = await fetch('/api/daw/state');
        const data = await res.json();
        
        if (data.status === 'ok') {
            dawState = data.data;
            dawUpdateUI();
            await refreshDensityMap(true);
            console.log('DAW initialized with WebSocket real-time updates');
        }
    } catch (e) {
        console.error('DAW init error:', e);
    }
}

function scheduleDensityRefresh() {
    if (densityRefreshTimer) return;
    densityRefreshTimer = setTimeout(async () => {
        densityRefreshTimer = null;
        await refreshDensityMap(true);
    }, 150);
}

// Snap a beat value to the selected grid resolution
function snapToGrid(beat) {
    const sel = document.getElementById('daw-snap-grid');
    const grid = sel ? parseFloat(sel.value) : 0;
    if (!grid || grid <= 0) return beat;
    return Math.round(beat / grid) * grid;
}

// Seek to a specific time in seconds (works during playback via seek_beat)
function dawSeekTo(seconds) {
    if (!dawState || dawState.is_recording) return;
    seconds = Math.max(0, seconds);
    const beatDuration = dawState.bpm ? 60 / dawState.bpm : 0;
    if (beatDuration <= 0) return;
    let beat = seconds / beatDuration;
    beat = snapToGrid(beat);
    if (dawSocket && dawSocket.connected) {
        dawSocket.emit('transport_cmd', {cmd: 'seek_beat', beat: beat});
    } else {
        fetch('/api/daw/seek_beat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({beat: beat})
        });
    }
    dawState.timeline_position = beat * beatDuration;
    dawUpdateTimelineUI();
}

// Parse time input (MM:SS.d) and seek
function dawApplyTimeInput(input) {
    if (!dawState || dawState.is_playing || dawState.is_recording) return;
    const text = input.value.trim();
    const match = text.match(/^(\d+):(\d{1,2})(?:\.(\d))?$/);
    if (match) {
        const mins = parseInt(match[1]);
        const secs = parseInt(match[2]);
        const deci = match[3] ? parseInt(match[3]) : 0;
        dawSeekTo(mins * 60 + secs + deci * 0.1);
    }
    // If format is invalid, just restore the current display
    dawUpdateTimelineUI();
}

function dawUpdateTimelineUI() {
    if (!dawState) return;
    const timelineSpan = document.getElementById('daw-timeline');
    const barBadge = document.getElementById('daw-bar-counter');
    const beatsPerMeasure = dawState.beats_per_measure || 4;
    const beatDuration = dawState.bpm ? 60 / dawState.bpm : 0;
    const timeSeconds = dawState.timeline_position || 0;

    if (timelineSpan && !timelineSpan.dataset.editing) {
        const minutes = Math.floor(timeSeconds / 60);
        const seconds = Math.floor(timeSeconds % 60);
        const deciseconds = Math.floor((timeSeconds % 1) * 10);
        timelineSpan.value = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${deciseconds}`;
        if (dawState.is_recording) {
            timelineSpan.style.background = '#dc3545';
            timelineSpan.style.color = '#fff';
        } else if (dawState.is_playing) {
            timelineSpan.style.background = '#0dcaf0';
            timelineSpan.style.color = '#0a0a12';
        } else {
            timelineSpan.style.background = 'rgba(0,0,0,0.5)';
            timelineSpan.style.color = '#00d4ff';
        }
    }

    if (barBadge && beatDuration > 0) {
        const totalBeats = timeSeconds / beatDuration;
        const currentBar = Math.max(1, Math.floor(totalBeats / beatsPerMeasure) + 1);
        barBadge.textContent = `Bar ${currentBar}`;
    }

    ensureRenderLoop();
    renderAllTracks();
}

function dawHandleTick(tick) {
    if (!tick || !dawState) return;

    const prevRecording = !!dawState.is_recording;
    const prevPlaying = !!dawState.is_playing;

    if (typeof tick.t === 'number') {
        dawState.timeline_position = tick.t;
    } else if (typeof tick.timeline_position === 'number') {
        dawState.timeline_position = tick.timeline_position;
    }

    if (typeof tick.is_recording === 'boolean') {
        dawState.is_recording = tick.is_recording;
    }
    if (typeof tick.is_playing === 'boolean') {
        dawState.is_playing = tick.is_playing;
    }
    if (typeof tick.bpm === 'number') {
        dawState.bpm = tick.bpm;
    }
    if (typeof tick.beats_per_measure === 'number') {
        dawState.beats_per_measure = tick.beats_per_measure;
    }

    let countsChanged = false;
    if (tick.track_counts) {
        dawState.track_counts = tick.track_counts;
        trackView.lastTrackCounts = { ...tick.track_counts };
        countsChanged = true;

        if (tick.track_durations) {
            dawState.track_durations = tick.track_durations;
        }
        if (typeof tick.max_duration === 'number') {
            dawState.max_duration = tick.max_duration;
        }

        scheduleDensityRefresh();
    }

    const stateChanged = countsChanged || prevRecording !== dawState.is_recording || prevPlaying !== dawState.is_playing;
    if (stateChanged) {
        dawUpdateUI();
    } else {
        dawUpdateTimelineUI();
    }
}

// Update UI based on state
function dawUpdateUI() {
    if (!dawState) return;
    
    // Update transport buttons
    const playBtn = document.getElementById('daw-play');
    const recBtn = document.getElementById('daw-rec');
    const stopBtn = document.getElementById('daw-stop');
    const tsSelect = document.getElementById('daw-ts-select');
    const tsBadge = document.getElementById('daw-time-signature');
    const barBadge = document.getElementById('daw-bar-counter');
    
    if (dawState.is_playing) {
        playBtn.classList.add('playing');
        playBtn.innerHTML = '<i class="fas fa-pause"></i>';
        playBtn.title = 'Pause';
    } else {
        playBtn.classList.remove('playing');
        playBtn.innerHTML = '<i class="fas fa-play"></i>';
        playBtn.title = 'Play';
    }
    
    // Update REC button
    if (dawState.is_recording) {
        recBtn.classList.add('recording');
        recBtn.innerHTML = '<i class="fas fa-circle"></i>';
        recBtn.title = 'Recording';
    } else {
        recBtn.classList.remove('recording');
        recBtn.innerHTML = '<i class="fas fa-circle"></i>';
        recBtn.title = 'Record';
    }
    
    // Update BPM
    document.getElementById('daw-bpm').value = dawState.bpm;
    const beatsPerMeasure = dawState.beats_per_measure || 4;
    trackView.meta.beatsPerMeasure = beatsPerMeasure;
    if (tsSelect && tsSelect.value !== String(beatsPerMeasure)) {
        tsSelect.value = String(beatsPerMeasure);
    }
    if (tsBadge) {
        tsBadge.textContent = `${beatsPerMeasure}/4`;
    }
    if (barBadge) {
        const beatDuration = dawState.bpm ? 60 / dawState.bpm : 0;
        const totalBeats = beatDuration > 0 ? (dawState.timeline_position / beatDuration) : 0;
        const currentBar = Math.max(1, Math.floor(totalBeats / beatsPerMeasure) + 1);
        barBadge.textContent = `Bar ${currentBar}`;
    }
    
    // Update metronome button
    const metronomeBtn = document.getElementById('daw-metronome');
    if (dawState.metronome_enabled) {
        metronomeBtn.classList.add('daw-tbtn-active');
    } else {
        metronomeBtn.classList.remove('daw-tbtn-active');
    }
    
    // Update loop button
    const loopBtn = document.getElementById('daw-loop');
    if (loopBtn) {
        if (dawState.loop_enabled) {
            loopBtn.classList.add('daw-tbtn-active');
        } else {
            loopBtn.classList.remove('daw-tbtn-active');
        }
    }

    // Update undo/redo buttons
    const undoBtn = document.getElementById('daw-undo');
    const redoBtn = document.getElementById('daw-redo');
    if (undoBtn) undoBtn.disabled = !dawState.can_undo;
    if (redoBtn) redoBtn.disabled = !dawState.can_redo;
    
    // Update Timeline display (mm:ss.d format)
    const timelineSpan = document.getElementById('daw-timeline');
    const timeSeconds = dawState.timeline_position || 0;
    if (!timelineSpan.dataset.editing) {
        const minutes = Math.floor(timeSeconds / 60);
        const seconds = Math.floor(timeSeconds % 60);
        const deciseconds = Math.floor((timeSeconds % 1) * 10);
        timelineSpan.value = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${deciseconds}`;
    }
    
    // Change color based on state
    if (dawState.is_recording) {
        timelineSpan.style.background = '#dc3545';
        timelineSpan.style.color = '#fff';
    } else if (dawState.is_playing) {
        timelineSpan.style.background = '#0dcaf0';
        timelineSpan.style.color = '#0a0a12';
    } else {
        timelineSpan.style.background = 'rgba(0,0,0,0.5)';
        timelineSpan.style.color = '#00d4ff';
    }

    if (!dawState.is_recording) {
        trackView.liveActivity = {};
    }
    
    // Calculate total events
    let totalEvents = 0;
    
    // Update tracks
    for (let i = 0; i < 16; i++) {
        const row = document.getElementById(`daw-track-${i}`);
        const armBtn = document.getElementById(`arm-btn-${i}`);
        const muteBtn = document.getElementById(`mute-btn-${i}`);
        const soloBtn = document.getElementById(`solo-btn-${i}`);
        const eventBadge = document.getElementById(`events-${i}`);
        const progressBar = document.getElementById(`progress-${i}`);
        const eventCount = dawState.track_counts[i] || 0;
        
        totalEvents += eventCount;
        
        // ARM button - Rimuovi TUTTE le classi prima di aggiungere quella corretta
        armBtn.classList.remove('btn-outline-danger', 'btn-danger');
        if (dawState.armed[i]) {
            armBtn.classList.add('btn-danger');
            armBtn.style.fontWeight = '700';
        } else {
            armBtn.classList.add('btn-outline-danger');
            armBtn.style.fontWeight = '400';
        }
        
        // MUTE button - Rimuovi TUTTE le classi prima di aggiungere quella corretta
        muteBtn.classList.remove('btn-outline-warning', 'btn-warning');
        if (dawState.muted[i]) {
            muteBtn.classList.add('btn-warning');
        } else {
            muteBtn.classList.add('btn-outline-warning');
        }
        
        // SOLO button
        if (soloBtn) {
            soloBtn.classList.remove('btn-outline-info', 'btn-info');
            if (dawState.solo && dawState.solo[i]) {
                soloBtn.classList.add('btn-info');
                soloBtn.style.fontWeight = '700';
            } else {
                soloBtn.classList.add('btn-outline-info');
                soloBtn.style.fontWeight = '400';
            }
        }
        
        // Event count
        eventBadge.innerText = eventCount;
        if (eventCount > 0) {
            eventBadge.className = 'badge bg-success';
            eventBadge.style.fontSize = '0.65rem';
        } else {
            eventBadge.className = 'badge bg-secondary';
            eventBadge.style.fontSize = '0.65rem';
        }
        
        // Progress bar (shows temporal progress/duration)
        const trackDuration = dawState.track_durations ? dawState.track_durations[i] : 0;
        const maxDuration = dawState.max_duration || 1;
        
        if (progressBar) {
            if (dawState.is_recording && dawState.armed[i]) {
                const displayMax = Math.max(maxDuration, dawState.timeline_position || 1);
                const progress = (dawState.timeline_position / displayMax) * 100;
                progressBar.style.width = Math.min(100, progress) + '%';
                progressBar.className = 'progress-bar bg-danger progress-bar-striped progress-bar-animated';
            } else if (dawState.is_playing && trackDuration > 0 && !dawState.muted[i]) {
                const progress = maxDuration > 0 ? (dawState.timeline_position / maxDuration) * 100 : 0;
                progressBar.style.width = Math.min(100, progress) + '%';
                progressBar.className = 'progress-bar bg-warning progress-bar-striped progress-bar-animated';
            } else if (trackDuration > 0) {
                const relativeWidth = maxDuration > 0 ? (trackDuration / maxDuration) * 100 : 100;
                progressBar.style.width = relativeWidth + '%';
                progressBar.className = dawState.muted[i] ? 'progress-bar bg-secondary' : 'progress-bar bg-success';
            } else {
                progressBar.style.width = '0%';
                progressBar.className = 'progress-bar bg-info';
            }
        }
        
        // Highlight recording track (priority) or active channel
        if (dawState.is_recording && dawState.armed[i]) {
            row.style.background = 'rgba(244, 67, 54, 0.15)';
        } else if (i === activeControlChannel) {
            row.style.background = 'rgba(255, 193, 7, 0.06)';
        } else {
            row.style.background = '';
        }
        // Active channel border
        row.style.borderLeft = i === activeControlChannel ? '3px solid #ffc107' : '';

        // Track color on CH cell
        const chCell = row.querySelector('.ch-cell');
        if (chCell) {
            chCell.style.color = getTrackColor(i);
        }
    }
    
    // Update total events badge
    const totalBadge = document.getElementById('daw-events-total');
    if (totalBadge) {
        totalBadge.innerText = totalEvents;
    }
    
    // Update status
    const statusDiv = document.getElementById('daw-status');
    const statusText = document.getElementById('daw-status-text');
    const statusPill = document.getElementById('daw-status-pill');
    
    if (dawState.is_recording) {
        statusDiv.style.display = 'block';
        statusDiv.className = 'daw-status-bar daw-status-recording text-center';
        const armedCh = Object.keys(dawState.armed).filter(k => dawState.armed[k]).map(k => parseInt(k) + 1);
        statusText.innerHTML = `<i class="fas fa-circle fa-pulse"></i> RECORDING Channel ${armedCh.join(', ')}`;
        if (statusPill) { statusPill.style.display = 'block'; statusPill.className = 'daw-status-pill daw-pill-rec'; statusPill.innerHTML = '<i class="fas fa-circle fa-pulse"></i> REC'; }
    } else if (dawState.is_playing) {
        statusDiv.style.display = 'block';
        statusDiv.className = 'daw-status-bar daw-status-playing text-center';
        statusText.innerHTML = '<i class="fas fa-play"></i> PLAYING...';
        if (statusPill) { statusPill.style.display = 'block'; statusPill.className = 'daw-status-pill daw-pill-play'; statusPill.innerHTML = '<i class="fas fa-play"></i> PLAY'; }
    } else {
        statusDiv.style.display = 'none';
        if (statusPill) { statusPill.style.display = 'none'; }
    }
    // Aggiorna range start/zoom limits e sincronizza slider
    const zoomSlider = document.getElementById('daw-zoom-range');
    const canvasWidth = getCanvasWidth();
    const visibleBeats = trackView.zoomLevel > 0 ? canvasWidth / trackView.zoomLevel : 0;
    const totalBeats = getTotalBeats();
    const maxStart = Math.max(0, totalBeats - visibleBeats);
    if (trackView.startTime > maxStart) {
        trackView.startTime = maxStart;
    }
    if (zoomSlider) {
        zoomSlider.value = trackView.zoomLevel;
    }

    // Aggiorna cache counts e refresh heatmap se servono
    refreshDensityMapIfNeeded(false);
    ensureRenderLoop();
}

// Rendering helper per le tracce (canvas)
function getCanvasWidth() {
    const canvasSample = document.querySelector('canvas[id^="track-canvas-"]');
    return canvasSample ? canvasSample.clientWidth : 400;
}

function attachCanvasDragHandlers() {
    const canvases = document.querySelectorAll('canvas[id^="track-canvas-"]');
    canvases.forEach(canvas => {
        canvas.style.touchAction = 'none';
        canvas.addEventListener('pointerdown', onCanvasPointerDown);
        canvas.addEventListener('pointermove', onCanvasPointerMove);
        canvas.addEventListener('pointerup', onCanvasPointerUp);
        canvas.addEventListener('pointercancel', onCanvasPointerUp);
        canvas.addEventListener('pointerleave', onCanvasPointerUp);
    });
}

function onCanvasPointerDown(e) {
    const ch = getChannelFromCanvas(e.target);
    const isEditable = dawState && !dawState.is_playing && !dawState.is_recording;

    // Double-tap detection → open Piano Roll
    const now = performance.now();
    const dtDelta = now - pianoRoll.lastTapTime;
    if (ch >= 0 && dtDelta < PR_DOUBLETAP_MS && pianoRoll.lastTapCh === ch) {
        console.log('[PianoRoll] Double-tap detected! ch=', ch, 'delta=', dtDelta.toFixed(0), 'ms');
        pianoRoll.lastTapTime = 0;
        openPianoRollForChannel(ch);
        return;
    }
    pianoRoll.lastTapTime = now;
    pianoRoll.lastTapCh = ch;

    if (trackView.drag.momentumId) {
        cancelAnimationFrame(trackView.drag.momentumId);
        trackView.drag.momentumId = null;
    }

    // Clip interaction (when transport is stopped) — note editing disabled on tracks, use Piano Roll
    if (isEditable && ch >= 0 && !selectMode) {
        const rect = e.target.getBoundingClientRect();
        const localX = e.clientX - rect.left;

        // Clip right-edge resize: detect click near right border of a clip
        const clipEdgeHit = hitTestClipEdge(ch, e.target, localX);
        if (clipEdgeHit) {
            clipResize = {
                ch,
                clipId: clipEdgeHit.id,
                startBeat: clipEdgeHit.start_beat,
                origEndBeat: clipEdgeHit.end_beat,
                ghostEndBeat: clipEdgeHit.end_beat,
                changed: false
            };
            e.target.setPointerCapture(e.pointerId);
            ensureRenderLoop();
            return;
        }

        // Clip drag: click on a clip block to start dragging
        const clipHit = hitTestClip(ch, e.target, localX);
        if (clipHit) {
            const clickBeat = trackView.startTime + (localX / trackView.zoomLevel);
            clipDrag = {
                ch,
                clipId: clipHit.id,
                startBeat: clipHit.start_beat,
                endBeat: clipHit.end_beat,
                offsetBeat: clickBeat - clipHit.start_beat,
                ghostBeat: clipHit.start_beat,
                changed: false
            };
            e.target.setPointerCapture(e.pointerId);
            ensureRenderLoop();
            return;
        }
    }

    trackView.drag.active = true;
    trackView.drag.lastX = e.clientX;
    trackView.drag.startX = e.clientX;
    trackView.drag.canvasRect = e.target.getBoundingClientRect();
    trackView.drag.didDrag = false;
    trackView.drag.lastT = performance.now();
    trackView.drag.velocity = 0;
    e.target.setPointerCapture(e.pointerId);

    // In select mode, start selection from click position
    if (selectMode && dawState && !dawState.is_playing && !dawState.is_recording) {
        const rect = trackView.drag.canvasRect;
        const clickX = e.clientX - rect.left;
        const beat = trackView.startTime + (clickX / trackView.zoomLevel);
        trackView.drag.selStartBeat = snapToGrid(beat);
        selectionInBeat = trackView.drag.selStartBeat;
        selectionOutBeat = null;
        renderAllTracks();
    }
}

function onCanvasPointerMove(e) {
    // Clip drag in progress
    if (clipDrag) {
        const rect = e.target.getBoundingClientRect();
        const localX = e.clientX - rect.left;
        const curBeat = trackView.startTime + (localX / trackView.zoomLevel);
        clipDrag.ghostBeat = Math.max(0, curBeat - clipDrag.offsetBeat);
        if (Math.abs(clipDrag.ghostBeat - clipDrag.startBeat) > 0.05) clipDrag.changed = true;
        ensureRenderLoop();
        return;
    }

    // Clip resize in progress
    if (clipResize) {
        const rect = e.target.getBoundingClientRect();
        const localX = e.clientX - rect.left;
        const curBeat = trackView.startTime + (localX / trackView.zoomLevel);
        clipResize.ghostEndBeat = Math.max(clipResize.startBeat + 0.125, snapToGrid(curBeat));
        if (Math.abs(clipResize.ghostEndBeat - clipResize.origEndBeat) > 0.05) clipResize.changed = true;
        ensureRenderLoop();
        return;
    }

    if (!trackView.drag.active) return;
    const now = performance.now();
    const dx = e.clientX - trackView.drag.lastX;
    const dt = Math.max(1, now - trackView.drag.lastT);
    if (Math.abs(e.clientX - trackView.drag.startX) > 5) trackView.drag.didDrag = true;

    // Select mode: update selection range
    if (selectMode && trackView.drag.selStartBeat != null && dawState && !dawState.is_playing && !dawState.is_recording) {
        const rect = trackView.drag.canvasRect;
        const curX = e.clientX - rect.left;
        const curBeat = snapToGrid(trackView.startTime + (curX / trackView.zoomLevel));
        const anchor = trackView.drag.selStartBeat;
        selectionInBeat = Math.min(anchor, curBeat);
        selectionOutBeat = Math.max(anchor, curBeat);
        ensureRenderLoop();
        // Don't scroll in select mode
        trackView.drag.lastX = e.clientX;
        trackView.drag.lastT = now;
        return;
    }

    trackView.drag.lastX = e.clientX;
    trackView.drag.lastT = now;

    const deltaBeats = dx / trackView.zoomLevel;
    trackView.startTime = Math.max(0, trackView.startTime - deltaBeats);
    clampStartTimeToTape();

    trackView.drag.velocity = dx / dt; // px per ms
    ensureRenderLoop();
}

function onCanvasPointerUp(e) {
    // Clip drag commit
    if (clipDrag) {
        try { e.target.releasePointerCapture(e.pointerId); } catch (_) {}
        commitClipDrag().catch(err => console.error('Clip drag error:', err));
        return;
    }

    // Clip resize commit
    if (clipResize) {
        try { e.target.releasePointerCapture(e.pointerId); } catch (_) {}
        commitClipResize().catch(err => console.error('Clip resize error:', err));
        return;
    }

    if (!trackView.drag.active) return;
    trackView.drag.active = false;
    e.target.releasePointerCapture(e.pointerId);

    // Select mode: finalize selection
    if (selectMode && trackView.drag.selStartBeat != null) {
        trackView.drag.selStartBeat = null;
        if (trackView.drag.didDrag && selectionInBeat !== null && selectionOutBeat !== null && selectionOutBeat > selectionInBeat) {
            updateSelectionUI();
        } else {
            // Click without drag in select mode = clear selection
            dawCancelSelection();
        }
        return;
    }

    // Click-to-seek: if no drag movement and not recording
    if (!trackView.drag.didDrag && dawState && !dawState.is_recording) {
        const rect = trackView.drag.canvasRect;
        const clickX = e.clientX - rect.left;
        const beatDuration = dawState.bpm ? 60 / dawState.bpm : 0;
        if (beatDuration > 0) {
            const clickBeat = trackView.startTime + (clickX / trackView.zoomLevel);
            dawSeekTo(clickBeat * beatDuration);
        }
        return;
    }
    startMomentum();
}

function startMomentum() {
    const friction = 0.92;
    const minVel = 0.02; // px/ms
    let v = trackView.drag.velocity;
    let lastT = performance.now();

    const step = () => {
        const now = performance.now();
        const dt = Math.max(1, now - lastT);
        lastT = now;

        const dx = v * dt;
        const deltaBeats = dx / trackView.zoomLevel;
        trackView.startTime = Math.max(0, trackView.startTime - deltaBeats);
        clampStartTimeToTape();

        v *= friction;
        if (Math.abs(v) < minVel) {
            trackView.drag.momentumId = null;
            return;
        }
        trackView.drag.momentumId = requestAnimationFrame(step);
    };

    if (Math.abs(v) >= minVel) {
        trackView.drag.momentumId = requestAnimationFrame(step);
    }
    ensureRenderLoop();
}

function getTotalBeats() {
    const beatsPerMeasure = dawState
        ? (dawState.beats_per_measure || trackView.meta.beatsPerMeasure || 4)
        : (trackView.meta.beatsPerMeasure || trackView.lastBeatsPerMeasure || 4);
    trackView.lastBeatsPerMeasure = beatsPerMeasure;
    const totalBeats = (trackView.meta.totalBeats && trackView.meta.totalBeats > 0)
        ? trackView.meta.totalBeats
        : trackView.totalBars * beatsPerMeasure;
    return totalBeats;
}

function clampStartTimeToTape() {
    const canvasWidth = getCanvasWidth();
    const visibleBeats = trackView.zoomLevel > 0 ? canvasWidth / trackView.zoomLevel : 0;
    const maxStart = Math.max(0, getTotalBeats() - visibleBeats);
    trackView.startTime = Math.min(Math.max(0, trackView.startTime), maxStart);
}

function ensureTape(ch, height) {
    const beatsPerMeasure = dawState ? (dawState.beats_per_measure || trackView.meta.beatsPerMeasure || 4) : (trackView.meta.beatsPerMeasure || 4);
    if (trackView.lastBeatsPerMeasure !== beatsPerMeasure) {
        trackView.tapes = {};
        trackView.noteMap = {};
        trackView.liveActivity = {};
        trackView.lastTrackCounts = {};
        trackView.lastBeatsPerMeasure = beatsPerMeasure;
    }
    const totalBeats = getTotalBeats();
    const width = Math.max(1, Math.ceil(totalBeats * trackView.tapeScale));
    let tape = trackView.tapes[ch];
    const needsNew = !tape || tape.width !== width || tape.height !== height;
    if (needsNew) {
        if (typeof OffscreenCanvas !== 'undefined') {
            tape = new OffscreenCanvas(width, height);
        } else {
            tape = document.createElement('canvas');
            tape.width = width;
            tape.height = height;
        }
        trackView.tapes[ch] = tape;
    }
    return tape;
}

function renderTrackTape(ch) {
    const canvas = document.getElementById(`track-canvas-${ch}`);
    if (!canvas) return;
    const height = canvas.height;
    const totalBeats = getTotalBeats();
    const beatsPerMeasure = dawState ? (dawState.beats_per_measure || 4) : 4;
    const tape = ensureTape(ch, height);
    const ctx = tape.getContext('2d');

    ctx.clearRect(0, 0, tape.width, tape.height);
    ctx.fillStyle = 'rgba(25, 25, 35, 0.9)';
    ctx.fillRect(0, 0, tape.width, tape.height);

    const trackData = trackView.noteMap[ch];
    const muted = dawState && dawState.muted && dawState.muted[ch];

    if (trackData && trackData.notes && trackData.notes.length) {
        const notes = trackData.notes;
        const minNote = trackData.min_note;
        const maxNote = trackData.max_note;
        const tColor = getTrackColor(ch);
        const noteRange = Math.max(1, maxNote - minNote);
        const padding = 2;
        const drawHeight = height - padding * 2;
        const noteH = Math.max(1, Math.min(drawHeight / (noteRange + 1), height * 0.18));
        const NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];

        for (const n of notes) {
            const [note, vel, startBeat, endBeat] = n;
            const x = startBeat * trackView.tapeScale;
            const w = Math.max(1, (endBeat - startBeat) * trackView.tapeScale);
            const yRatio = noteRange > 0 ? (maxNote - note) / noteRange : 0.5;
            const y = padding + yRatio * (drawHeight - noteH);
            const velRatio = vel / 127;
            const alpha = (0.45 + 0.55 * velRatio) * (muted ? 0.35 : 1);

            ctx.globalAlpha = alpha;
            ctx.fillStyle = tColor;
            const r = Math.min(2, w / 2, noteH / 2);
            if (r > 0.5 && w > 2 && noteH > 2) {
                ctx.beginPath();
                ctx.moveTo(x + r, y);
                ctx.arcTo(x + w, y, x + w, y + noteH, r);
                ctx.arcTo(x + w, y + noteH, x, y + noteH, r);
                ctx.arcTo(x, y + noteH, x, y, r);
                ctx.arcTo(x, y, x + w, y, r);
                ctx.closePath();
                ctx.fill();
            } else {
                ctx.fillRect(x, y, w, noteH);
            }

            // Top highlight line
            if (w > 3 && noteH > 3) {
                ctx.globalAlpha = alpha * 0.5;
                ctx.strokeStyle = 'rgba(255,255,255,0.35)';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(x + 1, y + 0.5);
                ctx.lineTo(x + w - 1, y + 0.5);
                ctx.stroke();
            }

            // MIDI note name when zoomed in
            if (w > 20 && noteH > 8) {
                const noteName = NOTE_NAMES[note % 12] + (Math.floor(note / 12) - 1);
                ctx.globalAlpha = alpha * 0.8;
                ctx.fillStyle = '#fff';
                ctx.font = `${Math.min(noteH - 2, 9)}px sans-serif`;
                ctx.fillText(noteName, x + 2, y + noteH - 2);
            }
        }
        ctx.globalAlpha = 1;
    }

    const live = trackView.liveActivity[ch] || 0;
    if (live > 0) {
        ctx.fillStyle = 'rgba(255, 99, 71, 0.7)';
        ctx.fillRect(0, 0, tape.width, 3);
    }
}

function dawViewSetStart(val) {
    trackView.startTime = Math.max(0, parseFloat(val) || 0);
    clampStartTimeToTape();
    ensureRenderLoop();
}

function dawViewSetZoom(val) {
    trackView.zoomLevel = Math.max(1, parseFloat(val) || trackView.zoomLevel);
    clampStartTimeToTape();
    ensureRenderLoop();
}

function ensureRenderLoop() {
    if (trackView.rafId === null) {
        const loop = () => {
            // ui-shell: con Studio nascosto (vista Suona) non si disegna e il loop si ferma, per non
            // consumare CPU sul Pi; shell.showView() emette 'resize' al rientro e ensureRenderLoop() riparte.
            const studioView = document.getElementById('view-studio');
            if (studioView && studioView.hidden) { trackView.rafId = null; return; }
            renderAllTracks();
            trackView.rafId = requestAnimationFrame(loop);
        };
        trackView.rafId = requestAnimationFrame(loop);
    }
}

function stopRenderLoop() {
    if (trackView.rafId !== null) {
        cancelAnimationFrame(trackView.rafId);
        trackView.rafId = null;
    }
}

async function refreshDensityMap(force = false) {
    if (!dawState) return;
    try {
        const res = await fetch('/api/daw/full_note_map');
        const data = await res.json();
        if (data.status === 'ok' && data.map) {
            const map = data.map;
            trackView.meta = {
                totalBeats: map.total_beats || 0,
                beatsPerMeasure: map.beats_per_measure || (dawState ? dawState.beats_per_measure : 4)
            };
            trackView.noteMap = map.tracks || {};
            trackView.totalBars = Math.max(4, Math.ceil((trackView.meta.totalBeats || (trackView.meta.beatsPerMeasure * 4)) / (trackView.meta.beatsPerMeasure || 4)));
            trackView.lastBeatsPerMeasure = trackView.meta.beatsPerMeasure || trackView.lastBeatsPerMeasure;
            trackView.tapes = {};
            trackView.liveActivity = {};
            trackView.lastTrackCounts = { ...(dawState.track_counts || {}) };

            // Drop stale note selections after remap
            const validIds = new Set();
            for (const [chStr, td] of Object.entries(trackView.noteMap)) {
                const ch = parseInt(chStr, 10);
                if (!td || !td.notes) continue;
                for (const n of td.notes) {
                    validIds.add(noteEntryId(ch, n));
                }
            }
            noteEditor.selected = new Set([...noteEditor.selected].filter(id => validIds.has(id)));

            for (let ch = 0; ch < 16; ch++) {
                renderTrackTape(ch);
            }
            clampStartTimeToTape();
            renderAllTracks();
        }
    } catch (e) {
        console.error('Note map fetch error:', e);
    }
}

function refreshDensityMapIfNeeded(force = false) {
    if (!dawState) return;
    const counts = dawState.track_counts || {};
    let changed = force;
    if (!changed) {
        for (let ch = 0; ch < 16; ch++) {
            const prev = trackView.lastTrackCounts[ch] || 0;
            const current = counts[ch] || 0;
            if (current !== prev) {
                changed = true;
                break;
            }
        }
    }

    if (!changed) return;
    return refreshDensityMap(true);
}

function renderAllTracks() {
    if (!dawState) return;
    const beatDuration = dawState.bpm ? 60 / dawState.bpm : 0;
    const width = getCanvasWidth();
    const visibleBeats = trackView.zoomLevel > 0 ? width / trackView.zoomLevel : 0;
    const totalBeats = getTotalBeats();
    const currentBeat = beatDuration > 0 ? (dawState.timeline_position / beatDuration) : 0;
    const halfWindowBeats = visibleBeats * 0.5;
    
    // During recording, extend maxStart to allow infinite scrolling
    const effectiveTotalBeats = dawState.is_recording 
        ? Math.max(totalBeats, currentBeat + halfWindowBeats)
        : totalBeats;
    const maxStart = Math.max(0, effectiveTotalBeats - visibleBeats);

    const shouldFollow = (dawState.is_playing || dawState.is_recording) && beatDuration > 0 && !trackView.drag.active;

    let playheadX = 0;
    let tapeStartTime = 0;

    if (shouldFollow) {
        if (currentBeat < halfWindowBeats) {
            // Fase 1: inizio traccia. Nastro fermo, playhead si muove.
            tapeStartTime = 0;
            playheadX = currentBeat * trackView.zoomLevel;
        } else if (!dawState.is_recording && currentBeat > totalBeats - halfWindowBeats) {
            // Fase 3: fine traccia (solo playback). Nastro al massimo, playhead si muove verso destra.
            tapeStartTime = maxStart;
            playheadX = (currentBeat - tapeStartTime) * trackView.zoomLevel;
        } else {
            // Fase 2: playhead al centro, nastro che scorre.
            tapeStartTime = currentBeat - halfWindowBeats;
            playheadX = width * 0.5;
        }
        tapeStartTime = Math.min(Math.max(0, tapeStartTime), maxStart);
        trackView.startTime = tapeStartTime;
    } else {
        // Modalità libera (utente sta scrollando o DAW in stop)
        clampStartTimeToTape();
        tapeStartTime = trackView.startTime;
        playheadX = (currentBeat - tapeStartTime) * trackView.zoomLevel;
    }

    // ── Ruler canvas rendering ──
    const beatsPerMeasure = dawState ? (dawState.beats_per_measure || 4) : 4;
    const rulerCanvas = document.getElementById('daw-ruler');
    if (rulerCanvas) {
        const rw = rulerCanvas.clientWidth || rulerCanvas.width;
        if (rulerCanvas.width !== rw) rulerCanvas.width = rw;
        const rh = rulerCanvas.height;
        const rctx = rulerCanvas.getContext('2d');
        rctx.clearRect(0, 0, rw, rh);
        rctx.fillStyle = 'rgba(15,15,25,0.95)';
        rctx.fillRect(0, 0, rw, rh);

        const firstBar = Math.floor(tapeStartTime / beatsPerMeasure);
        const lastBar = Math.ceil((tapeStartTime + visibleBeats) / beatsPerMeasure);
        for (let bar = firstBar; bar <= lastBar; bar++) {
            const barStartBeat = bar * beatsPerMeasure;
            const px = (barStartBeat - tapeStartTime) * trackView.zoomLevel;
            if (px < -50 || px > rw + 50) continue;
            rctx.fillStyle = 'rgba(255,255,255,0.6)';
            rctx.font = '10px sans-serif';
            rctx.fillText(String(bar + 1), px + 3, 11);
            rctx.strokeStyle = 'rgba(255,255,255,0.4)';
            rctx.lineWidth = 1;
            rctx.beginPath();
            rctx.moveTo(px, 14);
            rctx.lineTo(px, rh);
            rctx.stroke();
            for (let bt = 1; bt < beatsPerMeasure; bt++) {
                const bpx = px + bt * trackView.zoomLevel;
                if (bpx < 0 || bpx > rw) continue;
                rctx.strokeStyle = 'rgba(255,255,255,0.2)';
                rctx.lineWidth = 0.5;
                rctx.beginPath();
                rctx.moveTo(bpx, rh - 6);
                rctx.lineTo(bpx, rh);
                rctx.stroke();
            }
        }
        if (beatDuration > 0 && playheadX >= 0 && playheadX <= rw) {
            rctx.fillStyle = (dawState.is_playing || dawState.is_recording) ? 'rgba(255,80,80,0.9)' : 'rgba(0,200,255,0.8)';
            rctx.beginPath();
            rctx.moveTo(playheadX - 4, 0);
            rctx.lineTo(playheadX + 4, 0);
            rctx.lineTo(playheadX, 8);
            rctx.closePath();
            rctx.fill();
        }
    }

    for (let ch = 0; ch < 16; ch++) {
        const canvas = document.getElementById(`track-canvas-${ch}`);
        if (!canvas) continue;
        const h = canvas.height;
        const w = canvas.clientWidth || canvas.width;
        if (canvas.width !== w) canvas.width = w;

        if (!trackView.tapes[ch]) {
            renderTrackTape(ch);
        }
        const tape = trackView.tapes[ch];
        const vctx = canvas.getContext('2d');
        vctx.clearRect(0, 0, w, h);
        vctx.fillStyle = 'rgba(25, 25, 35, 0.9)';
        vctx.fillRect(0, 0, w, h);

        // Pitch lane + keyboard labels on left side
        const trackData = trackView.noteMap[ch];
        if (trackData && trackData.notes && trackData.notes.length) {
            const m = getTrackNoteMetrics(trackData, h);
            const laneW = 24;
            vctx.fillStyle = 'rgba(12, 12, 18, 0.75)';
            vctx.fillRect(0, 0, laneW, h);
            const NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
            for (let note = m.minNote; note <= m.maxNote; note++) {
                const yRatio = m.noteRange > 0 ? (m.maxNote - note) / m.noteRange : 0.5;
                const y = m.padding + yRatio * (m.drawHeight - m.noteH) + (m.noteH * 0.5);
                vctx.strokeStyle = (note % 12 === 0) ? 'rgba(255,255,255,0.20)' : 'rgba(255,255,255,0.06)';
                vctx.lineWidth = 1;
                vctx.beginPath();
                vctx.moveTo(laneW, y);
                vctx.lineTo(w, y);
                vctx.stroke();
                if (note % 12 === 0 && y > 8 && y < h - 2) {
                    vctx.fillStyle = 'rgba(220,220,220,0.75)';
                    vctx.font = '9px monospace';
                    vctx.fillText(NOTE_NAMES[note % 12] + (Math.floor(note / 12) - 1), 2, y - 1);
                }
            }
        }

        if (tape) {
            const sx = tapeStartTime * trackView.tapeScale;
            const sourceWidth = visibleBeats * trackView.tapeScale;
            vctx.drawImage(tape, sx, 0, sourceWidth, tape.height, 0, 0, w, h);
        }

        // Track color stripe on left edge
        const tColor = getTrackColor(ch);
        const muted = dawState.muted && dawState.muted[ch];
        vctx.globalAlpha = muted ? 0.25 : 0.8;
        vctx.fillStyle = tColor;
        vctx.fillRect(0, 0, 4, h);
        vctx.globalAlpha = 1;

        // Griglia direttamente sul canvas visibile (sempre nitida)
        const firstBeat = Math.floor(tapeStartTime);
        for (let b = firstBeat; b <= tapeStartTime + visibleBeats + 1; b++) {
            const px = (b - tapeStartTime) * trackView.zoomLevel;
            if (px < 0) continue;
            if (px > w) break;
            vctx.beginPath();
            vctx.moveTo(px, 0);
            vctx.lineTo(px, h);
            if (b % beatsPerMeasure === 0) {
                vctx.strokeStyle = 'rgba(255,255,255,0.45)';
                vctx.lineWidth = 1.5;
            } else {
                vctx.strokeStyle = 'rgba(255,255,255,0.25)';
                vctx.lineWidth = 1;
            }
            vctx.stroke();
        }

        // Sub-beat grid at high zoom
        if (trackView.zoomLevel > 40) {
            for (let b = firstBeat; b <= tapeStartTime + visibleBeats + 1; b++) {
                for (let sub = 1; sub <= 3; sub++) {
                    const subBeat = b + sub * 0.25;
                    const spx = (subBeat - tapeStartTime) * trackView.zoomLevel;
                    if (spx < 0) continue;
                    if (spx > w) break;
                    vctx.strokeStyle = 'rgba(255,255,255,0.08)';
                    vctx.lineWidth = 0.5;
                    vctx.beginPath();
                    vctx.moveTo(spx, 0);
                    vctx.lineTo(spx, h);
                    vctx.stroke();
                }
            }
        }

        // Shading alternato per battuta
        const firstBar = Math.floor(tapeStartTime / beatsPerMeasure);
        const lastBar = Math.ceil((tapeStartTime + visibleBeats) / beatsPerMeasure);
        for (let bar = firstBar; bar <= lastBar; bar++) {
            const barStartBeat = bar * beatsPerMeasure;
            const x0 = (barStartBeat - tapeStartTime) * trackView.zoomLevel;
            const barW = beatsPerMeasure * trackView.zoomLevel;
            vctx.fillStyle = 'rgba(255,255,255,0.04)';
            vctx.fillRect(x0, 0, barW, h);
        }

        // Selection overlay
        if (selectionInBeat !== null && selectionOutBeat !== null) {
            const selX0 = (selectionInBeat - tapeStartTime) * trackView.zoomLevel;
            const selX1 = (selectionOutBeat - tapeStartTime) * trackView.zoomLevel;
            if (selX1 > 0 && selX0 < w) {
                const sx = Math.max(0, selX0);
                const ex = Math.min(w, selX1);
                if (ch === activeControlChannel) {
                    vctx.fillStyle = 'rgba(255, 200, 50, 0.25)';
                } else {
                    vctx.fillStyle = 'rgba(180, 180, 180, 0.12)';
                }
                vctx.fillRect(sx, 0, ex - sx, h);
                // Selection edges
                vctx.strokeStyle = 'rgba(255, 200, 50, 0.7)';
                vctx.lineWidth = 1;
                if (selX0 >= 0 && selX0 <= w) {
                    vctx.beginPath(); vctx.moveTo(selX0, 0); vctx.lineTo(selX0, h); vctx.stroke();
                }
                if (selX1 >= 0 && selX1 <= w) {
                    vctx.beginPath(); vctx.moveTo(selX1, 0); vctx.lineTo(selX1, h); vctx.stroke();
                }
            }
        }

        // Clip overlay (MVP): draw clip blocks as recording-session objects
        const clipList = (dawState.clips && dawState.clips[ch]) ? dawState.clips[ch] : [];
        if (clipList.length) {
            for (const clip of clipList) {
                const isDragging = clipDrag && clipDrag.ch === ch && clipDrag.clipId === clip.id;
                const isResizing = clipResize && clipResize.ch === ch && clipResize.clipId === clip.id;
                // If dragging this clip, draw ghost at new position; dim the original
                const drawStart = isDragging ? clipDrag.ghostBeat : clip.start_beat;
                const drawEnd = isDragging ? (clipDrag.ghostBeat + (clip.end_beat - clip.start_beat))
                              : isResizing ? clipResize.ghostEndBeat
                              : clip.end_beat;

                // Draw original position dimmed while dragging or resizing
                if (isDragging || isResizing) {
                    const ox0 = (clip.start_beat - tapeStartTime) * trackView.zoomLevel;
                    const ox1 = (clip.end_beat - tapeStartTime) * trackView.zoomLevel;
                    if (ox1 > 0 && ox0 < w) {
                        const origX = Math.max(0, ox0);
                        const origEx = Math.min(w, ox1);
                        vctx.fillStyle = 'rgba(255, 193, 7, 0.08)';
                        vctx.fillRect(origX, 2, Math.max(1, origEx - origX), Math.max(8, h - 4));
                        vctx.strokeStyle = 'rgba(255, 193, 7, 0.25)';
                        vctx.lineWidth = 1;
                        vctx.setLineDash([3, 3]);
                        vctx.strokeRect(origX + 0.5, 2.5, Math.max(1, origEx - origX - 1), Math.max(7, h - 5));
                        vctx.setLineDash([]);
                    }
                }

                const cx0 = (drawStart - tapeStartTime) * trackView.zoomLevel;
                const cx1 = (drawEnd - tapeStartTime) * trackView.zoomLevel;
                if (cx1 <= 0 || cx0 >= w) continue;
                const x = Math.max(0, cx0);
                const ex = Math.min(w, cx1);
                const cw = Math.max(1, ex - x);
                if (isDragging || isResizing) {
                    // Ghost clip: brighter, with dashed border
                    vctx.fillStyle = 'rgba(255, 193, 7, 0.35)';
                    vctx.fillRect(x, 2, cw, Math.max(8, h - 4));
                    vctx.strokeStyle = 'rgba(255, 193, 7, 0.9)';
                    vctx.lineWidth = 2;
                    vctx.strokeRect(x + 0.5, 2.5, Math.max(1, cw - 1), Math.max(7, h - 5));
                } else {
                    const clipColor = ch === activeControlChannel
                        ? 'rgba(255, 193, 7, 0.22)'
                        : 'rgba(13, 202, 240, 0.14)';
                    vctx.fillStyle = clipColor;
                    vctx.fillRect(x, 2, cw, Math.max(8, h - 4));
                    vctx.strokeStyle = ch === activeControlChannel
                        ? 'rgba(255, 193, 7, 0.75)'
                        : 'rgba(13, 202, 240, 0.55)';
                    vctx.lineWidth = 1;
                    vctx.strokeRect(x + 0.5, 2.5, Math.max(1, cw - 1), Math.max(7, h - 5));
                }
                if (cw > 40) {
                    vctx.fillStyle = 'rgba(255,255,255,0.9)';
                    vctx.font = '10px sans-serif';
                    vctx.fillText(clip.name || `Clip ${clip.id}`, x + 4, 13);
                }
            }
        }

        // Loop region overlay
        if (dawState.loop_enabled && dawState.loop_start != null && dawState.loop_end != null && dawState.loop_end > dawState.loop_start) {
            const loopX0 = (dawState.loop_start - tapeStartTime) * trackView.zoomLevel;
            const loopX1 = (dawState.loop_end - tapeStartTime) * trackView.zoomLevel;
            if (loopX1 > 0 && loopX0 < w) {
                const lx = Math.max(0, loopX0);
                const lxEnd = Math.min(w, loopX1);
                vctx.fillStyle = 'rgba(0, 200, 255, 0.08)';
                vctx.fillRect(lx, 0, lxEnd - lx, h);
                // Loop boundary lines
                vctx.strokeStyle = 'rgba(0, 200, 255, 0.5)';
                vctx.lineWidth = 1.5;
                vctx.setLineDash([4, 3]);
                if (loopX0 >= 0 && loopX0 <= w) {
                    vctx.beginPath(); vctx.moveTo(loopX0, 0); vctx.lineTo(loopX0, h); vctx.stroke();
                }
                if (loopX1 >= 0 && loopX1 <= w) {
                    vctx.beginPath(); vctx.moveTo(loopX1, 0); vctx.lineTo(loopX1, h); vctx.stroke();
                }
                vctx.setLineDash([]);
            }
        }

        // playhead overlay (always visible when beatDuration > 0)
        if (beatDuration > 0 && playheadX >= 0 && playheadX <= w) {
            if (dawState.is_recording) {
                vctx.strokeStyle = 'rgba(255, 80, 80, 0.9)';
            } else if (dawState.is_playing) {
                vctx.strokeStyle = 'rgba(255, 80, 80, 0.9)';
            } else {
                vctx.strokeStyle = 'rgba(0, 200, 255, 0.8)';
            }
            vctx.lineWidth = 2;
            vctx.beginPath();
            vctx.moveTo(playheadX, 0);
            vctx.lineTo(playheadX, h);
            vctx.stroke();
        }
    }
    // Piano Roll (renders if visible)
    renderPianoRoll();
}

// ═══════════════ PIANO ROLL EDITOR ═══════════════

function togglePianoRoll() {
    const panel = document.getElementById('panel-pianoroll');
    if (!panel) return;
    pianoRoll.visible = !pianoRoll.visible;
    if (pianoRoll.visible) {
        panel.style.display = 'flex';
        const td = trackView.noteMap[activeControlChannel];
        if (td && td.notes && td.notes.length) {
            const mid = Math.round((td.min_note + td.max_note) / 2);
            const gridCanvas = document.getElementById('pr-grid');
            const visibleNotes = Math.floor((gridCanvas?.clientHeight || 260) / pianoRoll.rowHeight);
            pianoRoll.scrollNote = Math.min(120, mid + Math.floor(visibleNotes / 2));
        } else {
            pianoRoll.scrollNote = 84;
        }
        document.getElementById('pr-ch-display').textContent = activeControlChannel + 1;
        ensureRenderLoop();
    } else {
        panel.style.display = 'none';
    }
}

function openPianoRollForChannel(ch) {
    setControlChannel(ch);
    if (!pianoRoll.visible) togglePianoRoll();
    else {
        document.getElementById('pr-ch-display').textContent = ch + 1;
        const td = trackView.noteMap[ch];
        if (td && td.notes && td.notes.length) {
            const mid = Math.round((td.min_note + td.max_note) / 2);
            const gridCanvas = document.getElementById('pr-grid');
            const visibleNotes = Math.floor((gridCanvas?.clientHeight || 260) / pianoRoll.rowHeight);
            pianoRoll.scrollNote = Math.min(120, mid + Math.floor(visibleNotes / 2));
        }
    }
}

function renderPianoRoll() {
    if (!pianoRoll.visible) return;

    const kbCanvas = document.getElementById('pr-keyboard');
    const gridCanvas = document.getElementById('pr-grid');
    const velCanvas = document.getElementById('pr-velocity');
    if (!kbCanvas || !gridCanvas || !velCanvas) return;

    const ch = activeControlChannel;
    const gW = gridCanvas.clientWidth;
    const gH = gridCanvas.clientHeight;
    const kW = 52;
    const velH = 56;

    // Sync canvas pixel size to CSS size
    if (gridCanvas.width !== gW) gridCanvas.width = gW;
    if (gridCanvas.height !== gH) gridCanvas.height = gH;
    if (kbCanvas.height !== gH) kbCanvas.height = gH;
    kbCanvas.width = kW;
    if (velCanvas.width !== gW) velCanvas.width = gW;
    if (velCanvas.height !== velH) velCanvas.height = velH;

    const ctx = gridCanvas.getContext('2d');
    const kctx = kbCanvas.getContext('2d');
    const vctx = velCanvas.getContext('2d');

    const rH = pianoRoll.rowHeight;
    const topNote = pianoRoll.scrollNote;
    const visibleRows = Math.ceil(gH / rH) + 1;

    // === CLEAR ===
    ctx.clearRect(0, 0, gW, gH);
    ctx.fillStyle = 'rgba(22,22,32,1)';
    ctx.fillRect(0, 0, gW, gH);
    kctx.clearRect(0, 0, kW, gH);
    kctx.fillStyle = 'rgba(20,20,30,1)';
    kctx.fillRect(0, 0, kW, gH);
    vctx.clearRect(0, 0, gW, velH);
    vctx.fillStyle = 'rgba(18,18,28,1)';
    vctx.fillRect(0, 0, gW, velH);

    // === KEYBOARD + PITCH GRID ROWS ===
    for (let i = 0; i <= visibleRows; i++) {
        const note = topNote - i;
        if (note < 0 || note > 127) continue;
        const y = i * rH;
        const semitone = note % 12;
        const isBlack = PR_BLACK_KEYS.has(semitone);
        const isC = semitone === 0;

        // --- Keyboard key ---
        if (isBlack) {
            kctx.fillStyle = '#1a1a2e';
            kctx.fillRect(0, y, kW, rH);
            kctx.fillStyle = '#2a2a44';
            kctx.fillRect(0, y, kW * 0.65, rH);
        } else {
            kctx.fillStyle = '#d8d8e0';
            kctx.fillRect(0, y, kW, rH);
        }
        // Key separator
        kctx.strokeStyle = isC ? 'rgba(0,200,255,0.4)' : 'rgba(80,80,100,0.4)';
        kctx.lineWidth = isC ? 1 : 0.5;
        kctx.beginPath(); kctx.moveTo(0, y + rH); kctx.lineTo(kW, y + rH); kctx.stroke();

        // Key label
        if (isC) {
            kctx.fillStyle = '#111';
            kctx.font = 'bold 9px sans-serif';
            kctx.fillText('C' + (Math.floor(note / 12) - 1), 3, y + rH - 2);
        } else if (!isBlack && rH >= 10) {
            kctx.fillStyle = 'rgba(60,60,80,0.7)';
            kctx.font = '8px sans-serif';
            kctx.fillText(PR_NOTE_NAMES[semitone], 3, y + rH - 2);
        }

        // --- Grid row background ---
        ctx.fillStyle = isBlack ? 'rgba(12,12,22,0.7)' : 'rgba(28,28,42,0.4)';
        ctx.fillRect(0, y, gW, rH);
        // Horizontal pitch line
        ctx.strokeStyle = isC ? 'rgba(0,200,255,0.25)' : 'rgba(255,255,255,0.04)';
        ctx.lineWidth = isC ? 1 : 0.5;
        ctx.beginPath(); ctx.moveTo(0, y + rH); ctx.lineTo(gW, y + rH); ctx.stroke();
    }

    // === VERTICAL BEAT / BAR GRID ===
    const beatsPerMeasure = trackView.meta.beatsPerMeasure || 4;
    const startBeat = trackView.startTime;
    const visibleBeats = gW / trackView.zoomLevel;
    const firstBeat = Math.floor(startBeat);
    const lastBeat = Math.ceil(startBeat + visibleBeats);

    for (let beat = firstBeat; beat <= lastBeat; beat++) {
        const x = (beat - startBeat) * trackView.zoomLevel;
        const isBar = beat % beatsPerMeasure === 0;
        ctx.strokeStyle = isBar ? 'rgba(255,255,255,0.25)' : 'rgba(255,255,255,0.06)';
        ctx.lineWidth = isBar ? 1 : 0.5;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, gH); ctx.stroke();
        if (isBar && x > 2 && x < gW - 20) {
            ctx.fillStyle = 'rgba(255,255,255,0.25)';
            ctx.font = '9px monospace';
            ctx.fillText(Math.floor(beat / beatsPerMeasure) + 1, x + 3, 10);
        }
        // Velocity lane beat grid
        vctx.strokeStyle = isBar ? 'rgba(255,255,255,0.15)' : 'rgba(255,255,255,0.04)';
        vctx.lineWidth = isBar ? 0.5 : 0.25;
        vctx.beginPath(); vctx.moveTo(x, 0); vctx.lineTo(x, velH); vctx.stroke();
    }

    // === NOTES + VELOCITY BARS ===
    const trackData = trackView.noteMap[ch];
    const tColor = getTrackColor(ch);
    if (trackData && trackData.notes) {
        for (const n of trackData.notes) {
            const note = n[0], vel = n[1], nStart = n[2], nEnd = n[3];
            const x = (nStart - startBeat) * trackView.zoomLevel;
            const w = Math.max(2, (nEnd - nStart) * trackView.zoomLevel);
            if (x + w < 0 || x > gW) continue;

            const noteRow = topNote - note;
            if (noteRow < -1 || noteRow > visibleRows + 1) continue;
            const y = noteRow * rH;
            const velRatio = vel / 127;

            // --- Note rectangle ---
            ctx.globalAlpha = 0.5 + 0.5 * velRatio;
            ctx.fillStyle = tColor;
            const ny = y + 1;
            const nh = rH - 2;
            if (w > 4 && nh > 3) {
                const r = Math.min(3, nh / 3, w / 3);
                ctx.beginPath();
                ctx.moveTo(x + r, ny);
                ctx.lineTo(x + w - r, ny);
                ctx.arcTo(x + w, ny, x + w, ny + r, r);
                ctx.lineTo(x + w, ny + nh - r);
                ctx.arcTo(x + w, ny + nh, x + w - r, ny + nh, r);
                ctx.lineTo(x + r, ny + nh);
                ctx.arcTo(x, ny + nh, x, ny + nh - r, r);
                ctx.lineTo(x, ny + r);
                ctx.arcTo(x, ny, x + r, ny, r);
                ctx.fill();
            } else {
                ctx.fillRect(x, ny, w, nh);
            }
            ctx.globalAlpha = 1;

            // Note highlight (top edge shine)
            ctx.fillStyle = 'rgba(255,255,255,0.15)';
            ctx.fillRect(x, ny, w, 1);

            // Note name text if wide enough
            if (w > 28 && nh >= 9) {
                ctx.fillStyle = 'rgba(0,0,0,0.6)';
                ctx.font = '8px sans-serif';
                ctx.fillText(PR_NOTE_NAMES[note % 12] + (Math.floor(note / 12) - 1), x + 3, y + rH - 3);
            }

            // Selected note outline
            const nid = noteEntryId(ch, n);
            if (nid && noteEditor.selected.has(nid)) {
                ctx.strokeStyle = 'rgba(255, 200, 50, 0.95)';
                ctx.lineWidth = 2;
                ctx.strokeRect(x + 0.5, ny + 0.5, Math.max(1, w - 1), Math.max(1, nh - 1));
            }

            // --- Velocity bar ---
            const vBarH = velRatio * (velH - 6);
            vctx.fillStyle = tColor;
            vctx.globalAlpha = 0.5 + 0.5 * velRatio;
            vctx.fillRect(x, velH - vBarH - 3, Math.max(2, w), vBarH);
            vctx.globalAlpha = 1;
            // Velocity value on tall bars
            if (vBarH > 14 && w > 12) {
                vctx.fillStyle = 'rgba(255,255,255,0.7)';
                vctx.font = '7px monospace';
                vctx.fillText(vel, x + 1, velH - vBarH - 5);
            }
        }
    }

    // === GHOST NOTE (draw tool preview) ===
    if (prEditor.ghostNote) {
        const gn = prEditor.ghostNote;
        const gnRow = topNote - gn.note;
        const gnY = gnRow * rH + 1;
        const gnX = (gn.startBeat - startBeat) * trackView.zoomLevel;
        const gnW = Math.max(2, (gn.endBeat - gn.startBeat) * trackView.zoomLevel);
        ctx.globalAlpha = 0.35;
        ctx.fillStyle = tColor;
        ctx.fillRect(gnX, gnY, gnW, rH - 2);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = 'rgba(255,255,255,0.5)';
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.strokeRect(gnX, gnY, gnW, rH - 2);
        ctx.setLineDash([]);
    }

    // === BOX SELECTION RECTANGLE ===
    if (prEditor.boxSel) {
        const bs = prEditor.boxSel;
        const bx = Math.min(bs.x0, bs.x1);
        const by = Math.min(bs.y0, bs.y1);
        const bw = Math.abs(bs.x1 - bs.x0);
        const bh = Math.abs(bs.y1 - bs.y0);
        ctx.fillStyle = 'rgba(255,200,50,0.08)';
        ctx.fillRect(bx, by, bw, bh);
        ctx.strokeStyle = 'rgba(255,200,50,0.5)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        ctx.strokeRect(bx, by, bw, bh);
        ctx.setLineDash([]);
    }

    // === PLAYHEAD ===
    if (dawState && dawState.timeline_position != null) {
        const beatDuration = dawState.bpm ? 60 / dawState.bpm : 0;
        const currentBeat = beatDuration > 0 ? (dawState.timeline_position / beatDuration) : 0;
        const phX = (currentBeat - startBeat) * trackView.zoomLevel;
        if (phX >= 0 && phX <= gW) {
            ctx.strokeStyle = '#ff2222';
            ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.moveTo(phX, 0); ctx.lineTo(phX, gH); ctx.stroke();
            vctx.strokeStyle = '#ff2222';
            vctx.lineWidth = 1;
            vctx.beginPath(); vctx.moveTo(phX, 0); vctx.lineTo(phX, velH); vctx.stroke();
        }
    }
}

// ═══════════════ PIANO ROLL TOOL & INTERACTION ═══════════════

function setPRTool(tool) {
    pianoRoll.tool = tool;
    document.querySelectorAll('.pr-tool-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.prTool === tool);
    });
    const grid = document.getElementById('pr-grid');
    if (grid) {
        grid.style.cursor = tool === 'draw' ? 'crosshair' : (tool === 'erase' ? 'pointer' : 'default');
    }
    prEditor.ghostNote = null;
    ensureRenderLoop();
}

function updatePRSnap(val) {
    // Sync with main DAW snap grid
    const dawSnap = document.getElementById('daw-snap-grid');
    if (dawSnap) dawSnap.value = val;
}

function prSnapBeat(beat) {
    const sel = document.getElementById('pr-snap-grid');
    const snap = sel ? parseFloat(sel.value) : 0;
    if (!snap || snap <= 0) return Math.max(0, beat);
    return Math.max(0, Math.round(beat / snap) * snap);
}

// Piano Roll coordinate helpers
function prBeatFromX(x) { return trackView.startTime + (x / trackView.zoomLevel); }
function prNoteFromY(y) { return pianoRoll.scrollNote - Math.floor(y / pianoRoll.rowHeight); }
function prXFromBeat(beat) { return (beat - trackView.startTime) * trackView.zoomLevel; }
function prYFromNote(note) { return (pianoRoll.scrollNote - note) * pianoRoll.rowHeight; }

// Hit-test notes within the Piano Roll grid canvas
function hitTestPRNote(ch, px, py) {
    const trackData = trackView.noteMap[ch];
    if (!trackData || !trackData.notes) return null;
    const rH = pianoRoll.rowHeight;
    const topNote = pianoRoll.scrollNote;
    const startBeat = trackView.startTime;
    const gridCanvas = document.getElementById('pr-grid');
    const gW = gridCanvas ? gridCanvas.clientWidth : 800;

    for (let i = trackData.notes.length - 1; i >= 0; i--) {
        const n = trackData.notes[i];
        const note = n[0], nStart = n[2], nEnd = n[3];
        const x = (nStart - startBeat) * trackView.zoomLevel;
        const w = Math.max(2, (nEnd - nStart) * trackView.zoomLevel);
        if (x + w < 0 || x > gW) continue;

        const noteRow = topNote - note;
        const y = noteRow * rH;
        const ny = y + 1;
        const nh = rH - 2;

        if (px >= x && px <= x + w && py >= ny && py <= ny + nh) {
            const edgePad = Math.min(8, Math.max(3, w * 0.2));
            let edge = null;
            if ((x + w) - px <= edgePad) edge = 'right';
            else if (px - x <= edgePad) edge = 'left';
            return { entry: n, index: i, x, y: ny, w, h: nh, edge, id: noteEntryId(ch, n) };
        }
    }
    return null;
}

// Hit-test a note bar in the velocity lane
function hitTestPRVelocity(ch, px) {
    const trackData = trackView.noteMap[ch];
    if (!trackData || !trackData.notes) return null;
    const startBeat = trackView.startTime;

    for (let i = trackData.notes.length - 1; i >= 0; i--) {
        const n = trackData.notes[i];
        const nStart = n[2], nEnd = n[3];
        const x = (nStart - startBeat) * trackView.zoomLevel;
        const w = Math.max(4, (nEnd - nStart) * trackView.zoomLevel);
        if (px >= x && px <= x + w) {
            return { entry: n, index: i, id: noteEntryId(ch, n) };
        }
    }
    return null;
}

// Delete selected notes via API (atomic: on+off pair per call)
async function prDeleteSelectedNotes() {
    const ch = activeControlChannel;
    const trackData = trackView.noteMap[ch];
    if (!trackData || !trackData.notes || noteEditor.selected.size === 0) return;

    for (const id of noteEditor.selected) {
        const n = findCurrentNoteById(ch, id);
        if (!n) continue;
        const onIdx = n.length > 4 ? n[4] : -1;
        const offIdx = n.length > 5 ? n[5] : -1;
        await fetch(`/api/daw/track/${ch}/delete_note_pair`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ on_idx: onIdx, off_idx: offIdx })
        });
    }
    noteEditor.selected.clear();
    await refreshDensityMap(true);
}

// Add a new note via API (draw tool)
async function prAddNote(ch, note, velocity, startBeat, lengthBeats) {
    await fetch(`/api/daw/track/${ch}/add_note`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note, velocity, start_beat: startBeat, length_beats: lengthBeats })
    });
    await refreshDensityMap(true);
}

// Commit drag edits from PR (move/resize) to API — atomic note pair update
async function prCommitDragEdit() {
    const d = prEditor.drag;
    if (!d || !d.changed) return;
    for (const o of d.originals) {
        const n = findCurrentNoteById(d.ch, o.id);
        if (!n) continue;
        const updates = {};
        if (Math.abs(n[2] - o.start) > 0.0001) updates.on_beat = n[2];
        if (Math.abs(n[3] - o.end) > 0.0001) updates.off_beat = n[3];
        if (n[0] !== o.note) updates.note = n[0];
        if (Object.keys(updates).length) {
            await fetch(`/api/daw/track/${d.ch}/edit_note_pair`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ on_idx: o.onIdx, off_idx: o.offIdx, ...updates })
            });
        }
    }
    await refreshDensityMap(true);
}

// Commit velocity change from PR velocity lane (atomic pair API)
async function prCommitVelocityEdit(ch, noteId, newVel) {
    const n = findCurrentNoteById(ch, noteId);
    if (!n) return;
    const onIdx = n.length > 4 ? n[4] : -1;
    const offIdx = n.length > 5 ? n[5] : -1;
    await fetch(`/api/daw/track/${ch}/edit_note_pair`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            on_idx: onIdx, off_idx: offIdx,
            velocity: Math.max(1, Math.min(127, Math.round(newVel)))
        })
    });
    await refreshDensityMap(true);
}

// ═══════════════ PIANO ROLL GRID INTERACTION ═══════════════
(function initPianoRollGridInteraction() {
    const grid = document.getElementById('pr-grid');
    if (!grid) return;
    grid.style.touchAction = 'none';

    grid.addEventListener('pointerdown', onPRGridDown);
    grid.addEventListener('pointermove', onPRGridMove);
    grid.addEventListener('pointerup', onPRGridUp);
    grid.addEventListener('pointercancel', onPRGridUp);

    function isEditable() {
        return dawState && !dawState.is_playing && !dawState.is_recording;
    }

    function onPRGridDown(e) {
        if (!pianoRoll.visible || !isEditable()) return;
        const rect = grid.getBoundingClientRect();
        const px = e.clientX - rect.left;
        const py = e.clientY - rect.top;
        const ch = activeControlChannel;
        const tool = pianoRoll.tool;

        const hit = hitTestPRNote(ch, px, py);

        // === ERASE TOOL ===
        if (tool === 'erase') {
            if (hit) {
                noteEditor.selected.clear();
                noteEditor.selected.add(hit.id);
                prDeleteSelectedNotes();
            }
            return;
        }

        // === DRAW TOOL ===
        if (tool === 'draw') {
            if (hit) {
                // Click existing note in draw mode = erase it (like Ableton)
                noteEditor.selected.clear();
                noteEditor.selected.add(hit.id);
                prDeleteSelectedNotes();
                return;
            }
            const beat = prSnapBeat(prBeatFromX(px));
            const note = prNoteFromY(py);
            if (note < 0 || note > 127) return;
            prEditor.drag = {
                mode: 'draw', ch, startBeat: beat, note,
                startX: px, startY: py, changed: false
            };
            prEditor.ghostNote = {
                note, startBeat: beat,
                endBeat: prSnapBeat(beat + pianoRoll.defaultLength)
            };
            grid.setPointerCapture(e.pointerId);
            ensureRenderLoop();
            previewNote(ch, note, pianoRoll.defaultVelocity);
            return;
        }

        // === SELECT TOOL ===
        if (hit) {
            // Click on a note
            if (e.shiftKey) {
                if (noteEditor.selected.has(hit.id)) noteEditor.selected.delete(hit.id);
                else noteEditor.selected.add(hit.id);
            } else if (!noteEditor.selected.has(hit.id)) {
                noteEditor.selected.clear();
                noteEditor.selected.add(hit.id);
            }

            if (noteEditor.selected.size > 0) {
                const mode = hit.edge === 'right' ? 'resize-right' : (hit.edge === 'left' ? 'resize-left' : 'move');
                const originals = [];
                for (const id of noteEditor.selected) {
                    const n = findCurrentNoteById(ch, id);
                    if (!n) continue;
                    originals.push({
                        id, note: n[0], vel: n[1], start: n[2], end: n[3],
                        onIdx: n.length > 4 ? n[4] : -1,
                        offIdx: n.length > 5 ? n[5] : -1
                    });
                }
                prEditor.drag = {
                    mode, ch, startX: e.clientX, startY: e.clientY,
                    originals, changed: false
                };
                previewNote(ch, hit.entry[0], hit.entry[1]);
                grid.setPointerCapture(e.pointerId);
                ensureRenderLoop();
                return;
            }
        } else {
            // Click empty space: start box selection (or clear selection if no shift)
            if (!e.shiftKey) noteEditor.selected.clear();
            prEditor.boxSel = { x0: px, y0: py, x1: px, y1: py };
            prEditor.drag = { mode: 'box', ch, startX: px, startY: py };
            grid.setPointerCapture(e.pointerId);
            ensureRenderLoop();
        }
    }

    function onPRGridMove(e) {
        if (!pianoRoll.visible) return;
        const rect = grid.getBoundingClientRect();
        const px = e.clientX - rect.left;
        const py = e.clientY - rect.top;
        const ch = activeControlChannel;
        const d = prEditor.drag;

        // Update cursor based on hover
        if (!d && pianoRoll.tool === 'select') {
            const hit = hitTestPRNote(ch, px, py);
            if (hit) {
                grid.style.cursor = hit.edge === 'right' || hit.edge === 'left' ? 'ew-resize' : 'move';
            } else {
                grid.style.cursor = 'default';
            }
        }

        if (!d) return;

        // === DRAW: extend ghost note duration ===
        if (d.mode === 'draw') {
            const beat = prSnapBeat(prBeatFromX(px));
            const minEnd = prSnapBeat(d.startBeat + 0.125);
            prEditor.ghostNote = {
                note: d.note,
                startBeat: Math.min(d.startBeat, beat),
                endBeat: Math.max(beat, minEnd)
            };
            if (beat !== d.startBeat) d.changed = true;
            ensureRenderLoop();
            return;
        }

        // === BOX SELECTION ===
        if (d.mode === 'box') {
            prEditor.boxSel = { x0: d.startX, y0: d.startY, x1: px, y1: py };
            ensureRenderLoop();
            return;
        }

        // === MOVE / RESIZE ===
        if ((d.mode === 'move' || d.mode === 'resize-right' || d.mode === 'resize-left') && d.originals) {
            const deltaX = e.clientX - d.startX;
            const rawDeltaBeat = deltaX / trackView.zoomLevel;
            const beatDelta = prSnapBeat(rawDeltaBeat) - prSnapBeat(0);
            const deltaY = d.startY - e.clientY;
            const semitoneDelta = Math.round(deltaY / pianoRoll.rowHeight);

            for (const o of d.originals) {
                const n = findCurrentNoteById(d.ch, o.id);
                if (!n) continue;
                if (d.mode === 'move') {
                    const dur = Math.max(0.01, o.end - o.start);
                    n[2] = Math.max(0, prSnapBeat(o.start + rawDeltaBeat));
                    n[3] = n[2] + dur;
                    n[0] = Math.max(0, Math.min(127, o.note + semitoneDelta));
                } else if (d.mode === 'resize-right') {
                    n[3] = Math.max(n[2] + 0.01, prSnapBeat(o.end + rawDeltaBeat));
                } else if (d.mode === 'resize-left') {
                    const newStart = Math.max(0, prSnapBeat(o.start + rawDeltaBeat));
                    n[2] = Math.min(newStart, n[3] - 0.01);
                }
            }
            d.changed = true;
            ensureRenderLoop();
            return;
        }
    }

    function onPRGridUp(e) {
        if (!pianoRoll.visible) return;
        const d = prEditor.drag;
        if (!d) return;
        try { grid.releasePointerCapture(e.pointerId); } catch (_) {}

        const ch = activeControlChannel;

        // === DRAW: commit new note ===
        if (d.mode === 'draw' && prEditor.ghostNote) {
            const gn = prEditor.ghostNote;
            const len = Math.max(0.125, gn.endBeat - gn.startBeat);
            prAddNote(ch, gn.note, pianoRoll.defaultVelocity, gn.startBeat, len);
            prEditor.ghostNote = null;
            prEditor.drag = null;
            return;
        }

        // === BOX SELECT: finalize ===
        if (d.mode === 'box' && prEditor.boxSel) {
            const bs = prEditor.boxSel;
            const x0 = Math.min(bs.x0, bs.x1), x1 = Math.max(bs.x0, bs.x1);
            const y0 = Math.min(bs.y0, bs.y1), y1 = Math.max(bs.y0, bs.y1);

            const trackData = trackView.noteMap[ch];
            if (trackData && trackData.notes) {
                const rH = pianoRoll.rowHeight;
                const topNote = pianoRoll.scrollNote;
                const startBeat = trackView.startTime;
                for (const n of trackData.notes) {
                    const nx = (n[2] - startBeat) * trackView.zoomLevel;
                    const nw = Math.max(2, (n[3] - n[2]) * trackView.zoomLevel);
                    const noteRow = topNote - n[0];
                    const ny = noteRow * rH + 1;
                    const nh = rH - 2;
                    const intersects = !(nx > x1 || (nx + nw) < x0 || ny > y1 || (ny + nh) < y0);
                    if (intersects) noteEditor.selected.add(noteEntryId(ch, n));
                }
            }
            prEditor.boxSel = null;
            prEditor.drag = null;
            ensureRenderLoop();
            return;
        }

        // === MOVE/RESIZE: commit ===
        if ((d.mode === 'move' || d.mode === 'resize-right' || d.mode === 'resize-left') && d.changed) {
            prCommitDragEdit().catch(err => console.error('[PR] Drag commit error:', err));
        }

        prEditor.drag = null;
        ensureRenderLoop();
    }
})();

// ═══════════════ PIANO ROLL VELOCITY LANE INTERACTION ═══════════════
(function initPRVelocityInteraction() {
    const vel = document.getElementById('pr-velocity');
    if (!vel) return;
    vel.style.touchAction = 'none';

    vel.addEventListener('pointerdown', onVelDown);
    vel.addEventListener('pointermove', onVelMove);
    vel.addEventListener('pointerup', onVelUp);
    vel.addEventListener('pointercancel', onVelUp);

    function onVelDown(e) {
        if (!pianoRoll.visible) return;
        if (dawState && (dawState.is_playing || dawState.is_recording)) return;
        const rect = vel.getBoundingClientRect();
        const px = e.clientX - rect.left;
        const py = e.clientY - rect.top;
        const ch = activeControlChannel;
        const hit = hitTestPRVelocity(ch, px);
        if (!hit) return;

        const velH = vel.clientHeight || 56;
        const newVel = Math.max(1, Math.min(127, Math.round(127 * (1 - (py / (velH - 3))))));
        hit.entry[1] = newVel;

        prEditor.velDrag = { ch, noteId: hit.id, startY: py, origVel: hit.entry[1] };
        vel.setPointerCapture(e.pointerId);
        ensureRenderLoop();
    }

    function onVelMove(e) {
        const vd = prEditor.velDrag;
        if (!vd) return;
        const rect = vel.getBoundingClientRect();
        const py = e.clientY - rect.top;
        const velH = vel.clientHeight || 56;
        const newVel = Math.max(1, Math.min(127, Math.round(127 * (1 - (py / (velH - 3))))));

        const n = findCurrentNoteById(vd.ch, vd.noteId);
        if (n) n[1] = newVel;
        ensureRenderLoop();
    }

    function onVelUp(e) {
        const vd = prEditor.velDrag;
        if (!vd) return;
        try { vel.releasePointerCapture(e.pointerId); } catch (_) {}

        const n = findCurrentNoteById(vd.ch, vd.noteId);
        if (n) {
            prCommitVelocityEdit(vd.ch, vd.noteId, n[1]).catch(err => console.error('[PR] Vel edit error:', err));
        }
        prEditor.velDrag = null;
    }
})();

// ═══════════════ PIANO ROLL KEYBOARD INTERACTION (note preview) ═══════════════
(function initPRKeyboardInteraction() {
    const kb = document.getElementById('pr-keyboard');
    if (!kb) return;
    kb.style.touchAction = 'none';
    kb.addEventListener('pointerdown', function(e) {
        if (!pianoRoll.visible) return;
        const rect = kb.getBoundingClientRect();
        const py = e.clientY - rect.top;
        const note = prNoteFromY(py);
        if (note >= 0 && note <= 127) {
            previewNote(activeControlChannel, note, pianoRoll.defaultVelocity);
        }
    });
})();

// ═══════════════ PIANO ROLL KEYBOARD SHORTCUTS ═══════════════
(function initPRKeyboardShortcuts() {
    document.addEventListener('keydown', function(e) {
        if (!pianoRoll.visible) return;
        // Don't capture when typing in inputs
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;

        const isEditable = dawState && !dawState.is_playing && !dawState.is_recording;

        // Tool switching: 1=Select, 2=Draw, 3=Erase
        if (e.key === '1' && !e.ctrlKey && !e.altKey) { setPRTool('select'); return; }
        if (e.key === '2' && !e.ctrlKey && !e.altKey) { setPRTool('draw'); return; }
        if (e.key === '3' && !e.ctrlKey && !e.altKey) { setPRTool('erase'); return; }

        // Delete selected notes
        if ((e.key === 'Delete' || e.key === 'Backspace') && isEditable && noteEditor.selected.size > 0) {
            e.preventDefault();
            prDeleteSelectedNotes();
            return;
        }

        // Ctrl+A = select all notes in current channel
        if (e.key === 'a' && e.ctrlKey && !e.shiftKey) {
            e.preventDefault();
            const ch = activeControlChannel;
            const td = trackView.noteMap[ch];
            if (td && td.notes) {
                for (const n of td.notes) {
                    noteEditor.selected.add(noteEntryId(ch, n));
                }
                ensureRenderLoop();
            }
            return;
        }

        // Escape = deselect all
        if (e.key === 'Escape') {
            noteEditor.selected.clear();
            prEditor.ghostNote = null;
            prEditor.boxSel = null;
            prEditor.drag = null;
            ensureRenderLoop();
            return;
        }

        // Arrow key nudge (selected notes)
        if (isEditable && noteEditor.selected.size > 0 && ['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)) {
            e.preventDefault();
            const ch = activeControlChannel;
            const snap = parseFloat(document.getElementById('pr-snap-grid')?.value || '0.25') || 0.25;

            // Capture originals BEFORE modifying
            const originals = [];
            for (const id of noteEditor.selected) {
                const n = findCurrentNoteById(ch, id);
                if (!n) continue;
                originals.push({
                    id, note: n[0], vel: n[1], start: n[2], end: n[3],
                    onIdx: n.length > 4 ? n[4] : -1,
                    offIdx: n.length > 5 ? n[5] : -1
                });
            }

            // Now modify the notes in-place
            for (const id of noteEditor.selected) {
                const n = findCurrentNoteById(ch, id);
                if (!n) continue;
                if (e.key === 'ArrowUp') n[0] = Math.min(127, n[0] + 1);
                if (e.key === 'ArrowDown') n[0] = Math.max(0, n[0] - 1);
                if (e.key === 'ArrowRight') { n[2] += snap; n[3] += snap; }
                if (e.key === 'ArrowLeft') { n[2] = Math.max(0, n[2] - snap); n[3] = Math.max(n[2] + 0.01, n[3] - snap); }
            }

            prEditor.drag = { ch, mode: 'move', originals, changed: true, startX: 0, startY: 0 };
            prCommitDragEdit().then(() => { prEditor.drag = null; }).catch(err => console.error('[PR] Nudge error:', err));
            ensureRenderLoop();
            return;
        }
    });
})();

// Piano Roll vertical scroll (wheel) + Ctrl+wheel = zoom row height
(function initPianoRollWheel() {
    document.addEventListener('wheel', function(e) {
        if (!pianoRoll.visible) return;
        const grid = document.getElementById('pr-grid');
        const kb = document.getElementById('pr-keyboard');
        if (!grid || !kb) return;
        if (e.target !== grid && e.target !== kb) return;
        e.preventDefault();
        if (e.ctrlKey) {
            // Zoom row height
            pianoRoll.rowHeight = Math.max(6, Math.min(32, pianoRoll.rowHeight + (e.deltaY < 0 ? 1 : -1)));
        } else if (e.shiftKey) {
            // Horizontal scroll (shift+wheel)
            const hDelta = e.deltaY > 0 ? 2 : -2;
            trackView.startTime = Math.max(0, trackView.startTime + hDelta);
        } else {
            const delta = e.deltaY > 0 ? -3 : 3;
            pianoRoll.scrollNote = Math.max(12, Math.min(120, pianoRoll.scrollNote + delta));
        }
        ensureRenderLoop();
    }, { passive: false });
})();

// Piano Roll — floating overlay: resize from top edge, drag-to-move disabled (docked bottom)
(function initPianoRollResize() {
    const panel = document.getElementById('panel-pianoroll');
    const handle = document.getElementById('pr-resize-handle');
    if (!panel || !handle) return;

    let isDragging = false;
    let startY = 0;
    let startH = 0;

    function beginDrag(clientY) {
        isDragging = true;
        startY = clientY;
        startH = panel.getBoundingClientRect().height;
        document.body.style.cursor = 'ns-resize';
        document.body.style.userSelect = 'none';
    }

    function applyDrag(clientY) {
        if (!isDragging) return;
        // Drag up = bigger (top edge moves up)
        const newH = Math.max(160, Math.min(window.innerHeight - 60, startH - (clientY - startY)));
        panel.style.height = newH + 'px';
        pianoRoll.bodyHeight = newH;
        ensureRenderLoop();
    }

    function endDrag() {
        if (!isDragging) return;
        isDragging = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    }

    handle.addEventListener('mousedown', function(e) { beginDrag(e.clientY); e.preventDefault(); });
    handle.addEventListener('touchstart', function(e) { beginDrag(e.touches[0].clientY); e.preventDefault(); }, { passive: false });
    handle.addEventListener('dblclick', function() {
        panel.style.height = '280px';
        pianoRoll.bodyHeight = 280;
        ensureRenderLoop();
    });

    document.addEventListener('mousemove', function(e) { applyDrag(e.clientY); });
    document.addEventListener('mouseup', endDrag);
    document.addEventListener('touchmove', function(e) { if (isDragging) { applyDrag(e.touches[0].clientY); e.preventDefault(); } }, { passive: false });
    document.addEventListener('touchend', endDrag);
})();

window.addEventListener('resize', () => {
    clampStartTimeToTape();
    refreshDensityMapIfNeeded(false);
    ensureRenderLoop();
});

// Transport Controls
async function dawPlay() {
    try {
        if (!dawState) await dawLoadInitialState();

        const cmd = dawState.is_playing ? 'play_stop' : 'play_start';

        if (dawSocket && dawSocket.connected) {
            dawSocket.emit('transport_cmd', {cmd: cmd}, (ack) => {
                if (ack && ack.status !== 'ok') console.error('Play error:', ack.message);
            });
        } else {
            const endpoint = dawState.is_playing ? '/api/daw/play/stop' : '/api/daw/play/start';
            await fetch(endpoint, { method: 'POST', headers: {'Content-Type': 'application/json'} });
        }
    } catch (e) {
        console.error('Play error:', e);
        alert('Error during playback!');
    }
}

async function dawStop() {
    try {
        if (dawSocket && dawSocket.connected) {
            dawSocket.emit('transport_cmd', {cmd: 'stop_all'}, (ack) => {
                if (ack && ack.status !== 'ok') console.error('Stop error:', ack.message);
            });
        } else {
            await fetch('/api/daw/stop_all', { method: 'POST', headers: {'Content-Type': 'application/json'} });
        }
        console.log('DAW stopped');
    } catch (e) {
        console.error('Stop error:', e);
    }
}

async function dawPanic() {
    try {
        if (dawSocket && dawSocket.connected) {
            dawSocket.emit('transport_cmd', {cmd: 'panic'}, (ack) => {
                if (ack && ack.status !== 'ok') console.error('Panic error:', ack.message);
            });
        } else {
            await fetch('/api/daw/panic', { method: 'POST', headers: {'Content-Type': 'application/json'} });
        }
        console.log('DAW panic sent');
    } catch (e) {
        console.error('Panic error:', e);
    }
}

async function dawRewind() {
    try {
        if (dawSocket && dawSocket.connected) {
            dawSocket.emit('transport_cmd', {cmd: 'rewind'}, (ack) => {
                if (ack && ack.status !== 'ok') console.error('Rewind error:', ack.message);
            });
        } else {
            await fetch('/api/daw/rewind', { method: 'POST', headers: {'Content-Type': 'application/json'} });
        }
    } catch (e) {
        console.error('Rewind error:', e);
    }
}

async function dawSetBPM(bpm) {
    try {
        const res = await fetch('/api/daw/bpm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ bpm: parseInt(bpm) })
        });
        
        const data = await res.json();
        if (data.status !== 'ok') {
            alert(data.message);
            document.getElementById('daw-bpm').value = dawState.bpm;
        }
    } catch (e) {
        console.error('BPM error:', e);
    }
}

async function dawSetTimeSignature(beatsPerMeasure) {
    try {
        const res = await fetch('/api/daw/time_signature', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ beats_per_measure: parseInt(beatsPerMeasure) })
        });
        const data = await res.json();
        if (data.status !== 'ok') {
            throw new Error(data.message || 'Errore time signature');
        }
    } catch (e) {
        console.error('Time signature error:', e);
        // Ripristina il valore attuale dello stato se fallisce
        if (dawState) {
            const select = document.getElementById('daw-ts-select');
            if (select) select.value = String(dawState.beats_per_measure || 4);
        }
        alert('Impossibile impostare il time signature');
    }
}

async function dawToggleMetronome() {
    try {
        const res = await fetch('/api/daw/metronome/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await res.json();
        if (data.status === 'ok') {
            console.log('[DAW] Metronome:', data.enabled);
        }
    } catch (e) {
        console.error('Metronome error:', e);
    }
}

// Track Controls
async function dawToggleArm(channel) {
    try {
        // Previeni doppi touch
        if (armInProgress[channel]) {
            return;
        }
        armInProgress[channel] = true;
        
        if (!dawState) {
            await dawLoadInitialState();
        }
        
        const currentlyArmed = dawState.armed[channel];
        const currentlyRecording = dawState.is_recording;
        
        // Non permettere disarm durante la registrazione
        if (currentlyRecording && currentlyArmed) {
            alert('Stop recording first before disarming tracks');
            armInProgress[channel] = false;
            return;
        }
        
        // Toggle arm state
        const newState = !currentlyArmed;
        
        const response = await fetch(`/api/daw/track/${channel}/arm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ armed: newState })
        });
        
        const data = await response.json();
        
        // Aggiorna lo stato locale
        if (data.status === 'ok') {
            dawState.armed[channel] = data.armed;
            
            // Forza il repaint del DOM (critico per dispositivi touch)
            requestAnimationFrame(() => {
                requestAnimationFrame(() => {
                    dawUpdateUI();
                });
            });
        }
        
        setTimeout(() => {
            armInProgress[channel] = false;
        }, 300);
        
    } catch (e) {
        console.error('Arm error:', e);
        armInProgress[channel] = false;
    }
}

async function dawToggleMute(channel) {
    try {
        // Previeni doppi touch
        if (muteInProgress[channel]) {
            return;
        }
        muteInProgress[channel] = true;
        
        const newState = !dawState.muted[channel];
        
        const response = await fetch(`/api/daw/track/${channel}/mute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ muted: newState })
        });
        
        const data = await response.json();
        
        // Aggiorna lo stato locale
        if (data.status === 'ok') {
            dawState.muted[channel] = data.muted;
            renderTrackTape(channel);
            
            // Forza il repaint del DOM (critico per dispositivi touch)
            requestAnimationFrame(() => {
                requestAnimationFrame(() => {
                    dawUpdateUI();
                });
            });
        }
        
        setTimeout(() => {
            muteInProgress[channel] = false;
        }, 300);
        
    } catch (e) {
        console.error('Mute error:', e);
        muteInProgress[channel] = false;
    }
}

async function dawToggleSolo(channel) {
    try {
        if (soloInProgress[channel]) return;
        soloInProgress[channel] = true;

        const newState = !(dawState.solo && dawState.solo[channel]);

        const response = await fetch(`/api/daw/track/${channel}/solo`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ solo: newState })
        });

        const data = await response.json();
        if (data.status === 'ok') {
            if (!dawState.solo) dawState.solo = {};
            dawState.solo[channel] = data.solo;
            dawState.any_solo = data.any_solo;
            renderTrackTape(channel);
            requestAnimationFrame(() => {
                requestAnimationFrame(() => { dawUpdateUI(); });
            });
        }

        setTimeout(() => { soloInProgress[channel] = false; }, 300);
    } catch (e) {
        console.error('Solo error:', e);
        soloInProgress[channel] = false;
    }
}

async function dawRecord() {
    try {
        if (!dawState) {
            console.log('[DAW] dawState is null, loading initial state...');
            await dawLoadInitialState();
        }

        const currentlyRecording = dawState.is_recording;

        if (!currentlyRecording) {
            console.log('[DAW] Starting global recording...');
            const armedTracks = Object.values(dawState.armed).filter(armed => armed).length;
            if (armedTracks === 0) {
                alert('Please ARM at least one track before recording');
                return;
            }

            // Save current position to restore after recording
            recordStartPosition = dawState.timeline_position || 0;

            if (dawSocket && dawSocket.connected) {
                dawSocket.emit('transport_cmd', {cmd: 'record_start'}, (ack) => {
                    if (ack && ack.status !== 'ok') {
                        console.error('[DAW] record_start error:', ack.message);
                        alert(ack.message);
                    }
                });
            } else {
                const res = await fetch('/api/daw/record/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({})
                });
                const data = await res.json();
                if (data.status !== 'ok') alert(data.message);
            }
        } else {
            console.log('[DAW] Stopping global recording...');
            if (dawSocket && dawSocket.connected) {
                dawSocket.emit('transport_cmd', {cmd: 'record_stop'}, (ack) => {
                    if (ack && ack.status !== 'ok') console.error('[DAW] record_stop error:', ack.message);
                });
            } else {
                await fetch('/api/daw/record/stop', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                });
            }
        }
        console.log('[DAW] REC operation completed');
    } catch (e) {
        console.error('Record error:', e);
    }
}

// --- Selection (drag-to-select on canvas) ---
function dawToggleSelectMode() {
    selectMode = !selectMode;
    const btn = document.getElementById('daw-select-mode');
    if (selectMode) {
        btn.classList.add('daw-tbtn-active');
    } else {
        btn.classList.remove('daw-tbtn-active');
        dawCancelSelection();
    }
    // Change cursor on all canvases
    document.querySelectorAll('canvas[id^="track-canvas-"]').forEach(c => {
        c.style.cursor = selectMode ? 'crosshair' : '';
    });
}

function updateSelectionUI() {
    const bar = document.getElementById('daw-sel-bar');
    if (selectionInBeat !== null && selectionOutBeat !== null) {
        const beatsPerMeasure = dawState ? (dawState.beats_per_measure || 4) : 4;
        const barIn = Math.floor(selectionInBeat / beatsPerMeasure) + 1;
        const barOut = Math.ceil(selectionOutBeat / beatsPerMeasure);
        document.getElementById('daw-sel-ch').textContent = activeControlChannel + 1;
        document.getElementById('daw-sel-range').textContent = `Bar ${barIn} \u2192 ${barOut}  (beat ${selectionInBeat.toFixed(1)} - ${selectionOutBeat.toFixed(1)})`;
        bar.style.display = 'flex';
    } else {
        bar.style.display = 'none';
    }
    renderAllTracks();
}

function dawCancelSelection() {
    selectionInBeat = null;
    selectionOutBeat = null;
    updateSelectionUI();
}

async function dawDeleteSelection() {
    if (selectionInBeat === null || selectionOutBeat === null) return;
    const ch = activeControlChannel;
    try {
        await fetch(`/api/daw/track/${ch}/clear_range`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ start_beat: selectionInBeat, end_beat: selectionOutBeat })
        });
        selectionInBeat = null;
        selectionOutBeat = null;
        updateSelectionUI();
    } catch (e) {
        console.error('Delete selection error:', e);
    }
}

async function dawCopySelection() {
    if (selectionInBeat === null || selectionOutBeat === null) {
        showStatus('No selection to copy', 'warning');
        return;
    }
    try {
        const res = await fetch('/api/daw/copy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: activeControlChannel, start_beat: selectionInBeat, end_beat: selectionOutBeat })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showStatus(`Copied ${data.count} events`, 'success');
        } else {
            showStatus(data.error || 'Copy failed', 'danger');
        }
    } catch (e) {
        console.error('Copy selection error:', e);
    }
}

async function dawPasteAtPlayhead() {
    const bpm = dawState.bpm || 120;
    const currentBeat = (dawState.timeline_position || 0) / (60.0 / bpm);
    try {
        const res = await fetch('/api/daw/paste', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: activeControlChannel, beat: currentBeat })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showStatus(`Pasted ${data.count} events`, 'success');
        } else {
            showStatus(data.error || 'Paste failed', 'danger');
        }
    } catch (e) {
        console.error('Paste error:', e);
    }
}

async function dawClearTrack(channel) {
    if (!confirm(`Clear track ${channel + 1}?`)) return;
    
    try {
        await fetch(`/api/daw/track/${channel}/clear`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        // WebSocket aggiornerà automaticamente il UI
    } catch (e) {
        console.error('Clear track error:', e);
    }
}

async function dawClearAll() {
    if (!confirm('Clear ALL tracks? This cannot be undone!')) return;
    
    try {
        await fetch('/api/daw/clear_all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        dawSetSessionName(null);  // Reset session name → nuovo progetto
        // WebSocket aggiornerà automaticamente il UI
    } catch (e) {
        console.error('Clear all error:', e);
    }
}

// ── Duplicate / Rename / Color ────────────────────────────────
async function dawDuplicateTrack() {
    const src = activeControlChannel;
    // Build channel selector options
    let options = '';
    for (let ch = 0; ch < 16; ch++) {
        if (ch === src) continue;
        const hasData = dawState.has_data && dawState.has_data[ch];
        const name = (dawState.track_names && dawState.track_names[ch]) || `Track ${ch+1}`;
        const label = `Ch ${ch+1} - ${name}${hasData ? ' [has data]' : ' [empty]'}`;
        options += `<option value="${ch}">${label}</option>`;
    }
    // Create modal for channel selection
    const modal = document.createElement('div');
    modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:9999';
    modal.innerHTML = `
        <div style="background:#2b2b2b;border-radius:8px;padding:20px;min-width:300px;color:#fff">
            <h5 style="margin-top:0">Duplicate Track ${src+1}</h5>
            <p style="font-size:0.9em;color:#aaa">Events will be copied. Instrument on destination stays unchanged.</p>
            <label>Destination channel:</label>
            <select id="dup-dest-select" class="form-select form-select-sm mt-1 mb-3" style="background:#1e1e1e;color:#fff;border-color:#555">${options}</select>
            <div class="d-flex justify-content-end gap-2">
                <button class="btn btn-sm btn-secondary" id="dup-cancel-btn">Cancel</button>
                <button class="btn btn-sm btn-primary" id="dup-ok-btn">Duplicate</button>
            </div>
        </div>`;
    document.body.appendChild(modal);

    document.getElementById('dup-cancel-btn').onclick = () => modal.remove();
    document.getElementById('dup-ok-btn').onclick = async () => {
        const dest = parseInt(document.getElementById('dup-dest-select').value);
        const hasData = dawState.has_data && dawState.has_data[dest];
        if (hasData) {
            if (!confirm(`Channel ${dest+1} already has data. Events will be replaced. Continue?`)) return;
        }
        modal.remove();
        try {
            const res = await fetch(`/api/daw/track/${src}/duplicate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ dest_channel: dest })
            });
            const data = await res.json();
            if (data.status === 'ok') {
                showStatus(`Track ${src+1} duplicated to ${dest+1}`, 'success');
            } else {
                showStatus(data.message || 'Cannot duplicate', 'danger');
            }
        } catch (e) {
            console.error('Duplicate track error:', e);
        }
    };
}

async function dawRenameTrackPrompt() {
    const ch = activeControlChannel;
    const current = (dawState.track_names && dawState.track_names[ch]) || `Track ${ch+1}`;
    const name = prompt(`Rename Track ${ch+1}:`, current);
    if (name === null || !name.trim()) return;
    try {
        await fetch(`/api/daw/track/${ch}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim() })
        });
    } catch (e) {
        console.error('Rename track error:', e);
    }
}

async function dawCycleTrackColor(ch) {
    const current = (dawState.track_colors && dawState.track_colors[ch]) || ch;
    const next = (current + 1) % 16;
    try {
        await fetch(`/api/daw/track/${ch}/color`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ color_index: next })
        });
    } catch (e) {
        console.error('Set track color error:', e);
    }
}

const TRACK_PALETTE = [
    '#00d4ff', '#ff4444', '#44ff44', '#ffcc00',
    '#ff66ff', '#ff8844', '#44ffff', '#88ff44',
    '#ff4488', '#4488ff', '#ffff44', '#aa44ff',
    '#44ffaa', '#ff8888', '#88ccff', '#cccccc'
];

function getTrackColor(ch) {
    const idx = (dawState.track_colors && dawState.track_colors[ch] !== undefined) ? dawState.track_colors[ch] : ch;
    return TRACK_PALETTE[idx % 16];
}

// ── Quantizzazione post-registrazione ─────────────────────────
function dawToggleQuantBar() {
    const bar = document.getElementById('daw-quant-bar');
    const btn = document.getElementById('daw-quant-toggle');
    const visible = bar.style.display !== 'none';
    bar.style.display = visible ? 'none' : 'flex';
    if (btn) btn.classList.toggle('daw-tbtn-active', !visible);
}

async function dawQuantize() {
    const grid     = parseFloat(document.getElementById('quant-grid').value);
    const strength = parseInt(document.getElementById('quant-strength').value, 10);
    const swing    = parseInt(document.getElementById('quant-swing').value, 10);
    const chVal    = document.getElementById('quant-channel').value;
    const channels = (chVal === 'all') ? [] : [parseInt(chVal, 10)];

    const btn = document.getElementById('quant-apply-btn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

    try {
        const res = await fetch('/api/daw/quantize', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ grid, strength, swing, channels })
        });
        const data = await res.json();

        const statusDiv  = document.getElementById('daw-status');
        const statusText = document.getElementById('daw-status-text');

        if (data.status === 'ok') {
            if (statusDiv && statusText) {
                statusDiv.style.display = 'block';
                statusDiv.className = 'daw-status-bar daw-status-playing text-center';
                statusText.innerHTML =
                    `<i class="fas fa-magic"></i> ${data.message || 'Quantizzazione applicata'}`;
                setTimeout(() => { statusDiv.style.display = 'none'; }, 2500);
            }
        } else {
            alert('Quantize: ' + (data.message || 'Errore'));
        }
    } catch (e) {
        console.error('Quantize error:', e);
        alert('Errore di rete durante la quantizzazione.');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-bolt"></i> APPLY';
    }
}

async function dawTransposeActive(semitones) {
    if (!dawState || dawState.is_playing || dawState.is_recording) {
        showStatus('Stop playback/recording before transpose', 'warning');
        return;
    }

    const payload = {
        channel: activeControlChannel,
        semitones: semitones
    };

    if (selectionInBeat !== null && selectionOutBeat !== null && selectionOutBeat > selectionInBeat) {
        payload.start_beat = selectionInBeat;
        payload.end_beat = selectionOutBeat;
    }

    try {
        const res = await fetch('/api/daw/transpose', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.status === 'ok') {
            const scope = payload.start_beat != null ? 'selection' : 'track';
            showStatus(`Transpose ${semitones > 0 ? '+' : ''}${semitones} (${scope}) · ${data.modified} events`, 'success');
            await refreshDensityMap(true);
        } else {
            showStatus(data.message || 'Transpose failed', 'danger');
        }
    } catch (e) {
        console.error('Transpose error:', e);
        showStatus('Transpose network error', 'danger');
    }
}

// ── Undo / Redo ───────────────────────────────────────────────
async function dawUndo() {
    try {
        if (dawSocket && dawSocket.connected) {
            dawSocket.emit('transport_cmd', {cmd: 'undo'}, (ack) => {
                if (ack && ack.status !== 'ok') console.warn('Undo:', ack.message);
            });
        } else {
            await fetch('/api/daw/undo', { method: 'POST', headers: {'Content-Type': 'application/json'} });
        }
    } catch (e) { console.error('Undo error:', e); }
}

async function dawRedo() {
    try {
        if (dawSocket && dawSocket.connected) {
            dawSocket.emit('transport_cmd', {cmd: 'redo'}, (ack) => {
                if (ack && ack.status !== 'ok') console.warn('Redo:', ack.message);
            });
        } else {
            await fetch('/api/daw/redo', { method: 'POST', headers: {'Content-Type': 'application/json'} });
        }
    } catch (e) { console.error('Redo error:', e); }
}

// ── Loop ──────────────────────────────────────────────────────
async function dawToggleLoop() {
    try {
        if (dawSocket && dawSocket.connected) {
            dawSocket.emit('transport_cmd', {cmd: 'toggle_loop'}, (ack) => {
                if (ack && ack.status !== 'ok') console.warn('Loop toggle:', ack.message);
            });
        } else {
            await fetch('/api/daw/loop/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'} });
        }
    } catch (e) { console.error('Loop toggle error:', e); }
}

async function dawSetLoopFromSelection() {
    if (selectionInBeat === null || selectionOutBeat === null || selectionOutBeat <= selectionInBeat) {
        alert('Select a range on the timeline first');
        return;
    }
    try {
        const res = await fetch('/api/daw/loop_points', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ start_beat: selectionInBeat, end_beat: selectionOutBeat })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            if (dawState) {
                dawState.loop_start = selectionInBeat;
                dawState.loop_end = selectionOutBeat;
            }
            renderAllTracks();
        }
    } catch (e) { console.error('Set loop points error:', e); }
}

// ── Status toast ──────────────────────────────────────────────
let _statusTimer = null;
function showStatus(msg, type) {
    const statusDiv = document.getElementById('daw-status');
    const statusText = document.getElementById('daw-status-text');
    if (!statusDiv || !statusText) return;
    if (dawState.is_playing || dawState.is_recording) return; // don't overwrite transport status
    clearTimeout(_statusTimer);
    const icon = type === 'success' ? 'fa-check-circle' : type === 'danger' ? 'fa-exclamation-circle' : 'fa-info-circle';
    statusDiv.style.display = 'block';
    statusDiv.className = 'daw-status-bar text-center';
    statusText.innerHTML = `<i class="fas ${icon}"></i> ${msg}`;
    _statusTimer = setTimeout(() => { statusDiv.style.display = 'none'; }, 2000);
}

// ── Keyboard shortcuts ────────────────────────────────────────
document.addEventListener('keydown', (e) => {
    // Skip when typing in inputs
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;

    if (e.ctrlKey && e.key === 'z') { e.preventDefault(); dawUndo(); }
    if (e.ctrlKey && e.key === 'y') { e.preventDefault(); dawRedo(); }
    if (e.ctrlKey && e.key === 's') { e.preventDefault(); dawQuickSaveMidi(); }
    if (e.ctrlKey && e.key === 'c') { e.preventDefault(); dawCopySelection(); }
    if (e.ctrlKey && e.key === 'v') { e.preventDefault(); dawPasteAtPlayhead(); }
    if (e.altKey && e.shiftKey && e.key === 'ArrowUp') { e.preventDefault(); dawTransposeActive(12); }
    if (e.altKey && e.shiftKey && e.key === 'ArrowDown') { e.preventDefault(); dawTransposeActive(-12); }
    if (e.altKey && e.key === 'ArrowUp') { e.preventDefault(); dawTransposeActive(1); }
    if (e.altKey && e.key === 'ArrowDown') { e.preventDefault(); dawTransposeActive(-1); }
    if (e.key === ' ') { e.preventDefault(); dawPlay(); }
    if (e.key === 'l' || e.key === 'L') { dawToggleLoop(); }
    if (e.key === 'Escape' && noteEditor.selected.size > 0) {
        noteEditor.selected.clear();
        ensureRenderLoop();
    }
});


        // Clean event listeners
        window.addEventListener('load', () => {
             setupMixer();           // Populate instrument dropdowns
             setupChCellHandlers();  // Long-press handlers for CH cells
             initTrackSizeSystem();  // Track size modes (compact/medium/large)
             initDAW();              // Initialize DAW on page load
             initRecorderResize();   // Drag-to-resize recorder area
        });

        function initRecorderResize() {
            const handle = document.getElementById('daw-resize-handle');
            const main   = document.querySelector('.daw-main');
            if (!handle || !main) return;

            let isDragging = false;
            let startY = 0;
            let startH = 0;

            function applyHeight(h) {
                main.style.setProperty('flex', `0 0 ${h}px`, 'important');
                main.style.setProperty('height', `${h}px`, 'important');
            }

            function resetHeight() {
                main.style.removeProperty('flex');
                main.style.removeProperty('height');
            }

            // Mouse
            handle.addEventListener('mousedown', (e) => {
                isDragging = true;
                startY = e.clientY;
                startH = main.getBoundingClientRect().height;
                document.body.style.cursor = 'ns-resize';
                document.body.style.userSelect = 'none';
                e.preventDefault();
            });
            document.addEventListener('mousemove', (e) => {
                if (!isDragging) return;
                applyHeight(Math.max(60, startH + (e.clientY - startY)));
            });
            document.addEventListener('mouseup', () => {
                if (!isDragging) return;
                isDragging = false;
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            });

            // Touch
            handle.addEventListener('touchstart', (e) => {
                isDragging = true;
                startY = e.touches[0].clientY;
                startH = main.getBoundingClientRect().height;
                e.preventDefault();
            }, { passive: false });
            document.addEventListener('touchmove', (e) => {
                if (!isDragging) return;
                applyHeight(Math.max(60, startH + (e.touches[0].clientY - startY)));
            }, { passive: false });
            document.addEventListener('touchend', () => { isDragging = false; });

            // Double-click → reset to auto
            handle.addEventListener('dblclick', () => {
                resetHeight();
            });
        }

// ═══════════════════════════════════════════════════════════════
// ui-shell: ponte verso stato unico e Libreria (nessun comando musicale)
// ═══════════════════════════════════════════════════════════════
(function () {
    const MS = window.MatSynth;

    // CC cambiati da un'altra vista (pannello Suona): riallinea mappa e slider legacy.
    // Gli eventi 'legacy' sono i nostri stessi cambi (updateCC): ignorati per evitare l'eco.
    MS.state.subscribe('parts', (value, domain, meta) => {
        if (!meta || meta.origin === 'legacy' || meta.part_id === undefined) return;
        const part = meta.part_id, cc = meta.cc, val = meta.value;
        if (!channelCC[part]) channelCC[part] = {};
        channelCC[part][cc] = val;
        if (cc === 7) {
            const iv = document.getElementById('track-vol-' + part);
            if (iv) { iv.value = val; const l = document.getElementById('track-vol-val-' + part); if (l) l.textContent = val; }
        } else if (cc === 10) {
            const ip = document.getElementById('track-pan-' + part);
            if (ip) { ip.value = val; updateInlinePanLabel(part, val); }
        }
        if (part !== activeControlChannel) return;
        const slider = document.getElementById(cc === 7 ? 'vol-slider' : cc === 10 ? 'pan-slider' : 'cc-slider-' + cc);
        if (slider) slider.value = val;
        const badge = document.getElementById('val-' + cc);
        if (badge) { if (cc === 10) updatePanLabel(val); else badge.textContent = val; }
    });

    // Funzioni gia' presenti nel menu File, raggiungibili anche dalla Libreria (la finestra si chiude prima:
    // i modali legacy stanno sopra e non devono competere con il focus del pannello).
    function actionButton(box, label, icon, fn) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'ms-btn ms-btn-secondary';
        b.innerHTML = '<i class="fas ' + icon + '" aria-hidden="true"></i> ';
        b.appendChild(document.createTextNode(label));
        b.addEventListener('click', () => { MS.shell.closePanel(); fn(); });
        box.appendChild(b);
    }
    function note(box, text) {
        const p = document.createElement('p');
        p.className = 'ms-note';
        p.textContent = text;
        box.appendChild(p);
    }

    MS.library.registerSection('progetti', {
        title: 'Progetti',
        render(box) {
            note(box, 'I progetti nativi arriveranno con la fase Studio. Oggi la sessione si salva come file MIDI.');
            const row = document.createElement('div'); row.className = 'ms-actions'; box.appendChild(row);
            actionButton(row, 'Nuovo progetto', 'fa-file', () => dawNewProject());
            actionButton(row, 'Salva', 'fa-save', () => dawQuickSaveMidi());
            actionButton(row, 'Salva con nome', 'fa-clone', () => dawSaveAsMidi());
        }
    });

    MS.library.registerSection('midi', {
        title: 'MIDI',
        render(box) {
            note(box, 'File MIDI salvati sul Pi: apri, importa (sostituisci o aggiungi), scarica, elimina.');
            const row = document.createElement('div'); row.className = 'ms-actions'; box.appendChild(row);
            actionButton(row, 'Gestisci file MIDI', 'fa-folder-open', () => dawOpenMidiManager());
            actionButton(row, 'Importa da PC', 'fa-upload', () => document.getElementById('midi-file-input').click());
        }
    });
})();
