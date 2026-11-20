#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clipper - interface graphique locale pour decouper des extraits de VOD Twitch
et de videos YouTube.

Lance un petit serveur HTTP sur 127.0.0.1 et ouvre l'interface dans le navigateur.
Le decoupage est fait par yt-dlp + ffmpeg (executables places a cote de ce script).

Aucune dependance externe : uniquement la bibliotheque standard Python 3.8+.
"""

import http.server
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Le Python "embarquable" que Clipper.bat installe sur un poste vierge ignore le
# dossier du script (fichier ._pth) : sans cette ligne, "import installer"
# echouerait.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
WEB_DIR = os.path.join(BASE_DIR, "web")
YTDLP = os.path.join(BASE_DIR, "yt-dlp.exe")


def is_msix_python():
    """Python installe depuis le Microsoft Store (alias 'App Execution').

    C'est une application MSIX : ses ecritures dans %LOCALAPPDATA% sont
    silencieusement redirigees vers un conteneur prive
    (%LOCALAPPDATA%\\Packages\\PythonSoftwareFoundation...\\LocalCache\\Local),
    invisible pour yt-dlp.exe et ffmpeg.exe qui, eux, voient le vrai dossier.
    Resultat : le fichier de cookies temporaire disparait pour yt-dlp, qui
    telecharge alors sans authentification.
    """
    return "\\windowsapps\\" in (sys.executable or "").replace("/", "\\").lower()


# Donnees modifiables (config, clips, copies temporaires de cookies) : rangees
# hors du dossier de l'appli. Beaucoup d'installations vivent sous "Documents",
# que l'"Acces controle aux dossiers" de Windows Defender protege en ecriture
# par defaut (les creations de fichiers y echouent avec une FileNotFoundError
# trompeuse) ; %LOCALAPPDATA% n'est pas concerne par cette protection.
# Sous le Python du Store on vise le profil utilisateur, seul emplacement a la
# fois inaccessible a la redirection MSIX et non protege par Defender.
DATA_DIR_NAME = "Clipper"
OLD_DATA_DIR_NAME = "TwitchClipper"   # nom avant le renommage de l'appli

if is_msix_python():
    _data_parent = os.path.expanduser("~")
else:
    _data_parent = (os.environ.get("LOCALAPPDATA")
                    or os.path.join(os.path.expanduser("~"), "AppData", "Local"))
APPDATA_DIR = os.path.join(_data_parent, DATA_DIR_NAME)
CONFIG_PATH = os.path.join(APPDATA_DIR, "config.json")


def migrate_data_dir():
    """Reprend le dossier de donnees de l'ancien nom, pour ne pas abandonner
    derriere nous les clips deja telecharges et les reglages."""
    old = os.path.join(_data_parent, OLD_DATA_DIR_NAME)
    if os.path.exists(APPDATA_DIR) or not os.path.isdir(old):
        return
    try:
        os.rename(old, APPDATA_DIR)
        sys.stderr.write("[clipper] dossier de donnees repris depuis %s\n" % old)
    except OSError:
        pass

TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # client-id public du lecteur web Twitch

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_CONFIG = {
    "output_dir": os.path.join(APPDATA_DIR, "clips"),
    "cookies_file": os.path.join(BASE_DIR, "cookies.txt"),
    # Vides = "a cote de clipper.py" ; renseignez-les si les executables sont ailleurs.
    "ytdlp_path": "",
    "ffmpeg_dir": "",
    "use_cookies": True,
    "quality": "best",
    "precise_cut": False,
    "filename_template": "{channel}_{date}_{start}-{end}",
    "max_parallel": 1,
    # Mode vertical (9:16) : camera empilee au-dessus du gameplay.
    "vertical": False,
    "vertical_label": True,
    # Decoupes memorisees par chaine : on ne les regle qu'une fois par streamer.
    "layouts": {},
    # Ranges par plateforme : {"twitch": [...], "youtube": [...]}
    "recent_channels": {},
    "favorites": {},
    "last_channel": {},
    "platform": "twitch",
}

_config_lock = threading.Lock()


def ensure_writable_dir(path):
    """Cree le dossier et verifie qu'on peut vraiment y ecrire (mkdir seul ne
    suffit pas a detecter l'Acces controle aux dossiers de Windows Defender,
    qui bloque parfois la creation de FICHIERS tout en laissant passer celle
    de dossiers). Leve une erreur explicite plutot qu'un WinError 2 opaque."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        raise RuntimeError(
            "Impossible d'ecrire dans %s (%s).\n"
            "Cause probable sous Windows : l'Acces controle aux dossiers de\n"
            "Windows Defender protege ce dossier. Solution : Securite Windows >\n"
            "Protection contre les virus et menaces > Gerer les parametres de\n"
            "protection contre les ransomwares > Autoriser une application via\n"
            "l'acces controle aux dossiers > ajoutez python.exe (et yt-dlp.exe,\n"
            "ffmpeg.exe) - ou choisissez un autre dossier de sortie dans Reglages."
            % (path, exc)) from exc


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    # Un dossier de sortie pointant encore vers l'ancien nom serait recree a
    # vide a cote des clips existants : on le suit jusqu'au nouveau nom.
    old_clips = os.path.join(_data_parent, OLD_DATA_DIR_NAME, "clips")
    if os.path.normcase(cfg.get("output_dir") or "") == os.path.normcase(old_clips):
        cfg["output_dir"] = os.path.join(APPDATA_DIR, "clips")
    return cfg


def save_config(cfg):
    with _config_lock:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)


migrate_data_dir()
ensure_writable_dir(APPDATA_DIR)
CONFIG = load_config()


# --------------------------------------------------------------------------- #
#  Utilitaires
# --------------------------------------------------------------------------- #

def ytdlp_path():
    return CONFIG.get("ytdlp_path") or YTDLP


# --------------------------------------------------------------------------- #
#  Plateformes
# --------------------------------------------------------------------------- #

PLATFORMS = ("twitch", "youtube")
RE_YT_ID = re.compile(r"(?:youtu\.be/|[?&]v=|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")
RE_YT_BARE = re.compile(r"^[A-Za-z0-9_-]{11}$")
RE_TW_ID = re.compile(r"(?:twitch\.tv/videos/)?v?(\d{6,})")


def detect_platform(text, fallback="twitch"):
    low = str(text or "").lower()
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube"
    if "twitch.tv" in low:
        return "twitch"
    return fallback if fallback in PLATFORMS else "twitch"


def parse_video_id(value, platform="twitch"):
    value = (value or "").strip()
    if not value:
        return ""
    if detect_platform(value, platform) == "youtube":
        match = RE_YT_ID.search(value)
        if match:
            return match.group(1)
        return value if RE_YT_BARE.match(value) else ""
    match = RE_TW_ID.search(value)
    return match.group(1) if match else ""


def video_url(video_id, platform):
    if platform == "youtube":
        return "https://www.youtube.com/watch?v=%s" % video_id
    return "https://www.twitch.tv/videos/%s" % video_id


def channel_url(login, kind, platform):
    if platform == "youtube":
        tab = {"archives": "videos", "highlights": "streams", "uploads": "shorts"}.get(kind, "videos")
        handle = login if login.startswith(("@", "UC", "http")) else "@" + login
        if handle.startswith("http"):
            return handle.rstrip("/") + "/" + tab
        if handle.startswith("UC"):
            return "https://www.youtube.com/channel/%s/%s" % (handle, tab)
        return "https://www.youtube.com/%s/%s" % (handle, tab)
    return "https://www.twitch.tv/%s/videos?filter=%s&sort=time" % (
        urllib.parse.quote(login), kind)


_js_runtime = None


def js_runtime_args():
    """yt-dlp a besoin d'un moteur JavaScript pour deverrouiller les formats
    YouTube ; sans lui, certains formats manquent et les debits s'effondrent.
    Node est present sur la plupart des postes, on l'utilise s'il existe."""
    global _js_runtime
    if _js_runtime is None:
        _js_runtime = ["--js-runtimes", "node"] if shutil.which("node") else []
    return _js_runtime


def base_args(platform="twitch"):
    args = [ytdlp_path(), "--no-warnings", "--no-playlist"]
    if platform == "youtube":
        args += js_runtime_args()
    return args


def ffmpeg_dir():
    return CONFIG.get("ffmpeg_dir") or BASE_DIR


def ffmpeg_path():
    return os.path.join(ffmpeg_dir(), "ffmpeg.exe")


def ffprobe_path():
    return os.path.join(ffmpeg_dir(), "ffprobe.exe")


_ytdlp_meta = {"version": "", "latest": "", "checked": 0.0}
_ytdlp_lock = threading.Lock()


def ytdlp_version(refresh=False):
    with _ytdlp_lock:
        if _ytdlp_meta["version"] and not refresh:
            return _ytdlp_meta["version"]
    version = ""
    try:
        code, out, err = run_capture([ytdlp_path(), "--version"], timeout=60)
        lines = (out or "").strip().splitlines()
        version = lines[0].strip() if lines else ""
    except Exception:
        pass
    with _ytdlp_lock:
        _ytdlp_meta["version"] = version
    return version


def ytdlp_latest(force=False):
    """Derniere version stable publiee, via l'API GitHub. Silencieux en cas
    d'echec : l'appli doit rester utilisable hors ligne."""
    if not HTTPS_OK:
        return ""
    with _ytdlp_lock:
        fresh = time.time() - _ytdlp_meta["checked"] < 6 * 3600
        if _ytdlp_meta["latest"] and fresh and not force:
            return _ytdlp_meta["latest"]
    latest = ""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Clipper"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            latest = str((json.load(resp) or {}).get("tag_name") or "").strip()
    except Exception:
        pass
    with _ytdlp_lock:
        if latest:
            _ytdlp_meta["latest"] = latest
        _ytdlp_meta["checked"] = time.time()
    return latest or _ytdlp_meta["latest"]


def ytdlp_update():
    """Lance la mise a jour de yt-dlp et renvoie (succes, message)."""
    try:
        code, out, err = run_capture([ytdlp_path(), "-U"], timeout=600)
    except Exception as exc:
        return False, "Echec du lancement de yt-dlp : %s" % exc
    text = "\n".join(p.strip() for p in (out, err) if p and p.strip())
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    message = lines[-1] if lines else "Aucune sortie de yt-dlp."
    ytdlp_version(refresh=True)
    if code != 0:
        # Cas courant sous Windows : le dossier de l'appli est protege en
        # ecriture (Acces controle aux dossiers), yt-dlp ne peut pas se
        # remplacer lui-meme.
        message += ("\nyt-dlp n'a pas pu se remplacer. Si le dossier de l'appli est "
                    "protege par l'Acces controle aux dossiers de Windows Defender, "
                    "autorisez-y yt-dlp.exe, ou remplacez yt-dlp.exe a la main.")
    return code == 0, message


