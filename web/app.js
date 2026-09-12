/* Twitch Clipper - interface */
(function () {
  "use strict";

  var TOKEN = window.CLIP_TOKEN;
  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  var state = {
    config: {},
    kind: "archives",
    channel: null,
    videos: [],
    cursor: null,
    limit: 30,
    vod: null,
    player: null,
    playerReady: false,
    playerTimer: null,
    previewing: false,
    previewTimer: null,
    vertical: false,
    frame: { url: null, w: 0, h: 0 },
    layout: { cam: [0, 0, 490, 284], game: [493, 0, 934, 1080], split: 0.35 },
    current: 0,
    start: 0,
    end: 15,
    scrubbing: false,
    jobs: [],
    pollTimer: null
  };

  /* ------------------------------------------------------------ api --- */

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign({ "X-Clip-Token": TOKEN }, options.headers || {});
    if (options.body !== undefined && typeof options.body !== "string") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    return fetch(path, options).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) { throw new Error(data.error || ("Erreur " + resp.status)); }
        return data;
      });
    });
  }

  function mediaUrl(path) {
    return "/media?token=" + encodeURIComponent(TOKEN) + "&path=" + encodeURIComponent(path);
  }

  /* -------------------------------------------------------- helpers --- */

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function fmtTC(seconds, withDecimals) {
    seconds = Math.max(0, Number(seconds) || 0);
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = Math.floor(seconds % 60);
    var out = pad(h) + ":" + pad(m) + ":" + pad(s);
    if (withDecimals) {
      var frac = seconds - Math.floor(seconds);
      if (frac >= 0.05) { out += ("" + frac.toFixed(1)).slice(1); }
    }
    return out;
  }

  function fmtDur(seconds) {
    seconds = Math.max(0, Math.round(Number(seconds) || 0));
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    return h ? (h + "h" + pad(m) + "m") : (m ? (m + "m" + pad(s) + "s") : (s + "s"));
  }

  function parseTC(text) {
    text = String(text == null ? "" : text).trim().replace(",", ".");
    if (!text) { return 0; }
    if (text.indexOf(":") >= 0) {
      var parts = text.split(":").map(function (p) { return parseFloat(p) || 0; });
      while (parts.length < 3) { parts.unshift(0); }
      return parts[0] * 3600 + parts[1] * 60 + parts[2];
    }
    var m = text.match(/^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s?)?$/i);
    if (m && (m[1] || m[2] || m[3])) {
      return (parseFloat(m[1]) || 0) * 3600 + (parseFloat(m[2]) || 0) * 60 + (parseFloat(m[3]) || 0);
    }
    return parseFloat(text) || 0;
  }

  function fmtSize(bytes) {
    if (!bytes) { return ""; }
    var units = ["o", "Ko", "Mo", "Go"], i = 0, value = bytes;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return value.toFixed(value < 10 && i > 0 ? 1 : 0) + " " + units[i];
  }

  function fmtViews(n) {
    n = Number(n) || 0;
    if (n >= 1000000) { return (n / 1000000).toFixed(1).replace(".0", "") + " M"; }
    if (n >= 1000) { return Math.round(n / 1000) + " k"; }
    return String(n);
  }

  function fmtDate(iso) {
    if (!iso) { return ""; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return ""; }
    return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "short", year: "numeric" });
  }

  function isoDay(vod) {
    if (vod && vod.created_at) { return String(vod.created_at).slice(0, 10); }
    if (vod && vod.upload_date && vod.upload_date.length === 8) {
      return vod.upload_date.slice(0, 4) + "-" + vod.upload_date.slice(4, 6) + "-" + vod.upload_date.slice(6, 8);
    }
    return "";
  }

  function thumbUrl(url) {
    if (!url) { return ""; }
    return url.replace(/%?\{width\}/g, "320").replace(/%?\{height\}/g, "180");
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  }

  var toastTimer = null;
  function toast(message, kind) {
    var el = $("#toast");
    el.textContent = message;
    el.className = "toast " + (kind || "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, kind === "err" ? 7000 : 3200);
  }

  function setStatus(text, kind) {
    var pill = $("#statusPill");
    pill.textContent = text;
    pill.className = "pill " + (kind || "");
  }

  /* --------------------------------------------------------- config --- */

  function loadConfig() {
    return api("/api/config").then(function (cfg) {
      state.config = cfg;
      $("#preciseCheck").checked = !!cfg.precise_cut;
      $("#labelCheck").checked = cfg.vertical_label !== false;
      renderRecents();
      var settings = $("#btnSettings");
      settings.classList.toggle("has-update", !!cfg.ytdlp_update_available);
      settings.title = cfg.ytdlp_update_available
        ? "Réglages — yt-dlp " + cfg.ytdlp_latest + " disponible"
        : "Réglages";
      if (cfg.last_channel && !$("#channelInput").value) {
        $("#channelInput").value = cfg.last_channel;
      }
      return cfg;
    });
  }

  function saveConfig(patch) {
    return api("/api/config", { method: "POST", body: patch }).then(function (res) {
      // Fusion et non remplacement : une réponse incomplète ne doit pas effacer
      // l'état détecté de l'environnement (présence de yt-dlp, ffmpeg, dossiers).
      state.config = Object.assign({}, state.config, res.config || {});
      renderRecents();
      return state.config;
    });
  }

  // Chaînes épinglées d'abord, puis les récentes ; une chaîne épinglée reste
  // listée même quand elle sort de l'historique (plafonné à 12).
  function renderRecents() {
    var pinned = state.config.favorites || [];
    var seen = {};
    var chips = [];
    pinned.concat(state.config.recent_channels || []).forEach(function (name) {
      var key = String(name).toLowerCase();
      if (seen[key]) { return; }
      seen[key] = true;
      chips.push({ name: name, pinned: isPinned(name) });
    });
    $("#recentChannels").innerHTML = chips.map(function (c) {
      var active = state.channel && state.channel.login.toLowerCase() === c.name.toLowerCase();
      return '<span class="chip' + (active ? " is-active" : "") + (c.pinned ? " is-pinned" : "") + '">' +
        '<button class="chip-name" data-channel="' + escapeHtml(c.name) + '">' + escapeHtml(c.name) + "</button>" +
        '<button class="chip-pin" data-pin="' + escapeHtml(c.name) + '" title="' +
        (c.pinned ? "Détacher" : "Épingler") + '">' + (c.pinned ? "★" : "☆") + "</button>" +
        "</span>";
    }).join("");
  }

  function isPinned(name) {
    return (state.config.favorites || []).some(function (f) {
      return String(f).toLowerCase() === String(name).toLowerCase();
    });
  }

  function togglePin(name) {
    var pinned = isPinned(name);
    var favs = (state.config.favorites || []).filter(function (f) {
      return String(f).toLowerCase() !== String(name).toLowerCase();
    });
    if (!pinned) { favs.unshift(name); }
    saveConfig({ favorites: favs })
      .then(function () { toast(pinned ? "Chaîne détachée." : "Chaîne épinglée.", "ok"); })
      .catch(function (err) { toast(err.message, "err"); });
  }

  /* ------------------------------------------------------- listings --- */

  function loadChannel(login, append) {
    if (!login) { return; }
    setStatus("Chargement…", "busy");
    var url = "/api/channel?login=" + encodeURIComponent(login) +
      "&type=" + state.kind + "&limit=" + state.limit +
      (append && state.cursor ? "&cursor=" + encodeURIComponent(state.cursor) : "");
    return api(url).then(function (data) {
      state.channel = data.channel;
      state.cursor = data.cursor;
      state.videos = append ? state.videos.concat(data.videos) : data.videos;
      $("#channelInput").value = data.channel.login;
      $("#moreWrap").hidden = !data.has_more;
      renderChannelHead();
      renderVods();
      setStatus("Prêt");
      return loadConfig();
    }).catch(function (err) {
      setStatus("Erreur", "err");
      $("#vodList").innerHTML = '<div class="empty"><p>' + escapeHtml(err.message) + "</p></div>";
      $("#moreWrap").hidden = true;
      toast(err.message, "err");
    });
  }

  function renderChannelHead() {
    var head = $("#channelHead");
    if (!state.channel) { head.hidden = true; return; }
    head.hidden = false;
    head.innerHTML = (state.channel.avatar ? '<img alt="" src="' + escapeHtml(state.channel.avatar) + '">' : "") +
      "<div><b>" + escapeHtml(state.channel.display_name) + "</b><div class='muted small'>" +
      state.videos.length + " vidéo(s)</div></div>";
  }

  function renderVods() {
    var list = $("#vodList");
    if (!state.videos.length) {
      list.innerHTML = '<div class="empty"><p>Aucune vidéo dans cette catégorie.</p></div>';
      return;
    }
    list.innerHTML = state.videos.map(function (vod) {
      var active = state.vod && state.vod.id === vod.id;
      var sub = [];
      if (vod.created_at) { sub.push(escapeHtml(fmtDate(vod.created_at))); }
      if (vod.game) { sub.push(escapeHtml(vod.game)); }
      if (vod.views) { sub.push(fmtViews(vod.views) + " vues"); }
      return '<div class="vod-card' + (active ? " is-active" : "") + '" data-id="' + escapeHtml(vod.id) + '">' +
        '<div class="vod-thumb">' +
          (vod.thumbnail ? '<img loading="lazy" alt="" src="' + escapeHtml(thumbUrl(vod.thumbnail)) + '">' : "") +
          '<span class="dur">' + fmtDur(vod.duration) + "</span>" +
        "</div>" +
        '<div class="vod-info">' +
          '<div class="vod-name">' + escapeHtml(vod.title) + "</div>" +
          '<div class="vod-sub">' + sub.map(function (s) { return "<span>" + s + "</span>"; }).join("") + "</div>" +
        "</div></div>";
    }).join("");
  }

  /* --------------------------------------------------------- lecteur --- */

  function selectVod(vod, startAt) {
    state.vod = {
      id: vod.id,
      title: vod.title || "",
      duration: Number(vod.duration) || 0,
      channel: vod.channel || (state.channel ? state.channel.login : ""),
      channel_name: vod.channel_name || (state.channel ? state.channel.display_name : ""),
      date: isoDay(vod),
      qualities: vod.qualities || null
    };
    renderVods();
    renderVodHeader();

    // Le découpage doit rester possible même si le lecteur ne démarre pas :
    // on arme les contrôles avant de monter la prévisualisation.
    setRange(startAt || 0, (startAt || 0) + 15, true);
    $("#btnDownload").disabled = false;
    $("#btnPreviewCut").disabled = false;
    $("#scrubBar").disabled = !state.vod.duration;
    $("#totalTime").textContent = fmtTC(state.vod.duration);

    mountPlayer(state.vod.id, startAt || 0);

    if (state.labelAuto !== false) { $("#labelInput").value = state.vod.channel || ""; }
    if (state.frame.url) { URL.revokeObjectURL(state.frame.url); }
    state.frame = { url: null, w: 0, h: 0 };   // l'image de repérage change de VOD
    if (state.vertical) { loadFrame(); }

    // Métadonnées complètes (qualités disponibles, durée exacte, date)
    api("/api/vod?id=" + encodeURIComponent(vod.id)).then(function (info) {
      if (!state.vod || state.vod.id !== info.id) { return; }
      state.vod.duration = info.duration || state.vod.duration;
      state.vod.title = state.vod.title || info.title;
      state.vod.channel = info.channel || state.vod.channel;
      state.vod.channel_name = info.channel_name || state.vod.channel_name;
      state.vod.date = state.vod.date || isoDay(info);
      state.vod.qualities = info.qualities;
      $("#totalTime").textContent = fmtTC(state.vod.duration);
      $("#scrubBar").disabled = !state.vod.duration;
      renderQualities(info.qualities);
      renderVodHeader();
      updateCommand();
    }).catch(function (err) {
      renderQualities(null);
      toast("Métadonnées indisponibles : " + err.message, "err");
    });
  }

  function renderVodHeader() {
    var vod = state.vod;
    if (!vod) { return; }
    $("#vodTitle").textContent = vod.title || ("VOD " + vod.id);
    $("#vodTitle").title = vod.title || "";
    var bits = [];
    if (vod.channel_name || vod.channel) { bits.push(escapeHtml(vod.channel_name || vod.channel)); }
    if (vod.date) { bits.push(escapeHtml(fmtDate(vod.date))); }
    if (vod.duration) { bits.push("durée " + fmtDur(vod.duration)); }
    bits.push('<a class="muted" target="_blank" rel="noreferrer" href="https://www.twitch.tv/videos/' +
      escapeHtml(vod.id) + '">twitch.tv/videos/' + escapeHtml(vod.id) + "</a>");
    $("#vodMeta").innerHTML = bits.map(function (b) { return "<span>" + b + "</span>"; }).join("");
  }

  function renderQualities(qualities) {
    var select = $("#qualitySelect");
    var previous = select.value;
    var options = ['<option value="best">Meilleure</option>'];
    (qualities || []).forEach(function (q) {
      options.push('<option value="' + q.height + '">' + escapeHtml(q.label) + "</option>");
    });
    options.push('<option value="audio">Audio seul</option>');
    select.innerHTML = options.join("");
    var wanted = previous || state.config.quality || "best";
    select.value = Array.prototype.some.call(select.options, function (o) { return o.value === wanted; })
      ? wanted : "best";
  }

  function playerFallback(message) {
    var ph = $("#playerPlaceholder");
    ph.innerHTML = '<div><div class="ph-icon">&#9888;</div><p>' + escapeHtml(message) + "</p>" +
      "<p class='muted small'>Le découpage reste possible : saisissez les timecodes à la main." +
      (state.vod ? ' <a class="muted" target="_blank" rel="noreferrer" href="https://www.twitch.tv/videos/' +
        escapeHtml(state.vod.id) + '">Ouvrir la VOD sur Twitch</a>' : "") + "</p></div>";
    ph.hidden = false;
  }

  function mountPlayer(videoId, startAt) {
    var host = $("#player");
    $("#playerPlaceholder").hidden = true;
    state.playerReady = false;
    clearTimeout(state.playerTimer);
    stopCutPreview(false);
    // Détruire l'instance précédente : vider l'iframe ne désinscrit pas ses
    // écouteurs, qui s'accumulaient à chaque changement de VOD.
    if (state.player && typeof state.player.destroy === "function") {
      try { state.player.destroy(); } catch (e) { /* ignore */ }
    }
    state.player = null;
    host.innerHTML = "";
    fitPlayer();
    if (!window.Twitch || !window.Twitch.Player) {
      playerFallback("Lecteur Twitch indisponible (script non chargé).");
      return;
    }
    try {
      state.player = new Twitch.Player("player", {
        video: String(videoId),
        parent: [location.hostname],
        width: "100%",
        height: "100%",
        autoplay: false,
        muted: false,
        time: fmtTwitchTime(startAt || 0)
      });
      state.player.addEventListener(Twitch.Player.READY, function () {
        state.playerReady = true;
        clearTimeout(state.playerTimer);
        $("#playerPlaceholder").hidden = true;
        if (startAt) { try { state.player.seek(startAt); } catch (e) { /* ignore */ } }
      });
      // Si aucune iframe n'a été insérée, on le dit au lieu de laisser un cadre
      // noir. Si l'iframe est là mais muette (VOD abonnés...), on ne la masque
      // pas : le message d'erreur de Twitch lui-même est plus précis.
      state.playerTimer = setTimeout(function () {
        if (!state.playerReady && !host.querySelector("iframe")) {
          playerFallback("Le lecteur Twitch n'a pas pu être chargé.");
        }
      }, 10000);
    } catch (err) {
      playerFallback("Lecteur Twitch en erreur : " + err.message);
    }
  }

  // Inscrit le lecteur en 16:9 au plus grand dans le cadre disponible : selon la
  // forme du cadre c'est tantôt la largeur, tantôt la hauteur qui contraint, et
  // le CSS seul ne sait pas arbitrer entre les deux.
  function fitPlayer() {
    var wrap = $(".player-wrap"), player = $("#player");
    if (!wrap || !player) { return; }
    var box = wrap.getBoundingClientRect();
    if (!box.width || !box.height) { return; }
    var width = Math.min(box.width, box.height * 16 / 9);
    player.style.width = Math.round(width) + "px";
    player.style.height = Math.round(width * 9 / 16) + "px";
  }

  if (window.ResizeObserver) {
    new ResizeObserver(fitPlayer).observe($(".player-wrap"));
  } else {
    window.addEventListener("resize", fitPlayer);
  }

  function fmtTwitchTime(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    return Math.floor(seconds / 3600) + "h" + Math.floor((seconds % 3600) / 60) + "m" + (seconds % 60) + "s";
  }

  function playerTime() {
    if (!state.player || !state.playerReady) { return null; }
    try {
      var t = state.player.getCurrentTime();
      return (typeof t === "number" && isFinite(t)) ? t : null;
    } catch (e) { return null; }
  }

  function seekPlayer(seconds) {
    if (!state.player) { return; }
    try { state.player.seek(Math.max(0, seconds)); } catch (e) { /* ignore */ }
  }

  function seekBy(delta) {
    var t = playerTime();
    if (t === null) { toast("Lancez la lecture pour naviguer.", "err"); return; }
    seekPlayer(Math.max(0, t + delta));
  }

  function togglePlay() {
    if (!state.player || !state.playerReady) { return; }
    try {
      if (state.player.isPaused()) { state.player.play(); } else { state.player.pause(); }
    } catch (e) { /* ignore */ }
  }

  /* ------------------------------------------------ aperçu de la coupe --- */

  function setPreviewButton(playing) {
    var btn = $("#btnPreviewCut");
    btn.innerHTML = (playing ? "Arrêter l'aperçu" : "Lire la sélection") +
      ' <kbd class="kbd-inline">P</kbd>';
    btn.classList.toggle("btn-playing", playing);
  }

  function stopCutPreview(pause) {
    clearInterval(state.previewTimer);
    state.previewTimer = null;
    state.previewing = false;
    setPreviewButton(false);
    if (pause && state.player) {
      try { state.player.pause(); } catch (e) { /* ignore */ }
    }
  }

  // Lit uniquement [début, fin] puis s'arrête : permet de valider la coupe
  // avant de lancer un téléchargement.
  function startCutPreview() {
    if (state.previewing) { stopCutPreview(true); return; }
    if (!state.vod || !state.playerReady) { toast("Le lecteur n'est pas prêt.", "err"); return; }

    var start = state.start, end = state.end;
    seekPlayer(start);
    try { state.player.play(); } catch (e) { /* ignore */ }
    state.previewing = true;
    setPreviewButton(true);

    // On n'arme l'arrêt qu'une fois le lecteur réellement arrivé dans la
    // sélection : juste après un saut, getCurrentTime() renvoie encore
    // brièvement l'ancienne position, qui peut être au-delà de la fin.
    var armed = false;
    var deadline = Date.now() + (end - start) * 1000 + 20000;
    state.previewTimer = setInterval(function () {
      var t = playerTime();
      if (t === null) { return; }
      if (!armed) {
        if (t >= start - 2 && t < end) { armed = true; }
        else if (Date.now() > deadline) { stopCutPreview(true); }
        return;
      }
      if (t >= end - 0.05 || Date.now() > deadline) {
        stopCutPreview(true);
        seekPlayer(start);
      }
    }, 100);
  }

  setInterval(function () {
    var t = playerTime();
    if (t === null) { return; }
    state.current = t;
    $("#curTime").textContent = fmtTC(t);
    if (!state.scrubbing && state.vod && state.vod.duration) {
      $("#scrubBar").value = Math.round(1000 * t / state.vod.duration);
    }
  }, 300);

  /* ------------------------------------------------------ timecodes --- */

  function setRange(start, end, syncInputs) {
    var max = (state.vod && state.vod.duration) ? state.vod.duration : Infinity;
    start = Math.max(0, Math.min(start, max));
    end = Math.max(0, Math.min(end, max));
    if (end <= start) { end = Math.min(start + 1, max); }
    state.start = start;
    state.end = end;
    if (syncInputs !== false) {
      $("#startInput").value = fmtTC(start, true);
      $("#endInput").value = fmtTC(end, true);
      $("#durInput").value = fmtTC(end - start, true);
    }
    updateMarks();
    updateCommand();
  }

  function updateMarks() {
    var duration = state.vod && state.vod.duration;
    var startMark = $("#markStart"), endMark = $("#markEnd"), rangeMark = $("#markRange");
    if (!duration) {
      startMark.style.display = endMark.style.display = rangeMark.style.display = "none";
      return;
    }
    var a = 100 * state.start / duration, b = 100 * state.end / duration;
    startMark.style.display = endMark.style.display = rangeMark.style.display = "block";
    startMark.style.left = a + "%";
    endMark.style.left = b + "%";
    rangeMark.style.left = a + "%";
    rangeMark.style.width = Math.max(0.3, b - a) + "%";
  }

  function readInputs(source) {
    var start = parseTC($("#startInput").value);
    var end = parseTC($("#endInput").value);
    var dur = parseTC($("#durInput").value);
    if (source === "dur") { end = start + Math.max(0.1, dur); }
    else if (source === "start" && start >= end) { end = start + Math.max(0.1, dur || 15); }
    setRange(start, end, true);
  }

  function updateCommand() {
    var vod = state.vod;
    if (!vod) { $("#cmdPreview").textContent = "—"; return; }
    var quality = $("#qualitySelect").value;
    var format = quality === "audio" ? "ba/b"
      : (quality === "best" ? "bv*+ba/b" : "bv*[height<=" + quality + "]+ba/b[height<=" + quality + "]");
    var parts = ["yt-dlp"];
    if (state.config.use_cookies && state.config.cookies_present) { parts.push("--cookies cookies.txt"); }
    parts.push('--download-sections "*' + fmtTC(state.start, true) + "-" + fmtTC(state.end, true) + '"');
    if ($("#preciseCheck").checked) { parts.push("--force-keyframes-at-cuts"); }
    parts.push('-f "' + format + '"');
    parts.push('"https://www.twitch.tv/videos/' + vod.id + '"');
    var text = parts.join(" ");
    if (state.vertical) {
      // Le mode vertical ajoute une seconde passe : on montre les deux.
      var camH = Math.round(OUT_H * state.layout.split / 2) * 2;
      var c = state.layout.cam, g = state.layout.game;
      text += "\n\nffmpeg -i clip.mp4 -filter_complex \"" +
        "[0:v]crop=" + c[2] + ":" + c[3] + ":" + c[0] + ":" + c[1] +
        ",scale=" + OUT_W + ":" + camH + ":force_original_aspect_ratio=increase,crop=" + OUT_W + ":" + camH + "[cam];" +
        "[0:v]crop=" + g[2] + ":" + g[3] + ":" + g[0] + ":" + g[1] +
        ",scale=" + OUT_W + ":" + (OUT_H - camH) + ":force_original_aspect_ratio=increase,crop=" + OUT_W + ":" + (OUT_H - camH) + "[game];" +
        "[cam][game]vstack" + ($("#labelCheck").checked && labelText() ? ",drawtext=text='" + labelText() + "'…" : "") +
        "\" -c:v libx264 -crf 20 vertical.mp4";
    }
    $("#cmdPreview").textContent = text;
  }

  /* ------------------------------------------- composition verticale --- */

  var OUT_W = 1080, OUT_H = 1920;

  function defaultSplit() {
    return Number(state.config.default_split) || 0.32;
  }

  // Cadrage de référence, relevé à la main sur une source 1920x1080. Il est mis
  // à l'échelle si la vidéo source a une autre définition.
  var REF_W = 1920, REF_H = 1080;
  var REF_CAM = [0, 0, 499, 283];
  var REF_GAME = [514, 0, 893, 1080];

  function defaultLayout(fw, fh) {
    var sx = (fw || REF_W) / REF_W, sy = (fh || REF_H) / REF_H;
    var scaled = function (box) {
      return [Math.round(box[0] * sx), Math.round(box[1] * sy),
              Math.round(box[2] * sx), Math.round(box[3] * sy)];
    };
    return { cam: scaled(REF_CAM), game: scaled(REF_GAME), split: defaultSplit() };
  }

  function clampBox(box) {
    var fw = state.frame.w || 1920, fh = state.frame.h || 1080;
    var w = Math.max(40, Math.min(box[2], fw));
    var h = Math.max(40, Math.min(box[3], fh));
    return [
      Math.round(Math.max(0, Math.min(box[0], fw - w))),
      Math.round(Math.max(0, Math.min(box[1], fh - h))),
      Math.round(w), Math.round(h)
    ];
  }

  function channelKey() {
    var name = (state.vod && state.vod.channel) || (state.channel && state.channel.login) || "";
    return String(name).toLowerCase();
  }

  function ensureLayout() {
    var saved = (state.config.layouts || {})[channelKey()];
    var base = defaultLayout(state.frame.w || 1920, state.frame.h || 1080);
    if (saved && saved.cam && saved.game) {
      state.layout = { cam: clampBox(saved.cam), game: clampBox(saved.game),
                       split: Number(saved.split) || base.split };
    } else {
      state.layout = base;
    }
    $("#splitRange").value = Math.round(state.layout.split * 100);
    $("#splitValue").textContent = Math.round(state.layout.split * 100) + " %";
  }

  function saveLayout() {
    var key = channelKey();
    if (!key) { return; }
    var layouts = Object.assign({}, state.config.layouts || {});
    layouts[key] = state.layout;
    saveConfig({ layouts: layouts }).catch(function () { /* silencieux */ });
  }

  // Rectangle réellement occupé par l'image dans son cadre (object-fit: contain).
  function frameRect() {
    var host = $("#vframe");
    var W = host.clientWidth, H = host.clientHeight;
    var iw = state.frame.w, ih = state.frame.h;
    if (!iw || !ih) { return { x: 0, y: 0, w: W, h: H, scale: 1 }; }
    var scale = Math.min(W / iw, H / ih);
    return { x: (W - iw * scale) / 2, y: (H - ih * scale) / 2,
             w: iw * scale, h: ih * scale, scale: scale };
  }

  function renderBoxes() {
    var r = frameRect();
    [["cam", "#boxCam"], ["game", "#boxGame"]].forEach(function (pair) {
      var box = state.layout[pair[0]], el = $(pair[1]);
      el.style.left = (r.x + box[0] * r.scale) + "px";
      el.style.top = (r.y + box[1] * r.scale) + "px";
      el.style.width = (box[2] * r.scale) + "px";
      el.style.height = (box[3] * r.scale) + "px";
    });
  }

  // Reproduit le comportement de ffmpeg (agrandir puis recadrer au centre).
  function coverInto(img, region, boxW, boxH) {
    if (!state.frame.w || !boxW || !boxH) { return; }
    var scale = Math.max(boxW / region[2], boxH / region[3]);
    img.style.width = (state.frame.w * scale) + "px";
    img.style.height = (state.frame.h * scale) + "px";
    img.style.left = (-region[0] * scale + (boxW - region[2] * scale) / 2) + "px";
    img.style.top = (-region[1] * scale + (boxH - region[3] * scale) / 2) + "px";
  }

  function renderPreview() {
    var box = $("#vpreview").getBoundingClientRect();
    if (!box.height) { return; }
    var camH = Math.round(box.height * state.layout.split);
    var gameH = Math.round(box.height) - camH;
    $(".vpreview-cam").style.height = camH + "px";
    $(".vpreview-game").style.height = gameH + "px";
    coverInto($("#vprevCam"), state.layout.cam, box.width, camH);
    coverInto($("#vprevGame"), state.layout.game, box.width, gameH);
    var label = $("#vprevLabel");
    $("#vprevName").textContent = labelText();
    $("#vprevLogo").hidden = !state.config.logo_present;
    label.hidden = !($("#labelCheck").checked && labelText());
  }

  function labelText() {
    var raw = $("#labelInput").value || "";
    return raw.replace(/[^0-9A-Za-z _.\-]/g, "").trim().slice(0, 40);
  }

  function initBoxDrag(el, key) {
    var mode = null, origin = null, startBox = null;
    el.addEventListener("pointerdown", function (e) {
      if (!state.frame.w) { return; }
      mode = e.target.classList.contains("vbox-grip") ? "resize" : "move";
      origin = { x: e.clientX, y: e.clientY };
      startBox = state.layout[key].slice();
      el.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    el.addEventListener("pointermove", function (e) {
      if (!mode) { return; }
      var scale = frameRect().scale || 1;
      var dx = (e.clientX - origin.x) / scale, dy = (e.clientY - origin.y) / scale;
      var box = startBox.slice();
      if (mode === "move") { box[0] += dx; box[1] += dy; }
      else { box[2] += dx; box[3] += dy; }
      state.layout[key] = clampBox(box);
      renderBoxes();
      renderPreview();
    });
    ["pointerup", "pointercancel"].forEach(function (evt) {
      el.addEventListener(evt, function () {
        if (mode) { mode = null; saveLayout(); updateCommand(); }
      });
    });
  }

  function loadFrame() {
    if (!state.vod) { return; }
    var state_el = $("#vframeState");
    state_el.hidden = false;
    state_el.textContent = "Extraction de l'image…";
    var url = "/api/frame?id=" + encodeURIComponent(state.vod.id) +
      "&t=" + encodeURIComponent(state.start.toFixed(2));
    fetch(url, { headers: { "X-Clip-Token": TOKEN } }).then(function (resp) {
      if (!resp.ok) {
        return resp.json().catch(function () { return {}; }).then(function (d) {
          throw new Error(d.error || ("Erreur " + resp.status));
        });
      }
      return resp.blob();
    }).then(function (blob) {
      if (state.frame.url) { URL.revokeObjectURL(state.frame.url); }
      var objectUrl = URL.createObjectURL(blob);
      var img = new Image();
      img.onload = function () {
        state.frame = { url: objectUrl, w: img.naturalWidth, h: img.naturalHeight };
        $("#vframeImg").src = objectUrl;
        $("#vprevCam").src = objectUrl;
        $("#vprevGame").src = objectUrl;
        ensureLayout();
        state_el.hidden = true;
        renderBoxes();
        renderPreview();
        updateCommand();
      };
      img.src = objectUrl;
    }).catch(function (err) {
      state_el.hidden = false;
      state_el.textContent = "Image indisponible : " + err.message;
    });
  }

  function setFormat(vertical, persist) {
    state.vertical = !!vertical;
    $$("#formatToggle .tab").forEach(function (tab) {
      tab.classList.toggle("is-active", (tab.getAttribute("data-format") === "vertical") === state.vertical);
    });
    $("#verticalPanel").hidden = !state.vertical;
    if (state.vertical) {
      if (!$("#labelInput").value && state.vod) {
        $("#labelInput").value = state.vod.channel || "";
      }
      if (state.vod && !state.frame.w) { loadFrame(); }
      else { ensureLayout(); renderBoxes(); renderPreview(); }
    }
    updateCommand();
    if (persist) { saveConfig({ vertical: state.vertical }).catch(function () {}); }
  }

  /* ---------------------------------------------------------- files --- */

  function submitJob() {
    var vod = state.vod;
    if (!vod) { return; }
    if (state.end <= state.start) { toast("La fin doit être après le début.", "err"); return; }
    api("/api/jobs", {
      method: "POST",
      body: {
        video_id: vod.id,
        title: vod.title,
        channel: vod.channel || (state.channel ? state.channel.login : ""),
        date: vod.date,
        start: state.start,
        end: state.end,
        quality: $("#qualitySelect").value,
        precise: $("#preciseCheck").checked,
        vertical: state.vertical,
        layout: state.vertical ? state.layout : null,
        label: (state.vertical && $("#labelCheck").checked) ? labelText() : ""
      }
    }).then(function () {
      toast("Clip ajouté à la file (" + fmtDur(state.end - state.start) + ").", "ok");
      pollJobs();
    }).catch(function (err) { toast(err.message, "err"); });
  }

  function pollJobs() {
    clearTimeout(state.pollTimer);
    api("/api/jobs").then(function (data) {
      state.jobs = data.jobs || [];
      renderJobs();
      var active = state.jobs.some(function (j) { return j.state === "running" || j.state === "queued"; });
      setStatus(active ? "Téléchargement…" : "Prêt", active ? "busy" : "");
      state.pollTimer = setTimeout(pollJobs, active ? 700 : 4000);
    }).catch(function () {
      state.pollTimer = setTimeout(pollJobs, 5000);
    });
  }

  function renderJobs() {
    var list = $("#jobList");
    if (!state.jobs.length) {
      list.innerHTML = '<div class="empty small"><p>Aucun clip pour l\'instant.</p></div>';
      return;
    }
    list.innerHTML = state.jobs.slice().reverse().map(function (job) {
      var cls = job.state === "done" ? "done" : (job.state === "error" ? "error" : "");
      var meta = [];
      if (job.state === "running") {
        meta.push(job.phase + (job.speed ? " · " + job.speed : ""));
        if (job.eta) { meta.push("reste " + job.eta); }
      } else if (job.state === "done") {
        meta.push(fmtSize(job.size));
        meta.push(job.elapsed + "s");
      } else if (job.state === "queued") {
        meta.push("En attente");
      } else if (job.state === "canceled") {
        meta.push("Annulé");
      }
      var actions = "";
      if (job.state === "done") {
        actions = '<div class="job-actions">' +
          '<button class="btn btn-sm" data-preview="' + escapeHtml(job.path) + '" data-name="' + escapeHtml(job.title) + '">Aperçu</button>' +
          '<button class="btn btn-ghost btn-sm" data-open="' + escapeHtml(job.path) + '">Ouvrir</button>' +
          '<button class="btn btn-ghost btn-sm" data-reveal="' + escapeHtml(job.path) + '">Explorateur</button>' +
          "</div>";
      } else if (job.state === "running" || job.state === "queued") {
        actions = '<div class="job-actions"><button class="btn btn-ghost btn-sm" data-cancel="' +
          escapeHtml(job.id) + '">Annuler</button></div>';
      }
      return '<div class="job ' + cls + '">' +
        '<div class="job-top"><span class="job-name" title="' + escapeHtml(job.title) + '">' +
          escapeHtml(job.title || ("VOD " + job.video_id)) + "</span>" +
          '<span class="job-range">' + fmtTC(job.start) + " · " + fmtDur(job.duration) + "</span></div>" +
        '<div class="job-sub"><span>' + escapeHtml(meta.join(" · ")) + "</span><span>" +
          (job.state === "running" ? Math.round(job.progress) + " %" : "") + "</span></div>" +
        '<div class="bar"><span style="width:' + (job.state === "done" ? 100 : job.progress) + '%"></span></div>' +
        (job.error ? '<div class="job-err">' + escapeHtml(job.error) + "</div>" : "") +
        (job.error && job.log && job.log.length
          ? '<details class="job-log"><summary>Détails</summary><pre>' +
            escapeHtml(job.log.join("\n")) + "</pre></details>"
          : "") +
        actions +
        "</div>";
    }).join("");
  }

  /* --------------------------------------------------------- modales --- */

  function openModal(id) { $("#" + id).hidden = false; }
  function closeModal(id) {
    $("#" + id).hidden = true;
    if (id === "previewModal") { $("#previewVideo").pause(); $("#previewVideo").removeAttribute("src"); }
  }

  function renderYtdlpState() {
    var cfg = state.config;
    var line = "Version installée : <b>" + escapeHtml(cfg.ytdlp_version || "inconnue") + "</b>";
    if (cfg.ytdlp_update_available) {
      line += ' <span class="badge-update">' + escapeHtml(cfg.ytdlp_latest) + " disponible</span>";
    } else if (cfg.ytdlp_latest) {
      line += " — à jour";
    }
    $("#ytdlpState").innerHTML = line;
  }

  function openSettings() {
    var cfg = state.config;
    renderYtdlpState();
    $("#updateResult").hidden = true;
    $("#cfgOutput").value = cfg.output_dir || "";
    $("#cfgCookies").value = cfg.cookies_file || "";
    $("#cfgUseCookies").checked = !!cfg.use_cookies;
    $("#cfgTemplate").value = cfg.filename_template || "";
    $("#cfgParallel").value = String(cfg.max_parallel || 1);
    $("#cookieState").textContent = cfg.cookies_present ? "(fichier trouvé)" : "(fichier introuvable)";
    $("#toolState").innerHTML =
      "yt-dlp : " + (cfg.ytdlp_present ? '<span class="ok">détecté</span>' : '<span class="ko">absent</span>') +
      "<br>ffmpeg : " + (cfg.ffmpeg_present ? '<span class="ok">détecté</span>' : '<span class="ko">absent</span>') +
      "<br>dossier : <code>" + escapeHtml(cfg.base_dir || "") + "</code>";
    openModal("settingsModal");
    // La version de yt-dlp est détectée en tâche de fond : on rafraîchit au cas
    // où la modale s'ouvre avant la fin de cette détection.
    loadConfig().then(renderYtdlpState).catch(function () { /* ignore */ });
  }

  /* ----------------------------------------------------- événements --- */

  $("#channelForm").addEventListener("submit", function (e) {
    e.preventDefault();
    state.cursor = null;
    loadChannel($("#channelInput").value.trim(), false);
  });

  $("#recentChannels").addEventListener("click", function (e) {
    var pin = e.target.closest("[data-pin]");
    if (pin) { togglePin(pin.getAttribute("data-pin")); return; }
    var chip = e.target.closest("[data-channel]");
    if (!chip) { return; }
    state.cursor = null;
    loadChannel(chip.getAttribute("data-channel"), false);
  });

  $("#kindTabs").addEventListener("click", function (e) {
    var tab = e.target.closest(".tab");
    if (!tab) { return; }
    $$("#kindTabs .tab").forEach(function (t) { t.classList.toggle("is-active", t === tab); });
    state.kind = tab.getAttribute("data-kind");
    state.cursor = null;
    if (state.channel) { loadChannel(state.channel.login, false); }
  });

  $("#btnMore").addEventListener("click", function () {
    if (state.channel) { loadChannel(state.channel.login, true); }
  });

  $("#vodList").addEventListener("click", function (e) {
    var card = e.target.closest(".vod-card");
    if (!card) { return; }
    var id = card.getAttribute("data-id");
    var vod = state.videos.filter(function (v) { return v.id === id; })[0];
    if (vod) { selectVod(vod, 0); }
  });

  $("#directForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var raw = $("#directInput").value.trim();
    var idMatch = raw.match(/(\d{6,})/);
    if (!idMatch) { toast("Lien ou identifiant de VOD invalide.", "err"); return; }
    var at = 0;
    var timeMatch = raw.match(/[?&]t=([0-9hms]+)/i);
    if (timeMatch) { at = parseTC(timeMatch[1]); }
    setStatus("Chargement…", "busy");
    api("/api/vod?id=" + idMatch[1]).then(function (info) {
      selectVod(info, at);
      setStatus("Prêt");
      if (at) { toast("Début placé à " + fmtTC(at) + " (depuis le lien).", "ok"); }
    }).catch(function (err) {
      setStatus("Erreur", "err");
      toast(err.message, "err");
      selectVod({ id: idMatch[1], title: "VOD " + idMatch[1], duration: 0 }, at);
    });
  });

  ["startInput", "endInput", "durInput"].forEach(function (id) {
    var el = $("#" + id);
    var source = id.replace("Input", "");
    el.addEventListener("change", function () { readInputs(source); });
    el.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); readInputs(source); }
    });
  });

  $$("[data-mark]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var t = playerTime();
      if (t === null) { toast("Lancez la lecture pour utiliser le repère.", "err"); return; }
      if (btn.getAttribute("data-mark") === "start") {
        setRange(t, state.end > t ? state.end : t + (state.end - state.start || 15), true);
      } else {
        setRange(state.start < t ? state.start : Math.max(0, t - 15), t, true);
      }
    });
  });

  $$("[data-seek]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      seekPlayer(btn.getAttribute("data-seek") === "start" ? state.start : state.end);
    });
  });

  $$(".nudges").forEach(function (group) {
    group.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-delta]");
      if (!btn) { return; }
      var delta = parseFloat(btn.getAttribute("data-delta"));
      if (group.getAttribute("data-target") === "start") { setRange(state.start + delta, state.end, true); }
      else { setRange(state.start, state.end + delta, true); }
    });
  });

  $(".presets").addEventListener("click", function (e) {
    var btn = e.target.closest("[data-preset]");
    if (!btn) { return; }
    setRange(state.start, state.start + parseFloat(btn.getAttribute("data-preset")), true);
  });

  var scrub = $("#scrubBar");
  scrub.addEventListener("input", function () {
    state.scrubbing = true;
    if (state.vod && state.vod.duration) {
      $("#curTime").textContent = fmtTC(scrub.value / 1000 * state.vod.duration);
    }
  });
  scrub.addEventListener("change", function () {
    state.scrubbing = false;
    if (state.vod && state.vod.duration) { seekPlayer(scrub.value / 1000 * state.vod.duration); }
  });

  $("#qualitySelect").addEventListener("change", function () {
    updateCommand();
    saveConfig({ quality: $("#qualitySelect").value }).catch(function () {});
  });

  $("#preciseCheck").addEventListener("change", function () {
    updateCommand();
    saveConfig({ precise_cut: $("#preciseCheck").checked }).catch(function () {});
  });

  $("#btnDownload").addEventListener("click", submitJob);
  $("#btnPreviewCut").addEventListener("click", startCutPreview);

  $("#formatToggle").addEventListener("click", function (e) {
    var tab = e.target.closest("[data-format]");
    if (tab) { setFormat(tab.getAttribute("data-format") === "vertical", true); }
  });

  $("#btnReloadFrame").addEventListener("click", loadFrame);

  $("#splitRange").addEventListener("input", function () {
    state.layout.split = parseInt(this.value, 10) / 100;
    $("#splitValue").textContent = this.value + " %";
    renderPreview();
  });
  $("#splitRange").addEventListener("change", function () { saveLayout(); updateCommand(); });

  $("#labelInput").addEventListener("input", function () {
    state.labelAuto = false;   // l'utilisateur a choisi son texte, on n'y touche plus
    renderPreview();
    updateCommand();
  });
  $("#labelCheck").addEventListener("change", function () {
    renderPreview();
    updateCommand();
    saveConfig({ vertical_label: this.checked }).catch(function () {});
  });

  $("#btnResetLayout").addEventListener("click", function () {
    state.layout = defaultLayout(state.frame.w || 1920, state.frame.h || 1080);
    $("#splitRange").value = Math.round(state.layout.split * 100);
    $("#splitValue").textContent = Math.round(state.layout.split * 100) + " %";
    renderBoxes();
    renderPreview();
    saveLayout();
    updateCommand();
    toast("Cadres réinitialisés.", "ok");
  });

  initBoxDrag($("#boxCam"), "cam");
  initBoxDrag($("#boxGame"), "game");
  window.addEventListener("resize", function () {
    if (state.vertical && state.frame.w) { renderBoxes(); renderPreview(); }
  });

  $("#btnUpdateYtdlp").addEventListener("click", function () {
    var btn = this, result = $("#updateResult");
    btn.disabled = true;
    btn.textContent = "Mise à jour…";
    result.hidden = true;
    api("/api/ytdlp-update", { method: "POST", body: {} }).then(function (r) {
      result.textContent = r.message;
      result.className = "update-result small " + (r.ok ? "ok" : "ko");
      result.hidden = false;
      return loadConfig().then(renderYtdlpState);
    }).catch(function (err) {
      result.textContent = err.message;
      result.className = "update-result small ko";
      result.hidden = false;
    }).then(function () {
      btn.disabled = false;
      btn.textContent = "Mettre à jour";
    });
  });

  $("#btnCopyCmd").addEventListener("click", function () {
    navigator.clipboard.writeText($("#cmdPreview").textContent).then(function () {
      toast("Commande copiée.", "ok");
    }).catch(function () { toast("Copie impossible.", "err"); });
  });

  $("#jobList").addEventListener("click", function (e) {
    var btn = e.target.closest("button");
    if (!btn) { return; }
    var path;
    if ((path = btn.getAttribute("data-preview"))) {
      $("#previewTitle").textContent = btn.getAttribute("data-name") || "Aperçu";
      $("#previewInfo").textContent = path;
      $("#previewVideo").src = mediaUrl(path);
      $("#btnPreviewOpen").setAttribute("data-open", path);
      $("#btnPreviewReveal").setAttribute("data-reveal", path);
      openModal("previewModal");
    } else if ((path = btn.getAttribute("data-open"))) {
      api("/api/open", { method: "POST", body: { path: path } }).catch(function (err) { toast(err.message, "err"); });
    } else if ((path = btn.getAttribute("data-reveal"))) {
      api("/api/reveal", { method: "POST", body: { path: path } }).catch(function (err) { toast(err.message, "err"); });
    } else if (btn.getAttribute("data-cancel")) {
      api("/api/jobs/cancel", { method: "POST", body: { id: btn.getAttribute("data-cancel") } })
        .then(function () { pollJobs(); });
    }
  });

  $("#btnPreviewOpen").addEventListener("click", function () {
    api("/api/open", { method: "POST", body: { path: this.getAttribute("data-open") } })
      .catch(function (err) { toast(err.message, "err"); });
  });
  $("#btnPreviewReveal").addEventListener("click", function () {
    api("/api/reveal", { method: "POST", body: { path: this.getAttribute("data-reveal") } })
      .catch(function (err) { toast(err.message, "err"); });
  });

  $("#btnClear").addEventListener("click", function () {
    api("/api/jobs/clear", { method: "POST", body: {} }).then(function () { pollJobs(); });
  });

  $("#btnOpenFolder").addEventListener("click", function () {
    api("/api/open", { method: "POST", body: {} }).catch(function (err) { toast(err.message, "err"); });
  });

  $("#btnSettings").addEventListener("click", openSettings);

  $("#btnSaveSettings").addEventListener("click", function () {
    saveConfig({
      output_dir: $("#cfgOutput").value.trim(),
      cookies_file: $("#cfgCookies").value.trim(),
      use_cookies: $("#cfgUseCookies").checked,
      filename_template: $("#cfgTemplate").value.trim(),
      max_parallel: parseInt($("#cfgParallel").value, 10) || 1
    }).then(function () {
      closeModal("settingsModal");
      updateCommand();
      toast("Réglages enregistrés.", "ok");
    }).catch(function (err) { toast(err.message, "err"); });
  });

  $$("[data-close]").forEach(function (btn) {
    btn.addEventListener("click", function () { closeModal(btn.getAttribute("data-close")); });
  });
  $$(".modal").forEach(function (modal) {
    modal.addEventListener("click", function (e) { if (e.target === modal) { closeModal(modal.id); } });
  });

  // Navigation relative dans le lecteur, façon table de montage.
  var SEEK_KEYS = { j: -10, l: 10, arrowleft: -5, arrowright: 5, ",": -1, ".": 1 };

  document.addEventListener("keydown", function (e) {
    var tag = (e.target.tagName || "").toLowerCase();
    var typing = tag === "input" || tag === "textarea" || tag === "select";
    if (e.key === "Escape") {
      $$(".modal").forEach(function (m) { if (!m.hidden) { closeModal(m.id); } });
      return;
    }
    if (e.ctrlKey && e.key === "Enter" && state.vod) { e.preventDefault(); submitJob(); return; }
    if (typing || e.ctrlKey || e.altKey || e.metaKey || !state.vod) { return; }

    var key = String(e.key).toLowerCase();
    var t;
    if (key === "i") {
      t = playerTime();
      if (t !== null) { setRange(t, state.end > t ? state.end : t + 15, true); toast("Début : " + fmtTC(t), "ok"); }
    } else if (key === "o") {
      t = playerTime();
      if (t !== null) { setRange(state.start < t ? state.start : Math.max(0, t - 15), t, true); toast("Fin : " + fmtTC(t), "ok"); }
    } else if (key === "p") {
      e.preventDefault();
      startCutPreview();
    } else if (key === "k" || key === " " || e.code === "Space") {
      e.preventDefault();  // sinon la barre d'espace fait défiler la page
      togglePlay();
    } else if (Object.prototype.hasOwnProperty.call(SEEK_KEYS, key)) {
      e.preventDefault();  // sinon les flèches font défiler la page
      seekBy(SEEK_KEYS[key]);
    }
  });

  /* --------------------------------------------------------- démarrage --- */

  loadConfig().then(function (cfg) {
    setFormat(!!cfg.vertical, false);
    if (cfg.last_channel) { loadChannel(cfg.last_channel, false); }
  });
  pollJobs();
  setRange(0, 15, true);
})();
