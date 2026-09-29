# World Webcams

Collecte des webcams publiques du monde entier (OSM, Windy, ajouts utilisateurs), puis
exposition via une API, une carte open source et un fil « swipe » aléatoire.

```
 OSM (Overpass) ─┐                          ┌─> /webcams/random   (swipe)
 Windy (option) ─┼─> collector ─> PostGIS ──┼─> /tiles/{z}/{x}/{y}.pbf (carte MapLibre)
 Utilisateurs ───┘   ├ health     (dédup)    ├─> /webcams/nearby, /bbox, /{id}
                     └ discovery             └─> /auth, POST /webcams, /admin
                                  Caddy (HTTPS) : /api -> API, / -> site web
```

## Démarrage

```bash
BOT_CONTACT=https://mon-site.fr/bot ADMIN_EMAIL=moi@example.com scripts/init.sh
scripts/status.sh            # avancement du collector
```

`scripts/init.sh` est idempotent (relançable pour mettre à jour) : génère `.env` avec des secrets
aléatoires s'il n'existe pas, crée les dossiers de données, build, démarre, attend les migrations,
vérifie API / site / collector, et crée ou promeut le compte admin.

Derrière un reverse proxy existant (Apache, Traefik), publier en local :
`HTTP_PUBLISH=127.0.0.1:8210 HTTPS_PUBLISH=127.0.0.1:8211 PG_PUBLISH=127.0.0.1:8212 DATA_DIR=/srv/data/webcams scripts/init.sh`

> **User-Agent** : Overpass (HTTP 406) et Wikimedia (HTTP 400/403) refusent les UA sans contact
> réel ou avec un contact bidon (`example.com`). Mettre une vraie URL ou adresse dans `BOT_CONTACT`
> (ou `BOT_USER_AGENT` dans `.env`).

- Site : `http://<vps>/` (ou `https://<DOMAIN>/`), doc API : `/api/docs`
- Les données persistantes sont dans `${DATA_DIR}` (`/data/webcams` par défaut).
- Utilise un mot de passe Postgres sans caractères spéciaux d'URL (`openssl rand -hex 24`).

Lancer un job à la main :

```bash
docker compose run --rm collector python -m app.main run osm --bbox 41,8,43,10   # Corse
docker compose run --rm collector python -m app.main run health
```

Passer un compte en admin (modération des webcams ajoutées par les utilisateurs) :

```bash
docker compose exec postgres psql -U webcams -c "UPDATE users SET role='admin' WHERE email='moi@example.com'"
```

## Ce qui a changé par rapport au plan initial

| Plan initial | Choix retenu | Pourquoi |
|---|---|---|
| `latitude`/`longitude` + `geography` stockés séparément | `geom geometry(Point,4326)` + lat/lon en colonnes générées + index GIST sur `geom::geography` | une seule source de vérité ; `geometry` est natif pour bbox et tuiles vectorielles, l'index geography garde les requêtes en mètres rapides |
| `active` | `status` (pending/approved/rejected) + `is_live` + `preview_endpoint_id` | séparer modération et santé ; le fil swipe ne sert que des webcams qui fonctionnent |
| `ORDER BY random()` implicite | colonne `rand` + index partiel | tirage aléatoire en O(log n) même avec des millions de lignes |
| Une requête Overpass mondiale | 72 tuiles de 30°, découpées en 4 si trop lourdes, attente de slot via `/api/status`, miroirs | la requête mondiale timeout et c'est le meilleur moyen de se faire bannir |
| Dédup par distance seule (100 m) | URL identique → même webcam ; sinon < 25 m, ou < 150 m avec nom similaire (pg_trgm) ; jamais de fusion au sein d'une même source | évite de fusionner deux caméras d'un même bâtiment |
| — | `webcam_endpoints` avec health check, ETag/If-Modified-Since, détection d'image figée, backoff exponentiel | les hôtes morts ne reçoivent presque plus de requêtes |
| — | job *discovery* : cherche l'image/le flux réel dans la page `contact:webcam` (robots.txt respecté) | la majorité des webcams OSM ne pointent que vers une page HTML |
| — | tuiles vectorielles MVT servies par PostGIS avec clustering serveur < z9 | la carte reste fluide avec des millions de points, et le même endpoint sert le web et les apps mobiles |
| — | proxy `/webcams/{id}/snapshot` avec cache 60 s | HTTPS partout (pas de mixed content), pas de hotlink, et 1 requête/min max vers la source quel que soit le trafic |
| Nominatim | géocodage inverse **offline** (`reverse_geocoder`, GeoNames) | politique Nominatim + zéro dépendance réseau |
| Migrations dans `docker-entrypoint-initdb.d` | `dbmate` (`db/migrations`) | le schéma pourra évoluer sans reset de la base |
| — | comptes (argon2 + JWT), ajout de webcams modéré, garde anti-SSRF | les URL utilisateurs sont appelées par le serveur |

## Voir les webcams « en live »

La plupart des webcams du monde sont des **images rafraîchies** (toutes les 1 à 10 min), pas de la
vidéo. Ce qui est affiché, par ordre de préférence :

