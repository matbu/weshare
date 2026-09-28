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
cp .env.example .env        # remplir POSTGRES_PASSWORD, JWT_SECRET, BOT_USER_AGENT, DOMAIN
docker compose up -d --build
docker compose logs -f collector
```

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
