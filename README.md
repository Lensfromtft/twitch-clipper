# Clipper

Interface graphique locale pour découper des extraits de **VOD Twitch et de vidéos
YouTube** : on choisit une chaîne, une vidéo, on la prévisualise dans le lecteur
officiel de la plateforme, on place les timecodes, et le clip est téléchargé —
en vidéo, en vertical 9:16 ou en MP3.

C'est l'équivalent visuel de :

```
yt-dlp --cookies cookies.txt --download-sections "*02:09:50-02:10:05" "https://www.twitch.tv/videos/123456789"
```

## Installation

```
git clone https://github.com/Lensfromtft/twitch-clipper.git
cd twitch-clipper
```

Puis double-cliquez sur **`Clipper.bat`**. C'est tout : au premier lancement,
l'application télécharge elle-même ce qui lui manque (une trentaine de secondes,
une seule fois) et ouvre l'interface.

| Dépendance | Comment elle arrive |
|---|---|
| **Python 3.8+** | `Clipper.bat` le cherche, et propose `winget install Python.Python.3.12` s'il est absent |
| **yt-dlp.exe** | téléchargé depuis les [releases officielles](https://github.com/yt-dlp/yt-dlp/releases/latest) |
| **ffmpeg.exe**, **ffprobe.exe** | extraits de l'[archive maintenue par yt-dlp](https://github.com/yt-dlp/FFmpeg-Builds/releases/latest) |
| Modules Python | **aucun** — uniquement la bibliothèque standard |

Ces exécutables ne peuvent pas être versionnés : ffmpeg et ffprobe pèsent plus de
160 Mo chacun, au-delà de la limite de 100 Mo par fichier de GitHub. Les
télécharger à la demande a l'avantage de livrer une version à jour plutôt qu'une
copie figée.

Pour forcer une réinstallation : `python clipper.py --install`, ou
`python installer.py --force`. Pour utiliser des binaires déjà présents ailleurs
sur la machine, renseignez leurs chemins dans `Réglages` — l'installation
automatique est alors désactivée.

### Cookies (facultatif)

Pour les VOD réservées aux abonnés, exportez vos cookies Twitch au format
Netscape dans un fichier **`cookies.txt`** placé dans le même dossier (extension
navigateur type *Get cookies.txt LOCALLY*). Ce fichier est personnel : il donne
accès à votre compte, ne le partagez pas — il est exclu du dépôt par le
`.gitignore`. Sans lui, l'application fonctionne pour toutes les VOD publiques.

## Lancement

Double-cliquez sur **`Clipper.bat`**.

Une fenêtre console s'ouvre (elle doit rester ouverte : c'est le serveur) et
l'interface s'ouvre dans le navigateur par défaut, sur `http://localhost:8731`.
Pour arrêter : `Ctrl+C` dans la console, ou fermez-la.

## Utilisation

0. **Plateforme** — les onglets `Twitch` / `YouTube` en haut de la colonne de
   gauche basculent toute l'interface. Chaînes récentes, épinglées et dernière
   chaîne ouverte sont mémorisées **séparément** par plateforme. Coller un lien
   dans *Lien direct* bascule tout seul vers la bonne plateforme.

1. **Chaîne** — tapez `ponce` ou `twitch.tv/ponce`, puis `Voir`.
   Côté YouTube : `@Squeezie`, `youtube.com/@Squeezie` ou une URL de chaîne.
   Les onglets deviennent `Vidéos` / `Lives` / `Shorts`. Les VOD s'affichent
   avec vignette, durée, date et nombre de vues. Les onglets basculent entre
   diffusions (VOD), extraits et uploads. Les chaînes consultées restent en
   raccourci ; l'étoile (☆) en épingle une pour la garder toujours en tête de
   liste, même quand elle sort de l'historique.
2. **VOD** — un clic charge la vidéo dans le lecteur Twitch officiel : toute la VOD
   est navigable, comme sur le site.
3. **Timecodes** — naviguez dans le lecteur puis :
   - `Marquer` (ou la touche <kbd>I</kbd>) prend le temps courant comme **début** ;
   - `Marquer` (ou <kbd>O</kbd>) le prend comme **fin** ;
   - les boutons `±0,5s / ±1s / ±10s` ajustent au poil, `15s / 30s / 60s` fixent la durée ;
   - les trois champs acceptent `02:09:50`, `2h09m50s` ou un nombre de secondes, et
     restent cohérents entre eux (modifier la durée déplace la fin).
4. **Vérifier avant de télécharger** — `Lire la sélection` (<kbd>P</kbd>) joue
   uniquement la portion choisie puis s'arrête et rembobine. De quoi valider la
   coupe en quelques secondes plutôt qu'après le téléchargement.
