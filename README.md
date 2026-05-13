# moovair2mqtt

Bridge entre le thermostat **Moovair ST-1** (thermopompe centrale) et **Home Assistant** via MQTT.

Suit la même convention que [mysa2mqtt](https://github.com/bourquep/mysa2mqtt).

> **Note:** Ce projet est issu d'un reverse engineering de l'APK Android Moovair. Il n'est pas affilié à Moovair ou Midea.

---

## Fonctionnalités

- ✅ Lecture température ambiante, setpoint, mode, ventilateur
- ✅ Contrôle depuis Home Assistant (setpoint, mode, fan)
- ✅ Power ON/OFF
- ✅ Modes: Auto, Heat, Cool, Dry, Fan Only, Emergency Heat
- ✅ Ventilateur: Auto, Low, Medium, High
- ✅ Sensor "Aux-Heat" (élément résistif actif)
- ✅ MQTT Discovery automatique (Home Assistant détecte le device sans config)
- ✅ Re-login automatique si session expirée
- ⚠️ Détection automatique aux heat en mode normal: nécessite tests hivernaux (TODO)

---

## Prérequis

- Compte Moovair (email + mot de passe de l'app)
- Broker MQTT (ex: Mosquitto)
- Home Assistant avec intégration MQTT

> ⚠️ **Limitation connue:** Le cloud Moovair n'autorise qu'**une seule session active** à la fois. L'app Moovair et le bridge ne peuvent pas tourner simultanément — le bridge prend le dessus.

---

## Installation rapide (Docker)

### 1. docker-compose.yml

```yaml
services:
  moovair2mqtt:
    image: ghcr.io/VOTRE_USERNAME/moovair2mqtt:latest
    restart: unless-stopped
    environment:
      M2M_MOOVAIR_USERNAME: "votre.email@exemple.com"
      M2M_MOOVAIR_PASSWORD: "votre_password"
      M2M_MQTT_HOST: "192.168.1.x"        # IP de votre broker MQTT
      M2M_MQTT_PORT: "1883"
      # M2M_MQTT_USERNAME: ""             # si votre broker requiert auth
      # M2M_MQTT_PASSWORD: ""
      M2M_POLL_INTERVAL: "5"              # secondes entre chaque lecture
      M2M_LOG_LEVEL: "info"               # debug, info, warning, error
```

### 2. Lancer

```bash
docker compose up -d
```

### 3. Home Assistant

Home Assistant détecte automatiquement le thermostat via MQTT Discovery. Vérifiez dans:
**Paramètres → Appareils et Services → MQTT**

---

## Installation sur TrueNAS Scale

1. Dans TrueNAS Scale: **Apps → Custom App**
2. Image: `ghcr.io/VOTRE_USERNAME/moovair2mqtt:latest`
3. Ajouter les variables d'environnement (voir tableau ci-dessous)
4. Déployer

---

## Variables d'environnement

| Variable | Obligatoire | Défaut | Description |
|----------|------------|--------|-------------|
| `M2M_MOOVAIR_USERNAME` | ✅ | — | Email du compte Moovair |
| `M2M_MOOVAIR_PASSWORD` | ✅ | — | Mot de passe Moovair |
| `M2M_MQTT_HOST` | ✅ | — | IP ou hostname du broker MQTT |
| `M2M_MQTT_PORT` | — | `1883` | Port du broker MQTT |
| `M2M_MQTT_USERNAME` | — | — | Username MQTT (si auth activée) |
| `M2M_MQTT_PASSWORD` | — | — | Password MQTT (si auth activée) |
| `M2M_MQTT_TOPIC_PREFIX` | — | `moovair2mqtt` | Préfixe des topics MQTT |
| `M2M_POLL_INTERVAL` | — | `30` | Intervalle de lecture en secondes (min recommandé: 5) |
| `M2M_LOG_LEVEL` | — | `info` | Niveau de log (debug/info/warning/error) |

---

## Entités Home Assistant créées

| Entité | Type | Description |
|--------|------|-------------|
| Moovair | `climate` | Contrôle complet du thermostat |
| Moovair Aux-Heat | `binary_sensor` | Élément résistif actif (Emergency Heat) |

---

## Topics MQTT

Les topics suivent le format `{prefix}/{device_id}/{champ}`.

### État (bridge → HA)
| Topic | Valeurs |
|-------|---------|
| `.../current_temperature` | ex: `23.9` |
| `.../target_temperature` | ex: `23.0` |
| `.../mode` | `off`, `heat`, `cool`, `heat_cool`, `fan_only`, `dry` |
| `.../fan_mode` | `auto`, `low`, `medium`, `high` |
| `.../action` | `heating`, `cooling`, `idle`, `off`, `fan`, `drying` |
| `.../aux_heat` | `ON`, `OFF` |
| `.../availability` | `online`, `offline` |

### Commandes (HA → bridge)
| Topic | Valeurs |
|-------|---------|
| `.../set/mode` | `off`, `heat`, `cool`, `heat_cool`, `fan_only`, `dry` |
| `.../set/target_temperature` | ex: `22.0` |
| `.../set/fan_mode` | `auto`, `low`, `medium`, `high` |

---

## Notes techniques

- **Protocole:** Midea NetHomePlus cloud (même infrastructure que les appareils Midea)
- **Authentification:** SHA256 + AES/ECB avec clé dérivée
- **Communication device:** `app2base/data/transmit` + `appliance/transparent/send` via m0 packets
- **Températures:** Stockées en Fahrenheit dans le device (converties automatiquement)
- **Aux heat automatique:** Activé par le thermostat selon la température extérieure (balance point). La détection automatique dans les bytes du device nécessite des tests hivernaux (< -5°C à -15°C extérieur).

---

## Contribuer

Ce projet est open source. Pull requests bienvenues!

Pour reporter un bug ou proposer une amélioration: [ouvrir une issue](../../issues).

---

## Licence

MIT — voir [LICENSE](LICENSE)
