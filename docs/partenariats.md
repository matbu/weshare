# Partenariats et sources de webcams

État au 29/09/2026, base de test (webcams publiées, hors Windy, par domaine d'origine du média).

## Deux familles d'interlocuteurs

### A. Organismes publics (caméras routières, villes) : la majorité de la base

| Domaine | Webcams | Organisme |
|---|---:|---|
| madrid.es | 357 | Ville de Madrid |
| utah.gov | 350 | Utah DOT (UDOT) |
| trafficnz.info | 335 | Waka Kotahi NZ Transport Agency |
| tallinn.ee | 257 | Ville de Tallinn |
| dgt.es | 203 | Dirección General de Tráfico (Espagne) |
| gv.at | 181 | Administrations autrichiennes |
| wa.gov | 181 | Washington State DOT |
| vegvesen.no | 172 | Statens vegvesen (Norvège) |
| asfinag.at | 162 | ASFINAG (autoroutes autrichiennes) |
| bellevuewa.gov | 147 | Ville de Bellevue (WA) |
| mass511.com | 127 | MassDOT |
| ca.gov | 127 | Caltrans |
| nps.gov | 82 | US National Park Service |
| ctroads.org | 80 | Connecticut DOT |

**Démarche** : beaucoup publient leurs caméras en **open data avec une API officielle et une licence**
(à vérifier au cas par cas). Intérêt :
- licence explicite, donc affichage via notre proxy sans ambiguïté ;
- une API officielle est plus fiable que les liens OSM (positions exactes, caméras ajoutées ou retirées) ;
- chaque API peut devenir un collecteur dédié (`collector/app/sources/<organisme>.py`).

Priorité : les organismes avec le plus de caméras **et** une API open data documentée. Un contact n'est
nécessaire que si la licence est absente ou floue.

### B. Fournisseurs commerciaux de webcams

| Domaine | Webcams | Remarque |
|---|---:|---|
| foto-webcam.eu | 283 | réseau associatif/commercial (Alpes) |
| panomax.com | 166 | panoramas 360°, stations de ski |
| roundshot.com | 109 | panoramas 360°, lecteur intégrable |
| webcam-hd.com (Trinum) | 84 | panoramas, stations françaises |
| skaping.com | ~13 | stations françaises |
| windy.com | ~70 000 | source désactivée ; offre Pro 9 990 €/an |

Leurs images ne passent **jamais** par notre proxy (`shared/webcam_policy.py`) : on affiche leur lecteur
ou leur image servie par eux, avec crédit et lien. Un partenariat permettrait :
- un accord écrit, indispensable avant de monétiser avec de la publicité ;
- un flux officiel (liste des caméras, positions, images en taille voulue) ;
- de la visibilité pour eux (trafic renvoyé vers leurs clients, les stations).

## Modèles d'e-mail

Envoyés depuis **weee.share.stream@gmail.com**, l'adresse déclarée dans le User-Agent du bot.

### Organisme public (FR)

> **Objet :** Réutilisation des images de vos caméras sur World Webcams
>
> Bonjour,
>
> Je développe World Webcams, une carte mondiale des webcams publiques (web, puis iOS et Android).
> Vos caméras [routières / urbaines] y apparaissent déjà via OpenStreetMap, avec un lien vers votre site.
>
> Pour les afficher dans les règles et de façon fiable, je souhaiterais savoir :
> 1. si ces images sont publiées sous une licence ouverte, et laquelle ;
> 2. s'il existe une API ou un flux officiel (liste des caméras, positions, URL des images) ;
> 3. quelles sont vos conditions d'attribution et de fréquence de rafraîchissement.
>
> Notre robot (WorldWebcamsBot) respecte robots.txt et limite ses requêtes ; nous retirons toute caméra
> sur simple demande.
>
> Bien cordialement,
> [Nom] — World Webcams — weee.share.stream@gmail.com

### Fournisseur commercial (FR)

> **Objet :** Proposition de partenariat — diffusion de vos webcams sur World Webcams
>
> Bonjour,
>
> World Webcams est une carte mondiale et une application de découverte de webcams en direct.
> Nous affichons aujourd'hui [N] webcams [Roundshot / Panomax / …] via votre lecteur intégrable, avec un
> lien vers la page de chaque webcam.
>
> Nous aimerions en discuter avec vous pour :
> - nous assurer que cet usage vous convient, y compris si le site intègre de la publicité ;
> - accéder à un flux officiel (liste des caméras et positions) plutôt qu'à des liens collectés ;
> - vous apporter du trafic qualifié vers vos clients (stations, offices de tourisme).
>
> Nous retirons immédiatement toute webcam sur votre demande.
>
> Bien cordialement,
> [Nom] — World Webcams — weee.share.stream@gmail.com

### Provider / agency (EN)

> **Subject:** Showing your webcams on World Webcams
>
> Hello,
>
> I'm building World Webcams, a worldwide map and discovery app for public webcams (web, then iOS/Android).
> [N] of your cameras already appear there, credited and linked to your website.
>
> I'd like to make sure this complies with your terms, and ask whether you offer an official feed or API
> (camera list, positions, image URLs), a licence for reuse, and your attribution requirements — including
> for a site that may display advertising.
>
> Our crawler (WorldWebcamsBot) honours robots.txt and rate-limits itself, and we remove any camera on request.
>
> Best regards,
> [Name] — World Webcams — weee.share.stream@gmail.com