5. **Qualité** — la liste reprend les rendus réellement disponibles pour cette
   vidéo (1080p60, 720p60…), plus **Audio seul (m4a)** et **MP3**.

   Le MP3 est encodé en VBR qualité maximale (`--audio-quality 0`, ~200 kbit/s).
   Pour l'audio, la coupe précise est **toujours** forcée, même si la case est
   décochée : les fragments audio font une dizaine de secondes, et sans ce
   forçage un extrait déborde jusqu'à leur frontière (mesuré : 20 s rendues pour
   10 s demandées). En audio, le sélecteur de format et l'éditeur vertical
   disparaissent — ils n'auraient aucun effet.
   Tous les fichiers produits portent le **titre, l'auteur, la date et l'URL
   source** en métadonnées (`--add-metadata`) ; les fichiers audio reçoivent en
   plus la vignette en pochette (`--embed-thumbnail`). Un clip isolé reste ainsi
   identifiable des mois plus tard.
6. **Télécharger** (ou <kbd>Ctrl</kbd>+<kbd>Entrée</kbd>) — le clip part dans une file
   d'attente avec progression, vitesse et temps restant. Une fois terminé : `Aperçu`
   le lit dans l'interface, `Ouvrir` le lance dans votre lecteur, `Explorateur` le
   montre dans son dossier.

Le panneau **Commande équivalente** affiche la ligne `yt-dlp` correspondant aux
réglages en cours, avec un bouton pour la copier.

## Mode vertical (9:16)

Le sélecteur `16:9 / Vertical 9:16`, à gauche de la qualité, bascule la sortie en
format portrait **1080×1920** : la caméra est empilée au-dessus d'une portion du
gameplay, avec le nom de la chaîne incrusté.

Un éditeur s'ouvre alors sous les contrôles :

- une **image de la VOD**, extraite au timecode de début (bouton `Image à ce
  timecode` pour la rafraîchir après avoir déplacé le début) ;
- deux cadres à déplacer et redimensionner directement dessus : **Caméra**
  (violet) et **Gameplay** (vert) ;
- un **aperçu 9:16 en direct** à droite, qui reproduit exactement le rendu final ;
- la **hauteur de la caméra** (part de l'image occupée par le haut, 15 à 60 %) ;
- le **nom affiché** et son activation.

Les cadres sont **mémorisés par chaîne** : réglés une fois pour un streamer, ils
sont réappliqués à toutes ses VOD. `Réinitialiser les cadres` revient aux valeurs
par défaut, calculées d'après le format de sortie.

Ils sont enregistrés avec la définition de l'image sur laquelle ils ont été
tracés, et remis à l'échelle si le clip téléchargé en a une autre — par exemple
des cadres tracés sur une image 1080p appliqués à un clip demandé en 720p.

Chaque zone est mise à l'échelle en « couvrir » (agrandissement puis recadrage
centré) : inutile de respecter un rapport précis en traçant les cadres, il n'y
aura jamais de bandes noires.

Le mode vertical implique un réencodage (H.264 / AAC), en une seconde passe après
le téléchargement — la barre de progression la distingue sous le libellé
`Montage vertical`. Comptez quelques secondes pour un clip court. Si cette passe
échoue, le clip 16:9 déjà téléchargé est **conservé** et son nom est indiqué dans
le message d'erreur, pour éviter d'avoir à le retélécharger.

Le nom incrusté accepte lettres, chiffres, espace, `.`, `-` et `_` (les autres
caractères sont retirés, car ils casseraient le filtre de rendu).

### Logo

Si un fichier **`Logo.png`** est présent à côté de `clipper.py`, il est incrusté
à gauche du nom de la chaîne (84 px de côté, transparence respectée). Sans lui,
seul le nom est affiché. L'aperçu de l'interface le reprend à l'identique.

### Cadrage par défaut

Tant qu'une chaîne n'a pas de réglage enregistré, ce cadrage de référence
s'applique (relevé à la main sur une source 1920×1080, mis à l'échelle si la
vidéo a une autre définition) :

```
cam   = [0, 0, 499, 283]
game  = [514, 0, 893, 1080]
split = 0.32
```

Il est défini dans `REF_CAM` / `REF_GAME` / `DEFAULT_SPLIT` ([clipper.py](clipper.py))
et repris à l'identique côté interface ([web/app.js](web/app.js)).
`Réinitialiser les cadres` y revient.

### Raccourcis clavier

