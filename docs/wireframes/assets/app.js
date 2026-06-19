/* ============================================================================
   Lumen Copilot — prototype interactions
   Builds the shared chrome (topbar + left rail), powers live theme switching,
   the command palette, and all the data-attribute behaviors the screens use.
   No dependencies. Works from file:// (icons are injected inline — no fetch).
   ============================================================================ */
(function () {
  "use strict";

  /* ---- icon sprite (injected inline so external <use> works on file://) -- */
  var ICONS = {
    "i-sparkle":
      '<path d="M12 3l1.7 4.8L18.5 9.5l-4.8 1.7L12 16l-1.7-4.8L5.5 9.5l4.8-1.7z"/><path d="M19 14l.7 1.8L21.5 16.5l-1.8.7L19 19l-.7-1.8-1.8-.7 1.8-.7z"/>',
    "i-chat":
      '<path d="M21 11.5a8.5 8.5 0 0 1-12.4 7.5L4 20.5l1.5-4.6A8.5 8.5 0 1 1 21 11.5z"/>',
    "i-search": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    "i-doc":
      '<path d="M14 3v5h5"/><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M9 13h6M9 17h4"/>',
    "i-folder":
      '<path d="M3 7a2 2 0 0 1 2-2h3.5l2 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "i-plug":
      '<path d="M12 22v-4"/><path d="M8 8V4M16 8V4"/><path d="M6 8h12v2a6 6 0 0 1-12 0z"/>',
    "i-shield":
      '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
    "i-shield-check":
      '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "i-sliders":
      '<path d="M3 7h7M14 7h7M3 12h11M18 12h3M3 17h3M10 17h11"/><circle cx="12" cy="7" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="8" cy="17" r="2"/>',
    "i-send": '<path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/>',
    "i-paperclip":
      '<path d="M21.4 11.05l-9.2 9.2a5 5 0 0 1-7.07-7.07l9.2-9.2a3.3 3.3 0 0 1 4.66 4.66l-9.2 9.2a1.6 1.6 0 0 1-2.32-2.2l8.5-8.5"/>',
    "i-plus": '<path d="M12 5v14M5 12h14"/>',
    "i-check": '<path d="M20 6L9 17l-5-5"/>',
    "i-x": '<path d="M18 6L6 18M6 6l12 12"/>',
    "i-chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "i-chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "i-arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "i-arrow-up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "i-external":
      '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M19 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h6"/>',
    "i-lock":
      '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "i-globe":
      '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z"/>',
    "i-upload": '<path d="M12 16V4M6 10l6-6 6 6"/><path d="M4 20h16"/>',
    "i-user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "i-users":
      '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 21a6.5 6.5 0 0 1 13 0"/><path d="M16 5.2a3.5 3.5 0 0 1 0 6.6"/><path d="M17.5 21a6.5 6.5 0 0 0-2.5-5.1"/>',
    "i-filter": '<path d="M3 5h18l-7 8.2V20l-4-2.2v-4.6z"/>',
    "i-dots":
      '<circle cx="5" cy="12" r="1.6" style="fill:currentColor;stroke:none"/><circle cx="12" cy="12" r="1.6" style="fill:currentColor;stroke:none"/><circle cx="19" cy="12" r="1.6" style="fill:currentColor;stroke:none"/>',
    "i-dots-v":
      '<circle cx="12" cy="5" r="1.6" style="fill:currentColor;stroke:none"/><circle cx="12" cy="12" r="1.6" style="fill:currentColor;stroke:none"/><circle cx="12" cy="19" r="1.6" style="fill:currentColor;stroke:none"/>',
    "i-clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
    "i-alert":
      '<path d="M12 3l9.5 16.5H2.5z"/><path d="M12 10v4"/><path d="M12 17.5h.01"/>',
    "i-info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
    "i-refresh": '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v4h-4"/>',
    "i-database":
      '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "i-bolt": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    "i-command":
      '<path d="M9 9h6v6H9z"/><path d="M9 9V6a3 3 0 1 0-3 3z"/><path d="M15 9h3a3 3 0 1 0-3-3z"/><path d="M15 15v3a3 3 0 1 0 3-3z"/><path d="M9 15H6a3 3 0 1 0 3 3z"/>',
    "i-sun":
      '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "i-moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    "i-grid":
      '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
    "i-eye":
      '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
    "i-copy":
      '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
    "i-thumb-up":
      '<path d="M7 11v9H3v-9z"/><path d="M7 11l4-8a2 2 0 0 1 2.4 2.5L12.5 9H18a2 2 0 0 1 2 2.3l-1.1 6A2 2 0 0 1 17 20H7"/>',
    "i-thumb-down":
      '<path d="M17 13V4h4v9z"/><path d="M17 13l-4 8a2 2 0 0 1-2.4-2.5l.9-3.5H6a2 2 0 0 1-2-2.3l1.1-6A2 2 0 0 1 7 4h10"/>',
    "i-cpu":
      '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
    "i-layers": '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
    "i-trash":
      '<path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/>',
    "i-edit":
      '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "i-book":
      '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M19 19H6"/>',
    "i-flag": '<path d="M5 21V4"/><path d="M5 4h11l-1.5 4L16 12H5"/>',
    "i-key":
      '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3"/><path d="M16 7l3 3"/><path d="M18.5 4.5l2 2"/>',
    "i-link":
      '<path d="M9.5 13.5l5-5"/><path d="M8 10l-2 2a3.5 3.5 0 0 0 5 5l2-2"/><path d="M16 14l2-2a3.5 3.5 0 0 0-5-5l-2 2"/>',
    "i-list":
      '<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
    "i-pin": '<path d="M12 17v5"/><path d="M9 3h6l-1 7 3 3H7l3-3z"/>',
    "i-history":
      '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3 8"/><path d="M3 3v5h5"/><path d="M12 8v4.5l3 1.8"/>',
    "i-check-circle":
      '<circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>',
    "i-slash-circle":
      '<circle cx="12" cy="12" r="9"/><path d="M6 6l12 12"/>',
    "i-mail":
      '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M4 7l8 6 8-6"/>',
    "i-tag":
      '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.4" style="fill:currentColor;stroke:none"/>',
    "i-zap-off": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  };

  function injectSprite() {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("aria-hidden", "true");
    svg.style.cssText = "position:absolute;width:0;height:0;overflow:hidden";
    var inner = "";
    for (var id in ICONS) {
      inner +=
        '<symbol id="' + id + '" viewBox="0 0 24 24">' + ICONS[id] + "</symbol>";
    }
    svg.innerHTML = inner;
    document.body.insertBefore(svg, document.body.firstChild);
  }
  function svg(id, cls) {
    return (
      '<svg class="icon ' + (cls || "") + '"><use href="#' + id + '"/></svg>'
    );
  }
  window.lumenIcon = svg;

  /* ---- navigation model ------------------------------------------------- */
  var NAV = [
    {
      label: "Workspace",
      items: [
        { id: "chat", icon: "i-chat", label: "Assistant", href: "chat.html" },
        { id: "search", icon: "i-search", label: "Search", href: "search.html" },
        {
          id: "documents",
          icon: "i-doc",
          label: "Documents",
          href: "documents.html",
          badge: "24",
        },
      ],
    },
    {
      label: "Administration",
      items: [
        { id: "sources", icon: "i-plug", label: "Sources", href: "sources.html" },
        {
          id: "audit",
          icon: "i-shield",
          label: "Audit log",
          href: "audit.html",
        },
        { id: "admin", icon: "i-sliders", label: "Admin", href: "admin.html" },
      ],
    },
  ];
  var FOOT = [
    { id: "foundations", icon: "i-grid", label: "Foundations", href: "reference/foundations.html" },
    { id: "components", icon: "i-layers", label: "Components", href: "reference/components.html" },
  ];

  // Each theme carries a light + dark mini-palette for the preview cards.
  var THEMES = [
    { id: "aurora", label: "Aurora",
      light: { bg: "#eef1f8", sf: "#ffffff", bd: "#e5e8f0", tx: "#14161f", ac: "#4f46e5", g1: "#6366f1", g2: "#a78bfa" },
      dark:  { bg: "#0c0e16", sf: "#161a27", bd: "#272d42", tx: "#e9ebf6", ac: "#818cf8", g1: "#818cf8", g2: "#c4b5fd" } },
    { id: "graphite", label: "Graphite",
      light: { bg: "#f3f4f6", sf: "#ffffff", bd: "#e3e5ea", tx: "#15171c", ac: "#0284c7", g1: "#0ea5e9", g2: "#22d3ee" },
      dark:  { bg: "#0a0c11", sf: "#12151d", bd: "#232a36", tx: "#e8ebf2", ac: "#38bdf8", g1: "#38bdf8", g2: "#67e8f9" } },
    { id: "meridian", label: "Meridian",
      light: { bg: "#f4f1e9", sf: "#fdfcf8", bd: "#e4ddcc", tx: "#1f1c14", ac: "#9c7a1e", g1: "#4e8a3f", g2: "#c79a2e" },
      dark:  { bg: "#14130c", sf: "#1c1a10", bd: "#322d1b", tx: "#efeada", ac: "#e2b04e", g1: "#5fa24a", g2: "#e2b04e" } },
    { id: "indigo", label: "Indigo",
      light: { bg: "#eeeefa", sf: "#ffffff", bd: "#e2e3f3", tx: "#16161f", ac: "#6d28d9", g1: "#7c3aed", g2: "#a78bfa" },
      dark:  { bg: "#0d0a1a", sf: "#171331", bd: "#2b2350", tx: "#ebe9f7", ac: "#a78bfa", g1: "#a78bfa", g2: "#c4b5fd" } },
    { id: "sunset", label: "Sunset",
      light: { bg: "#fbf0ea", sf: "#fffaf6", bd: "#f0ddce", tx: "#241712", ac: "#e0561f", g1: "#f97316", g2: "#ef4444" },
      dark:  { bg: "#170d0a", sf: "#211210", bd: "#3a201a", tx: "#f4e4dd", ac: "#fb7a45", g1: "#fb923c", g2: "#f87171" } },
    { id: "forest", label: "Forest",
      light: { bg: "#ecf2ec", sf: "#ffffff", bd: "#dce8dc", tx: "#141914", ac: "#15803d", g1: "#15803d", g2: "#4ade80" },
      dark:  { bg: "#08110b", sf: "#101a13", bd: "#213328", tx: "#e6f0e9", ac: "#4ade80", g1: "#4ade80", g2: "#86efac" } },
    { id: "slate", label: "Slate",
      light: { bg: "#f1f2f4", sf: "#ffffff", bd: "#e2e4e8", tx: "#16181d", ac: "#475569", g1: "#64748b", g2: "#94a3b8" },
      dark:  { bg: "#0b0d10", sf: "#14171c", bd: "#252a33", tx: "#e7eaef", ac: "#94a3b8", g1: "#94a3b8", g2: "#cbd5e1" } },
  ];
  var ACCENTS = ["#2563eb", "#7c3aed", "#14b8a6", "#22c55e", "#ef4444", "#f97316", "#ec4899"];
  var MODES = [
    { id: "light", label: "Light", icon: "i-sun" },
    { id: "dark", label: "Dark", icon: "i-moon" },
    { id: "system", label: "System", icon: "i-globe" },
  ];
  var DENS = [
    { id: "compact", label: "Compact", fs: 0.92, space: 0.8, radius: 0.9 },
    { id: "cozy", label: "Cozy", fs: 1.0, space: 1.0, radius: 1.0 },
    { id: "comfortable", label: "Comfortable", fs: 1.08, space: 1.28, radius: 1.1 },
  ];
  var NAVS = [
    { id: "full", label: "Full nav" },
    { id: "icons", label: "Icons only" },
    { id: "centered", label: "Centered" },
  ];
  var FONTS = [
    { id: "inter", label: "Inter", stack: '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif' },
    { id: "system", label: "System", stack: 'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif' },
    { id: "rounded", label: "Rounded", stack: '"Nunito", "SF Pro Rounded", ui-rounded, "Segoe UI", system-ui, sans-serif' },
  ];
  function findById(arr, id) {
    return arr.filter(function (x) { return x.id === id; })[0];
  }

  /* ---- appearance: theme × mode + accent + density + nav + font -------- */
  function getAppearance() {
    return {
      theme: localStorage.getItem("lumen-theme") || "aurora",
      mode: localStorage.getItem("lumen-mode") || "system",
      accent: localStorage.getItem("lumen-accent") || "", // "" = theme default
      density: localStorage.getItem("lumen-density") || "cozy",
      nav: localStorage.getItem("lumen-nav") || "full",
      font: localStorage.getItem("lumen-font") || "inter",
    };
  }
  function resolveMode(mode) {
    if (mode === "system") {
      return window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    }
    return mode;
  }
  var roundedLoaded = false;
  function ensureRoundedFont() {
    if (roundedLoaded) return;
    roundedLoaded = true;
    var l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = "https://fonts.googleapis.com/css2?family=Nunito:wght@400;500;600;700&display=swap";
    document.head.appendChild(l);
  }
  function applyAppearance(a) {
    a = a || getAppearance();
    var el = document.documentElement;
    el.setAttribute("data-theme", a.theme);
    el.setAttribute("data-mode", resolveMode(a.mode));
    el.setAttribute("data-nav", a.nav);
    if (a.accent) {
      el.style.setProperty("--accent", a.accent);
      el.style.setProperty("--accent-contrast", "#ffffff");
    } else {
      el.style.removeProperty("--accent");
      el.style.removeProperty("--accent-contrast");
    }
    var f = findById(FONTS, a.font) || FONTS[0];
    if (a.font === "rounded") ensureRoundedFont();
    el.style.setProperty("--font-sans", f.stack);
    var d = findById(DENS, a.density) || DENS[1];
    el.style.setProperty("--fs", d.fs);
    el.style.setProperty("--space", d.space);
    el.style.setProperty("--radius", d.radius);
    localStorage.setItem("lumen-theme", a.theme);
    localStorage.setItem("lumen-mode", a.mode);
    localStorage.setItem("lumen-accent", a.accent);
    localStorage.setItem("lumen-density", a.density);
    localStorage.setItem("lumen-nav", a.nav);
    localStorage.setItem("lumen-font", a.font);
    syncAppearanceUI(a);
  }
  function syncAppearanceUI(a) {
    a = a || getAppearance();
    var th = findById(THEMES, a.theme) || THEMES[0];
    document.querySelectorAll("[data-theme-name]").forEach(function (el) {
      el.textContent = th.label;
    });
    document.querySelectorAll("[data-mode-icon]").forEach(function (el) {
      el.innerHTML = svg((findById(MODES, a.mode) || MODES[0]).icon, "icon-sm");
    });
    function activate(attr, val) {
      document.querySelectorAll("[" + attr + "]").forEach(function (el) {
        el.classList.toggle("active", el.getAttribute(attr) === val);
      });
    }
    activate("data-theme-opt", a.theme);
    activate("data-theme-card", a.theme);
    activate("data-mode-opt", a.mode);
    activate("data-dens-opt", a.density);
    activate("data-nav-opt", a.nav);
    activate("data-font-opt", a.font);
    document.querySelectorAll("[data-accent-opt]").forEach(function (el) {
      el.classList.toggle("active", el.getAttribute("data-accent-opt") === a.accent);
    });
    // theme preview cards mirror the active mode
    var modeKey = resolveMode(a.mode);
    document.querySelectorAll("[data-theme-opt]").forEach(function (card) {
      var t = findById(THEMES, card.getAttribute("data-theme-opt"));
      if (!t) return;
      var p = t[modeKey];
      card.style.setProperty("--c-bg", p.bg);
      card.style.setProperty("--c-sf", p.sf);
      card.style.setProperty("--c-bd", p.bd);
      card.style.setProperty("--c-tx", p.tx);
      card.style.setProperty("--c-ac", p.ac);
      card.style.setProperty("--c-ac2", p.g2);
      var bar = card.querySelector(".aps-prev-bar");
      if (bar) bar.style.background = "linear-gradient(90deg," + p.g1 + "," + p.g2 + ")";
    });
  }
  function setTheme(t) { var a = getAppearance(); a.theme = t; applyAppearance(a); }
  function setMode(m) { var a = getAppearance(); a.mode = m; applyAppearance(a); }
  function setAccent(c) { var a = getAppearance(); a.accent = c || ""; applyAppearance(a); }
  function setDensity(d) { var a = getAppearance(); a.density = d; applyAppearance(a); }
  function setNav(n) { var a = getAppearance(); a.nav = n; applyAppearance(a); }
  function setFont(f) { var a = getAppearance(); a.font = f; applyAppearance(a); }
  function resetAppearance() {
    localStorage.removeItem("lumen-accent");
    applyAppearance();
  }
  if (window.matchMedia) {
    try {
      window
        .matchMedia("(prefers-color-scheme: dark)")
        .addEventListener("change", function () {
          if (getAppearance().mode === "system") applyAppearance();
        });
    } catch (e) {}
  }
  window.Lumen = {
    setTheme: setTheme,
    setMode: setMode,
    setAccent: setAccent,
    setDensity: setDensity,
    setNav: setNav,
    setFont: setFont,
    reset: resetAppearance,
    apply: applyAppearance,
    get: getAppearance,
    toast: showToast,
    openPanel: function () { openOverlay("appearance-panel"); },
    openCmdk: function () { toggleCmdk(true); },
  };

  /* ---- build shared chrome --------------------------------------------- */
  function base() {
    return document.body.getAttribute("data-base") || "";
  }
  function href(h) {
    var b = base();
    return (b ? b + "/" : "") + h;
  }
  function buildChrome() {
    var app = document.querySelector(".app");
    if (!app) return; // launcher / reference pages without shell
    var screen = document.body.getAttribute("data-screen") || "";

    // brand cell
    var brand = document.createElement("div");
    brand.className = "brandcell";
    brand.innerHTML =
      '<div class="brand-logo">' +
      svg("i-sparkle") +
      "</div>" +
      '<div><div class="brand-name">Lumen</div><div class="brand-sub">Copilot</div></div>';

    // topbar
    var topbar = document.createElement("header");
    topbar.className = "topbar";
    topbar.innerHTML =
      '<button class="btn-icon btn-ghost" data-rail-toggle aria-label="Menu">' +
      svg("i-list") +
      "</button>" +
      '<div class="topbar-brand"><div class="brand-logo">' +
      svg("i-sparkle") +
      '</div><div class="brand-name">Lumen</div></div>' +
      '<div class="omni" data-open-cmdk role="search">' +
      svg("i-search", "icon-sm") +
      "<span>Search or ask across your workspace…</span>" +
      '<span class="kbd">Ctrl K</span></div>' +
      '<div class="topbar-spacer"></div>' +
      '<div class="topbar-actions">' +
      '<button class="btn btn-sm btn-subtle" data-drawer-open="appearance-panel" aria-label="Appearance"><span class="iflex" data-mode-icon>' +
      svg("i-sun", "icon-sm") +
      '</span><span data-theme-name>Aurora</span>' +
      svg("i-chevron-down", "icon-sm") +
      "</button>" +
      '<div class="tenant tip" data-tip="Active tenant — hard isolation"><span class="dot"></span>Northwind Inc.</div>' +
      '<button class="btn-icon btn-ghost tip" data-tip="Notifications">' +
      svg("i-info") +
      "</button>" +
      '<div class="avatar" data-open-user>AM</div>' +
      "</div>";

    // rail
    var rail = document.createElement("nav");
    rail.className = "rail";
    var html = "";
    NAV.forEach(function (group) {
      html += '<div class="rail-group-label">' + group.label + "</div>";
      group.items.forEach(function (it) {
        html +=
          '<a class="navlink' +
          (it.id === screen ? " active" : "") +
          '" href="' +
          href(it.href) +
          '">' +
          svg(it.icon) +
          "<span>" +
          it.label +
          "</span>" +
          (it.badge ? '<span class="badge-count">' + it.badge + "</span>" : "") +
          "</a>";
      });
    });
    html += '<div class="rail-spacer"></div><div class="rail-foot">';
    FOOT.forEach(function (it) {
      html +=
        '<a class="navlink' +
        (it.id === screen ? " active" : "") +
        '" href="' +
        href(it.href) +
        '">' +
        svg(it.icon) +
        "<span>" +
        it.label +
        "</span></a>";
    });
    html +=
      '<a class="navlink" href="' +
      href("index.html") +
      '">' +
      svg("i-grid") +
      "<span>Look gallery</span></a></div>";
    rail.innerHTML = html;

    // centered top-nav (visible only when nav layout = "centered")
    var topnav = document.createElement("nav");
    topnav.className = "topnav";
    var tn = "";
    NAV.forEach(function (group) {
      group.items.forEach(function (it) {
        tn +=
          '<a class="' + (it.id === screen ? "active" : "") + '" href="' +
          href(it.href) + '">' + svg(it.icon) + "<span>" + it.label + "</span></a>";
      });
    });
    topnav.innerHTML = tn;
    topbar.appendChild(topnav);

    app.insertBefore(brand, app.firstChild);
    app.insertBefore(topbar, brand.nextSibling);
    app.insertBefore(rail, topbar.nextSibling);

    // mobile rail toggle
    topbar.querySelector("[data-rail-toggle]").addEventListener("click", function () {
      rail.classList.toggle("hidden");
    });
  }

  /* ---- popover menus (theme, user) ------------------------------------- */
  function buildPopovers() {
    var host = document.createElement("div");
    host.id = "popovers";
    document.body.appendChild(host);

    function closeAll() {
      host.querySelectorAll(".menu").forEach(function (m) {
        m.remove();
      });
    }
    document.addEventListener("click", function (e) {
      if (e.target.closest("[data-open-user]")) {
        e.stopPropagation();
        if (host.querySelector("#m-user")) return closeAll();
        closeAll();
        openUserMenu(e.target.closest("[data-open-user]"), host);
        return;
      }
      if (!e.target.closest(".menu")) closeAll();
    });
  }
  function place(menu, anchor) {
    var r = anchor.getBoundingClientRect();
    menu.style.top = r.bottom + 8 + "px";
    menu.style.right = window.innerWidth - r.right + "px";
  }
  function buildAppearancePanel() {
    if (document.getElementById("appearance-panel")) return;
    var panel = document.createElement("aside");
    panel.className = "drawer drawer-wide";
    panel.id = "appearance-panel";

    var h =
      '<div class="aps-head">' +
      '<div class="aps-head-ico">' + svg("i-sliders") + "</div>" +
      '<div class="grow"><div class="t-semibold" style="font-size:calc(16px*var(--fs))">Appearance &amp; preferences</div>' +
      '<div class="t-sm t-muted mt-1">Make Lumen yours — applies instantly, saved on this device.</div></div>' +
      '<button class="btn-icon btn-ghost" data-drawer-close aria-label="Close">' + svg("i-x") + "</button>" +
      "</div>";

    h += '<div class="drawer-body">';

    // THEME
    h += '<div class="aps-sec"><span class="lbl">Theme</span><div class="aps-themes">';
    THEMES.forEach(function (t) {
      h +=
        '<button class="aps-theme" data-theme-opt="' + t.id + '">' +
        '<div class="aps-prev"><div class="aps-prev-bar"></div><div class="aps-prev-bar2"></div>' +
        '<div class="aps-prev-dot"></div>' +
        '<svg class="aps-prev-doc" viewBox="0 0 24 24"><path d="M14 3v5h5"/>' +
        '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/></svg></div>' +
        '<div class="aps-theme-name">' + t.label + "</div></button>";
    });
    h += "</div></div>";

    // MODE
    h += '<div class="aps-sec"><span class="lbl">Mode</span><div class="segmented aps-seg">';
    MODES.forEach(function (md) {
      h += '<button data-mode-opt="' + md.id + '">' + svg(md.icon, "icon-sm") + md.label + "</button>";
    });
    h += "</div></div>";

    // ACCENT
    h += '<div class="aps-sec"><span class="lbl">Accent</span><div class="aps-accents">';
    ACCENTS.forEach(function (c) {
      h += '<button class="aps-accent" data-accent-opt="' + c + '" style="background:' + c + '" aria-label="Accent ' + c + '"></button>';
    });
    h += '<span class="aps-accent-current" data-accent-current title="Current accent"></span>';
    h += '<button class="aps-link" data-accent-reset>Reset</button></div></div>';

    // DENSITY
    h += '<div class="aps-sec"><span class="lbl">Density &amp; spacing</span><div class="segmented aps-seg">';
    DENS.forEach(function (d) {
      h += '<button data-dens-opt="' + d.id + '">' + d.label + "</button>";
    });
    h += "</div></div>";

    // NAVIGATION & LAYOUT
    h += '<div class="aps-sec"><span class="lbl">Navigation &amp; layout</span><div class="segmented aps-seg">';
    NAVS.forEach(function (n) {
      h += '<button data-nav-opt="' + n.id + '">' + n.label + "</button>";
    });
    h += "</div></div>";

    // INTERFACE FONT
    h += '<div class="aps-sec"><span class="lbl">Interface font</span><div class="segmented aps-seg">';
    FONTS.forEach(function (f) {
      h += '<button data-font-opt="' + f.id + '" style="font-family:' + f.stack.replace(/"/g, "'") + '">' + f.label + "</button>";
    });
    h += "</div></div>";

    // LIVE PREVIEW
    h +=
      '<div class="aps-sec"><span class="lbl">Live preview</span><div class="aps-live">' +
      '<div class="flex items-center justify-between gap-3">' +
      '<div><div class="t-sm t-muted">Returns in review</div>' +
      '<div style="font-size:calc(30px*var(--fs));font-weight:700;font-family:var(--font-display);letter-spacing:-.02em;margin-top:2px">18</div></div>' +
      '<svg class="aps-donut" viewBox="0 0 36 36"><circle cx="18" cy="18" r="15.5" style="stroke:var(--surface-3)"/>' +
      '<circle cx="18" cy="18" r="15.5" style="stroke:var(--accent);stroke-linecap:round;stroke-dasharray:97.4;stroke-dashoffset:26.3"/></svg>' +
      "</div>" +
      '<div class="flex wrap gap-2 mt-3">' +
      '<span class="badge badge-ok"><span class="dot"></span>T0</span>' +
      '<span class="badge badge-info"><span class="dot"></span>T1</span>' +
      '<span class="badge badge-warn"><span class="dot"></span>T2</span>' +
      '<span class="badge badge-warn"><span class="dot"></span>T3</span>' +
      '<span class="badge badge-ok">96%</span></div>' +
      '<div class="flex gap-2 mt-3">' +
      '<button class="btn btn-primary btn-sm">Primary</button>' +
      '<button class="btn btn-sm">Default</button>' +
      '<button class="btn btn-sm btn-ghost">' + svg("i-doc", "icon-sm") + "evidence</button>" +
      "</div></div></div>";

    h += "</div>"; // drawer-body
    panel.innerHTML = h;
    document.body.appendChild(panel);

    panel.querySelectorAll("[data-theme-opt]").forEach(function (el) {
      el.addEventListener("click", function () { setTheme(el.getAttribute("data-theme-opt")); });
    });
    panel.querySelectorAll("[data-mode-opt]").forEach(function (el) {
      el.addEventListener("click", function () { setMode(el.getAttribute("data-mode-opt")); });
    });
    panel.querySelectorAll("[data-accent-opt]").forEach(function (el) {
      el.addEventListener("click", function () { setAccent(el.getAttribute("data-accent-opt")); });
    });
    panel.querySelector("[data-accent-reset]").addEventListener("click", function () { resetAppearance(); });
    panel.querySelectorAll("[data-dens-opt]").forEach(function (el) {
      el.addEventListener("click", function () { setDensity(el.getAttribute("data-dens-opt")); });
    });
    panel.querySelectorAll("[data-nav-opt]").forEach(function (el) {
      el.addEventListener("click", function () { setNav(el.getAttribute("data-nav-opt")); });
    });
    panel.querySelectorAll("[data-font-opt]").forEach(function (el) {
      el.addEventListener("click", function () { setFont(el.getAttribute("data-font-opt")); });
    });
  }
  function openUserMenu(anchor, host) {
    var m = document.createElement("div");
    m.className = "menu";
    m.id = "m-user";
    m.innerHTML =
      '<div style="padding:8px 10px"><div class="t-medium">Avery Madison</div><div class="t-xs t-subtle">avery@northwind.com · Knowledge Worker</div></div>' +
      '<div class="menu-sep"></div>' +
      '<div class="menu-item">' + svg("i-user") + "Profile & memory</div>" +
      '<div class="menu-item">' + svg("i-sliders") + "Preferences</div>" +
      '<div class="menu-item">' + svg("i-shield") + "Privacy controls</div>" +
      '<div class="menu-sep"></div>' +
      '<a class="menu-item" href="' + href("login.html") + '">' + svg("i-lock") + "Sign out</a>";
    host.appendChild(m);
    place(m, anchor);
  }

  /* ---- command palette -------------------------------------------------- */
  var cmdk;
  function buildCmdk() {
    cmdk = document.createElement("div");
    cmdk.className = "cmdk";
    var items = [];
    NAV.forEach(function (g) {
      g.items.forEach(function (it) {
        items.push({
          icon: it.icon,
          label: "Go to " + it.label,
          hint: "Navigate",
          href: href(it.href),
        });
      });
    });
    items.push({ icon: "i-plus", label: "New chat", hint: "Action", href: href("chat.html") });
    items.push({ icon: "i-upload", label: "Upload documents", hint: "Action", href: href("documents.html") });
    items.push({ icon: "i-grid", label: "Open look gallery", hint: "Design", href: href("index.html") });
    THEMES.forEach(function (t) {
      items.push({ icon: "i-grid", label: "Theme: " + t.label, hint: "Theme", theme: t.id });
    });
    MODES.forEach(function (md) {
      items.push({ icon: md.icon, label: "Mode: " + md.label, hint: "Mode", mode: md.id });
    });
    DENS.forEach(function (d) {
      items.push({ icon: "i-sliders", label: "Density: " + d.label, hint: "Density", density: d.id });
    });

    var list =
      '<div class="cmdk-sec">Jump to</div>' +
      items
        .map(function (it, i) {
          return (
            '<div class="cmdk-item' +
            (i === 0 ? " active" : "") +
            '" data-i="' +
            i +
            '">' +
            svg(it.icon) +
            "<span>" +
            it.label +
            '</span><span class="hint">' +
            it.hint +
            "</span></div>"
          );
        })
        .join("");
    cmdk.innerHTML =
      '<div class="cmdk-card">' +
      '<div class="cmdk-input">' +
      svg("i-search") +
      '<input placeholder="Type a command or search…" />' +
      '<span class="kbd">Esc</span></div>' +
      '<div class="cmdk-list">' +
      list +
      "</div></div>";
    document.body.appendChild(cmdk);

    var input = cmdk.querySelector("input");
    var listEl = cmdk.querySelector(".cmdk-list");
    function run(it) {
      if (it.theme) {
        setTheme(it.theme);
        toggleCmdk(false);
        showToast("Theme: " + it.label.split(": ")[1]);
      } else if (it.mode) {
        setMode(it.mode);
        toggleCmdk(false);
        showToast("Mode: " + it.label.split(": ")[1]);
      } else if (it.density) {
        setDensity(it.density);
        toggleCmdk(false);
        showToast("Density: " + it.label.split(": ")[1]);
      } else if (it.href) {
        location.href = it.href;
      }
    }
    listEl.addEventListener("click", function (e) {
      var el = e.target.closest(".cmdk-item");
      if (el) run(items[+el.getAttribute("data-i")]);
    });
    input.addEventListener("input", function () {
      var q = input.value.toLowerCase();
      var first = null;
      cmdk.querySelectorAll(".cmdk-item").forEach(function (el) {
        var it = items[+el.getAttribute("data-i")];
        var show = it.label.toLowerCase().indexOf(q) > -1;
        el.classList.toggle("hidden", !show);
        el.classList.remove("active");
        if (show && !first) first = el;
      });
      if (first) first.classList.add("active");
    });
    cmdk.addEventListener("keydown", function (e) {
      var vis = [].slice
        .call(cmdk.querySelectorAll(".cmdk-item"))
        .filter(function (x) {
          return !x.classList.contains("hidden");
        });
      var idx = vis.findIndex(function (x) {
        return x.classList.contains("active");
      });
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (idx > -1) vis[idx].classList.remove("active");
        (vis[(idx + 1) % vis.length] || vis[0]).classList.add("active");
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (idx > -1) vis[idx].classList.remove("active");
        (vis[(idx - 1 + vis.length) % vis.length] || vis[0]).classList.add(
          "active"
        );
      } else if (e.key === "Enter") {
        var a = cmdk.querySelector(".cmdk-item.active:not(.hidden)");
        if (a) run(items[+a.getAttribute("data-i")]);
      }
    });
    cmdk.addEventListener("click", function (e) {
      if (e.target === cmdk) toggleCmdk(false);
    });
  }
  function toggleCmdk(on) {
    if (!cmdk) return;
    cmdk.classList.toggle("open", on);
    if (on) {
      var inp = cmdk.querySelector("input");
      inp.value = "";
      inp.dispatchEvent(new Event("input"));
      setTimeout(function () {
        inp.focus();
      }, 30);
    }
  }

  /* ---- toast ----------------------------------------------------------- */
  function showToast(msg, kind) {
    var host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.className = "toast-host";
      document.body.appendChild(host);
    }
    var t = document.createElement("div");
    t.className = "toast";
    t.innerHTML =
      svg(kind === "info" ? "i-info" : "i-check-circle") +
      "<span>" +
      msg +
      "</span>";
    if (kind === "info") t.querySelector(".icon").style.color = "var(--info)";
    host.appendChild(t);
    setTimeout(function () {
      t.style.transition = "opacity .3s, transform .3s";
      t.style.opacity = "0";
      t.style.transform = "translateY(8px)";
      setTimeout(function () {
        t.remove();
      }, 320);
    }, 2600);
  }

  /* ---- generic delegated behaviors ------------------------------------- */
  function wireBehaviors() {
    document.addEventListener("click", function (e) {
      var el;

      // open cmdk
      if (e.target.closest("[data-open-cmdk]")) return toggleCmdk(true);

      // tabs:  [data-tab="grp:id"] -> shows [data-panel="grp:id"]
      if ((el = e.target.closest("[data-tab]"))) {
        var key = el.getAttribute("data-tab");
        var grp = key.split(":")[0];
        document
          .querySelectorAll('[data-tab^="' + grp + ':"]')
          .forEach(function (b) {
            b.classList.toggle("active", b === el);
          });
        document
          .querySelectorAll('[data-panel^="' + grp + ':"]')
          .forEach(function (p) {
            p.classList.toggle(
              "hidden",
              p.getAttribute("data-panel") !== key
            );
          });
        return;
      }

      // segmented / single-select toggle group
      if ((el = e.target.closest("[data-seg]"))) {
        var g = el.getAttribute("data-seg").split(":")[0];
        document.querySelectorAll('[data-seg^="' + g + ':"]').forEach(function (b) {
          b.classList.toggle("active", b === el);
        });
        return;
      }

      // multi-select chip / checkbox / switch
      if ((el = e.target.closest("[data-toggle]"))) {
        el.classList.toggle("active");
        el.classList.toggle("on");
        var cb = el.querySelector(".checkbox");
        if (cb) cb.classList.toggle("on");
        return;
      }
      if ((el = e.target.closest(".filter-row"))) {
        var box = el.querySelector(".checkbox");
        if (box) box.classList.toggle("on");
        return;
      }
      if ((el = e.target.closest(".switch"))) {
        el.classList.toggle("on");
        return;
      }

      // drawers
      if ((el = e.target.closest("[data-drawer-open]"))) {
        openOverlay(el.getAttribute("data-drawer-open"));
        return;
      }
      if (e.target.closest("[data-drawer-close],[data-overlay-close]")) {
        closeOverlays();
        return;
      }
      // modals
      if ((el = e.target.closest("[data-modal-open]"))) {
        openOverlay(el.getAttribute("data-modal-open"));
        return;
      }
      // backdrop
      if (e.target.classList && e.target.classList.contains("backdrop")) {
        closeOverlays();
        return;
      }

      // toast trigger
      if ((el = e.target.closest("[data-toast]"))) {
        showToast(el.getAttribute("data-toast"));
        return;
      }

      // citation / source linking
      if ((el = e.target.closest("[data-cite]"))) {
        linkCite(el.getAttribute("data-cite"), el);
        return;
      }
      if ((el = e.target.closest("[data-source-open]"))) {
        linkCite(el.getAttribute("data-source-open"), el);
        return;
      }

      // accordion (trace, faq)
      if ((el = e.target.closest("[data-acc]"))) {
        var body = document.getElementById(el.getAttribute("data-acc"));
        if (body) body.classList.toggle("hidden");
        var chev = el.querySelector(".chev");
        if (chev)
          chev.style.transform = body.classList.contains("hidden")
            ? ""
            : "rotate(90deg)";
        return;
      }
    });

    // ESC closes overlays + cmdk
    document.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        toggleCmdk(!cmdk.classList.contains("open"));
      }
      if (e.key === "Escape") {
        toggleCmdk(false);
        closeOverlays();
      }
    });

    wireComposer();
    wireSearch();
    wireUpload();
  }

  function ensureBackdrop() {
    var b = document.querySelector(".backdrop");
    if (!b) {
      b = document.createElement("div");
      b.className = "backdrop";
      document.body.appendChild(b);
    }
    return b;
  }
  function openOverlay(id) {
    var el = document.getElementById(id);
    if (!el) return;
    ensureBackdrop().classList.add("open");
    el.classList.add("open");
  }
  function closeOverlays() {
    document.querySelectorAll(".drawer.open,.modal.open").forEach(function (e) {
      e.classList.remove("open");
    });
    var b = document.querySelector(".backdrop");
    if (b) b.classList.remove("open");
  }

  function linkCite(n, src) {
    document.querySelectorAll("[data-cite]").forEach(function (c) {
      c.classList.toggle(
        "active",
        c.getAttribute("data-cite") === n && c === src
      );
    });
    var target = document.querySelector('[data-source="' + n + '"]');
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.style.transition = "background .2s";
      var prev = target.style.background;
      target.style.background = "var(--accent-weak)";
      setTimeout(function () {
        target.style.background = prev;
      }, 1100);
    }
    // open inspector if present
    var insp = document.getElementById("inspector");
    if (insp) {
      insp.classList.remove("hidden");
      var slot = insp.querySelector("[data-cite-slot]");
      if (slot)
        slot.querySelectorAll("[data-cite-detail]").forEach(function (d) {
          d.classList.toggle(
            "hidden",
            d.getAttribute("data-cite-detail") !== n
          );
        });
    }
    // open drawer inspector if present (audit-style)
    if (document.getElementById("cite-drawer")) openOverlay("cite-drawer");
  }

  /* ---- chat composer (scripted demo answer) ---------------------------- */
  function wireComposer() {
    var form = document.querySelector("[data-chat-form]");
    if (!form) return;
    var input = form.querySelector("textarea");
    var thread = document.querySelector("[data-thread]");
    input.addEventListener("input", function () {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 180) + "px";
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });
    form.querySelector("[data-send]").addEventListener("click", send);
    function send() {
      var q = input.value.trim();
      if (!q || !thread) return;
      var u = document.createElement("div");
      u.className = "turn bubble-user";
      u.innerHTML =
        '<div class="who"><div class="avatar sm">AM</div></div>' +
        '<div class="turn-body"><div class="name">You</div><div>' +
        escapeHtml(q) +
        "</div></div>";
      thread.appendChild(u);
      input.value = "";
      input.style.height = "auto";
      thread.parentElement.scrollTop = thread.parentElement.scrollHeight;

      var a = document.createElement("div");
      a.className = "turn";
      a.innerHTML =
        '<div class="who"><div class="brand-logo" style="width:30px;height:30px">' +
        svg("i-sparkle") +
        "</div></div>" +
        '<div class="turn-body"><div class="name">Lumen ' +
        '<span class="badge badge-accent">' +
        svg("i-cpu") +
        "GPT-4o</span></div>" +
        '<div class="trace" style="margin-bottom:12px"><div class="trace-step">' +
        svg("i-refresh", "icon-sm") +
        '<span>Searching permitted sources…</span></div></div>' +
        '<div class="assistant-md" data-typing>Grounding your question across your permitted workspace…</div></div>';
      thread.appendChild(a);
      thread.parentElement.scrollTop = thread.parentElement.scrollHeight;
      setTimeout(function () {
        var md = a.querySelector("[data-typing]");
        md.innerHTML =
          "<p>Based on your permitted sources, here's a grounded summary " +
          'with citations you can verify.<span class="cite" data-cite="1">1</span></p>' +
          "<ul><li>Decision and rationale are recorded in the planning doc" +
          '<span class="cite" data-cite="2">2</span></li>' +
          "<li>Two sources agree; one older note conflicts and is flagged below" +
          '<span class="cite" data-cite="3">3</span></li></ul>' +
          '<div class="answer-meta">' +
          svg("i-shield-check", "icon-sm") +
          "<span>Permission-checked</span>" +
          svg("i-clock", "icon-sm") +
          "<span>3 sources · freshest 2d ago</span>" +
          '<div class="answer-actions">' +
          '<button class="act-btn tip" data-tip="Helpful">' + svg("i-thumb-up", "icon-sm") + "</button>" +
          '<button class="act-btn tip" data-tip="Not helpful">' + svg("i-thumb-down", "icon-sm") + "</button>" +
          '<button class="act-btn tip" data-tip="Copy" data-toast="Answer copied">' + svg("i-copy", "icon-sm") + "</button>" +
          "</div></div>";
        thread.parentElement.scrollTop = thread.parentElement.scrollHeight;
      }, 850);
    }
  }

  /* ---- search live filter ---------------------------------------------- */
  function wireSearch() {
    var input = document.querySelector("[data-search-input]");
    if (!input) return;
    input.addEventListener("input", function () {
      var q = input.value.toLowerCase();
      var n = 0;
      document.querySelectorAll("[data-result]").forEach(function (r) {
        var hit = r.getAttribute("data-text").toLowerCase().indexOf(q) > -1;
        r.classList.toggle("hidden", !hit);
        if (hit) n++;
      });
      var c = document.querySelector("[data-result-count]");
      if (c) c.textContent = n;
    });
  }

  /* ---- upload simulation ----------------------------------------------- */
  function wireUpload() {
    var zone = document.querySelector("[data-upload]");
    if (!zone) return;
    zone.addEventListener("click", function () {
      var list = document.querySelector("[data-upload-list]");
      if (!list) return;
      var names = [
        ["FY25-Budget-Plan.xlsx", "src-sheet", "XLS"],
        ["Vendor-MSA-Final.pdf", "src-pdf", "PDF"],
        ["Onboarding-Runbook.docx", "src-doc", "DOC"],
      ];
      var pick = names[Math.floor(list.children.length) % names.length];
      var row = document.createElement("div");
      row.className = "flex items-center gap-3 p-3 inset";
      row.style.marginTop = "10px";
      row.innerHTML =
        '<div class="src src-sm ' +
        pick[1] +
        '">' +
        pick[2] +
        "</div>" +
        '<div class="grow"><div class="t-medium t-sm">' +
        pick[0] +
        '</div><div class="bar" style="margin-top:6px"><span style="width:6%"></span></div></div>' +
        '<span class="t-xs t-subtle" data-pct>6%</span>';
      list.appendChild(row);
      var bar = row.querySelector(".bar > span");
      var pct = row.querySelector("[data-pct]");
      var v = 6;
      var iv = setInterval(function () {
        v += Math.random() * 22;
        if (v >= 100) {
          v = 100;
          clearInterval(iv);
          pct.outerHTML = '<span class="badge badge-ok">' + lumenIcon("i-check", "icon-sm") + "Indexed</span>";
          showToast(pick[0] + " ingested & indexed");
        }
        bar.style.width = v + "%";
        if (pct.isConnected) pct.textContent = Math.round(v) + "%";
      }, 380);
    });
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* ---- boot ------------------------------------------------------------ */
  function boot() {
    injectSprite();
    // Set attributes/scales early to avoid a flash of unthemed content…
    var ap = getAppearance();
    var el = document.documentElement;
    el.setAttribute("data-theme", ap.theme);
    el.setAttribute("data-mode", resolveMode(ap.mode));
    el.setAttribute("data-nav", ap.nav);
    var d0 = findById(DENS, ap.density) || DENS[1];
    el.style.setProperty("--fs", d0.fs);
    el.style.setProperty("--space", d0.space);
    el.style.setProperty("--radius", d0.radius);
    buildChrome();
    buildPopovers();
    buildCmdk();
    buildAppearancePanel();
    // …then run the full apply so freshly-injected controls/labels sync.
    applyAppearance(ap);
    wireBehaviors();
    // resolve any pre-rendered icons declared as <i data-icon="...">
    document.querySelectorAll("[data-icon]").forEach(function (el) {
      el.outerHTML = svg(el.getAttribute("data-icon"), el.getAttribute("data-icon-cls") || "");
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