def hhmmss(seconds, sep=("h", "m", "s")):
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return "%02d%s%02d%s%02d%s" % (h, sep[0], m, sep[1], s, sep[2])


def safe_filename(name, maxlen=120):
    name = re.sub(r"[\\/:*?\"<>|%\r\n\t]", " ", str(name))
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:maxlen].strip() or "clip"


def work_dir():
    """Dossier de travail des outils externes. Jamais celui de l'appli : s'il
    est sous Documents, l'Acces controle aux dossiers de Defender empeche
    yt-dlp d'y creer ses fichiers temporaires."""
    folder = os.path.join(tempfile.gettempdir(), "clipper_work")
    try:
        os.makedirs(folder, exist_ok=True)
        return folder
    except OSError:
        return tempfile.gettempdir()


def run_capture(args, timeout=120):
    """Execute une commande et renvoie (returncode, stdout, stderr)."""
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
        cwd=work_dir(),
        timeout=timeout,
    )
    return (proc.returncode,
            (proc.stdout or b"").decode("utf-8", "replace"),
            (proc.stderr or b"").decode("utf-8", "replace"))


def ensure_https_support():
    """Retablit HTTPS sur les installations de type Anaconda.

    Lance hors environnement active, leur python.exe ne trouve pas les DLL
    OpenSSL de <prefix>\\Library\\bin : "import ssl" echoue et urllib repond
    "unknown url type: https". On ajoute ces dossiers puis on reessaie.
    """
    try:
        import ssl  # noqa: F401
        return True
    except ImportError:
        pass
    for sub in ("Library\\bin", "Library\\mingw-w64\\bin", "Library\\usr\\bin", "DLLs"):
        folder = os.path.join(sys.prefix, sub)
        if not os.path.isdir(folder):
            continue
        try:
            os.add_dll_directory(folder)  # Windows, Python 3.8+
        except (AttributeError, OSError):
            pass
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    try:
        import ssl  # noqa: F401
        return True
    except ImportError:
        return False


HTTPS_OK = ensure_https_support()


