#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Twitch Clipper - interface graphique locale pour decouper des extraits de VOD Twitch.

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
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
YTDLP = os.path.join(BASE_DIR, "yt-dlp.exe")
FFMPEG = os.path.join(BASE_DIR, "ffmpeg.exe")
FFPROBE = os.path.join(BASE_DIR, "ffprobe.exe")

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
if is_msix_python():
    APPDATA_DIR = os.path.join(os.path.expanduser("~"), "TwitchClipper")
else:
    APPDATA_DIR = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"),
        "TwitchClipper")
CONFIG_PATH = os.path.join(APPDATA_DIR, "config.json")

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
    "recent_channels": [],
    "favorites": [],
    "last_channel": "",
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
    return cfg


def save_config(cfg):
    with _config_lock:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)


ensure_writable_dir(APPDATA_DIR)
CONFIG = load_config()


# --------------------------------------------------------------------------- #
#  Utilitaires
# --------------------------------------------------------------------------- #

def ytdlp_path():
    return CONFIG.get("ytdlp_path") or YTDLP


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
            headers={"Accept": "application/vnd.github+json", "User-Agent": "TwitchClipper"})
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


def run_capture(args, timeout=120):
    """Execute une commande et renvoie (returncode, stdout, stderr)."""
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
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


def list_videos(login, kind="archives", limit=30, cursor=None):
    key = (login.lower(), kind, int(limit), cursor or "")
    cached = cache_get(key)
    if cached:
        return cached
    if not HTTPS_OK:  # inutile d'essayer : Python n'a pas de support HTTPS ici
        result = list_videos_ytdlp(login, kind, limit)
    else:
        try:
            result = list_videos_gql(login, kind, limit, cursor)
        except LookupError:
            raise
        except Exception as exc:  # API publique indisponible -> repli yt-dlp
            sys.stderr.write("[clipper] GraphQL indisponible (%s), repli yt-dlp\n" % exc)
            result = list_videos_ytdlp(login, kind, limit)
    cache_put(key, result)
    return result


# --------------------------------------------------------------------------- #
#  Metadonnees d'une VOD
# --------------------------------------------------------------------------- #

_vod_cache = {}
_vod_lock = threading.Lock()


def cookies_file():
    """Fichier de cookies effectif ; si le chemin enregistre n'existe plus
    (dossier deplace), on retombe sur le cookies.txt voisin du script."""
    path = CONFIG.get("cookies_file") or ""
    if path and os.path.isfile(path):
        return path
    fallback = os.path.join(BASE_DIR, "cookies.txt")
    return fallback if os.path.isfile(fallback) else ""


def cookie_args():
    if not CONFIG.get("use_cookies"):
        return []
    path = cookies_file()
    return ["--cookies", path] if path else []


def vod_info(video_id):
    video_id = str(video_id).lstrip("vV")
    with _vod_lock:
        hit = _vod_cache.get(video_id)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    url = "https://www.twitch.tv/videos/%s" % video_id
    args = [ytdlp_path(), "-J", "--no-warnings", "--no-playlist"] + cookie_args() + [url]
    code, out, err = run_capture(args, timeout=120)
    out = out.strip()
    if not out:
        msg = ""
        for line in err.splitlines():
            if line.startswith("ERROR:"):
                msg = line.split(":", 1)[1].strip()
        raise LookupError(msg or "VOD inaccessible (%s)" % video_id)

    data = json.loads(out)
    qualities, seen = [], set()
    for fmt in data.get("formats") or []:
        height = fmt.get("height")
        if not height or fmt.get("ext") == "mhtml" or height in seen:
            continue
        seen.add(height)
        qualities.append({
            "height": height,
            "fps": int(fmt.get("fps") or 0),
            "label": fmt.get("format_id") or ("%dp" % height),
        })
    qualities.sort(key=lambda q: q["height"], reverse=True)

    info = {
        "id": video_id,
        "url": url,
        "title": data.get("title") or "(sans titre)",
        "duration": float(data.get("duration") or 0),
        "timestamp": data.get("timestamp"),
        "upload_date": data.get("upload_date"),
        "channel": data.get("uploader_id") or data.get("uploader") or "",
        "channel_name": data.get("uploader") or "",
        "views": data.get("view_count") or 0,
        "thumbnail": data.get("thumbnail") or "",
        "qualities": qualities,
    }
    with _vod_lock:
        _vod_cache[video_id] = (time.time(), info)
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


def stream_url(video_id):
    """URL directe du flux video, mise en cache : l'extraction d'une image de
    reperage doit rester quasi instantanee quand on deplace le curseur."""
    with _stream_lock:
        hit = _stream_cache.get(video_id)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
    args = ([ytdlp_path(), "-g", "--no-warnings", "--no-playlist",
             "-f", "bv*[height<=1080]/bv*/b"] + cookie_args() +
            ["https://www.twitch.tv/videos/%s" % video_id])
    code, out, err = run_capture(args, timeout=120)
    url = (out or "").strip().splitlines()
    if not url:
        msg = ""
        for line in err.splitlines():
            if line.startswith("ERROR:"):
                msg = line.split(":", 1)[1].strip()
        raise LookupError(msg or "Flux introuvable pour la VOD %s" % video_id)
    with _stream_lock:
        _stream_cache[video_id] = (time.time(), url[0])
    return url[0]


