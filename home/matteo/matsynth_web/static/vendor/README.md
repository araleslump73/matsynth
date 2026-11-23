# Asset di terze parti (`static/vendor/`)

Servono perché MatSynth funziona offline (hotspot del dispositivo, nessun accesso a Internet): niente CDN né Google Fonts.
Le stesse versioni che la pagina caricava prima da CDN. File copiati senza modifiche dal pacchetto npm ufficiale.

| Componente | Versione | Licenza | File in questa cartella | Provenienza |
| --- | --- | --- | --- | --- |
| Bootstrap | 5.3.0 | MIT (`bootstrap/LICENSE`) | `bootstrap/css/bootstrap.min.css`, `bootstrap/js/bootstrap.bundle.min.js` | pacchetto npm `bootstrap@5.3.0`, `dist/` (https://registry.npmjs.org/bootstrap/-/bootstrap-5.3.0.tgz) |
| Font Awesome Free | 6.4.0 | icone CC BY 4.0, font OFL 1.1, codice MIT (`fontawesome/LICENSE.txt`) | `fontawesome/css/fontawesome.min.css`, `fontawesome/css/solid.min.css`, `fontawesome/webfonts/fa-solid-900.woff2` | pacchetto npm `@fortawesome/fontawesome-free@6.4.0` (https://registry.npmjs.org/@fortawesome/fontawesome-free/-/fontawesome-free-6.4.0.tgz) |
| Socket.IO client | 4.5.4 | MIT (`socket.io/LICENSE`) | `socket.io/socket.io.min.js` | pacchetto npm `socket.io-client@4.5.4`, `dist/` (https://registry.npmjs.org/socket.io-client/-/socket.io-client-4.5.4.tgz) |

## Verifica di integrità

I tre tarball sono stati scaricati in una cartella temporanea e il loro SHA-512 coincide con `dist.integrity` del registro npm:

- `bootstrap-5.3.0.tgz`: `sha512-UnBV3E3v4STVNQdms6jSGO2CvOkjUMdDAVR2V5N4uCMdaIkaQjbcEAMqRimDHIs4uqBYzDAKCQwCB+97tJgHQw==`
- `fontawesome-free-6.4.0.tgz`: `sha512-0NyytTlPJwB/BF5LtRV8rrABDbe3TdTXqNB3PdZ+UUUZAEIrdOJdmABqKjt4AXwIoJNaRVVZEXxpNrqvE1GAYQ==`
- `socket.io-client-4.5.4.tgz`: `sha512-ZpKteoA06RzkD32IbqILZ+Cnst4xewU7ZYK12aS1mzHftFFjpoMz69IuhP/nL25pJfao/amoPI527KnuhFm01g==`

SHA-256 dei file copiati qui:

```
7f1d37f0d90b6385354c2ac10e2bb91563c46bd7a266ed351222ebcac8496c2a  bootstrap/css/bootstrap.min.css
aa53d582f97eb594c2a5cc5824574707f9ba9837bce3046bfa5f3556860f4e04  bootstrap/js/bootstrap.bundle.min.js
3dc869c82a722d9fd7c7d881a453ee3d269d461917c7a27901ad357d9dcbbfc4  fontawesome/css/fontawesome.min.css
b43dcc895ec8fa778047b69062f1920729246b946fba0c34cddd15e558a801e3  fontawesome/css/solid.min.css
7152a6933ee3d690ec2af3d09da9d701723d16aa3410a6d80f28ff8866f3b880  fontawesome/webfonts/fa-solid-900.woff2
18a36a927dac54650b18b903f8f8778219e02e13946e581d9b3e1e4995f7435b  socket.io/socket.io.min.js
```

## Scelte per risparmiare spazio sul Pi

- Font Awesome: solo `fontawesome.min.css` + `solid.min.css` + `fa-solid-900.woff2` (l'interfaccia usa solo icone `fas`). Non ci sono i font `regular`/`brands` né `.ttf`/`.svg`; `solid.min.css` cita ancora un `.ttf` come ripiego, che i browser moderni non richiedono.
- I file `.min.*` terminano con un commento `sourceMappingURL` verso `.map` non inclusi: il browser li richiede solo con gli strumenti di sviluppo aperti.
- `.gitattributes` in questa cartella disattiva la conversione dei fine riga, così gli hash restano verificabili.
- Totale: circa 600 KB.

## Aggiornare un componente

Scaricare il tarball npm della nuova versione in una cartella temporanea, confrontare lo SHA-512 con `npm view <pacchetto>@<versione> dist.integrity`, copiare i soli file elencati sopra e aggiornare questa tabella.
