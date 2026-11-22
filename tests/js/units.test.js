'use strict';
// Test unitari di units.js (contracts.md 1.2-1.3). Esecuzione: node --test tests/js
const test = require('node:test');
const assert = require('node:assert/strict');
const units = require('../../home/matteo/matsynth_web/static/js/units.js');

test('SNAP_OPTIONS coincide con la tabella 1.3 (A12)', () => {
    assert.deepEqual(units.SNAP_OPTIONS.map(o => [o.value, o.label]), [
        [0, 'Libero'], [1, '1/4'], [0.5, '1/8'], [0.25, '1/16'], [0.125, '1/32'], [0.0625, '1/64']
    ]);
});

test('SNAP_OPTIONS non contiene le vecchie etichette ambigue ed e immutabile', () => {
    const labels = units.SNAP_OPTIONS.map(o => o.label);
    assert.ok(!labels.includes('1/2'), 'la vecchia etichetta "1/2" (0.5 beat) non deve esistere');
    assert.ok(!labels.includes('1'), 'la vecchia etichetta "1" non deve esistere');
    assert.throws(() => units.SNAP_OPTIONS.push({ value: 9, label: 'x' }));
    assert.throws(() => { units.SNAP_OPTIONS[1].label = 'x'; });
});

test('QUANT_OPTIONS = griglie valide di daw_quantize', () => {
    assert.deepEqual(units.QUANT_OPTIONS.map(o => o.value), [1, 0.5, 0.25, 0.125, 0.0625]);
});

test('snapLabel', () => {
    assert.equal(units.snapLabel(0.5), '1/8');
    assert.equal(units.snapLabel('0.25'), '1/16');
    assert.equal(units.snapLabel(0), 'Libero');
    assert.equal(units.snapLabel(0.3), null);
});

test('beatsToBBT 4/4', () => {
    assert.equal(units.beatsToBBT(0, 4).text, '1.1.1');
    assert.equal(units.beatsToBBT(0.25, 4).text, '1.1.2');
    assert.equal(units.beatsToBBT(1, 4).text, '1.2.1');
    assert.equal(units.beatsToBBT(3.75, 4).text, '1.4.4');
    assert.equal(units.beatsToBBT(4, 4).text, '2.1.1');
    const r = units.beatsToBBT(7.75, 4);
    assert.deepEqual([r.bar, r.beat, r.sixteenth], [2, 4, 4]);
});

test('beatsToBBT 3/4 e robustezza', () => {
    assert.equal(units.beatsToBBT(3, 3).text, '2.1.1');
    assert.equal(units.beatsToBBT(5.5, 3).text, '2.3.3');
    assert.equal(units.beatsToBBT(-1, 4).text, '1.1.1');
    assert.equal(units.beatsToBBT(NaN, 4).text, '1.1.1');
    assert.equal(units.beatsToBBT(4, 0).text, '2.1.1');                // metro non valido -> 4
    assert.equal(units.beatsToBBT(0.7499999999999, 4).text, '1.1.4');  // errore di arrotondamento float
});

test('conversioni beat <-> secondi', () => {
    assert.equal(units.beatsToSeconds(4, 120), 2);
    assert.equal(units.secondsToBeats(2, 120), 4);
    assert.equal(units.beatsToSeconds(1, 60), 1);
    assert.ok(Number.isNaN(units.beatsToSeconds(1, 0)));
    assert.ok(Number.isNaN(units.secondsToBeats(1, -5)));
    assert.ok(Number.isNaN(units.beatsToSeconds(1, 'x')));
    const b = 13.37;
    assert.ok(Math.abs(units.secondsToBeats(units.beatsToSeconds(b, 97), 97) - b) < 1e-12);
});

test('formatTime come il display legacy MM:SS.d', () => {
    assert.equal(units.formatTime(0), '00:00.0');
    assert.equal(units.formatTime(65.37), '01:05.3');
    assert.equal(units.formatTime(-3), '00:00.0');
    assert.equal(units.formatTime(NaN), '00:00.0');
    assert.equal(units.formatTime(600), '10:00.0');
});

test('fillSnapSelect: value stringhe storiche e selezione', () => {
    const doc = { createElement: () => ({ selected: false }) };
    const select = { ownerDocument: doc, textContent: 'x', children: [], appendChild(o) { this.children.push(o); } };
    units.fillSnapSelect(select, 0.125);
    assert.deepEqual(select.children.map(o => o.value), ['0', '1', '0.5', '0.25', '0.125', '0.0625']);
    assert.deepEqual(select.children.map(o => o.selected), [false, false, false, false, true, false]);
    assert.doesNotThrow(() => units.fillSnapSelect(null, 0));
});
