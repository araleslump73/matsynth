/* MatSynth - units.js
 * Unita' musicali e tabella snap UNICA (contracts.md 1.2-1.3). Nessun altro file
 * deve ridefinire etichette o conversioni: le select snap si riempiono da qui (A12).
 * Modulo puro, senza dipendenze: gira anche sotto Node per i test (tests/js).
 * Perche' puro e senza DOM obbligatorio: costo zero sul Pi e testabile senza browser. */
(function (root, factory) {
    'use strict';
    var units = factory();
    if (typeof module === 'object' && module.exports) module.exports = units;
    if (typeof window !== 'undefined' && root === window) {
        root.MatSynth = root.MatSynth || {};
        root.MatSynth.units = units;
    }
})(typeof window !== 'undefined' ? window : this, function () {
    'use strict';

    // Valori in beat (1 beat = 1 semiminima), etichette in valori di nota.
    // Congelata: nessuno deve poterla alterare a runtime.
    var SNAP_OPTIONS = Object.freeze([
        Object.freeze({ value: 0, label: 'Libero' }),
        Object.freeze({ value: 1, label: '1/4' }),
        Object.freeze({ value: 0.5, label: '1/8' }),
        Object.freeze({ value: 0.25, label: '1/16' }),
        Object.freeze({ value: 0.125, label: '1/32' }),
        Object.freeze({ value: 0.0625, label: '1/64' })
    ]);

    // Griglie di quantizzazione valide (app.py:daw_quantize): le stesse senza "Libero".
    var QUANT_OPTIONS = Object.freeze(SNAP_OPTIONS.filter(function (o) { return o.value > 0; }));

    function snapLabel(value) {
        var v = Number(value);
        for (var i = 0; i < SNAP_OPTIONS.length; i++) {
            if (SNAP_OPTIONS[i].value === v) return SNAP_OPTIONS[i].label;
        }
        return null;
    }

    // BPM non valido -> NaN (non 0): un calcolo errato non deve passare per "tempo zero".
    function validBpm(bpm) {
        var b = Number(bpm);
        return isFinite(b) && b > 0 ? b : NaN;
    }

    function beatsToSeconds(beats, bpm) {
        var b = validBpm(bpm);
        return Number(beats) * 60 / b;
    }

    function secondsToBeats(seconds, bpm) {
        var b = validBpm(bpm);
        return Number(seconds) * b / 60;
    }

    // battuta.beat.sedicesimo (tutti da 1), contracts.md 1.2.
    // L'epsilon evita che 0.75 espresso come 0.7499999999 mostri il sedicesimo sbagliato.
    function beatsToBBT(beats, beatsPerMeasure) {
        var bpm = Number(beatsPerMeasure);
        if (!(bpm >= 1)) bpm = 4;
        var b = Number(beats);
        if (!isFinite(b) || b < 0) b = 0;
        b += 1e-9;
        var bar = Math.floor(b / bpm) + 1;
        var inBar = b - (bar - 1) * bpm;
        var beat = Math.floor(inBar) + 1;
        var sixteenth = Math.floor((inBar - Math.floor(inBar)) * 4) + 1;
        return { bar: bar, beat: beat, sixteenth: sixteenth, text: bar + '.' + beat + '.' + sixteenth };
    }

    // "MM:SS.d" come il display legacy (#daw-timeline).
    function formatTime(seconds) {
        var s = Number(seconds);
        if (!isFinite(s) || s < 0) s = 0;
        var m = Math.floor(s / 60);
        var rest = s - m * 60;
        var whole = Math.floor(rest);
        var tenth = Math.floor((rest - whole) * 10);
        return (m < 10 ? '0' : '') + m + ':' + (whole < 10 ? '0' : '') + whole + '.' + tenth;
    }

    // Riempie una <select> con la tabella unica. I value restano stringhe identiche a
    // quelle storiche ("0", "1", "0.5", ...) cosi' il codice legacy che legge .value non cambia.
    function fillSnapSelect(select, selectedValue, options) {
        if (!select) return;
        var list = options || SNAP_OPTIONS;
        select.textContent = '';
        for (var i = 0; i < list.length; i++) {
            var opt = select.ownerDocument.createElement('option');
            opt.value = String(list[i].value);
            opt.textContent = list[i].label;
            if (list[i].value === Number(selectedValue)) opt.selected = true;
            select.appendChild(opt);
        }
    }

    return {
        SNAP_OPTIONS: SNAP_OPTIONS,
        QUANT_OPTIONS: QUANT_OPTIONS,
        snapLabel: snapLabel,
        beatsToSeconds: beatsToSeconds,
        secondsToBeats: secondsToBeats,
        beatsToBBT: beatsToBBT,
        formatTime: formatTime,
        fillSnapSelect: fillSnapSelect
    };
});
