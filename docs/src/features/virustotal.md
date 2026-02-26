# VirusTotal Integration

Cuckoo3 intègre VirusTotal pour enrichir automatiquement les rapports d'analyse avec les
résultats des moteurs antivirus. L'intégration est incluse dans le projet et ne nécessite
qu'une clé API pour être activée.

---

## Ce que ça apporte

Pour chaque analyse, Cuckoo interroge VirusTotal avec le **hash SHA256** du fichier soumis
(ou l'URL pour les analyses d'URL). Si le sample est connu de VT, un onglet **"Antivirus"**
apparaît dans le rapport avec :

- Le nombre de moteurs AV qui détectent le sample comme malicious
- Un tableau détaillé : moteur / version / méthode / résultat pour chaque AV engine
- Un badge de score dans le résumé de l'analyse (suspicious / malicious)

> Si le sample n'est pas encore connu de VirusTotal (malware inédit ou custom),
> l'onglet n'apparaît pas — c'est le comportement attendu.

---

## Activation

### 1. Obtenir une clé API VirusTotal

1. Créer un compte sur [virustotal.com](https://www.virustotal.com)
2. Aller dans **Profil → API Key**
3. Copier la clé (64 caractères hexadécimaux)

> La clé gratuite (Public API) est suffisante pour un usage basique.
> Elle est limitée à 4 requêtes/minute et 500 requêtes/jour.

### 2. Configurer la clé dans Cuckoo

Éditer le fichier `~/.cuckoocwd/conf/processing/virustotal.yaml` :

```yaml
enabled: True
key: <ta_clé_api_ici>
min_suspicious: 3
min_malicious: 5
```

Via la ligne de commande :

```bash
sed -i 's/^key:.*/key: <ta_clé_api_ici>/' ~/.cuckoocwd/conf/processing/virustotal.yaml
```

### 3. Redémarrer Cuckoo

La clé est chargée au démarrage du moteur de processing :

```bash
# En dev
pkill -f "cuckoo --cwd" && cuckoo --cwd ~/.cuckoocwd

# En production (systemd)
sudo systemctl restart cuckoo
```

---

## Configuration complète

Fichier : `~/.cuckoocwd/conf/processing/virustotal.yaml`

```yaml
# Active ou désactive l'intégration VirusTotal.
enabled: True

# Clé API VirusTotal.
key: <ta_clé_api_ici>

# Nombre minimum de moteurs AV qui doivent détecter le sample
# comme malicious pour que Cuckoo le marque comme "suspicious".
min_suspicious: 3

# Nombre minimum de moteurs AV qui doivent détecter le sample
# comme malicious pour que Cuckoo le marque comme "malicious".
min_malicious: 5
```

### Paramètres `min_suspicious` et `min_malicious`

Ces seuils permettent d'éviter les faux positifs :

| Seuil | Valeur recommandée | Effet |
|-------|--------------------|-------|
| `min_suspicious` | 3 | À partir de 3 détections → score "suspicious" |
| `min_malicious` | 5 | À partir de 5 détections → score "malicious" |

Ajuster ces valeurs selon le niveau de tolérance aux faux positifs souhaité.

---

## Fonctionnement interne

Le module VT s'exécute pendant la phase de **pré-processing**, avant l'analyse comportementale.

```
Soumission du sample
       │
       ▼
  Pré-processing
       │
       ├─ Identification (SHA256, type de fichier...)
       │
       └─ VirusTotal lookup (SHA256 ou URL)
              │
              ├─ Connu → résultats stockés dans pre.virustotal
              │          → signature ajoutée si score atteint
              │
              └─ Inconnu → pas de données VT (pas d'erreur)
                           → l'onglet n'apparaît pas dans le rapport
```

### Fichiers concernés

| Fichier | Rôle |
|---------|------|
| `common/cuckoo/common/virustotal.py` | Client API VT (lookup hash/URL, soumission) |
| `processing/cuckoo/processing/pre/virustotal.py` | Module de pré-processing |
| `web/cuckoo/web/templates/analysis/components/virustotal.html.jinja2` | Affichage dans le rapport |

---

## Limites de l'API gratuite

| Limite | Valeur |
|--------|--------|
| Requêtes par minute | 4 |
| Requêtes par jour | 500 |
| Taille max fichier soumis | 32 MB |

Pour un usage intensif (nombreuses analyses), envisager une clé API premium.
