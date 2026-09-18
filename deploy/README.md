# Déploiement local — tableau de bord temps réel

Ce dossier contient **tout ce qui est nécessaire pour faire tourner la plateforme
temps réel** (couches 6 et 7) sur la machine, derrière le nginx de l'hôte :

| Élément | Valeur |
| --- | --- |
| URL publique | <https://tristeubadingview.duckdns.org> |
| Authentification | HTTP Basic (`auth_basic`), realm `Trading` |
| Conteneur | `trading-realtime` (image `trading-realtime:latest`) |
| Écoute interne | `0.0.0.0:8080` dans le conteneur, publié sur **`127.0.0.1:3030`** |
| Terminaison TLS | **nginx de l'hôte**, jamais le conteneur |
| Protection | `limit_req` nginx + prison fail2ban `nginx-auth` (partagée) |

---

## 1. Le conteneur

```
deploy/
├── Dockerfile.realtime   # python:3.11-slim + le paquet + l'extra `exchange` (ccxt)
├── docker-compose.yml    # le service, ses volumes et son healthcheck
├── profiles.json         # les profils déployés (paper uniquement)
└── .env                  # TB_OPERATOR_TOKEN — git-ignoré, jamais dans l'image
```

```bash
docker compose -f deploy/docker-compose.yml up -d --build   # construire et démarrer
docker compose -f deploy/docker-compose.yml logs -f         # suivre les journaux
docker compose -f deploy/docker-compose.yml down            # arrêter (volumes conservés)
docker compose -f deploy/docker-compose.yml down -v         # arrêter ET effacer l'état
```

Le conteneur exécute `python -m trading_backtest realtime run` : moteur **et**
serveur de surveillance. Il tourne en utilisateur non privilégié (`appuser`),
avec `restart: unless-stopped` et un healthcheck qui interroge `GET /api/health`.

Deux profils **paper** tournent sur des données de marché réelles (Binance via
ccxt) : `btc-paper` (BTC/USDT 1h) et `eth-paper` (ETH/USDT 15m).
`realtime.start_at` vaut `null` : le moteur suit l'horloge murale, il ne rejoue
**pas** un historique.

### Volumes (l'état survit à un redémarrage)