| Type | Comment | Rafraîchissement |
|---|---|---|
| `image` | proxy `/api/webcams/{id}/snapshot` (HTTPS, cache partagé, requêtes conditionnelles) | toutes les 3 s côté site (servi par le cache) ; la source est interrogée toutes les 3 s tant que l'image change, puis de plus en plus rarement (jusqu'à 60 s) tant qu'elle ne change pas, quel que soit le nombre de spectateurs |
| `hls` / `mjpeg` / `youtube` | lecture directe | vrai direct |
| `iframe` | lecteur du fournisseur (panorama 360°, timelapse…) si la page autorise l'intégration | celui du fournisseur |

Le job *discovery* transforme les pages web (90 % des liens OSM) en médias :
extracteurs dédiés dans `collector/app/providers.py` (Roundshot, webcam-hd/Trinum, Skaping), extraction
générique sinon, et la page elle-même en `iframe` quand `X-Frame-Options` / CSP l'autorisent.
Skaping n'a pas d'URL stable : l'endpoint garde la page et `resolver = 'skaping'`
(`shared/webcam_resolvers.py`, utilisé par le health check et par le proxy).

**Détection des flux** (`collector/app/probe.py`) : décidée d'après ce que l'URL sert réellement,
jamais d'après l'URL seule. Content-Type d'abord, puis signature des premiers octets : JPEG `FF D8 FF`,
MJPEG = au moins 2 images JPEG dans un `multipart/x-mixed-replace`, HLS `#EXTM3U` (playlist maître
suivie jusqu'à un segment qui doit répondre ; `#EXT-X-ENDLIST` = enregistrement), DASH `<MPD`
(`type="dynamic"` = direct), MP4 boîte `ftyp`/`moov`/`moof` (enregistrement). Le téléchargement s'arrête
dès que la réponse est connue. Un endpoint dont le contenu ne correspond pas à son type est reclassé
et revérifié aussitôt. Le MPEG-TS brut et le RTSP ne sont pas lisibles par un navigateur : ignorés.

Une image est considérée **figée** (retirée du fil live) si elle n'a pas changé depuis 48 h ou si la
source annonce un `Last-Modified` de plus de 48 h.

Ajouter un fournisseur : une fonction dans `providers.py` (+ un resolver si l'URL change à chaque
capture), un test dans `collector/tests/test_providers.py`.

## Modération, sources et retraits

Page **`/admin.html`** (compte `moderator` ou `admin`, lien « Modération » dans le menu ☰) :

- **Sources** : activer / désactiver OSM, Windy, ajouts utilisateurs. Une source désactivée : ses
  webcams sont masquées (sauf si une autre source active les recense), ses lecteurs ne sont plus
  proposés, sa collecte s'arrête (y compris un import en cours). Rien n'est supprimé.
- **Demandes de retrait** (formulaire public « Retirer ma webcam ») : une demande pour une webcam la
  masque immédiatement ; le modérateur la retire définitivement, la rétablit, ou bloque tout le site.
- **Sites bloqués** : plus de collecte, d'intégration ni de proxy pour ces hôtes.
- **Webcams en attente** : ajouts des utilisateurs à publier ou refuser.

**Politique d'affichage** (`shared/webcam_policy.py`) : les images des fournisseurs commerciaux
(Roundshot, Skaping, Trinum, Panomax, foto-webcam, Windy…) ne passent jamais par notre proxy ; on affiche
leur lecteur ou leur image servie par eux. Le proxy est réservé aux caméras publiques / open data.
Les lecteurs tiers (YouTube, Windy, fournisseurs) ne sont chargés qu'avec le consentement du visiteur.
Mentions légales et confidentialité : `web/legal.html` (champs éditeur **à compléter**).
Stratégie de partenariats et modèles d'e-mail : [`docs/partenariats.md`](docs/partenariats.md).

## Ne pas se faire bannir

Tout passe par `collector/app/polite.py` :

- User-Agent honnête avec contact (`BOT_USER_AGENT`) : les opérateurs contactent/whitelistent, ils bannissent les anonymes ;
- une seule requête simultanée par hôte, espacées de 3 s (10 s pour Overpass), avec jitter ;
- backoff exponentiel sur 429/5xx, `Retry-After` respecté ;
- `robots.txt` et `Crawl-delay` respectés pour le crawl des pages ;
- requêtes conditionnelles, re-check toutes les 6 h seulement, backoff jusqu'à 7 jours pour les hôtes morts ;
- OSM rafraîchi une fois par semaine seulement.

Pas de rotation d'IP/proxies : c'est ce qui transforme un bot toléré en bot banni.

## Tests

```bash
cd collector && pip install -r requirements.txt pytest && python -m pytest tests
```

## Roadmap

1. **V1 (ici)** : OSM mondial, health check, discovery, carte web, swipe, comptes.
2. **Open data** : ajouter un fichier dans `collector/app/sources/` qui produit des `WebcamRecord` et un `Job` dans `main.py` (caméras trafic DIR/Bison Futé, 511 US/Canada, ports, stations de ski…).
3. **Windy** : renseigner `WINDY_API_KEY` (vérifier leurs CGU avant d'afficher les données publiquement).
4. **Apps iOS/Android** : Expo (React Native) + `@maplibre/maplibre-react-native`, qui consomme **les mêmes** tuiles `/api/tiles` et le même `/api/webcams/random`.
5. Si le trafic grossit : cache HTTP devant `/api/tiles` et `/snapshot` (Cloudflare ou Varnish), rate limiting par IP.

Attribution requise : données © contributeurs OpenStreetMap (ODbL).
