@echo off
title Clipper
cd /d "%~dp0"
setlocal enabledelayedexpansion

rem --------------------------------------------------------------------------
rem  Choix de l'interpreteur Python.
rem  On evite volontairement l'alias "Microsoft Store" (...\WindowsApps\...) :
rem  c'est une application MSIX dont les ecritures dans %LOCALAPPDATA% sont
rem  redirigees vers un conteneur prive que yt-dlp.exe et ffmpeg.exe ne voient
rem  pas -> le fichier de cookies temporaire leur est invisible et les VOD
rem  reservees aux abonnes echouent avec "You must be logged into an account".
rem --------------------------------------------------------------------------
set "PYEXE="
set "PYARGS="

rem 1) le lanceur officiel py (installations python.org)
where py >nul 2>nul && (
    set "PYEXE=py"
    set "PYARGS=-3"
)

rem 2) un python du PATH qui ne soit pas l'alias du Store.
rem    Le filtre se fait par substitution de chaine plutot qu'avec "find" :
rem    selon le PATH, c'est le find de Git Bash ou de MSYS qui repondait, avec
rem    une autre syntaxe, et l'alias du Store passait quand meme.
if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        set "CANDIDAT=%%P"
        if "!CANDIDAT:WindowsApps=!"=="!CANDIDAT!" if not defined PYEXE set "PYEXE=%%P"
    )
)

rem 3) emplacements d'installation habituels
if not defined PYEXE (
    for %%D in (
        "%USERPROFILE%\anaconda3\python.exe"
        "%USERPROFILE%\miniconda3\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "%ProgramFiles%\Python313\python.exe"
        "%ProgramFiles%\Python312\python.exe"
        "%ProgramFiles%\Python311\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
    ) do if not defined PYEXE if exist %%D set "PYEXE=%%~fD"
)

rem 4) Python portable deja installe par ce lanceur lors d'un lancement precedent
set "PYHOME=%LOCALAPPDATA%\Clipper\python"
if not defined PYEXE if exist "%PYHOME%\python.exe" set "PYEXE=%PYHOME%\python.exe"

rem --------------------------------------------------------------------------
rem  Toujours rien : poste vierge. On telecharge la distribution "embarquable"
rem  officielle de Python (python.org, ~10 Mo) dans %LOCALAPPDATA%\Clipper\python.
rem  Ni droits administrateur, ni installeur, ni modification du PATH : il
rem  suffit de supprimer ce dossier pour la retirer. Elle contient tout ce dont
rem  Clipper a besoin (bibliotheque standard + HTTPS).
rem
rem  Remarque : sur un Windows neuf, "where python" trouve un faux python.exe
rem  (alias qui ouvre le Microsoft Store sans rien executer). On ne s'y fie donc
rem  qu'en tout dernier recours, et seulement s'il execute vraiment du Python.
rem  Les lignes ci-dessous sont volontairement hors de tout bloc ( ) : cmd lit
rem  un bloc en une fois et y fige les variables.
rem --------------------------------------------------------------------------
set "PYVER=3.12.10"
set "PYZIP=python-%PYVER%-embed-amd64.zip"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "PYZIP=python-%PYVER%-embed-arm64.zip"
if not defined PYEXE echo Python est introuvable : installation d'une copie portable ^(une seule fois, ~10 Mo^)...
if not defined PYEXE powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $zip=Join-Path $env:TEMP 'clipper-python.zip'; Invoke-WebRequest -UseBasicParsing -Uri ('https://www.python.org/ftp/python/' + $env:PYVER + '/' + $env:PYZIP) -OutFile $zip; if (Test-Path $env:PYHOME) { Remove-Item -Recurse -Force $env:PYHOME }; Expand-Archive -Force $zip $env:PYHOME; Remove-Item $zip"
if not defined PYEXE if exist "%PYHOME%\python.exe" set "PYEXE=%PYHOME%\python.exe"

rem 5) en dernier recours, le Python du Store s'il est reellement installe
rem    (l'appli s'adapte et previent).
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"

if not defined PYEXE (
    echo.
    echo Impossible d'installer Python automatiquement ^(pas d'acces a python.org ?^).
    echo Installez Python 3 depuis https://www.python.org/downloads/ puis
    echo relancez ce fichier. Cochez "Add python.exe to PATH" au passage.
    echo.
    pause >nul
    exit /b 1
)

rem --------------------------------------------------------------------------
rem  Installations de type conda/Anaconda : lancees hors environnement active,
rem  elles ne trouvent pas les DLL OpenSSL de <prefix>\Library\bin. Python perd
rem  alors HTTPS ("unknown url type: https") et la liste des VOD retombe sur
rem  yt-dlp, plus lente. On reproduit donc ce que fait "conda activate".
rem --------------------------------------------------------------------------
for %%I in ("%PYEXE%") do set "PYDIR=%%~dpI"
if exist "%PYDIR%Library\bin" (
    set "PATH=%PYDIR%;%PYDIR%Library\mingw-w64\bin;%PYDIR%Library\usr\bin;%PYDIR%Library\bin;%PYDIR%Scripts;%PATH%"
)

echo Interpreteur : %PYEXE% %PYARGS%
echo.
"%PYEXE%" %PYARGS% clipper.py %*

echo.
echo Serveur arrete. Appuyez sur une touche pour fermer cette fenetre.
pause >nul