| Touche | Effet | | Touche | Effet |
|---|---|---|---|---|
| <kbd>I</kbd> | marquer le début | | <kbd>J</kbd> <kbd>L</kbd> | reculer / avancer de 10 s |
| <kbd>O</kbd> | marquer la fin | | <kbd>←</kbd> <kbd>→</kbd> | ± 5 s |
| <kbd>P</kbd> | lire la sélection | | <kbd>,</kbd> <kbd>.</kbd> | ± 1 s |
| <kbd>K</kbd> / <kbd>Espace</kbd> | lecture / pause | | <kbd>Ctrl</kbd>+<kbd>Entrée</kbd> | télécharger |

Ils sont rappelés dans l'interface sous le bouton de téléchargement. Le lecteur
Twitch étant dans un cadre isolé, cliquez une fois en dehors de celui-ci si les
touches ne répondent pas : le clavier lui appartient tant qu'il a le focus.

### Lien direct

Le champ *Lien direct* accepte une URL de VOD ou un identifiant. Si l'URL contient
un timecode (`...?t=2h09m50s` — le format que Twitch met dans « Copier le lien à
cet instant »), le début est placé automatiquement.

## Coupe précise

Par défaut le clip est extrait **sans réencodage** : c'est quasi instantané et la
coupe tombe déjà au bon endroit dans les faits (mesuré à ~30 ms près sur les VOD
testées), mais elle peut se caler sur l'image-clé la plus proche.

Cochez **Coupe précise** pour forcer la coupe exactement à l'image demandée
(`--force-keyframes-at-cuts`) : la vidéo est alors réencodée, ce qui prend
nettement plus de temps sur un long extrait en 1080p60.

## Cookies

`cookies.txt` sert à accéder aux VOD réservées aux abonnés. Il **n'est jamais
modifié** : yt-dlp travaille sur `cookies.work.txt`, une copie conservée dans le
dossier de données de l'application.

Cette copie est persistante, et c'est volontaire. yt-dlp réécrit son fichier de
cookies à chaque exécution parce que Google fait tourner la session : avec une
copie jetable, la rotation était perdue à chaque appel et YouTube finissait par
répondre *« Sign in to confirm you're not a bot »*. La copie est rafraîchie
depuis `cookies.txt` dès que celui-ci est plus récent, et remise à plat après un
échec (un refus amène yt-dlp à réécrire le fichier amputé).

Pour la **prévisualisation**, c'est la session Twitch de votre navigateur qui compte :
une VOD abonnés ne s'affichera dans le lecteur que si vous êtes connecté à Twitch
dans ce navigateur.

## Réglages

Bouton `Réglages` : dossier de sortie, fichier de cookies, modèle de nom de fichier,
nombre de téléchargements simultanés. Tout est enregistré dans
`%LOCALAPPDATA%\Clipper\config.json`, et les clips vont par défaut dans
`%LOCALAPPDATA%\Clipper\clips` — volontairement hors du dossier de l'appli
(voir *Problème connu* plus bas).

La section **yt-dlp** y affiche la version installée. L'application compare au
démarrage avec la dernière version publiée (API GitHub, en tâche de fond) et
signale une mise à jour disponible par une pastille sur le bouton `Réglages` ;
le bouton `Mettre à jour` lance `yt-dlp -U`. Utile : les extracteurs Twitch
cassent périodiquement, et c'est presque toujours la première chose à tenter.

Modèle de nom par défaut : `{channel}_{date}_{start}-{end}` →
`ponce_2026-09-10_02h09m50s-02h10m05s.mp4`.
Variables disponibles : `{channel}` `{title}` `{date}` `{start}` `{end}` `{dur}`
`{id}` `{quality}`.

`config.json` accepte aussi deux clés non exposées dans l'interface, utiles si vous
déplacez les exécutables : `ytdlp_path` (chemin complet de `yt-dlp.exe`) et
`ffmpeg_dir` (dossier contenant `ffmpeg.exe`).

## Problème connu : « You must be logged into an account » alors que cookies.txt est bon

Symptôme : la prévisualisation et la liste des VOD marchent, mais tout
téléchargement d'une VOD réservée aux abonnés échoue comme si aucun cookie
n'était fourni.