def gql(query, timeout=20):
    payload = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        TWITCH_GQL,
        data=payload,
        headers={"Client-ID": TWITCH_CLIENT_ID, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def gql_escape(value):
    return json.dumps(str(value))


# --------------------------------------------------------------------------- #
#  Listing des VOD d'une chaine
# --------------------------------------------------------------------------- #

_list_cache = OrderedDict()
_list_cache_lock = threading.Lock()
CACHE_TTL = 180  # secondes


def cache_get(key):
    with _list_cache_lock:
        hit = _list_cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        if hit:
            _list_cache.pop(key, None)
    return None


def cache_put(key, value):
    with _list_cache_lock:
        _list_cache[key] = (time.time(), value)
        while len(_list_cache) > 40:
            _list_cache.popitem(last=False)


BROADCAST_TYPES = {
    "archives": "ARCHIVE",
    "highlights": "HIGHLIGHT",
    "uploads": "UPLOAD",
    "all": None,
}


def list_videos_gql(login, kind="archives", limit=30, cursor=None):
    btype = BROADCAST_TYPES.get(kind, "ARCHIVE")
    type_arg = ", type: %s" % btype if btype else ""
    after = ", after: %s" % gql_escape(cursor) if cursor else ""
    query = """
    query {
      user(login: %s) {
        id
        login
        displayName
        profileImageURL(width: 70)
        videos(first: %d%s%s, sort: TIME) {
          pageInfo { hasNextPage }
          edges {
            cursor
            node {
              id title lengthSeconds createdAt viewCount broadcastType
              previewThumbnailURL(width: 320, height: 180)
              game { displayName }
            }
          }
        }
      }
    }
    """ % (gql_escape(login), int(limit), type_arg, after)

    data = gql(query)
    user = (data.get("data") or {}).get("user")
    if not user:
        raise LookupError("Chaine introuvable : %s" % login)

    videos = user.get("videos") or {"edges": [], "pageInfo": {}}
    items = []
    last_cursor = None
    for edge in videos.get("edges") or []:
        node = edge.get("node") or {}
        last_cursor = edge.get("cursor") or last_cursor
        items.append({
            "id": node.get("id"),
            "url": "https://www.twitch.tv/videos/%s" % node.get("id"),
            "title": node.get("title") or "(sans titre)",
            "duration": node.get("lengthSeconds") or 0,
            "created_at": node.get("createdAt"),
            "views": node.get("viewCount") or 0,
            "thumbnail": node.get("previewThumbnailURL") or "",
            "game": (node.get("game") or {}).get("displayName") or "",
            "kind": (node.get("broadcastType") or "").lower(),
        })
    return {
        "channel": {
            "login": user.get("login") or login,
            "display_name": user.get("displayName") or login,
            "avatar": user.get("profileImageURL") or "",
        },
        "videos": items,
        "cursor": last_cursor,
        "has_more": bool((videos.get("pageInfo") or {}).get("hasNextPage")),
        "source": "twitch",
    }


def list_videos_ytdlp(login, kind="archives", limit=30):
    """Repli si l'API publique Twitch change : on passe par yt-dlp."""
    url = "https://www.twitch.tv/%s/videos?filter=%s&sort=time" % (
        urllib.parse.quote(login), kind)
    code, out, err = run_capture(
        [ytdlp_path(), "--flat-playlist", "-J", "--no-warnings",
         "--playlist-items", "1:%d" % int(limit), url],
        timeout=180)
    out = out.strip()
    if not out:
        raise LookupError((err.strip().splitlines() or ["Chaine introuvable"])[-1])
    data = json.loads(out)
    items = []
    for entry in data.get("entries") or []:
        vid = str(entry.get("id") or "").lstrip("vV")
        items.append({
            "id": vid,
            "url": entry.get("url") or "https://www.twitch.tv/videos/%s" % vid,
            "title": entry.get("title") or "(sans titre)",
            "duration": entry.get("duration") or 0,
            "created_at": None,
            "views": entry.get("view_count") or 0,
            "thumbnail": entry.get("thumbnail") or "",
            "game": "",
            "kind": kind,
        })
    return {
        "channel": {"login": login, "display_name": login, "avatar": ""},
        "videos": items,
        "cursor": None,
        "has_more": False,
        "source": "yt-dlp",
    }


def list_videos_youtube(login, kind="archives", limit=30):
    """Liste les videos d'une chaine YouTube via yt-dlp (pas d'API publique
    equivalente au GraphQL de Twitch)."""
    url = channel_url(login, kind, "youtube")
    args = ([ytdlp_path(), "--flat-playlist", "-J", "--no-warnings"] + js_runtime_args()
            + ["--playlist-items", "1:%d" % int(limit)])
    out, err, _ = run_ytdlp(args, url, "youtube")
    out = out.strip()
    if not out or out == "null":
        raise LookupError(friendly_error(err) or "Chaine YouTube introuvable : %s" % login)
    data = json.loads(out)
    items = []
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        thumbs = entry.get("thumbnails") or []
        items.append({
            "id": entry["id"],
            "url": video_url(entry["id"], "youtube"),
            "title": entry.get("title") or "(sans titre)",
            "duration": entry.get("duration") or 0,
            "created_at": None,
            "views": entry.get("view_count") or 0,
            "thumbnail": (entry.get("thumbnail")
                          or (thumbs[0].get("url") if thumbs else "")
                          or "https://i.ytimg.com/vi/%s/hqdefault.jpg" % entry["id"]),
            "game": "",
            "kind": kind,
        })
    return {
        "channel": {"login": login,
                    "display_name": data.get("channel") or data.get("uploader") or login,
                    "avatar": ""},
        "videos": items,
        "cursor": None,
        "has_more": False,
        "source": "yt-dlp",
        "platform": "youtube",
    }


def list_videos(login, kind="archives", limit=30, cursor=None, platform="twitch"):
    key = (platform, login.lower(), kind, int(limit), cursor or "")
    cached = cache_get(key)
    if cached:
        return cached

    if platform == "youtube":
        result = list_videos_youtube(login, kind, limit)
    elif not HTTPS_OK:  # inutile d'essayer : Python n'a pas de support HTTPS ici
        result = list_videos_ytdlp(login, kind, limit)
    else:
        try:
            result = list_videos_gql(login, kind, limit, cursor)
        except LookupError:
            raise
        except Exception as exc:  # API publique indisponible -> repli yt-dlp
            sys.stderr.write("[clipper] GraphQL indisponible (%s), repli yt-dlp\n" % exc)
            result = list_videos_ytdlp(login, kind, limit)
    result.setdefault("platform", platform)
    cache_put(key, result)
    return result


# --------------------------------------------------------------------------- #
#  Metadonnees d'une VOD
# --------------------------------------------------------------------------- #

_vod_cache = {}
_vod_lock = threading.Lock()


def friendly_error(stderr_text):
    """Traduit les messages yt-dlp les plus frequents en conseil actionnable."""
    message = ""
    for line in (stderr_text or "").splitlines():
        if line.startswith("ERROR:"):
            message = line.split(":", 1)[1].strip()
    low = message.lower()
    if "confirm you" in low and "bot" in low:
        return ("YouTube demande une authentification (protection anti-robot). "
                "Exportez vos cookies YouTube dans cookies.txt : le meme fichier "
                "peut contenir Twitch et YouTube.")
    if "must be logged into an account" in low:
        return ("Contenu reserve aux abonnes et aucun cookie valide n'a ete "
                "transmis. Verifiez cookies.txt (il a peut-etre expire).")
    if "does not have access" in low:
        return "Votre compte n'a pas acces a ce contenu reserve aux abonnes."
    if "private video" in low or "video unavailable" in low:
        return "Video privee ou indisponible."
    if "members-only" in low or "join this channel" in low:
        return "Video reservee aux membres de la chaine."
    # Chaine absente : arrive surtout quand on cherche un nom de chaine Twitch
    # alors que l'onglet YouTube est actif (ou l'inverse).
    if "unable to download api page" in low or "404" in low or "not found" in low:
        return ("Chaine introuvable. Verifiez l'onglet de plateforme en haut a "
                "gauche : ce nom existe peut-etre sur l'autre plateforme.")
    if "does not have a videos tab" in low or "this channel does not have" in low:
        return "Cette chaine n'a pas d'onglet correspondant a cette categorie."
    return message


def cookies_file():
    """Fichier de cookies effectif ; si le chemin enregistre n'existe plus
    (dossier deplace), on retombe sur le cookies.txt voisin du script."""
    path = CONFIG.get("cookies_file") or ""
    if path and os.path.isfile(path):
        return path
    fallback = os.path.join(BASE_DIR, "cookies.txt")
    return fallback if os.path.isfile(fallback) else ""


COOKIE_JAR = os.path.join(APPDATA_DIR, "cookies.work.txt")
_cookie_lock = threading.Lock()


def cookie_jar():
    """Copie de travail persistante des cookies, a donner a yt-dlp.

    yt-dlp reecrit son fichier de cookies en fin d'execution : la session
    Google y tourne a chaque appel. Lui confier directement le cookies.txt de
    l'utilisateur le modifie sans prevenir ; lui donner une copie jetable perd
    la rotation, et YouTube finit par repondre "Sign in to confirm you're not
    a bot". Une copie qui vit dans nos donnees resout les deux : elle encaisse
    les reecritures, et on la rafraichit des que la source est plus recente.
    """
    source = cookies_file()
    if not source:
        return ""
    with _cookie_lock:
        try:
            if (not os.path.isfile(COOKIE_JAR)
                    or os.path.getsize(COOKIE_JAR) < 64
                    or os.path.getmtime(source) > os.path.getmtime(COOKIE_JAR)):
                shutil.copyfile(source, COOKIE_JAR)
            return COOKIE_JAR
        except OSError:
            return source


def restore_jar():
    """Remet la copie de travail a l'etat du cookies.txt de l'utilisateur.

    Quand yt-dlp echoue, il reecrit quand meme son fichier de cookies, ampute
    de ce que le serveur vient de refuser (mesure : 6199 puis 5930 octets).
    Sans cette remise a plat, chaque echec grignote la copie jusqu'a la rendre
    inutilisable et l'authentification se degrade toute seule.
    """
    source = cookies_file()
    if not source or not os.path.isfile(COOKIE_JAR):
        return
    with _cookie_lock:
        try:
            shutil.copyfile(source, COOKIE_JAR)
        except OSError:
            pass


def cookie_args():
    if not CONFIG.get("use_cookies"):
        return []
    path = cookie_jar()
    return ["--cookies", path] if path else []


RE_NEEDS_AUTH = re.compile(
    r"confirm you|sign in|log in|not a bot|members-only|join this channel|"
    r"is private|private video|age.restricted|confirm your age|"
    r"subscriber|--cookies", re.I)


def needs_cookies(stderr_text):
    """YouTube ne reclame une authentification que ponctuellement (protection
    anti-robot, contenu restreint). On distingue ces cas d'une vraie panne."""
    return bool(RE_NEEDS_AUTH.search(stderr_text or ""))


def attempts_for(platform):
    """Ordre des tentatives d'authentification.

    Sur Twitch les cookies servent des le depart (VOD reservees aux abonnes).
    Sur YouTube on commence anonyme : la plupart des videos sont publiques, et
    rattacher des telechargements repetes a un compte Google expose celui-ci a
    une restriction. Les cookies ne sortent que si YouTube les exige.
    """
    cookies = cookie_args()
    if platform != "youtube":
        return [cookies]
    return [[], cookies] if cookies else [[]]


RE_BOT = re.compile(r"not a bot|too many requests|HTTP Error 429|rate.?limit", re.I)


def capture_patient(args, timeout=180, essais=3):
    """run_capture avec reprise sur le controle anti-robot de YouTube.

    Celui-ci se declenche par a-coups sur des appels rapproches et repond en
    deux secondes : quelques secondes d'attente valent mieux qu'un echec sec.
    """
    code, out, err = 1, "", ""
    for tour in range(essais):
        code, out, err = run_capture(args, timeout=timeout)
        if (out.strip() and out.strip() != "null") or not RE_BOT.search(err):
            return code, out, err
        if COOKIE_JAR in args:
            restore_jar()
        if tour + 1 < essais:
            time.sleep(3 * (tour + 1))
    return code, out, err


def run_ytdlp(args, url, platform, timeout=180):
    """Execute yt-dlp en montant l'authentification d'un cran si besoin.
    Renvoie (stdout, stderr, cookies_utilises)."""
    out = err = ""
    for extra in attempts_for(platform):
        code, out, err = capture_patient(args + extra + [url], timeout=timeout)
        if out.strip() and out.strip() != "null":
            return out, err, bool(extra)
        if not needs_cookies(err):
            break
    return out, err, False


def vod_info(video_id, platform="twitch"):
    if platform != "youtube":
        video_id = str(video_id).lstrip("vV")
    cache_key = (platform, video_id)
    with _vod_lock:
        hit = _vod_cache.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    url = video_url(video_id, platform)
    out, err, _ = run_ytdlp(base_args(platform) + ["-J"], url, platform)
    out = out.strip()
    if not out or out == "null":
        raise LookupError(friendly_error(err) or "Video inaccessible (%s)" % video_id)

    data = json.loads(out)
    qualities, seen = [], set()
    for fmt in data.get("formats") or []:
        height = fmt.get("height")
        if not height or fmt.get("ext") == "mhtml" or height in seen:
            continue
        seen.add(height)
        fps = int(fmt.get("fps") or 0)
        # Libelle calcule plutot que l'identifiant de format : celui de YouTube
        # est un code interne ("313", "137") illisible pour l'utilisateur.
        qualities.append({
            "height": height,
            "fps": fps,
            "label": "%dp%s" % (height, fps if fps >= 50 else ""),
        })
    qualities.sort(key=lambda q: q["height"], reverse=True)

    info = {
        "id": video_id,
        "url": url,
        "title": data.get("title") or "(sans titre)",
        "duration": float(data.get("duration") or 0),
        "timestamp": data.get("timestamp"),
        "upload_date": data.get("upload_date"),
        "channel": (data.get("channel") or data.get("uploader_id")
                    or data.get("uploader") or "").lstrip("@"),
        "channel_name": data.get("uploader") or data.get("channel") or "",
        "views": data.get("view_count") or 0,
        "thumbnail": data.get("thumbnail") or "",
        "qualities": qualities,
        "platform": platform,
    }
    with _vod_lock:
        _vod_cache[cache_key] = (time.time(), info)
    return info


# --------------------------------------------------------------------------- #
#  Image de reperage et composition verticale
# --------------------------------------------------------------------------- #

VERTICAL_W, VERTICAL_H = 1080, 1920
DEFAULT_SPLIT = 0.32       # part de hauteur occupee par la camera
# Cadrage de reference releve a la main sur une source 1920x1080.
REF_W, REF_H = 1920, 1080
REF_CAM = (0, 0, 499, 283)
REF_GAME = (514, 0, 893, 1080)
LOGO_SIZE = 84             # cote du logo incruste, en pixels de sortie
LABEL_SIZE = 54            # taille du nom de la chaine
_stream_cache = {}
_stream_lock = threading.Lock()


def stream_url(video_id, platform="twitch"):
    """URL directe du flux video, mise en cache : l'extraction d'une image de
    reperage doit rester quasi instantanee quand on deplace le curseur."""
    key = (platform, video_id)
    with _stream_lock:
        hit = _stream_cache.get(key)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
    out, err, _ = run_ytdlp(base_args(platform) + ["-g", "-f", format_selector("1080", platform)],
                            video_url(video_id, platform), platform)
    url = (out or "").strip().splitlines()
    if not url:
        raise LookupError(friendly_error(err) or "Flux introuvable (%s)" % video_id)
    with _stream_lock:
        _stream_cache[key] = (time.time(), url[0])
    return url[0]


def extract_frame_direct(video_id, seconds, platform):
    """Saut directement dans le flux distant. Rapide, mais ne fonctionne que si
    le serveur accepte les requetes par plage (cas de Twitch).

    Saut en deux temps : un saut rapide quelques secondes avant, puis un saut
    precis decode depuis l'image-cle. Un saut rapide seul tombe parfois au
    milieu d'un segment et rend une image a moitie noire.
    """
    lead = min(4.0, seconds)
    args = [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
            "-ss", "%.3f" % (seconds - lead),
            "-i", stream_url(video_id, platform),
            "-ss", "%.3f" % lead,
            "-frames:v", "1", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=NO_WINDOW, timeout=90)
    return proc.stdout


def extract_frame_ytdlp(video_id, seconds, platform):
    """Repli : on fait telecharger un fragment d'une seconde par yt-dlp, puis on
    en tire une image. Necessaire pour YouTube, dont les URL de flux sont
    bridees des qu'on les attaque directement avec ffmpeg."""
    folder = os.path.join(tempfile.gettempdir(), "clipper_frames")
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, "f%d" % int(time.time() * 1000))
    args = (base_args(platform) + [
        "--ffmpeg-location", ffmpeg_dir(),
        "--download-sections", "*%.2f-%.2f" % (seconds, seconds + 1.0),
        "-f", format_selector("720", platform),
        "-o", base + ".%(ext)s"] + cookie_args() + [video_url(video_id, platform)])
    run_capture(args, timeout=240)

    produced = None
    for name in sorted(os.listdir(folder)):
        if name.startswith(os.path.basename(base)) and not name.endswith((".part", ".ytdl")):
            produced = os.path.join(folder, name)
            break
    if not produced:
        return b""
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", produced,
             "-frames:v", "1", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW, timeout=90)
        return proc.stdout
    finally:
        try:
            os.remove(produced)
        except OSError:
            pass


def extract_frame_fast(video_id, seconds):
    """Image de reperage YouTube par plages d'octets (meme principe que le
    telechargement rapide). Quelques secondes, contre plus d'une minute en
    faisant telecharger un fragment entier par yt-dlp."""
    pris = None
    for tentative in range(2):
        video, _audio = formats_with_auth(video_id, "720")
        if not video:
            return b""
        pris = fetch_span(base_url_of(video), video.get("http_headers"),
                          seconds, seconds + 0.5, marge=1.0)
        if pris:
            break
        # Les URL memorisees ont pu expirer : on oublie et on redemande une fois.
        forget_formats(video_id)
    if not pris:
        return b""
    donnees, depart = pris
    dossier = work_dir()
    brut = os.path.join(dossier, "frame_%d.mp4" % int(time.time() * 1000))
    try:
        with open(brut, "wb") as fh:
            fh.write(donnees)
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
             "-ss", "%.3f" % max(0.0, seconds - depart), "-i", brut,
             "-frames:v", "1", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW, cwd=dossier, timeout=120)
        return proc.stdout or b""
    finally:
        try:
            os.remove(brut)
        except OSError:
            pass


