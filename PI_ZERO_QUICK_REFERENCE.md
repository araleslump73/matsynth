# MatSynth - Quick Reference per Raspberry Pi Zero 2W

## 🚀 Setup Iniziale (Una volta sola)

```bash
# 1. Clona repository
cd /home/matteo
git clone https://github.com/araleslump73/matsynth.git
cd matsynth

# 2. Ottimizza sistema per Pi Zero 2W
sudo ./setup_pi_zero_optimizations.sh

# 3. Riavvia
sudo reboot

# 4. Installa dipendenze
pip install -r requirements.txt

# 5. Avvia MatSynth
sudo systemctl start matsynth.service
```

## 📊 Monitoraggio

```bash
# Performance in tempo reale
./monitor_performance.sh

# Memoria
free -h

# Temperatura
vcgencmd measure_temp

# Processi MatSynth
ps aux | grep -E "fluidsynth|python3" | grep -v grep
```

## 🎛️ Parametri Chiave Ottimizzati

| Parametro | Valore | Perché |
|-----------|--------|--------|
| CPU cores | 2 | Lascia 2 core per OS |
| Buffer size | 128 | Bilanciamento CPU/latenza |
| Polyphony | 64 | Limita uso RAM/CPU |
| Reverb room | 0.6 | Leggero (era 0.9) |
| Chorus voices | 1 | Dimezza CPU (era 2) |

**Latenza risultante**: ~3ms @ 44.1kHz

## 💾 Soundfont Raccomandati

| Tipo | Dimensione | Prestazioni | Tempo Caricamento |
|------|------------|-------------|-------------------|
| SF3 | < 30MB | ✅ Ottimo | 2-3 secondi |
| SF3 | 30-50MB | ✅ Buono | 3-5 secondi |
| SF2 | < 100MB | ⚠️ OK | 5-15 secondi |
| SF2 | > 100MB | ❌ Evitare | Crash/Timeout |

**Regola d'oro**: Usa sempre SF3 su Pi Zero 2W

## ⚙️ Comandi Utili

```bash
# Riavvia MatSynth
sudo systemctl restart matsynth.service

# Status servizio
sudo systemctl status matsynth.service

# Log in tempo reale
sudo journalctl -u matsynth.service -f

# Test swap
sudo swapon --show

# CPU governor
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

# Frequenza CPU
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq

# Lista soundfont
ls -lh /usr/share/sounds/sf2/
```

## 🌡️ Temperature Target

| Temperatura | Status | Azione |
|-------------|--------|--------|
| < 60°C | 🟢 Perfetto | Nessuna |
| 60-70°C | 🟡 Buono | Monitorare |
| 70-80°C | 🟠 Caldo | Aggiungi dissipatore |
| > 80°C | 🔴 Critico | Throttling attivo! |

## 🔧 Troubleshooting Rapido

### Problema: Out of Memory
```bash
# Verifica swap
free -h

# Se swap < 512MB
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile  # CONF_SWAPSIZE=512
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

### Problema: Audio Crackles
```bash
# Aumenta buffer in startfluid.sh
-z 256  # invece di 128

# Verifica temperatura
vcgencmd measure_temp

# Se > 75°C, riduce performance
```

### Problema: Web Interface Lenta
```bash
# Verifica threaded mode in app.py
app.run(threaded=True)  # Deve essere True

# Restart
sudo systemctl restart matsynth.service
```

### Problema: Soundfont Non Si Carica
```bash
# Verifica dimensioni
ls -lh /usr/share/sounds/sf2/

# Se SF2 > 100MB, converti in SF3 o usa uno più piccolo

# Aumenta timeout in index.html (riga ~438)
setTimeout(() => location.reload(); }, 10000);  # 10s
```

## 📈 Metriche Target

| Metrica | Target | Come Verificare |
|---------|--------|-----------------|
| RAM FluidSynth | < 150MB | `ps aux \| grep fluidsynth` |
| RAM Flask | < 50MB | `ps aux \| grep python3` |
| CPU idle | < 10% | `htop` |
| CPU picco | < 80% | `htop` durante note |
| Temperatura | < 70°C | `vcgencmd measure_temp` |
| Latenza | ~3ms | Teorica (buffer 128) |

## 🎯 Checklist Funzionamento Ottimale

- [ ] Swap = 512MB (`free -h`)
- [ ] CPU governor = performance
- [ ] Temperatura < 70°C
- [ ] Soundfont SF3 < 50MB
- [ ] Flask threaded=True
- [ ] Servizi inutili disabilitati
- [ ] MatSynth service running

## 🔗 Link Utili

- Guida completa: `OPTIMIZATION_GUIDE.md`
- README principale: `README.md`
- GitHub: https://github.com/araleslump73/matsynth

## 📞 Supporto

- GitHub Issues: https://github.com/araleslump73/matsynth/issues
- Wiki FluidSynth: https://github.com/FluidSynth/fluidsynth/wiki

---

**Tip**: Salva questo file come riferimento rapido o stampalo!
