/* MatSynth - library.js
 * Finestra Libreria con sezioni registrabili (Scene / Progetti / MIDI / SoundFont, piano sez. 4).
 *   library.registerSection(id, {title, render(container), onShow?})
 * Una sezione registrata con lo stesso id sostituisce la precedente: le fasi ui-live (Scene, SoundFont)
 * e ui-studio (Progetti, MIDI) rimpiazzano i segnaposto senza toccare questo file.
 * Il contenitore (#ms-library-tabs / #ms-library-body) e' creato dalla shell; la finestra (apertura, focus,
 * chiusura) e' di shell.openPanel. Nessuna richiesta di rete partirebbe da qui: apertura = solo UI. */
(function (root, factory) {
    'use strict';
    var mod = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = mod;
    if (typeof window !== 'undefined' && root === window) {
        root.MatSynth = root.MatSynth || {};
        root.MatSynth.library = mod;
    }
})(typeof window !== 'undefined' ? window : this, function (root) {
    'use strict';

    var sections = [];          // [{id, title, render, onShow}] in ordine di registrazione
    var active = null;          // id sezione mostrata
    var mounted = null;         // {tabs, body}
    var rendered = {};          // id -> true se render() gia' eseguito in questo container

    function find(id) {
        for (var i = 0; i < sections.length; i++) if (sections[i].id === id) return i;
        return -1;
    }

    function registerSection(id, def) {
        if (typeof id !== 'string' || !id) throw new Error('library.registerSection: id mancante');
        if (!def || typeof def.render !== 'function') throw new Error('library.registerSection: render() mancante');
        var entry = { id: id, title: def.title || id, render: def.render, onShow: def.onShow || null };
        var i = find(id);
        if (i >= 0) sections[i] = entry; else sections.push(entry);
        delete rendered[id];                       // nuova definizione: da ridisegnare alla prossima apertura
        if (mounted) buildTabs();
        if (mounted && active === id) show(id);
    }

    function listSections() { return sections.map(function (s) { return { id: s.id, title: s.title }; }); }

    function buildTabs() {
        var tabs = mounted.tabs;
        tabs.textContent = '';
        sections.forEach(function (s) {
            var b = root.document.createElement('button');
            b.type = 'button';
            b.className = 'ms-tab';
            b.id = 'ms-lib-tab-' + s.id;
            b.setAttribute('role', 'tab');
            b.setAttribute('aria-controls', 'ms-library-body');
            b.setAttribute('aria-selected', s.id === active ? 'true' : 'false');
            b.tabIndex = s.id === active ? 0 : -1;
            b.dataset.section = s.id;
            b.textContent = s.title;
            tabs.appendChild(b);
        });
    }

    function show(id) {
        var i = find(id);
        if (i < 0) i = 0;
        if (!sections.length || !mounted) return;
        var s = sections[i];
        active = s.id;
        buildTabs();
        var body = mounted.body;
        body.setAttribute('aria-labelledby', 'ms-lib-tab-' + s.id);
        body.textContent = '';
        s.render(body);                            // si ridisegna a ogni apertura: dati sempre freschi
        rendered[s.id] = true;
        if (s.onShow) { try { s.onShow(body); } catch (e) { console.error('[library] onShow', e); } }
    }

    function mount(tabsEl, bodyEl) {
        mounted = { tabs: tabsEl, body: bodyEl };
        tabsEl.addEventListener('click', function (e) {
            var t = e.target.closest ? e.target.closest('.ms-tab') : null;
            if (t) show(t.dataset.section);
        });
        tabsEl.addEventListener('keydown', function (e) {
            if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
            var i = find(active);
            var n = sections.length;
            if (!n) return;
            i = (i + (e.key === 'ArrowRight' ? 1 : n - 1)) % n;
            show(sections[i].id);
            var el = root.document.getElementById('ms-lib-tab-' + sections[i].id);
            if (el) el.focus();
            e.preventDefault();
        });
        buildTabs();
    }

    return { registerSection: registerSection, listSections: listSections, show: show, mount: mount,
             activeSection: function () { return active; } };
});
