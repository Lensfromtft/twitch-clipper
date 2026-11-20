# -*- coding: utf-8 -*-
"""Installation des outils externes : yt-dlp, ffmpeg et ffprobe.

Le depot ne peut pas les embarquer : ffmpeg.exe et ffprobe.exe pesent plus de
200 Mo chacun, alors que GitHub refuse tout fichier de plus de 100 Mo. Ils sont
donc recuperes depuis leurs depots officiels au premier lancement, ce qui a
l'avantage de livrer une version a jour plutot qu'une copie figee.

Utilisable seul :
    python installer.py              installe ce qui manque
    python installer.py --force      reinstalle tout
"""

import hashlib
import os
import shutil
import ssl
import sys
import tempfile
import time
import urllib.request
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# yt-dlp publie un .exe autonome ; l'URL "latest" suit les nouvelles versions.
YTDLP_REPO = "yt-dlp/yt-dlp"
YTDLP_ASSET = "yt-dlp.exe"
YTDLP_SUMS = "SHA2-256SUMS"
YTDLP_URL = "https://github.com/%s/releases/latest/download/%s" % (YTDLP_REPO, YTDLP_ASSET)

# Build ffmpeg maintenu par l'equipe yt-dlp : c'est celui qu'elle recommande, et
# il corrige des defauts de decoupe presents dans les builds generiques.
FFMPEG_REPO = "yt-dlp/FFmpeg-Builds"
FFMPEG_ASSET = "ffmpeg-master-latest-win64-gpl.zip"
FFMPEG_SUMS = "checksums.sha256"
FFMPEG_URL = "https://github.com/%s/releases/latest/download/%s" % (FFMPEG_REPO, FFMPEG_ASSET)

TOOLS = ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe")

# Un .exe tronque par une coupure reseau est pire qu'un fichier absent : il
# echoue a l'execution avec un message incomprehensible. On refuse donc tout
# telechargement manifestement trop petit.
MIN_SIZE = {"yt-dlp.exe": 5 << 20, "ffmpeg.exe": 20 << 20, "ffprobe.exe": 20 << 20}


def missing_tools(folder=None):
    """Outils absents ou visiblement incomplets."""
    folder = folder or BASE_DIR
    absents = []
    for name in TOOLS:
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or os.path.getsize(path) < MIN_SIZE[name]:
            absents.append(name)
    return absents


_last_draw = [0.0, -1]


def show_progress(label, done, total):
    # Redessiner a chaque bloc de 256 Ko noie une sortie redirigee vers un
    # fichier (53 Ko de journal pour un telechargement) : on s'en tient a un
    # rafraichissement par pourcent, et au plus deux par seconde.
    maintenant = time.time()
    pourcent = int(done * 100 / total) if total > 0 else -1
    if pourcent == _last_draw[1] and maintenant - _last_draw[0] < 0.5:
        return
    _last_draw[0], _last_draw[1] = maintenant, pourcent

    if total > 0:
        part = done / float(total)
        barre = "#" * int(part * 28)
        texte = "  %-12s [%-28s] %3d%%  %.1f Mo" % (
            label, barre, part * 100, done / 1048576.0)
    else:
        texte = "  %-12s %.1f Mo" % (label, done / 1048576.0)
    sys.stdout.write("\r" + texte + " " * 6)
    sys.stdout.flush()


def release_sha256(repo, sums_name, asset):
    """Somme SHA-256 publiee par le projet, et URL de l'asset dans la MEME release.

    "releases/latest/download/<fichier>" redirige vers
    "releases/download/<tag>/<fichier>". On lit le tag sur cette redirection
    et on telecharge ensuite l'asset depuis ce tag precis : FFmpeg-Builds
    publie plusieurs fois par jour, et deux appels a "latest" pourraient sinon
    tomber sur deux builds differents (somme et fichier incoherents).
    """
    requete = urllib.request.Request(
        "https://github.com/%s/releases/latest/download/%s" % (repo, sums_name),
        headers={"User-Agent": "Clipper"})
    with urllib.request.urlopen(requete, timeout=30) as reponse:
        finale = reponse.geturl()
        texte = reponse.read().decode("utf-8", "replace")
    url = "https://github.com/%s/releases/latest/download/%s" % (repo, asset)
    marqueur = "/releases/download/"
    if ("github.com/%s%s" % (repo, marqueur)) in finale:
        tag = finale.split(marqueur, 1)[1].split("/")[0]
        url = "https://github.com/%s/releases/download/%s/%s" % (repo, tag, asset)
    for ligne in texte.splitlines():
        morceaux = ligne.strip().split()
        if len(morceaux) == 2 and morceaux[1].lstrip("*") == asset:
            return morceaux[0].lower(), url
    raise IOError("somme de controle absente pour %s" % asset)