def extract_frame(video_id, seconds, platform="twitch"):
    seconds = max(0.0, float(seconds))
    if platform == "twitch":
        image = extract_frame_direct(video_id, seconds, platform)
        if image:
            return image
    else:
        try:
            image = extract_frame_fast(video_id, seconds)
            if image:
                return image
        except LookupError:
            # Refus d'acces : le repli yt-dlp se heurterait au meme mur, en
            # une minute et demie au lieu de deux secondes. On le dit tout de
            # suite plutot que de faire patienter pour rien.
            raise
        except Exception as exc:
            sys.stderr.write("[clipper] image rapide indisponible (%s)\n" % exc)
    image = extract_frame_ytdlp(video_id, seconds, platform)
    if not image:
        raise LookupError("Image indisponible a ce timecode.")
    return image


def font_file():
    folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in ("arialbd.ttf", "arial.ttf", "segoeuib.ttf", "seguisb.ttf"):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return ""


def even(value):
    value = int(round(value))
    return value - (value % 2)


def clamp_box(box, frame_w, frame_h, fallback):
    """Ramene une zone (x, y, w, h) dans les limites de l'image source."""
    try:
        x, y, w, h = [float(v) for v in box]
    except (TypeError, ValueError):
        x, y, w, h = fallback
    w = max(16.0, min(float(w), frame_w))
    h = max(16.0, min(float(h), frame_h))
    x = max(0.0, min(float(x), frame_w - w))
    y = max(0.0, min(float(y), frame_h - h))
    return even(x), even(y), even(w), even(h)


RE_LABEL = re.compile(r"[^0-9A-Za-z _.\-]")


def logo_file(platform="twitch"):
    """Logo optionnel incruste a cote du nom de la chaine.

    Un logo par plateforme : incruster le logo Twitch sur un extrait YouTube
    serait faux. Sans fichier correspondant, seul le nom est affiche.
    """
    noms = {
        "twitch": ("Logo.png", "logo.png", "Logo_twitch.png"),
        "youtube": ("Logo_youtube.png", "logo_youtube.png", "Logo.youtube.png"),
    }.get(platform, ("Logo.png",))
    for name in noms:
        path = os.path.join(BASE_DIR, name)
        if os.path.isfile(path):
            return path
    return ""


