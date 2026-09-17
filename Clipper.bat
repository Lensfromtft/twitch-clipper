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

rem 4) en dernier recours, l'alias du Store (l'appli s'adapte et previent)
if not defined PYEXE (
    where python >nul 2>nul && set "PYEXE=python"
)

rem --------------------------------------------------------------------------
rem  Toujours rien : Python n'est pas installe. winget est fourni avec
rem  Windows 11 et Windows 10 (2004+) ; on propose l'installation plutot que de
rem  renvoyer vers un telechargement manuel.
rem --------------------------------------------------------------------------
if not defined PYEXE (
    echo Python est introuvable sur cette machine.
    echo.
    where winget >nul 2>nul
    if errorlevel 1 (
        echo Installez Python 3 depuis https://www.python.org/downloads/ puis
        echo relancez ce fichier. Cochez "Add python.exe to PATH" au passage.
        echo.
        pause >nul
        exit /b 1
    )
    echo Installation automatique par winget ^(gestionnaire de paquets Windows^).
    echo Appuyez sur une touche pour lancer, ou fermez la fenetre pour annuler.
    pause >nul
    winget install --exact --id Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements
    echo.
    echo Termine. Relancez Clipper.bat : le PATH doit etre recharge.
    echo.
    pause >nul
    exit /b 0
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
