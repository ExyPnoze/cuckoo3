# Live Sandbox — Résumé de présentation

## Qu'est-ce que c'est ?

La feature **Live Sandbox** permet à un analyste de **regarder une analyse en cours en temps réel**,
directement depuis l'interface web — sans attendre la fin de l'analyse et la génération du rapport.

---

## Ce que voit l'analyste

```
┌──────────────────────────────┬─────────────────────────────┐
│                              │  4200  malware.exe ← 3108   │
│   Bureau Windows en direct  │  4200  sc.exe ← 4200        │
│   (noVNC, interactif)        │  4200  schtasks.exe ← 4200  │
│                              ├─────────────────────────────┤
│   → On voit le malware       │  TCP 192.168.30.10→8.8.8.8  │
│     s'exécuter en direct     │  UDP 192.168.30.10→1.1.1.1  │
│                              ├─────────────────────────────┤
│                              │  WRITE  C:\Users\...\evil   │
│                              ├─────────────────────────────┤
│                              │  SET  HKCU\Run\persistence  │
└──────────────────────────────┴─────────────────────────────┘
```

**4 panels temps réel** (WebSocket) :
- **Process** — processus créés par le malware (filtrés : pas de svchost, pas d'OS noise)
- **Network** — connexions sortantes réelles (filtrées : pas de trafic Cuckoo/multicast)
- **Files** — fichiers créés/modifiés/supprimés
- **Registry** — clés registry touchées

---

## Architecture technique

```
Malware (VM Windows)
  │  monitor injecté (Threemon)
  │  capture tout en protobuf binaire
  ▼
Result Server  ──queue──►  LiveEventBroker
(port 2042)                 • parse protobuf incrémental
                            • filtre le bruit OS/Cuckoo
                            • dispatch JSON aux subscribers
                                    │
                             WebSocket (JWT auth)
                                    │
                             Browser live.js
                                    │
                        ┌───────────┴───────────┐
                   noVNC (VNC)          Telemetry panels
                   websockify           (process/net/file/reg)
```

---

## Composants développés

### Backend (Python)

| Fichier | Ce qu'il fait |
|---------|--------------|
| `node/live.py` | Parse les events protobuf en temps réel, filtre le bruit, dispatch aux WS |
| `node/vncproxy.py` | Lance websockify, gère les tokens VNC par tâche |
| `node/webapi.py` | Endpoint WebSocket `/task/<id>/ws` avec auth JWT |
| `node/startup.py` | Intègre broker + proxy VNC dans le cycle de vie du node |
| `common/livejwt.py` | Token HMAC-SHA256 signé pour authentifier les sessions |
| `web/live/views.py` | API session info + page HTML |

### Frontend (JavaScript + HTML)

| Fichier | Ce qu'il fait |
|---------|--------------|
| `static/js/live.js` | Client WebSocket, chargement noVNC, routing des events |
| `templates/analysis/task_live.html.jinja2` | Layout 2 colonnes (VNC + telemetry) |

### Déploiement

| Fichier | Ce qu'il fait |
|---------|--------------|
| `scripts/deploy/install.sh` | Setup complet first-time sur Azure VM (~200 lignes, idempotent) |
| `scripts/deploy/update.sh` | Pull branch + reinstall + restart sans rebuild |
| `scripts/deploy/conf-templates/` | Templates YAML avec placeholders auto-substitués |

---

## Sécurité

### Authentification à deux niveaux

```
1. JWT (telemetry)                  2. VNC Token
───────────────────────────         ─────────────────────────
HMAC-SHA256, signé avec             UUID aléatoire par tâche
live.secret (32 bytes random)       Enregistré dans websockify
TTL : 1 heure                       token file
Vérifié par le node WebAPI          Invalidé à la fin de la tâche
```

### Filtrage du bruit

Le broker filtre **avant** d'envoyer sur le WebSocket :

- **Réseau** : supprime le trafic vers le result server (upload de l'agent), multicast, broadcast, loopback
- **Process** : supprime les 30+ svchost légitimes, les process Windows telemetry/update/licensing, le stager Cuckoo

→ L'analyste ne voit que **le trafic malware**

---

## Chiffres

| Métrique | Valeur |
|---------|--------|
| Fichiers modifiés | 34 |
| Lignes ajoutées | ~2 650 |
| Commits | 12 |
| Dépendances ajoutées | websockify (système) |
| Nouveaux services systemd | 4 (rooter, cuckoo, web, api) |

---

## Démo — Ce qu'on observe sur un ransomware

1. Le malware s'exécute → apparaît dans le panel **Process** (`malware.exe ← explorer.exe`)
2. Crée des processus enfants → `sc.exe`, `schtasks.exe`, `icacls.exe` visibles en temps réel
3. Contacte son C2 → panel **Network** (`TCP 192.168.30.10 → 185.x.x.x:443`)
4. Chiffre les fichiers → panel **Files** (centaines de WRITE sur `C:\Users\...`)
5. Établit la persistance → panel **Registry** (`SET HKCU\Run\...`)
6. Analyse terminée → banner "Analysis finished → View Report"

---

## Limitations connues / Roadmap

| Limitation | Priorité |
|-----------|---------|
| Process panel = liste plate (pas d'arbre hierarchique visuel) | Moyen |
| MAX 200 events par panel (les anciens sont perdus) | Bas |
| Screenshots reçus mais non affichés dans le panel | Bas |
| VNC uniquement pour QEMU (pas Proxmox/KVM encore) | Moyen |
| Mode distribué : toujours routé sur le premier node | Moyen |