def vertical_filter(layout, label, frame_w, frame_h, with_logo=False):
    """Graphe de filtres : camera en haut, gameplay dessous, nom de la chaine.

    Chaque zone est mise a l'echelle en 'couvrir' (agrandissement puis recadrage
    centre) pour que l'utilisateur n'ait pas a respecter un rapport precis en
    tracant ses cadres.
    """
    split = min(0.7, max(0.15, float(layout.get("split") or DEFAULT_SPLIT)))
    cam_h = even(VERTICAL_H * split)
    game_h = VERTICAL_H - cam_h

    # Les cadres ont ete traces sur une image de reperage dont la definition
    # n'est pas forcement celle du clip telecharge (image 720p contre clip
    # 1080p, ou l'inverse selon la qualite choisie). On les remet a l'echelle
    # de la source : sans cela le cadrage est faux des que les deux different.
    ref = layout.get("frame") or (REF_W, REF_H)
    try:
        ref_w = float(ref[0]) or REF_W
        ref_h = float(ref[1]) or REF_H
    except (TypeError, ValueError, IndexError):
        ref_w, ref_h = REF_W, REF_H
    rx, ry = frame_w / ref_w, frame_h / ref_h
    rescale = lambda b: (b[0] * rx, b[1] * ry, b[2] * rx, b[3] * ry) if b else None

    dx, dy = frame_w / float(REF_W), frame_h / float(REF_H)
    default = lambda b: (b[0] * dx, b[1] * dy, b[2] * dx, b[3] * dy)
    cx, cy, cw, ch = clamp_box(rescale(layout.get("cam")), frame_w, frame_h, default(REF_CAM))
    gx, gy, gw, gh = clamp_box(rescale(layout.get("game")), frame_w, frame_h, default(REF_GAME))

    cover = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1"
             % (VERTICAL_W, cam_h, VERTICAL_W, cam_h))
    cover_game = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1"
                  % (VERTICAL_W, game_h, VERTICAL_W, game_h))

    graph = ("[0:v]crop=%d:%d:%d:%d,%s[cam];"
             "[0:v]crop=%d:%d:%d:%d,%s[game];"
             "[cam][game]vstack=inputs=2[st]"
             % (cw, ch, cx, cy, cover, gw, gh, gx, gy, cover_game))

    text = RE_LABEL.sub("", str(label or "")).strip()[:40]
    font = font_file()
    if not text or not font:
        return graph + ";[st]null[v]"

    top = cam_h + 40          # juste sous la camera, comme sur la maquette
    source = "st"
    text_x = 48
    if with_logo:
        graph += (";[1:v]scale=%d:%d[logo];[st][logo]overlay=48:%d[withlogo]"
                  % (LOGO_SIZE, LOGO_SIZE, top))
        source = "withlogo"
        text_x = 48 + LOGO_SIZE + 22

    # Le chemin de la police doit etre entre apostrophes ET son ':' echappe :
    # sans les apostrophes, ffmpeg coupe l'option sur le ':' du lecteur.
    graph += (";[%s]drawtext=fontfile='%s':text='%s':fontcolor=white:fontsize=%d:"
              "shadowcolor=black@0.65:shadowx=3:shadowy=3:x=%d:y=%d[v]"
              % (source, font.replace("\\", "/").replace(":", "\\:"), text,
                 LABEL_SIZE, text_x, top + (LOGO_SIZE - LABEL_SIZE) // 2))
    return graph


def source_size(path):
    code, out, err = run_capture(
        [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path], timeout=60)
    try:
        w, h = (out or "").strip().splitlines()[0].split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080


# --------------------------------------------------------------------------- #
#  Telechargement (file d'attente)
# --------------------------------------------------------------------------- #

RE_TIME = re.compile(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
RE_SPEED = re.compile(r"speed=\s*([0-9.]+)x")
RE_PROG = re.compile(r"^PROG\|([^|]*)\|\s*([0-9.]+)%\|([^|]*)\|([^|]*)")
RE_DEST = re.compile(r"^\[download\] Destination:")
RE_FORMAT = re.compile(r"Downloading \d+ format\(s\): (.+)$")
RE_FFMPEG_ERROR = re.compile(
    r"^(Error|\[AVFilterGraph|\[libx264 @ .*Error)|Invalid argument|No option name|"
    r"Conversion failed|Unable to (?:find|open)")


class Job(object):
    _seq = 0
    _seq_lock = threading.Lock()

    def __init__(self, spec):
        with Job._seq_lock:
            Job._seq += 1
            self.id = "j%d" % Job._seq
        self.spec = spec
        self.state = "queued"          # queued | running | done | error | canceled
        self.phase = "En attente"
        self.progress = 0.0
        # Plage de la barre pour l'etape en cours : le montage vertical ajoute
        # une seconde etape, la barre ne doit pas repartir de zero.
        self.span = (0.0, 100.0)
        self.speed = ""
        self.eta = ""
        self.error = ""
        self.path = ""
        self.size = 0
        self.log = []
        self.created = time.time()
        self.started = None
        self.finished = None
        self.proc = None
        self.canceled = False
        self.lock = threading.Lock()

    def add_log(self, line):
        with self.lock:
            self.log.append(line)
            if len(self.log) > 300:
                del self.log[:100]

    def as_dict(self):
        with self.lock:
            spec = self.spec
            return {
                "id": self.id,
                "state": self.state,
                "phase": self.phase,
                "progress": round(self.progress, 1),
                "speed": self.speed,
                "eta": self.eta,
                "error": self.error,
                "path": self.path,
                "size": self.size,
                "title": spec.get("title", ""),
                "channel": spec.get("channel", ""),
                "video_id": spec.get("video_id", ""),
                "start": spec.get("start", 0),
                "end": spec.get("end", 0),
                "duration": max(0.0, spec.get("end", 0) - spec.get("start", 0)),
                "quality": spec.get("quality", "best"),
                "precise": spec.get("precise", False),
                "created": self.created,
                "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1),
                "log": self.log[-40:],
            }


class JobManager(object):
    def __init__(self):
        self.jobs = OrderedDict()
        self.queue = []
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.workers = []
        self._ensure_workers()

    def _ensure_workers(self):
        want = max(1, min(3, int(CONFIG.get("max_parallel", 1) or 1)))
        while len(self.workers) < want:
            th = threading.Thread(target=self._worker, daemon=True)
            th.start()
            self.workers.append(th)

    def submit(self, spec):
        self._ensure_workers()
        job = Job(spec)
        with self.wake:
            self.jobs[job.id] = job
            self.queue.append(job)
            self.wake.notify()
        return job

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def list(self):
        with self.lock:
            jobs = list(self.jobs.values())
        return [j.as_dict() for j in jobs]

    def clear_finished(self):
        with self.lock:
            for jid in [j.id for j in self.jobs.values()
                        if j.state in ("done", "error", "canceled")]:
                self.jobs.pop(jid, None)

    def cancel(self, job_id):
        job = self.get(job_id)
        if not job:
            return False
        job.canceled = True
        with self.wake:
            if job in self.queue:
                self.queue.remove(job)
                job.state = "canceled"
                job.phase = "Annule"
                job.finished = time.time()
                return True
        proc = job.proc
        if proc and proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=NO_WINDOW, timeout=15)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return True

    def _worker(self):
        while True:
            with self.wake:
                while not self.queue:
                    self.wake.wait()
                job = self.queue.pop(0)
            try:
                run_job(job)
            except Exception as exc:  # filet de securite
                job.state = "error"
                job.phase = "Erreur"
                job.error = str(exc)
                job.finished = time.time()


JOBS = JobManager()


_base_lock = threading.Lock()
_reserved_bases = set()


def build_output_base(spec):
    with _base_lock:
        return _build_output_base(spec)


def _build_output_base(spec):
    out_dir = CONFIG.get("output_dir") or os.path.join(APPDATA_DIR, "clips")
    ensure_writable_dir(out_dir)
    template = CONFIG.get("filename_template") or DEFAULT_CONFIG["filename_template"]
    fields = {
        "channel": safe_filename(spec.get("channel") or "twitch", 40),
        "title": safe_filename(spec.get("title") or "clip", 60),
        "date": safe_filename(spec.get("date") or time.strftime("%Y-%m-%d"), 20),
        "start": hhmmss(spec.get("start", 0)),
        "end": hhmmss(spec.get("end", 0)),
        "dur": hhmmss(max(0.0, spec.get("end", 0) - spec.get("start", 0))),
        "id": safe_filename(spec.get("video_id") or "", 20),
        "quality": safe_filename(spec.get("quality") or "best", 10),
    }
    try:
        name = template.format(**fields)
    except Exception:
        name = "{channel}_{date}_{start}-{end}".format(**fields)
    name = safe_filename(name)

    exts = (".mp4", ".mkv", ".m4a", ".webm", ".mp3", ".opus")

    def taken(candidate):
        return (candidate in _reserved_bases
                or any(os.path.exists(candidate + ext) for ext in exts))

    base = os.path.join(out_dir, name)
    if taken(base):
        suffix = 2
        while taken("%s (%d)" % (base, suffix)):
            suffix += 1
        base = "%s (%d)" % (base, suffix)
    _reserved_bases.add(base)  # evite la collision entre jobs simultanes
    return base


AUDIO_QUALITIES = ("audio", "mp3")


def format_selector(quality, platform="twitch"):
    if quality in AUDIO_QUALITIES:
        # m4a d'abord : l'extension annoncee correspond alors au contenu.
        return "ba[ext=m4a]/ba/b"

    limit = ""
    if quality and quality != "best":
        try:
            limit = "[height<=%d]" % int(str(quality).rstrip("p"))
        except ValueError:
            limit = ""

    if platform != "youtube":
        return "bv*%s+ba/b%s/bv*+ba/b" % (limit, limit)

    # Deux preferences propres a YouTube, dans cet ordre :
    #  - https plutot que HLS : chercher un point precis dans un long manifeste
    #    HLS oblige ffmpeg a le parcourir (plus de 3 min sur une video d'1 h) ;
    #  - H.264 + AAC plutot qu'AV1/VP9 + Opus : ces derniers font trebucher
    #    nombre de lecteurs et de logiciels de montage.
    return ("bv*[vcodec^=avc1][protocol^=https]%s+ba[acodec^=mp4a]/"
            "bv*[vcodec^=avc1]%s+ba/"
            "bv*[protocol^=https]%s+ba/"
            "b%s/b" % (limit, limit, limit, limit))


# --------------------------------------------------------------------------- #
#  Extraction rapide YouTube : on vise les octets utiles au lieu de laisser
#  ffmpeg lire le flux depuis son debut.
#
#  Mesure a l'origine de ce chemin : un extrait de 30 s pris a 1 h 21 dans une
#  video de 6 h 33 ne se terminait pas en 15 minutes (ffmpeg transferait 1,2 Go).
#  En lisant l'index "sidx" du MP4 fragmente, on ne telecharge que 15,6 Mo.
# --------------------------------------------------------------------------- #

def mp4_boxes(data):
    """Boites MP4 de premier niveau : [(nom, offset, taille)]."""
    out, pos = [], 0
    while pos + 8 <= len(data):
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        name = data[pos + 4:pos + 8].decode("ascii", "replace")
        if size < 8:
            break
        out.append((name, pos, size))
        pos += size
    return out


def http_range(url, headers, start, end, timeout=300):
    req = urllib.request.Request(
        url, headers=dict(headers or {}, Range="bytes=%d-%d" % (start, end)))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def read_sidx(url, headers):
    """Lit l'index du flux. Renvoie (init, segments) ou None si absent.

    init      : ftyp + moov, sans la boite sidx (son index decrit le fichier
                complet ; le laisser ferait chercher la suite a 1,2 Go).
    segments  : [(debut_s, fin_s, offset_octet, taille_octet)]
    """
    head = http_range(url, headers, 0, 262143)
    boxes = mp4_boxes(head)
    index = {name: (offset, size) for name, offset, size in boxes}
    if "sidx" not in index or "moov" not in index:
        return None

    init = b"".join(head[o:o + s] for n, o, s in boxes if n in ("ftyp", "moov"))
    sidx_off, sidx_size = index["sidx"]
    body = head[sidx_off + 8:sidx_off + sidx_size]
    if len(body) < sidx_size - 8:
        return None

    version = body[0]
    timescale = struct.unpack(">I", body[8:12])[0] or 1
    if version == 0:
        earliest, first_offset = struct.unpack(">II", body[12:20])
        pos = 20
    else:
        earliest, first_offset = struct.unpack(">QQ", body[12:28])
        pos = 28
    pos += 2  # reserved
    count = struct.unpack(">H", body[pos:pos + 2])[0]
    pos += 2

    segments = []
    moment = earliest / float(timescale)
    octet = sidx_off + sidx_size + first_offset
    for _ in range(count):
        if pos + 12 > len(body):
            break
        ref, duration, _sap = struct.unpack(">III", body[pos:pos + 12])
        pos += 12
        taille = ref & 0x7FFFFFFF
        duree = duration / float(timescale)
        segments.append((moment, moment + duree, octet, taille))
        moment += duree
        octet += taille
    return (init, segments) if segments else None


def fetch_span(url, headers, start, end, marge=2.0):
    """Telecharge l'init et les seuls segments couvrant [start, end].
    Renvoie (donnees, instant_de_depart) ou None."""
    lu = read_sidx(url, headers)
    if not lu:
        return None
    init, segments = lu
    vises = [s for s in segments if s[1] > start - marge and s[0] < end + marge]
    if not vises:
        return None
    premier, dernier = vises[0], vises[-1]
    corps = http_range(url, headers, premier[2], dernier[2] + dernier[3] - 1)
    return init + corps, premier[0]


def pick_formats(data, quality):
    """Meilleur couple (video avc1, audio mp4a) parmi les formats fragmentes."""
    formats = [f for f in (data.get("formats") or []) if isinstance(f, dict) and f.get("fragments")]

    hauteur = 1080
    try:
        hauteur = int(str(quality).rstrip("p"))
    except (TypeError, ValueError):
        pass

    videos = [f for f in formats
              if str(f.get("vcodec") or "").startswith("avc1") and (f.get("height") or 0) <= hauteur]
    audios = [f for f in formats if f.get("vcodec") == "none"
              and str(f.get("acodec") or "").startswith("mp4a")]
    if not videos or not audios:
        return None, None
    videos.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    audios.sort(key=lambda f: f.get("abr") or 0, reverse=True)
    return videos[0], audios[0]


_formats_cache = {}
_formats_lock = threading.Lock()
FORMATS_TTL = 3 * 3600          # les URL googlevideo tiennent plusieurs heures


def cached_formats(video_id, quality, cookies, essais=3):
    """youtube_formats avec memoire courte.

    Placer les cadres du mode vertical demande une image de reperage a chaque
    deplacement du curseur : redemander la liste des formats coutait une dizaine
    de secondes pour un resultat identique.
    """
    cle = (video_id, str(quality))
    with _formats_lock:
        trouve = _formats_cache.get(cle)
    if trouve and time.time() - trouve[0] < FORMATS_TTL:
        return trouve[1], trouve[2]
    video, audio = youtube_formats(video_id, quality, cookies, essais)
    if video and audio:
        with _formats_lock:
            _formats_cache[cle] = (time.time(), video, audio)
    return video, audio


def forget_formats(video_id):
    """A appeler quand une plage d'octets est refusee : les URL ont expire."""
    with _formats_lock:
        for cle in [k for k in _formats_cache if k[0] == video_id]:
            _formats_cache.pop(cle, None)


def formats_with_auth(video_id, quality):
    """Formats fragmentes en montant l'authentification d'un cran si besoin.

    Renvoie (video, audio) ; leve LookupError si YouTube refuse jusqu'au bout.
    """
    video = audio = None
    refus = None
    auth = attempts_for("youtube")           # anonyme d'abord, cookies ensuite
    for index, essai in enumerate(auth):
        # Inutile d'insister en anonyme quand des cookies attendent juste
        # derriere : passer au cran suivant est plus rapide qu'une reprise.
        final = index + 1 == len(auth)
        try:
            video, audio = cached_formats(video_id, quality, essai,
                                          essais=3 if final else 1)
        except LookupError as exc:
            refus = exc
            continue
        if video:
            return video, audio
    if refus:
        raise refus
    return None, None


def youtube_formats(video_id, quality, cookies, essais=3):
    """Formats fragmentes (video + audio) avec leurs en-tetes d'origine.

    YouTube ne sert pas toujours la meme liste : selon le client retenu par
    yt-dlp, la reponse retombe parfois a cinq formats sans piste audio mp4a,
    inexploitables pour une decoupe par plages d'octets (mesure : une fois sur
    deux). Redemander coute quelques secondes et evite de basculer pour rien
    sur le telechargement integral.
    """
    args = (base_args("youtube") + ["-J", "--extractor-args", "youtube:formats=dashy"]
            + cookies + [video_url(video_id, "youtube")])
    err = ""
    for tour in range(max(1, essais)):
        code, out, err = capture_patient(args, timeout=240, essais=1)
        if out.strip() and out.strip() != "null":
            video, audio = pick_formats(json.loads(out), quality)
            if video and audio:
                return video, audio
            err = ""            # la video existe, c'est la liste qui est pauvre
        elif not RE_BOT.search(err):
            break               # vrai refus : insister n'apportera rien
        elif COOKIE_JAR in args:
            restore_jar()
        if tour + 1 < max(1, essais):
            time.sleep(2 * (tour + 1))
    if err:
        raise LookupError(friendly_error(err) or "Formats indisponibles")
    return None, None


def base_url_of(fmt):
    """URL du flux sans le parametre de plage ajoute par yt-dlp."""
    frag = (fmt.get("fragments") or [{}])[0]
    url = frag.get("url") or fmt.get("url") or ""
    return re.sub(r"[?&]range=[0-9]+-[0-9]+", "", url)


def youtube_fast_section(job, spec, start, end, base, cookies):
    """Chemin rapide YouTube. Renvoie le fichier produit, ou None pour laisser
    la main a la methode classique."""
    job.phase = "Preparation"
    video, audio = cached_formats(spec["video_id"], spec.get("quality"), cookies)
    if not video or not audio:
        return None

    job.phase = "Telechargement"
    morceaux = {}
    for nom, fmt, marge in (("v", video, 2.0), ("a", audio, 6.0)):
        if job.canceled:
            return None
        pris = fetch_span(base_url_of(fmt), fmt.get("http_headers"), start, end, marge)
        if not pris:
            # URL peut-etre perimee : la memoire est purgee pour le prochain essai.
            forget_formats(spec["video_id"])
            return None
        morceaux[nom] = pris
        bump_progress(job, 40 if nom == "v" else 70)

    if job.canceled:
        return None
    brut = {}
    for nom in ("v", "a"):
        chemin = "%s.%s.part.mp4" % (base, nom)
        with open(chemin, "wb") as fh:
            fh.write(morceaux[nom][0])
        brut[nom] = chemin

    job.phase = "Finalisation"
    decalage = max(0.0, start - morceaux["v"][1])
    cible = base + ".mp4"
    fusion = base + ".merge.mp4"
    try:
        args = [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
                "-fflags", "+genpts", "-i", brut["v"],
                "-fflags", "+genpts", "-i", brut["a"],
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                "-avoid_negative_ts", "make_zero", "-muxdelay", "0",
                "-y", fusion]
        code, out, err = run_capture(args, timeout=600)
        if code != 0 or not os.path.isfile(fusion):
            job.add_log("Assemblage rapide impossible : %s" % (err or out)[:120])
            return None

        args = [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
                "-ss", "%.3f" % decalage, "-i", fusion,
                "-t", "%.3f" % (end - start), "-c", "copy",
                "-movflags", "+faststart", "-y", cible]
        code, out, err = run_capture(args, timeout=600)
    finally:
        # Les fragments bruts doivent partir meme si l'assemblage echoue :
        # sinon find_output les prendrait pour le clip lors du repli classique.
        for reste in (brut["v"], brut["a"], fusion):
            try:
                os.remove(reste)
            except OSError:
                pass
    if code != 0 or not os.path.isfile(cible) or os.path.getsize(cible) == 0:
        job.add_log("Decoupe rapide impossible : %s" % (err or out)[:120])
        return None
    job.add_log("Extraction rapide : %.1f Mo transferes"
                % (sum(len(morceaux[n][0]) for n in ("v", "a")) / 1048576.0))
    bump_progress(job, 99)
    return cible


def prepare_cookies(job, cookies, base):
    """Arguments d'authentification pour un telechargement.

    cookie_args() pointe deja sur la copie de travail (voir cookie_jar) : on la
    passe telle quelle pour que la rotation de session operee par yt-dlp serve
    aux appels suivants. Le cookies.txt de l'utilisateur, lui, n'est jamais
    ouvert en ecriture. Renvoie (fichier temporaire a supprimer, arguments).
    """
    if not cookies:
        return None, []
    if not os.path.isfile(cookies[1]):
        job.add_log("Cookies introuvables : tentative sans authentification.")
        return None, []
    return None, list(cookies)


def run_job(job):
    spec = job.spec
    job.state = "running"
    job.phase = "Preparation"
    job.started = time.time()

    start = float(spec.get("start", 0))
    end = float(spec.get("end", 0))
    clip_len = max(0.1, end - start)
    base = build_output_base(spec)
    audio_only = spec.get("quality") in AUDIO_QUALITIES
    platform = spec.get("platform") or "twitch"
    if spec.get("vertical"):
        job.span = (0.0, 60.0)  # le montage vertical occupera les 40 % restants

    args = [
        ytdlp_path(),
        "--no-warnings", "--no-color", "--newline", "--no-playlist",
        "--ffmpeg-location", ffmpeg_dir(),
        "--download-sections", "*%.2f-%.2f" % (start, end),
        "-f", format_selector(spec.get("quality"), platform),
        "--retries", "5", "--fragment-retries", "10",
        "-o", base + ".%(ext)s",
        "--progress-template",
        "PROG|%(progress.status)s|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
    ] + (js_runtime_args() if platform == "youtube" else [])

    # Titre, auteur, date et URL source dans le fichier : un clip isole reste
    # identifiable. Pour l'audio on ajoute la vignette en pochette.
    args.append("--add-metadata")
    if audio_only:
        args.append("--embed-thumbnail")

    if spec.get("quality") == "mp3":
        args += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif not audio_only:
        args += ["--merge-output-format", "mp4"]

    # Note : "--downloader-args ffmpeg_i:-seekable 1" a ete essaye ici, sans
    # aucun gain mesurable (meme extrait, meme video : les deux variantes
    # depassent 4 min 40). Le goulot n'est pas la recherche de position mais le
    # bridage impose par YouTube ("Sleeping N seconds as required by the site"),
    # qu'aucune option de ffmpeg ne contourne.

    # Les fragments audio font une dizaine de secondes : sans coupe forcee, un
    # extrait sonore deborde jusqu'a la frontiere du fragment (mesure : 20 s
    # rendues pour 10 s demandees). On force donc toujours pour l'audio.
    if spec.get("precise") or audio_only:
        args.append("--force-keyframes-at-cuts")

    url = spec.get("url") or video_url(spec.get("video_id"), platform)
    # Sur YouTube on tente d'abord sans cookies (voir attempts_for) ; sur Twitch
    # ils servent des le depart. La liste donne l'ordre des essais.
    auth_attempts = attempts_for(platform)

    job.add_log('$ yt-dlp --download-sections "*%s-%s" %s' % (
        hhmmss(start, (":", ":", "")), hhmmss(end, (":", ":", "")), url))

    error_lines = []
    code = 1

    # Chemin rapide YouTube : ne telecharger que les octets de l'extrait.
    # Reserve a la video (un MP3 se coupe deja vite) et suivi d'un repli
    # silencieux sur la methode classique si l'index n'est pas exploitable.
    if platform == "youtube" and not audio_only:
        for essai in attempts_for(platform):
            if job.canceled:
                break
            try:
                rapide = youtube_fast_section(job, spec, start, end, base, essai)
            except Exception as exc:
                job.add_log("Extraction rapide indisponible : %s" % str(exc)[:110])
                rapide = None
            if rapide:
                job.span = (0.0, 100.0)
                produced = rapide
                job.finished = time.time()
                if spec.get("vertical"):
                    composed = compose_vertical(job, produced, base, spec, clip_len, error_lines)
                    if not composed and job.canceled:
                        break
                    if not composed:
                        job.state, job.phase = "error", "Erreur"
                        job.error = ((error_lines[-1] if error_lines else "Montage vertical impossible.")
                                     + "  Le clip 16:9 a ete conserve sous le nom %s."
                                     % os.path.basename(produced))
                        job.path = produced
                        job.size = os.path.getsize(produced)
                        with _base_lock:
                            _reserved_bases.discard(base)
                        return
                    produced = composed
                job.state, job.phase = "done", "Termine"
                job.progress = 100.0
                job.path = produced
                job.size = os.path.getsize(produced)
                job.speed = job.eta = ""
                with _base_lock:
                    _reserved_bases.discard(base)
                return
        if not job.canceled:
            job.progress = 0.0
            job.add_log("Repli sur le telechargement classique.")
    for index, cookies in enumerate(auth_attempts):
        if job.canceled:
            break
        if index:
            job.add_log("Contenu restreint : nouvelle tentative avec les cookies.")
            job.progress = 0.0
        tmp_cookies, extra = prepare_cookies(job, cookies, base)
        proc = subprocess.Popen(
            args + extra + [url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, creationflags=NO_WINDOW, cwd=work_dir())
        job.proc = proc
        pump_output(job, proc, clip_len, error_lines)
        code = proc.wait()
        job.proc = None
        if tmp_cookies:
            try:
                os.remove(tmp_cookies)
            except OSError:
                pass
        if code != 0 and extra:
            restore_jar()   # un echec ampute la copie de travail, voir restore_jar
        # On ne remonte l'authentification que si l'echec vient bien de la.
        if code == 0 or not needs_cookies("\n".join(error_lines[-3:])):
            break
        cleanup_partial(base)

    with _base_lock:
        _reserved_bases.discard(base)  # le fichier produit protege desormais le nom

    produced = find_output(base)
    job.finished = time.time()

    if job.canceled:
        job.state, job.phase = "canceled", "Annule"
        job.path, job.size = "", 0
        # Annuler veut dire "ne rien garder" : le clip a moitie ecrit part aussi.
        cleanup_partial(base, tout=True)
        return
    if produced and os.path.getsize(produced) > 0:
        if spec.get("vertical") and not audio_only:   # pas de montage sur un MP3
            composed = compose_vertical(job, produced, base, spec, clip_len, error_lines)
            if not composed:
                job.state, job.phase = "error", "Erreur"
                # Le telechargement, lui, a reussi : on garde le 16:9 plutot que
                # d'obliger a le retelecharger, et on dit ou il se trouve.
                job.error = ((error_lines[-1] if error_lines else "Le montage vertical a echoue.")
                             + "  Le clip 16:9 a ete conserve sous le nom %s."
                             % os.path.basename(produced))
                job.path = produced
                cleanup_partial(base)
                return
            produced = composed
        job.state, job.phase = "done", "Termine"
        job.progress = 100.0
        job.path = produced
        job.size = os.path.getsize(produced)
        job.speed = job.eta = ""
    else:
        job.state, job.phase = "error", "Erreur"
        job.error = (error_lines[-1] if error_lines
                     else "yt-dlp s'est arrete (code %s) sans produire de fichier." % code)
        cleanup_partial(base)


def compose_vertical(job, source, base, spec, clip_len, error_lines):
    """Recompose le clip en 9:16 : camera empilee au-dessus du gameplay.

    Renvoie le chemin du fichier vertical, ou None en cas d'echec. Le fichier
    16:9 intermediaire est supprime : c'est le format vertical qui a ete demande.
    """
    job.phase = "Montage vertical"
    job.span = (60.0, 100.0)
    job.speed = job.eta = ""

    frame_w, frame_h = source_size(source)
    logo = logo_file(spec.get("platform") or "twitch")
    graph = vertical_filter(spec.get("layout") or {}, spec.get("label"),
                            frame_w, frame_h, with_logo=bool(logo))
    target = base + ".vertical.mp4"

    args = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-stats",
            "-i", source]
    if logo:
        args += ["-i", logo]          # devient [1:v] dans le graphe de filtres
    args += ["-filter_complex", graph,
             "-map", "[v]", "-map", "0:a?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", "-y", target]
    job.add_log("Montage vertical %dx%d" % (VERTICAL_W, VERTICAL_H))

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=0, creationflags=NO_WINDOW, cwd=work_dir())
    job.proc = proc
    pump_output(job, proc, clip_len, error_lines)
    code = proc.wait()
    job.proc = None

    if job.canceled:
        try:
            os.remove(target)
        except OSError:
            pass
        return None
    if code != 0 or not os.path.isfile(target) or os.path.getsize(target) == 0:
        # pump_output ne retient que les lignes reconnues comme des erreurs :
        # sans ce complement, un echec du montage n'affiche aucune piste.
        job.add_log("ffmpeg (montage vertical) a quitte avec le code %s" % code)
        if not error_lines:
            job.add_log("Filtres : " + graph[:400])
        try:
            os.remove(target)
        except OSError:
            pass
        return None

    final = base + ".mp4"
    try:
        os.remove(source)
        os.replace(target, final)
    except OSError:
        return target  # on garde le fichier vertical sous son nom temporaire
    return final


def bump_progress(job, percent):
    """Avance la barre dans la plage de l'etape en cours, sans jamais reculer."""
    low, high = job.span
    value = low + (high - low) * min(99.5, max(0.0, percent)) / 100.0
    job.progress = max(job.progress, min(99.0, value))


def pump_output(job, proc, clip_len, error_lines):
    """Lit la sortie d'un processus ligne par ligne. ffmpeg separe ses lignes de
    statistiques par des retours chariot, d'ou le decoupage sur \\r comme \\n."""
    pending = b""
    while True:
        chunk = proc.stdout.read(256)
        if not chunk:
            break
        pending += chunk.replace(b"\r\n", b"\n")
        while True:
            positions = [i for i in (pending.find(b"\n"), pending.find(b"\r")) if i >= 0]
            if not positions:
                break
            idx = min(positions)
            line = pending[:idx].decode("utf-8", "replace").strip()
            pending = pending[idx + 1:]
            if line:
                handle_output_line(job, line, clip_len, error_lines)
    tail = pending.decode("utf-8", "replace").strip()
    if tail:
        handle_output_line(job, tail, clip_len, error_lines)


def handle_output_line(job, line, clip_len, error_lines):
    if line.startswith("PROG|"):
        match = RE_PROG.match(line)
        if match and match.group(1) != "finished":
            bump_progress(job, float(match.group(2)))
            job.speed = match.group(3).strip()
            job.eta = match.group(4).strip()
            job.phase = "Telechargement"
        return

    time_match = RE_TIME.search(line)
    if time_match:
        done = (int(time_match.group(1)) * 3600 + int(time_match.group(2)) * 60
                + float(time_match.group(3)))
        bump_progress(job, 100.0 * done / clip_len)
        if job.phase != "Montage vertical":
            job.phase = "Telechargement"
        speed_match = RE_SPEED.search(line)
        if speed_match:
            rate = float(speed_match.group(1))
            job.speed = "%.1fx" % rate
            if rate > 0:
                job.eta = "%ds" % max(0, int((clip_len - done) / rate))
        return

    if line.startswith("ERROR:"):
        error_lines.append(line[6:].strip())
        job.add_log(line)
        return

    # ffmpeg tourne en -loglevel error : ses messages ne portent pas le prefixe
    # "ERROR:" de yt-dlp mais doivent remonter a l'utilisateur de la meme facon.
    if RE_FFMPEG_ERROR.search(line):
        error_lines.append(line.strip())
        job.add_log(line)
        return

    if RE_DEST.match(line):
        job.phase = "Telechargement"
    elif "Merging formats" in line or "[Merger]" in line or "[VideoRemuxer]" in line:
        job.phase = "Finalisation"
    elif "Extracting URL" in line:
        job.phase = "Preparation"

    fmt = RE_FORMAT.search(line)
    if fmt:
        job.add_log("Qualite retenue : %s" % fmt.group(1).strip())
    elif is_useful_line(line):
        job.add_log(line)


# Lignes yt-dlp a conserver dans le journal ; tout le bruit interne de ffmpeg
# (flux, metadonnees, statistiques libx264...) est ecarte.
RE_USEFUL = re.compile(r"^\[(twitch|info|download|Merger|VideoRemuxer|ExtractAudio|Fixup|generic)")


def is_useful_line(line):
    return bool(RE_USEFUL.match(line)) or line.startswith(("WARNING:", "$ "))


RESIDUS = (".part", ".ytdl", ".cookies.tmp", ".merge.mp4", ".part.mp4", ".tmp")
MEDIAS = (".mp4", ".mkv", ".m4a", ".mp3", ".webm", ".opus")


def find_output(base):
    folder = os.path.dirname(base)
    prefix = os.path.basename(base)
    best, best_mtime = None, -1
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        # "prefix + point" et pas seulement "prefix" : le base "clip" ne doit
        # pas ramasser le fichier du base voisin "clip (2)".
        if not name.startswith(prefix + ".") or name.endswith(RESIDUS):
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        mtime = os.path.getmtime(path)
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def cleanup_partial(base, tout=False):
    """Supprime les residus d'un job. Avec tout=True (annulation), retire aussi
    le clip lui-meme : un telechargement annule ne doit rien laisser derriere.

    Seuls les noms de la forme base + ".quelquechose" sont acceptes : sinon le
    base "clip" emporterait les fichiers du base voisin "clip (2)".
    """
    folder = os.path.dirname(base)
    prefix = os.path.basename(base)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix + "."):
            continue
        if name.endswith(RESIDUS) or (tout and name.endswith(MEDIAS)):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