| Volume | Chemin | Contenu |
| --- | --- | --- |
| `trading-state` | `/app/data/realtime` | base SQLite (`state.db`), fichier d'arrêt d'urgence, journaux JSON |
| `trading-cache` | `/app/data/cache` | cache OHLCV (évite de re-télécharger la fenêtre d'échauffement) |

### Secrets

`deploy/.env` porte le **jeton opérateur** de l'API de surveillance (bouton
« arrêt d'urgence » du tableau de bord) ; il est git-ignoré **et** exclu du
contexte de construction par `.dockerignore`, donc il n'entre jamais dans une
couche d'image. Les clés d'exchange (inutilisées ici, tous les profils sont en
`paper`) ne viennent que de l'environnement — voir `docs/realtime.md` §3.

---

## 2. nginx : le même motif que `culia` / `tristeub`

Trois blocs ont été ajoutés à `/opt/homebrew/etc/nginx/nginx.conf` :

1. **une zone de limitation** et **un upstream** :

   ```nginx
   limit_req_zone $binary_remote_addr zone=trading_per_ip:10m rate=100r/s;

   upstream trading_realtime {
     server 127.0.0.1:3030;
     keepalive 16;
   }
   ```

2. **le vhost port 80** (public via PF `80 -> 8088`) : défi ACME servi depuis
   `/opt/homebrew/var/www/certbot`, puis `301` vers HTTPS ;

3. **le vhost TLS** sur `8446` (public via PF `443 -> 8445`, routage SNI par le
   bloc `stream`, `proxy_protocol`) avec le certificat Let's Encrypt, les en-têtes
   de sécurité, `auth_basic`, `limit_req zone=trading_per_ip burst=100 nodelay`
   et le proxy vers l'upstream.

Le certificat a été obtenu par la même méthode que les autres domaines :

```bash
certbot certonly --webroot -w /opt/homebrew/var/www/certbot \
  -d tristeubadingview.duckdns.org \
  --config-dir /opt/homebrew/etc/letsencrypt \
  --non-interactive --agree-tos --keep-until-expiring --key-type ecdsa
```

`sauvegarde : nginx.conf.bak-trading-<horodatage>` a été écrit avant modification,
et `nginx -t` est exécuté avant chaque `nginx -s reload`.

### État, erreurs et fuite d'information

Le nginx ne expose que le tableau de bord : le conteneur n'est publié que sur
`127.0.0.1`, et l'API de surveillance reste **derrière** l'authentification.
Le jeton opérateur (mutations) est une seconde barrière, indépendante du Basic
auth : sans lui, `POST /api/kill-switch` répond `403`.

---

## 3. fail2ban

Aucune prison dédiée n'a été ajoutée : la prison existante `[nginx-auth]`
(`/opt/homebrew/etc/fail2ban/jail.local`) surveille **le journal d'erreurs de
nginx entier** avec le filtre `nginx-http-auth`, donc elle couvre déjà ce vhost —
c'est exactement ce qui protège `culia.duckdns.org` et `tristeub.duckdns.org`
(`maxretry = 5`, `findtime = 10m`, `bantime = 1h`, bannissement via l'ancre pf
`com.apple/f2b/nginx-auth`, ports 80 et 443).

Vérification faite pendant le déploiement : une tentative avec un mauvais mot de
passe a produit une ligne

```
user "tristeub": password mismatch, client: <ip>, server: tristeubadingview.duckdns.org, request: "GET / HTTP/2.0"
```

et le journal de fail2ban a grossi de 120 octets dans la seconde — la prison
compte donc bien les échecs **de cette route**.

> **À savoir** : le démon tourne actuellement lancé à la main
> (`fail2ban-server --async`, root) et non par launchd — `launchctl print
> system/homebrew.mxcl.fail2ban` répond `not running`, ce qui est le cas
> **préexistant** sur cette machine, partagé avec les autres vhosts protégés. Le
> plist `/Library/LaunchDaemons/homebrew.mxcl.fail2ban.plist` existe (avec
> `RunAtLoad`) ; après un redémarrage de la machine, vérifier avec
> `sudo fail2ban-client status nginx-auth` que la prison est bien remontée.

---

## 4. Exploitation

```bash
# état des profils, en local (sans passer par nginx)
curl -s http://127.0.0.1:3030/api/profiles | python3 -m json.tool

# vhost et certificat
nginx -t
certbot certificates --config-dir /opt/homebrew/etc/letsencrypt

# journaux structurés JSON (console + fichier durable)
docker logs --tail 50 trading-realtime
docker exec trading-realtime tail -20 /app/data/realtime/logs/realtime-*.log

# arrêt d'urgence global (le tableau de bord ne peut RIEN faire d'autre)
docker exec trading-realtime touch /app/data/realtime/KILL_SWITCH
docker exec trading-realtime rm /app/data/realtime/KILL_SWITCH
```

Le niveau de journal se règle par `TB_LOG_LEVEL` (`INFO` par défaut) dans
`deploy/docker-compose.yml`.

---

## 5. Ce que ce déploiement n'est pas

- **pas d'authentification par utilisateur** : un unique couple Basic auth
  partagé, plus un jeton opérateur — niveau « réseau de confiance » ;
- **pas de TLS dans le conteneur** : tout passe par nginx, qui est le seul
  terminus TLS ;
- **pas de trading réel** : les deux profils sont en `paper` ; le mode `live`
  exigerait `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`, des clés d'exchange en
  environnement et un profil `mode: live` — aucun n'est configuré ici ;
- **pas de haute disponibilité** : un conteneur, une base SQLite mono-écrivain ;
- **pas d'exercice contre un vrai exchange pour le mode live** : la suite de tests
  ne couvre le courtier live que par des doublures (voir `docs/realtime.md` §7).
