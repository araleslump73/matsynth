'use strict';
// Punto d'ingresso per `node --test tests/js`: con Node 22 la cartella viene risolta come modulo (index.js),
// con Node <= 20 viene scandita ricorsivamente per *.test.js (index.js non corrisponde ai pattern: nessun doppione).
const fs = require('node:fs');
fs.readdirSync(__dirname).filter(f => f.endsWith('.test.js')).sort().forEach(f => require('./' + f));