# --------------------------------------------------------------------------- #
#  Serveur HTTP
# --------------------------------------------------------------------------- #

TOKEN = secrets.token_urlsafe(18)


def per_platform(key, default=None):
    """Lit une preference rangee par plateforme, en migrant l'ancien format
    (une simple liste, du temps ou seul Twitch existait)."""
    value = CONFIG.get(key)
    if not isinstance(value, dict):
        value = {"twitch": value} if value else {}
        CONFIG[key] = value
    for name in PLATFORMS:
        value.setdefault(name, [] if default is None else default)
    return value


def remember_channel(login, platform="twitch"):
    if not login:
        return
    recents = per_platform("recent_channels")
    kept = [c for c in recents.get(platform, []) if str(c).lower() != login.lower()]
    kept.insert(0, login)
    recents[platform] = kept[:12]
    last = per_platform("last_channel", default="")
    last[platform] = login
    save_config(CONFIG)


def config_payload():
    """Config + etat detecte de l'environnement.

    Lecture ET ecriture renvoient exactement la meme structure : l'interface
    remplace son etat par la reponse, et un POST qui omettrait ces champs lui
    ferait croire que yt-dlp et ffmpeg ont disparu.
    """
    per_platform("recent_channels")
    per_platform("favorites")
    per_platform("last_channel", default="")
    cfg = dict(CONFIG)
    cfg["cookies_present"] = bool(cookies_file())
    cfg["platforms"] = list(PLATFORMS)
    cfg["js_runtime"] = bool(js_runtime_args())
    cfg["base_dir"] = BASE_DIR
    cfg["data_dir"] = APPDATA_DIR
    cfg["ytdlp_present"] = os.path.isfile(ytdlp_path())
    cfg["ffmpeg_present"] = os.path.isfile(ffmpeg_path())
    cfg["logo_present"] = {p: bool(logo_file(p)) for p in PLATFORMS}
    cfg["default_split"] = DEFAULT_SPLIT
    # Lecture du cache uniquement : la detection tourne en tache de fond au
    # demarrage, cette requete ne doit jamais l'attendre.
    with _ytdlp_lock:
        current, latest = _ytdlp_meta["version"], _ytdlp_meta["latest"]
    cfg["ytdlp_version"] = current
    cfg["ytdlp_latest"] = latest
    cfg["ytdlp_update_available"] = bool(current and latest and current != latest)
    return cfg