def extract_frame(video_id, seconds):
    """Une image JPEG a l'instant demande, directement depuis le flux.

    Saut en deux temps : un saut rapide quelques secondes avant, puis un saut
    precis decode depuis l'image-cle. Un saut rapide seul tombe parfois au
    milieu d'un segment et rend une image a moitie noire.
    """
    seconds = max(0.0, float(seconds))
    lead = min(4.0, seconds)
    args = [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
            "-ss", "%.3f" % (seconds - lead),
            "-i", stream_url(video_id),
            "-ss", "%.3f" % lead,
            "-frames:v", "1", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=NO_WINDOW, timeout=180)
    if not proc.stdout:
        raise LookupError((proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
                          or "Image indisponible")
    return proc.stdout


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


def logo_file():
    """Logo optionnel incruste a cote du nom de la chaine."""
    for name in ("Logo.png", "logo.png"):
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

    # Memes valeurs de repli que l'interface, mises a l'echelle de la source.
    sx, sy = frame_w / float(REF_W), frame_h / float(REF_H)
    scaled = lambda b: (b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy)
    cx, cy, cw, ch = clamp_box(layout.get("cam"), frame_w, frame_h, scaled(REF_CAM))
    gx, gy, gw, gh = clamp_box(layout.get("game"), frame_w, frame_h, scaled(REF_GAME))

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

    exts = (".mp4", ".mkv", ".m4a", ".webm")

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


def format_selector(quality):
    if quality == "audio":
        return "ba/b"
    if quality and quality != "best":
        try:
            height = int(str(quality).rstrip("p"))
            return "bv*[height<=%d]+ba/b[height<=%d]/bv*+ba/b" % (height, height)
        except ValueError:
            pass
    return "bv*+ba/b"


def run_job(job):
    spec = job.spec
    job.state = "running"
    job.phase = "Preparation"
    job.started = time.time()

    start = float(spec.get("start", 0))
    end = float(spec.get("end", 0))
    clip_len = max(0.1, end - start)
    base = build_output_base(spec)
    audio_only = spec.get("quality") == "audio"
    if spec.get("vertical"):
        job.span = (0.0, 60.0)  # le montage vertical occupera les 40 % restants

    args = [
        ytdlp_path(),
        "--no-warnings", "--no-color", "--newline", "--no-playlist",
        "--ffmpeg-location", ffmpeg_dir(),
        "--download-sections", "*%.2f-%.2f" % (start, end),
        "-f", format_selector(spec.get("quality")),
        "--retries", "5", "--fragment-retries", "10",
        "-o", base + ".%(ext)s",
        "--progress-template",
        "PROG|%(progress.status)s|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
    ]
    if not audio_only:
        args += ["--merge-output-format", "mp4"]
    if spec.get("precise"):
        args.append("--force-keyframes-at-cuts")

    # Copie de travail des cookies : yt-dlp reecrit le fichier a la fin de son
    # execution, on evite ainsi de modifier le cookies.txt de l'utilisateur.
    tmp_cookies = None
    cookies = cookie_args()
    if cookies:
        candidate = base + ".cookies.tmp"
        try:
            shutil.copyfile(cookies[1], candidate)
            # On ne fait confiance a la copie que si elle est bien la et non vide :
            # sinon yt-dlp partirait sans authentification, en silence.
            if os.path.getsize(candidate) > 0:
                tmp_cookies = candidate
                args += ["--cookies", tmp_cookies]
        except Exception:
            pass
        if not tmp_cookies:
            args += cookies
            job.add_log("Cookies : copie temporaire impossible, "
                        "utilisation directe de %s" % cookies[1])
    args.append(spec.get("url") or "https://www.twitch.tv/videos/%s" % spec.get("video_id"))

    job.add_log('$ yt-dlp --download-sections "*%s-%s" %s' % (
        hhmmss(start, (":", ":", "")), hhmmss(end, (":", ":", "")), spec.get("url", "")))

    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0, creationflags=NO_WINDOW, cwd=BASE_DIR)
    job.proc = proc

    error_lines = []
    pump_output(job, proc, clip_len, error_lines)

    code = proc.wait()
    job.proc = None
    with _base_lock:
        _reserved_bases.discard(base)  # le fichier produit protege desormais le nom
    if tmp_cookies:
        try:
            os.remove(tmp_cookies)
        except OSError:
            pass

    produced = find_output(base)
    job.finished = time.time()

    if job.canceled:
        job.state, job.phase = "canceled", "Annule"
        cleanup_partial(base)
        return
    if produced and os.path.getsize(produced) > 0:
        if spec.get("vertical"):
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
    logo = logo_file()
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
                            bufsize=0, creationflags=NO_WINDOW, cwd=BASE_DIR)
    job.proc = proc
    pump_output(job, proc, clip_len, error_lines)
    code = proc.wait()
    job.proc = None

    if job.canceled:
        return None
    if code != 0 or not os.path.isfile(target) or os.path.getsize(target) == 0:
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