Cause : le `python` du **Microsoft Store** (l'alias dans `WindowsApps`). C'est une
application MSIX ; ses écritures dans `%LOCALAPPDATA%` sont redirigées en silence
vers un conteneur privé (`%LOCALAPPDATA%\Packages\PythonSoftwareFoundation...`).
`yt-dlp.exe` et `ffmpeg.exe`, eux, voient le vrai dossier. La copie temporaire du
fichier de cookies était donc écrite dans le conteneur et restait **invisible pour
yt-dlp**, qui partait sans authentification.

Le lanceur `Clipper.bat` écarte maintenant cet alias et cherche un vrai
Python (lanceur `py`, PATH, Anaconda, python.org). Il affiche l'interpréteur
retenu au démarrage. Si malgré tout l'app tourne sous le Python du Store, elle le
détecte, le signale, et bascule ses données vers `%USERPROFILE%\Clipper`,
seul emplacement à la fois hors redirection MSIX et hors protection Defender.

## YouTube : ce qu'il faut savoir

**Les cookies ne sont pas nécessaires.** Contrairement à Twitch, où ils servent
dès le départ pour les VOD réservées aux abonnés, YouTube fonctionne en anonyme
pour la quasi-totalité des vidéos. L'application tente donc **d'abord sans
cookies**, et ne les présente que si YouTube les réclame : protection anti-robot
(« Sign in to confirm you're not a bot »), vidéo privée, réservée aux membres ou
limitée par l'âge. La bascule est automatique et signalée dans le journal du
téléchargement.

Ce choix n'est pas cosmétique : rattacher chaque requête à votre compte Google
expose **le compte** à une restriction, et pas seulement votre adresse IP. Mieux
vaut ne l'engager que lorsque c'est indispensable.

Si vous voulez tout de même fournir des cookies YouTube, ajoutez-les au **même**
`cookies.txt` : yt-dlp sélectionne les cookies par domaine, un seul fichier sert
les deux plateformes.

**Node.js accélère les téléchargements.** yt-dlp a besoin d'un moteur JavaScript
pour déchiffrer les paramètres de YouTube ; sans lui, des formats manquent et les
débits s'effondrent. S'il trouve `node` dans le PATH, l'application ajoute
automatiquement `--js-runtimes node`. Les réglages indiquent s'il a été détecté.

**Formats privilégiés.** Deux préférences propres à YouTube :

- `https` plutôt que HLS — chercher un point précis dans un long manifeste HLS
  oblige ffmpeg à le parcourir (plus de 3 min mesurées sur une vidéo d'1 h) ;
- **H.264 + AAC** plutôt qu'AV1/VP9 + Opus. YouTube sert volontiers de l'AV1,
  que beaucoup de lecteurs et de logiciels de montage refusent. La qualité est
  donc plafonnée à 1080p sur YouTube (au-delà, seuls VP9 et AV1 existent), en
  échange d'un fichier lisible partout.

**Image de repérage du mode vertical.** Sur Twitch elle est extraite directement
du flux (~6 s) ; sur YouTube ce chemin renvoie une erreur 403 — l'URL est liée au
client qui l'a demandée et ffmpeg n'a pas ses en-têtes. Faire télécharger une
seconde de vidéo par yt-dlp fonctionnait mais demandait **93 s**, assez pour que
le navigateur abandonne la requête et affiche « Image indisponible ».

L'image passe donc par le même mécanisme de plages d'octets que la découpe (voir
ci-dessous) : **2,8 s** une fois la liste des formats en mémoire, une dizaine de
secondes au premier appel. Cette liste est conservée trois heures par vidéo,
durée de validité des URL `googlevideo` ; elle est oubliée dès qu'une plage est
refusée, et redemandée.

**Extraction rapide sur les longues vidéos.** Couper tard dans une longue vidéo
YouTube était impraticable avec `--download-sections` : ffmpeg lit le flux
**depuis le début** jusqu'au point demandé. Pour un extrait à 1 h 21 dans une
vidéo de 6 h 33 (6,5 Go), cela représentait 1,2 Go transférés — plus de 15
minutes sans aboutir.

L'application emprunte donc un autre chemin pour la vidéo YouTube :

1. `--extractor-args "youtube:formats=dashy"` expose le flux en **DASH
   fragmenté** plutôt qu'en fichier monolithique ;
2. les 128 premiers Ko contiennent une boîte **`sidx`**, l'index qui associe
   chaque instant à une plage d'octets (lu en 0,1 s) ;
3. seuls les segments couvrant l'extrait sont téléchargés, par requêtes HTTP
   `Range` — le serveur y répond en 0,1 s ;
4. l'en-tête et ces segments sont réassemblés, puis découpés.

Mesure sur le cas ci-dessus : **18,8 Mo transférés en 22 s** au lieu de 1,2 Go
sans fin. Deux détails comptent dans l'assemblage : la boîte `sidx` doit être
retirée (son index décrit le fichier complet, ffmpeg chercherait la suite à
1,2 Go et s'arrêterait après 5 s), et les horodatages doivent être remis à zéro
avant la découpe, sans quoi le saut absolu échoue.

**Deux réponses de YouTube demandent d'insister.** La protection anti-robot se
déclenche par à-coups sur des appels rapprochés et répond en deux secondes ; et
selon le client retenu par yt-dlp, la liste des formats retombe parfois à cinq
entrées sans piste audio `mp4a`, inexploitables pour une découpe par plages
d'octets (mesuré une fois sur deux). Dans les deux cas l'application redemande,
quelques secondes plus tard, plutôt que d'échouer ou de basculer pour rien sur le
téléchargement intégral.

Si l'index est absent ou le format inattendu, l'application **retombe
silencieusement** sur `--download-sections`. Les mesures suivantes n'ont rien
donné et sont écartées : `--downloader-args "ffmpeg_i:-seekable 1"`,
`-multiple_requests 1`, le téléchargeur natif de yt-dlp, une qualité inférieure
et les variantes HLS (aucune n'est proposée pour ces vidéos).

**Bridage en cas d'usage intensif.** Après une série rapide de requêtes, YouTube
impose des pauses (`Sleeping N seconds as required by the site`) et les
téléchargements passent de quelques secondes à plusieurs minutes. Rien à corriger
côté application : l'option `--downloader-args "ffmpeg_i:-seekable 1"`, souvent
citée comme remède, a été mesurée sans effet ici. Le débit revient de lui-même
après une pause d'usage.

## Problème connu : « GraphQL indisponible (unknown url type: https) »

Message au démarrage, avec une liste de VOD plus lente et sans date ni jeu.

Cause : une installation **Anaconda** lancée hors environnement activé. Son
`python.exe` ne trouve pas les DLL OpenSSL de `<prefix>\Library\bin`, donc
`import ssl` échoue et Python perd le HTTPS (yt-dlp, lui, n'est pas concerné :
il embarque son propre SSL, d'où une app qui marche mais en mode dégradé).

Le lanceur ajoute désormais les dossiers conda au `PATH`, comme le ferait
`conda activate`. L'app applique en plus le même correctif elle-même au
démarrage, donc elle fonctionne aussi si vous la lancez à la main.

## La prévisualisation reste noire sur une VOD abonnés

Le téléchargement et la prévisualisation utilisent **deux authentifications
différentes** : yt-dlp utilise `cookies.txt`, alors que le lecteur embarqué
utilise la session Twitch de votre navigateur. Pour prévisualiser une VOD
réservée aux abonnés, il faut donc être connecté à Twitch *dans le navigateur qui
ouvre l'application*. Sinon Twitch affiche « Cette vidéo est réservée aux
abonnés » dans le lecteur — le découpage et le téléchargement, eux, fonctionnent
quand même.

## Problème connu : « FileNotFoundError [WinError 2] » au démarrage

Si l'appli vivait dans un dossier protégé par l'**Accès contrôlé aux dossiers**
de Windows Defender (Documents, Bureau, Images… sont protégés par défaut), toute
création de fichier ou de dossier par `python.exe` y échouait avec un message
trompeur (« fichier introuvable » au lieu de « accès refusé »).

C'est pour cela que la configuration et les clips sont maintenant écrits dans
`%LOCALAPPDATA%\Clipper`, qui n'est pas concerné par cette protection —
le dossier de l'appli lui-même (lu, jamais modifié) peut rester où il est.

Si l'erreur revient (par exemple après avoir changé le dossier de sortie dans
Réglages vers un dossier protégé), l'appli l'affiche maintenant clairement.
Deux solutions : choisir un dossier de sortie hors de Documents/Bureau/Images,
ou autoriser `python.exe`, `yt-dlp.exe` et `ffmpeg.exe` dans *Sécurité Windows
→ Protection contre les virus et menaces → Gérer les paramètres de protection
contre les ransomwares → Autoriser une application via l'accès contrôlé aux
dossiers*.

## Détails techniques

- `clipper.py` : serveur HTTP (bibliothèque standard uniquement) lié à `127.0.0.1`,
  API JSON, file de téléchargement, suivi de progression lu sur la sortie de ffmpeg.
- `web/` : l'interface (une page, sans framework).
- `installer.py` : récupération de yt-dlp, ffmpeg et ffprobe au premier lancement.
- `Clipper.bat` : choix de l'interpréteur Python et lancement.
- La liste des VOD passe par l'API publique de Twitch (rapide, avec dates et jeux)
  et retombe automatiquement sur `yt-dlp --flat-playlist` si celle-ci change.
- Les appels API sont protégés par un jeton de session et refusent toute requête
  dont l'en-tête `Host` n'est pas local ; les fichiers ne sont servis que depuis le
  dossier de sortie.