def open_in_explorer(target, select=False):
    """Ouvre un dossier (ou revele/lance un fichier) dans l'explorateur Windows.

    os.startfile() passe par ShellExecute, qui reclame un apartment COM : appele
    depuis un thread du serveur HTTP il rend la main sans erreur mais n'ouvre
    aucune fenetre. On lance donc explorer.exe, processus separe sans cette
    contrainte. La ligne de commande est passee telle quelle : explorer n'accepte
    "/select," qu'avec le chemin entre guillemets juste derriere.
    """
    target = os.path.normpath(target)
    command = ('explorer.exe /select,"%s"' if select else 'explorer.exe "%s"') % target
    subprocess.Popen(command)


# Seuls les clips produits peuvent etre lances depuis l'interface : jamais un
# executable, un script ou un raccourci, meme s'il se trouve dans le dossier de
# sortie (celui-ci est modifiable dans les reglages).
OPENABLE_EXTS = (".mp4", ".mkv", ".m4a", ".webm", ".mp3", ".opus")


def inside_output_dir(path):
    """Un fichier ou dossier n'est servi/ouvert que s'il vient du dossier de
    sortie courant ou s'il a ete produit par un job de cette session (dossier
    change entre-temps)."""
    try:
        target = os.path.realpath(path)
    except Exception:
        return False
    try:
        out = os.path.realpath(CONFIG.get("output_dir") or os.path.join(APPDATA_DIR, "clips"))
        if os.path.commonpath([out, target]) == out:
            return True
    except Exception:
        pass  # volumes differents : on retombe sur la liste des clips produits
    for job in JOBS.list():
        try:
            produit = job.get("path")
            if produit and target in (os.path.realpath(produit),
                                      os.path.realpath(os.path.dirname(produit))):
                return True
        except Exception:
            continue
    return False


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Clipper"
    protocol_version = "HTTP/1.1"

    # ---- helpers ---------------------------------------------------------- #

    def log_message(self, fmt, *args):
        pass  # console silencieuse

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_ok(self, data):
        self._send(200, json.dumps(data, ensure_ascii=False))

    def json_err(self, code, message):
        self._send(code, json.dumps({"error": str(message)}, ensure_ascii=False))

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def local_host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def token_ok(self, query):
        given = self.headers.get("X-Clip-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(str(given), TOKEN)

    # ---- routes ----------------------------------------------------------- #

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if not self.local_host_ok():
            return self.json_err(403, "Acces local uniquement")
        try:
            if path == "/" or path == "/index.html":
                return self.serve_index()
            if path == "/logo.png":
                logo = logo_file((query.get("platform") or ["twitch"])[0])
                if not logo:
                    return self.json_err(404, "Aucun logo")
                with open(logo, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            if path.startswith("/static/"):
                return self.serve_static(path[len("/static/"):])
            if path == "/media":
                if not self.token_ok(query):
                    return self.json_err(403, "Jeton invalide")
                return self.serve_media((query.get("path") or [""])[0])
            if path.startswith("/api/"):
                if not self.token_ok(query):
                    return self.json_err(403, "Jeton invalide")
                return self.api_get(path, query)
            return self.json_err(404, "Introuvable")
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self.json_err(500, exc)
            except Exception:
                pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if not self.local_host_ok():
            return self.json_err(403, "Acces local uniquement")
        if not self.token_ok(query):
            return self.json_err(403, "Jeton invalide")
        try:
            return self.api_post(path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self.json_err(500, exc)
            except Exception:
                pass

    def api_get(self, path, query):
        one = lambda key, default="": (query.get(key) or [default])[0]

        if path == "/api/ping":
            return self.json_ok({"ok": True})

        if path == "/api/config":
            return self.json_ok(config_payload())

        if path == "/api/channel":
            platform = detect_platform(one("login"), one("platform", "twitch"))
            login = parse_channel(one("login"), platform)
            if not login:
                return self.json_err(400, "Nom de chaine manquant")
            try:
                data = list_videos(login, one("type", "archives"),
                                   int(one("limit", "30") or 30), one("cursor") or None,
                                   platform)
            except LookupError as exc:
                return self.json_err(404, exc)
            remember_channel(data["channel"]["login"], platform)
            return self.json_ok(data)

        if path == "/api/vod":
            platform = detect_platform(one("id"), one("platform", "twitch"))
            vid = parse_video_id(one("id"), platform)
            if not vid:
                return self.json_err(400, "Identifiant de video invalide")
            try:
                info = vod_info(vid, platform)
            except LookupError as exc:
                return self.json_err(404, exc)
            return self.json_ok(info)

        if path == "/api/frame":
            platform = one("platform", "twitch")
            vid = parse_video_id(one("id"), platform)
            if not vid:
                return self.json_err(400, "Identifiant de video invalide")
            try:
                image = extract_frame(vid, float(one("t", "0") or 0), platform)
            except LookupError as exc:
                return self.json_err(404, exc)
            except Exception as exc:
                return self.json_err(500, exc)
            return self._send(200, image, "image/jpeg")

        if path == "/api/jobs":
            return self.json_ok({"jobs": JOBS.list()})

        return self.json_err(404, "Route inconnue")

    def api_post(self, path):
        body = self.read_json()

        if path == "/api/config":
            for key in ("output_dir", "cookies_file", "use_cookies", "quality",
                        "precise_cut", "filename_template", "max_parallel",
                        "favorites", "last_channel", "vertical", "vertical_label",
                        "layouts", "platform"):
                if key in body:
                    CONFIG[key] = body[key]
            save_config(CONFIG)
            JOBS._ensure_workers()
            return self.json_ok({"ok": True, "config": config_payload()})

        if path == "/api/jobs":
            try:
                start = float(body.get("start", 0))
                end = float(body.get("end", 0))
            except (TypeError, ValueError):
                return self.json_err(400, "Timecodes invalides")
            if end <= start:
                return self.json_err(400, "La fin doit etre apres le debut")
            platform = body.get("platform") or "twitch"
            if platform not in PLATFORMS:
                platform = "twitch"
            vid = parse_video_id(body.get("video_id") or body.get("url") or "", platform)
            if not vid:
                return self.json_err(400, "Video invalide")
            spec = {
                "video_id": vid,
                "platform": platform,
                "url": video_url(vid, platform),
                "title": body.get("title") or "",
                "channel": body.get("channel") or "",
                "date": body.get("date") or "",
                "start": start,
                "end": end,
                "quality": body.get("quality") or "best",
                "precise": bool(body.get("precise")),
                "vertical": bool(body.get("vertical")),
                "layout": body.get("layout") or {},
                "label": body.get("label") or "",
            }
            job = JOBS.submit(spec)
            return self.json_ok({"job": job.as_dict()})

        if path == "/api/jobs/cancel":
            return self.json_ok({"ok": JOBS.cancel(body.get("id"))})

        if path == "/api/jobs/clear":
            JOBS.clear_finished()
            return self.json_ok({"ok": True})

        if path == "/api/ytdlp-update":
            ok, message = ytdlp_update()
            return self.json_ok({"ok": ok, "message": message,
                                 "version": ytdlp_version(),
                                 "latest": ytdlp_latest(force=True)})

        if path in ("/api/reveal", "/api/open"):
            target = body.get("path") or CONFIG.get("output_dir")
            if not target or not os.path.exists(target):
                return self.json_err(404, "Fichier introuvable")
            if not inside_output_dir(target):
                return self.json_err(403, "Chemin hors du dossier de sortie")
            if (path == "/api/open" and os.path.isfile(target)
                    and not target.lower().endswith(OPENABLE_EXTS)):
                return self.json_err(403, "Type de fichier non autorise")
            try:
                open_in_explorer(target, select=(path == "/api/reveal" and os.path.isfile(target)))
            except Exception as exc:
                return self.json_err(500, exc)
            return self.json_ok({"ok": True})

        if path == "/api/quit":
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return self.json_ok({"ok": True})

        return self.json_err(404, "Route inconnue")

    # ---- fichiers --------------------------------------------------------- #

    def serve_index(self):
        try:
            with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            return self.json_err(500, "web/index.html introuvable")
        html = html.replace("__TOKEN__", TOKEN)
        self._send(200, html, "text/html; charset=utf-8")

    def serve_static(self, rel):
        rel = urllib.parse.unquote(rel)
        root = os.path.realpath(WEB_DIR)
        target = os.path.realpath(os.path.join(WEB_DIR, rel))
        try:
            dedans = os.path.commonpath([root, target]) == root
        except ValueError:  # autre lecteur
            dedans = False
        if not dedans or not os.path.isfile(target):
            return self.json_err(404, "Introuvable")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def serve_media(self, raw_path):
        path = urllib.parse.unquote(raw_path or "")
        if not path or not os.path.isfile(path) or not inside_output_dir(path):
            return self.json_err(404, "Fichier introuvable")
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "video/mp4"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            match = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if match:
                partial = True
                if match.group(1):
                    start = int(match.group(1))
                    if match.group(2):
                        end = min(size - 1, int(match.group(2)))
                elif match.group(2):  # suffixe : derniers N octets
                    start = max(0, size - int(match.group(2)))
        if start >= size:
            return self._send(416, b"", ctype, {"Content-Range": "bytes */%d" % size})

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionAbortedError):
                    return
                remaining -= len(chunk)


def parse_channel(value, platform="twitch"):
    """Normalise une saisie de chaine : nom nu, @handle ou URL complete."""
    value = (value or "").strip()
    if not value:
        return ""
    if platform == "youtube":
        for marker in ("youtube.com/", "youtu.be/"):
            if marker in value:
                value = value.split(marker, 1)[1]
        value = value.split("?")[0].strip("/")
        for prefix in ("c/", "user/", "channel/"):
            if value.startswith(prefix):
                value = value[len(prefix):]
        value = value.split("/")[0]
        return re.sub(r"[^A-Za-z0-9_@.\-]", "", value)
    if "twitch.tv/" in value:
        value = value.split("twitch.tv/", 1)[1]
    value = value.split("?")[0].split("/")[0]
    return re.sub(r"[^A-Za-z0-9_]", "", value)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """Le navigateur abandonne ses requetes en cours des qu'on change de
        timecode : la coupure est normale, pas la peine d'en afficher la trace.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        http.server.ThreadingHTTPServer.handle_error(self, request, client_address)


def find_port(preferred=8731):
    for port in [preferred] + list(range(8732, 8760)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def ensure_tools():
    """Installe yt-dlp et ffmpeg au premier lancement.

    Le depot ne peut pas les embarquer (plus de 100 Mo par fichier chez GitHub) :
    une installation depuis zero se resume donc a cloner puis lancer.
    """
    manquants = [name for name, path in (("yt-dlp.exe", ytdlp_path()),
                                         ("ffmpeg.exe", ffmpeg_path()),
                                         ("ffprobe.exe", ffprobe_path()))
                 if not os.path.isfile(path)]
    if not manquants:
        return
    # Outils places ailleurs par l'utilisateur (reglages ytdlp_path/ffmpeg_dir) :
    # ce n'est pas a nous d'aller ecrire dans ces dossiers-la.
    if CONFIG.get("ytdlp_path") or CONFIG.get("ffmpeg_dir"):
        print("ATTENTION : introuvable -> %s" % ", ".join(manquants))
        return
    try:
        import installer
        installer.ensure_tools(BASE_DIR)
    except Exception as exc:
        print("ATTENTION : installation automatique impossible (%s)" % exc)
        print("  Manquant dans %s -> %s" % (BASE_DIR, ", ".join(manquants)))


def main():
    if "--install" in sys.argv:
        import installer
        sys.exit(1 if installer.ensure_tools(BASE_DIR, force=True) else 0)

    ensure_tools()

    if not HTTPS_OK:
        print("ATTENTION : ce Python n'a pas de support HTTPS (module ssl introuvable).")
        print("  La liste des VOD passera par yt-dlp : plus lente et sans date ni jeu.")
        print("  Anaconda : lancez l'appli via Clipper.bat, qui ajoute les DLL requises.")

    if is_msix_python():
        print("ATTENTION : Python provient du Microsoft Store (%s)." % sys.executable)
        print("  Ses ecritures dans %LOCALAPPDATA% sont redirigees dans un conteneur")
        print("  que yt-dlp.exe ne voit pas. Les donnees sont donc placees dans")
        print("  %s ." % APPDATA_DIR)
        print("  Pour un fonctionnement standard, installez Python depuis python.org")
        print("  (le lanceur .bat le detectera automatiquement).")

    try:
        ensure_writable_dir(CONFIG.get("output_dir") or os.path.join(APPDATA_DIR, "clips"))
    except RuntimeError as exc:
        print("=" * 62)
        print(" ERREUR AU DEMARRAGE")
        print("=" * 62)
        print(str(exc))
        sys.exit(1)

    port = find_port()
    server = Server(("127.0.0.1", port), Handler)
    # On sert sur 127.0.0.1 mais on ouvre "localhost" : le lecteur Twitch
    # embarque n'accepte qu'un nom de domaine dans son parametre "parent".
    url = "http://localhost:%d/?token=%s" % (port, TOKEN)

    print("=" * 62)
    print(" Clipper")
    print(" Interface  : %s" % url)
    print(" Clips      : %s" % CONFIG.get("output_dir"))
    print(" Fermer     : Ctrl+C dans cette fenetre")
    print("=" * 62)

    # Version installee + derniere version publiee, en tache de fond : la
    # verification ne doit pas retarder l'ouverture de l'interface.
    threading.Thread(target=lambda: (ytdlp_version(), ytdlp_latest()), daemon=True).start()

    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArret.")


if __name__ == "__main__":
    main()