def find_output(base):
    folder = os.path.dirname(base)
    prefix = os.path.basename(base)
    best, best_mtime = None, -1
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        if not name.startswith(prefix) or name.endswith((".part", ".ytdl", ".cookies.tmp", ".tmp")):
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        mtime = os.path.getmtime(path)
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def cleanup_partial(base):
    folder = os.path.dirname(base)
    prefix = os.path.basename(base)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith(prefix) and name.endswith((".part", ".ytdl", ".cookies.tmp")):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


# --------------------------------------------------------------------------- #
#  Serveur HTTP
# --------------------------------------------------------------------------- #

TOKEN = secrets.token_urlsafe(18)


def remember_channel(login):
    if not login:
        return
    recents = [c for c in CONFIG.get("recent_channels", []) if c.lower() != login.lower()]
    recents.insert(0, login)
    CONFIG["recent_channels"] = recents[:12]
    CONFIG["last_channel"] = login
    save_config(CONFIG)


def config_payload():
    """Config + etat detecte de l'environnement.

    Lecture ET ecriture renvoient exactement la meme structure : l'interface
    remplace son etat par la reponse, et un POST qui omettrait ces champs lui
    ferait croire que yt-dlp et ffmpeg ont disparu.
    """
    cfg = dict(CONFIG)
    cfg["cookies_present"] = bool(cookies_file())
    cfg["base_dir"] = BASE_DIR
    cfg["data_dir"] = APPDATA_DIR
    cfg["ytdlp_present"] = os.path.isfile(ytdlp_path())
    cfg["ffmpeg_present"] = os.path.isfile(ffmpeg_path())
    cfg["logo_present"] = bool(logo_file())
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


def inside_output_dir(path):
    """Un fichier n'est servi/ouvert que s'il vient du dossier de sortie courant
    ou s'il a ete produit par un job de cette session (dossier change entre-temps)."""
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
            if job.get("path") and os.path.realpath(job["path"]) == target:
                return True
        except Exception:
            continue
    return False


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TwitchClipper"
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
                logo = logo_file()
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
            login = parse_channel(one("login"))
            if not login:
                return self.json_err(400, "Nom de chaine manquant")
            try:
                data = list_videos(login, one("type", "archives"),
                                   int(one("limit", "30") or 30), one("cursor") or None)
            except LookupError as exc:
                return self.json_err(404, exc)
            remember_channel(data["channel"]["login"])
            return self.json_ok(data)

        if path == "/api/vod":
            vid = parse_video_id(one("id"))
            if not vid:
                return self.json_err(400, "Identifiant de VOD invalide")
            try:
                info = vod_info(vid)
            except LookupError as exc:
                return self.json_err(404, exc)
            return self.json_ok(info)

        if path == "/api/frame":
            vid = parse_video_id(one("id"))
            if not vid:
                return self.json_err(400, "Identifiant de VOD invalide")
            try:
                image = extract_frame(vid, float(one("t", "0") or 0))
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
                        "layouts"):
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
            vid = parse_video_id(body.get("video_id") or body.get("url") or "")
            if not vid:
                return self.json_err(400, "VOD invalide")
            spec = {
                "video_id": vid,
                "url": "https://www.twitch.tv/videos/%s" % vid,
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
            if os.path.isfile(target) and not inside_output_dir(target):
                return self.json_err(403, "Chemin hors du dossier de sortie")
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
        target = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not target.startswith(os.path.realpath(WEB_DIR)) or not os.path.isfile(target):
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


RE_VIDEO_ID = re.compile(r"(?:twitch\.tv/videos/)?v?(\d{6,})")


def parse_video_id(value):
    value = (value or "").strip()
    match = RE_VIDEO_ID.search(value)
    return match.group(1) if match else ""


def parse_channel(value):
    value = (value or "").strip()
    if not value:
        return ""
    if "twitch.tv/" in value:
        value = value.split("twitch.tv/", 1)[1]
    value = value.split("?")[0].split("/")[0]
    return re.sub(r"[^A-Za-z0-9_]", "", value)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def find_port(preferred=8731):
    for port in [preferred] + list(range(8732, 8760)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def main():
    missing = [name for name, path in (("yt-dlp.exe", ytdlp_path()), ("ffmpeg.exe", ffmpeg_path()))
               if not os.path.isfile(path)]
    if missing:
        print("ATTENTION : introuvable dans %s -> %s" % (BASE_DIR, ", ".join(missing)))

    if not HTTPS_OK:
        print("ATTENTION : ce Python n'a pas de support HTTPS (module ssl introuvable).")
        print("  La liste des VOD passera par yt-dlp : plus lente et sans date ni jeu.")
        print("  Anaconda : lancez l'appli via Twitch Clipper.bat, qui ajoute les DLL requises.")

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
    print(" Twitch Clipper")
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