def download(url, target, label, sha256=None):
    """Telecharge vers un fichier temporaire, puis met en place d'un bloc.

    Passer par un temporaire evite de laisser un .exe a moitie ecrit si la
    connexion lache : l'outil reste "absent" et la prochaine tentative repart
    proprement.
    """
    folder = os.path.dirname(target) or "."
    fd, partiel = tempfile.mkstemp(prefix=".dl", dir=folder)
    os.close(fd)
    try:
        requete = urllib.request.Request(url, headers={"User-Agent": "Clipper"})
        with urllib.request.urlopen(requete, timeout=60) as reponse:
            total = int(reponse.headers.get("Content-Length") or 0)
            recu = 0
            empreinte = hashlib.sha256()
            with open(partiel, "wb") as sortie:
                while True:
                    bloc = reponse.read(262144)
                    if not bloc:
                        break
                    sortie.write(bloc)
                    empreinte.update(bloc)
                    recu += len(bloc)
                    show_progress(label, recu, total)
        sys.stdout.write("\n")
        if total and recu < total:
            raise IOError("telechargement incomplet (%d / %d octets)" % (recu, total))
        # Un executable qu'on va lancer doit etre exactement celui publie par
        # le projet : on refuse tout fichier dont l'empreinte differe.
        if sha256 and empreinte.hexdigest().lower() != sha256:
            raise IOError("somme de controle SHA-256 invalide, fichier rejete")
        os.replace(partiel, target)
    finally:
        if os.path.exists(partiel):
            try:
                os.remove(partiel)
            except OSError:
                pass


def install_ytdlp(folder):
    somme, url = release_sha256(YTDLP_REPO, YTDLP_SUMS, YTDLP_ASSET)
    download(url, os.path.join(folder, "yt-dlp.exe"), "yt-dlp", somme)


def install_ffmpeg(folder, besoins=("ffmpeg.exe", "ffprobe.exe")):
    """Recupere ffmpeg et ffprobe depuis l'archive officielle.

    L'archive contient une arborescence "ffmpeg-<version>/bin/*.exe" dont le
    nom change a chaque build : on repere les executables par leur position
    plutot que par un chemin fige.
    """
    fd, archive = tempfile.mkstemp(prefix="ffmpeg", suffix=".zip", dir=folder)
    os.close(fd)
    try:
        somme, url = release_sha256(FFMPEG_REPO, FFMPEG_SUMS, FFMPEG_ASSET)
        download(url, archive, "ffmpeg", somme)
        with zipfile.ZipFile(archive) as zf:
            voulus = {}
            for membre in zf.namelist():
                nom = os.path.basename(membre)
                if nom in besoins and "/bin/" in membre.replace("\\", "/"):
                    voulus[nom] = membre
            manque = [n for n in besoins if n not in voulus]
            if manque:
                raise IOError("archive ffmpeg inattendue, absent : %s" % ", ".join(manque))
            for nom, membre in voulus.items():
                print("  extraction  %s" % nom)
                with zf.open(membre) as source:
                    cible = os.path.join(folder, nom)
                    provisoire = cible + ".part"
                    with open(provisoire, "wb") as sortie:
                        shutil.copyfileobj(source, sortie, 1 << 20)
                    os.replace(provisoire, cible)
    finally:
        try:
            os.remove(archive)
        except OSError:
            pass


def ensure_tools(folder=None, force=False):
    """Installe ce qui manque. Renvoie la liste des outils toujours absents."""
    folder = folder or BASE_DIR
    absents = list(TOOLS) if force else missing_tools(folder)
    if not absents:
        return []

    print("Outils manquants : %s" % ", ".join(absents))
    print("Telechargement depuis les depots officiels (une seule fois)...")

    if "yt-dlp.exe" in absents:
        try:
            install_ytdlp(folder)
        except Exception as exc:
            print("  echec yt-dlp : %s" % exc)

    besoins = tuple(n for n in ("ffmpeg.exe", "ffprobe.exe") if n in absents)
    if besoins:
        try:
            install_ffmpeg(folder, besoins)
        except Exception as exc:
            print("  echec ffmpeg : %s" % exc)

    reste = missing_tools(folder)
    if reste:
        print("Toujours absent : %s" % ", ".join(reste))
        print("Telechargement manuel :")
        print("  %s" % YTDLP_URL)
        print("  %s  (placer bin\\ffmpeg.exe et bin\\ffprobe.exe a cote de clipper.py)"
              % FFMPEG_URL)
    else:
        print("Outils prets dans %s" % folder)
    return reste


def https_ready():
    """Verifie que Python sait parler HTTPS avant de tenter quoi que ce soit."""
    try:
        ssl.create_default_context()
        return True
    except Exception:
        return False


def main():
    if not https_ready():
        print("Ce Python n'a pas de support HTTPS (module ssl inutilisable).")
        print("Anaconda : lancez l'installation via Clipper.bat, qui ajoute les DLL requises.")
        return 1
    reste = ensure_tools(BASE_DIR, force="--force" in sys.argv)
    return 1 if reste else 0


if __name__ == "__main__":
    sys.exit(main())
