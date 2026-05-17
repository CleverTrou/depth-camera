/* ──────────────────────────────────────────────────────────────────────────
   xr-bridge.js — shared WebXR runtime for Depth Camera.

   Responsibilities:
     • Detect immersive-ar / immersive-vr support.
     • Render a persistent slide-down bar when XR is available.
     • Enter / exit a WebXR session with DOM Overlay so the existing page
       floats over passthrough.
     • Lock the Glass aesthetic during the session and restore on exit.
     • Bump hit-target sizing (≥60 px) and font scale via data-xr-active.
     • Drive a floating Exit pill that stays in the user's view.
     • Translate Apple transient-pointer + paired-mouse + controller events
       into a hover-glow class on focusable DOM elements.
     • Expose a small Tweaks panel for: headset preview, panel transparency,
       force pinch reticle, and force the bar visible.
     • Provide hooks for pages that ALSO want to render Three.js content
       inside the same XR session (used by viewer.html for the point cloud).

   Public API — window.DepthXR:
     init({ onEnter, onExit, want3D, threeBuilder })
     enterImmersive()
     exitImmersive()
     isInSession   getter
     isHeadset     getter — UA detected OR preview forced
   ────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  /* ── Storage keys ─────────────────────────────────────────────────────── */
  var K = {
    ui:        'depth-ui-mode',
    prevUi:    'depth-xr-prev-aesthetic',
    barDismiss:'depth-xr-bar-dismissed',
    preview:   'depth-xr-preview',
    panelOp:   'depth-xr-panel-opacity', // 50..100
    reticle:   'depth-xr-force-reticle',
    tweaks:    'depth-xr-tweaks-open',
  };
  function ls(k, fb) { try { var v = localStorage.getItem(k); return v == null ? fb : v; } catch(_) { return fb; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch(_) {} }
  function lsDel(k)    { try { localStorage.removeItem(k); } catch(_) {} }

  /* ── UA / query flags ─────────────────────────────────────────────────── */
  var ua          = navigator.userAgent || '';
  var headsetUA   = /OculusBrowser|Quest|Pico|VisionOS|Wolvic/i.test(ua);
  var qsHeadset   = /[?&]headset=1\b/.test(location.search);
  var qsPreview   = /[?&]xrpreview=1\b/.test(location.search);
  var previewOn   = qsPreview || ls(K.preview, '') === '1';
  if (qsPreview) lsSet(K.preview, '1');

  /* ── State ────────────────────────────────────────────────────────────── */
  var state = {
    barEl:    null,
    pillEl:   null,
    reticleEl:null,
    overlayRoot: null,
    session:  null,
    sessionType: null,   // 'immersive-ar' | 'immersive-vr'
    onEnter:  null,
    onExit:   null,
    supportAR: false,
    supportVR: false,
    panelOpacity: parseInt(ls(K.panelOp, '70'), 10),
    forceReticle: ls(K.reticle, '') === '1',
  };

  /* ── Headset visual flag (passthrough preview without a real session) ── */
  function applyHeadsetFlag() {
    if (headsetUA || qsHeadset || previewOn || state.session) {
      document.body.dataset.headset = '1';
    } else {
      delete document.body.dataset.headset;
    }
    /* Preview vs real session distinction — only set xrPreview when we're
       simulating, not when running over real passthrough (the headset
       supplies its own camera feed in that case and we MUST NOT cover it). */
    if (previewOn && !state.session) {
      document.body.dataset.xrPreview = '1';
    } else {
      delete document.body.dataset.xrPreview;
    }
  }
  applyHeadsetFlag();

  /* ── Inject the XR-mode CSS layer once ────────────────────────────────── */
  function injectCSS() {
    if (document.getElementById('xr-bridge-css')) return;
    var css = (
      /* Slide-down bar */
      '.xrbar{position:fixed;top:0;left:0;right:0;z-index:99990;display:flex;align-items:center;gap:14px;padding:10px 18px;' +
        'font-family:"JetBrains Mono",ui-monospace,monospace;font-size:12px;letter-spacing:0.06em;color:#a8e0ff;' +
        'background:rgba(6,18,36,0.78);-webkit-backdrop-filter:blur(18px) saturate(140%);backdrop-filter:blur(18px) saturate(140%);' +
        'border-bottom:1px solid rgba(140,215,255,0.32);box-shadow:0 12px 30px -16px rgba(0,40,80,0.7),inset 0 -1px 0 rgba(140,215,255,0.12);' +
        'transform:translateY(-110%);transition:transform 320ms cubic-bezier(.2,.7,.2,1);} ' +
      '.xrbar.is-open{transform:translateY(0)}' +
      '.xrbar__live{display:inline-block;width:8px;height:8px;border-radius:50%;background:#86d8ff;box-shadow:0 0 12px #86d8ff;animation:xrpulse 2s ease-in-out infinite}' +
      '@keyframes xrpulse{0%,100%{opacity:.5;transform:scale(.85)}50%{opacity:1;transform:scale(1.05)}}' +
      '.xrbar__title{flex:1;text-transform:uppercase;letter-spacing:0.14em;display:flex;align-items:center;gap:10px;min-width:0}' +
      '.xrbar__title b{color:#fff;text-shadow:0 0 12px rgba(140,215,255,0.55);font-weight:500}' +
      '.xrbar__tag{padding:3px 8px;border:1px solid rgba(140,215,255,0.35);color:#86d8ff;font-size:10px;letter-spacing:0.18em}' +
      '.xrbar__actions{display:flex;align-items:center;gap:8px;flex-shrink:0}' +
      '.xrbar__btn{font:inherit;font-size:12px;letter-spacing:0.1em;text-transform:uppercase;padding:8px 14px;border-radius:2px;' +
        'background:linear-gradient(180deg,oklch(0.92 0.16 198),oklch(0.74 0.18 215));border:1px solid rgba(190,235,255,0.6);' +
        'color:#021018;font-weight:600;cursor:pointer;box-shadow:inset 0 1px 0 rgba(255,255,255,0.5),0 0 22px -4px rgba(140,215,255,0.7);transition:filter 120ms}' +
      '.xrbar__btn:hover{filter:brightness(1.1)}' +
      '.xrbar__btn--ghost{background:rgba(140,215,255,0.06);color:#86d8ff;box-shadow:none;border-color:rgba(140,215,255,0.32);font-weight:400}' +
      '.xrbar__btn--ghost:hover{background:rgba(140,215,255,0.14)}' +
      '.xrbar__caret{display:inline-grid;place-items:center;width:28px;height:28px;cursor:pointer;border:1px solid rgba(140,215,255,0.32);' +
        'border-radius:2px;color:#86d8ff;background:transparent;font-size:11px;transition:transform 220ms,background 120ms}' +
      '.xrbar__caret:hover{background:rgba(140,215,255,0.1)}' +
      '.xrbar.is-expanded .xrbar__caret{transform:rotate(180deg)}' +

      /* Bar expansion: tweaks tray */
      '.xrbar__tray{position:fixed;top:54px;right:14px;z-index:99989;width:300px;padding:14px;' +
        'background:rgba(6,18,36,0.85);-webkit-backdrop-filter:blur(20px) saturate(140%);backdrop-filter:blur(20px) saturate(140%);' +
        'border:1px solid rgba(140,215,255,0.28);border-radius:4px;color:#a8e0ff;font-family:"JetBrains Mono",ui-monospace,monospace;font-size:11px;' +
        'box-shadow:0 18px 50px -12px rgba(0,40,80,0.6),0 0 20px -6px rgba(140,215,255,0.4);' +
        'opacity:0;pointer-events:none;transform:translateY(-6px);transition:opacity 200ms,transform 200ms}' +
      '.xrbar.is-expanded ~ .xrbar__tray,.xrbar__tray.is-open{opacity:1;pointer-events:auto;transform:translateY(0)}' +
      '.xrbar__tray h4{margin:0 0 10px;font-size:10px;letter-spacing:0.18em;text-transform:uppercase;color:#86d8ff;font-weight:500}' +
      '.xrbar__row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 0;border-top:1px solid rgba(140,215,255,0.12)}' +
      '.xrbar__row:first-of-type{border-top:0}' +
      '.xrbar__row label{font-size:11px;color:#a8e0ff;letter-spacing:0.04em;flex:1}' +
      '.xrbar__row input[type=range]{flex:1;accent-color:#86d8ff}' +
      '.xrbar__sw{position:relative;width:36px;height:20px;border:1px solid rgba(140,215,255,0.32);border-radius:12px;cursor:pointer;flex-shrink:0;background:rgba(140,215,255,0.06)}' +
      '.xrbar__sw::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#86d8ff;transition:left 160ms,box-shadow 160ms}' +
      '.xrbar__sw.is-on{background:rgba(140,215,255,0.22);border-color:#86d8ff}' +
      '.xrbar__sw.is-on::after{left:18px;box-shadow:0 0 12px #86d8ff}' +

      /* Floating exit pill (only while in session) */
      '.xrpill{position:fixed;left:50%;bottom:max(28px, env(safe-area-inset-bottom));transform:translateX(-50%);z-index:99995;' +
        'display:none;align-items:center;gap:10px;padding:14px 22px;border-radius:999px;' +
        'background:rgba(6,18,36,0.85);-webkit-backdrop-filter:blur(20px) saturate(140%);backdrop-filter:blur(20px) saturate(140%);' +
        'border:1px solid rgba(140,215,255,0.45);box-shadow:0 18px 60px -10px rgba(0,40,80,0.7),0 0 36px -8px rgba(140,215,255,0.65);' +
        'color:#a8e0ff;font-family:"JetBrains Mono",ui-monospace,monospace;font-size:13px;letter-spacing:0.1em;' +
        'cursor:pointer;text-transform:uppercase;transition:filter 120ms,transform 120ms}' +
      '.xrpill.is-on{display:inline-flex;animation:xrpillin 320ms cubic-bezier(.2,.7,.2,1)}' +
      '.xrpill:hover{filter:brightness(1.15);transform:translateX(-50%) scale(1.02)}' +
      '@keyframes xrpillin{from{opacity:0;transform:translate(-50%,12px)}to{opacity:1;transform:translateX(-50%)}}' +
      '.xrpill__dot{width:8px;height:8px;border-radius:50%;background:#ff6b6b;box-shadow:0 0 12px #ff6b6b;animation:xrpulse 1.4s ease-in-out infinite}' +

      /* Gaze reticle (transient-pointer hover indicator on focused element) */
      '.xrgaze{position:fixed;pointer-events:none;z-index:99994;border:2px solid #86d8ff;border-radius:8px;' +
        'box-shadow:0 0 0 1px rgba(255,255,255,0.4),0 0 18px 4px rgba(140,215,255,0.45),inset 0 0 14px rgba(140,215,255,0.18);' +
        'opacity:0;transition:opacity 160ms,top 90ms,left 90ms,width 90ms,height 90ms}' +
      '.xrgaze.is-on{opacity:1}' +

      /* Hover glow on any focusable element when XR session is active */
      'body[data-xr-active="1"] :is(button,a,select,input,[role=button],[tabindex],.chip--btn,.dock__pill,.xrsel):not(:disabled):hover{' +
        'box-shadow:0 0 0 2px rgba(140,215,255,0.55),0 0 24px -2px rgba(140,215,255,0.75);' +
        'background-color:rgba(140,215,255,0.16);outline:none;transition:box-shadow 120ms,background-color 120ms;}' +

      /* ──────────────────────────────────────────────────────────────────
         XR-MODE LAYOUT: ≥60 px hit targets, large fonts, generous spacing
         ────────────────────────────────────────────────────────────────── */
      'body[data-xr-active="1"]{font-size:18px;--xr-tap:60px}' +
      'body[data-xr-active="1"] .btn,body[data-xr-active="1"] .chip,body[data-xr-active="1"] .dock__pill,' +
      'body[data-xr-active="1"] .vhdr__back,body[data-xr-active="1"] .fov-tab{min-height:60px;padding-left:22px;padding-right:22px;font-size:15px}' +
      'body[data-xr-active="1"] .chip{padding:10px 16px;font-size:13px;min-height:44px}' +
      'body[data-xr-active="1"] .dock__pill-label{font-size:16px}' +
      'body[data-xr-active="1"] .dock__pill-hint{font-size:12px}' +
      'body[data-xr-active="1"] input,body[data-xr-active="1"] select{min-height:60px;font-size:16px;padding:14px 16px}' +
      'body[data-xr-active="1"] label{font-size:13px}' +
      'body[data-xr-active="1"] .dock{padding:16px 22px;gap:14px}' +
      'body[data-xr-active="1"] .vhdr{padding:14px 22px}' +
      'body[data-xr-active="1"] .vhdr__back{width:60px;height:60px;font-size:24px}' +
      'body[data-xr-active="1"] .row__link{min-height:96px;padding:18px 22px 18px 0;gap:22px}' +
      'body[data-xr-active="1"] .section{padding:28px;margin-bottom:22px}' +

      /* The dom-overlay root needs to fill the screen during the session */
      'body[data-xr-active="1"] .xr-overlay-root{position:fixed;inset:0;display:flex;flex-direction:column}' +

      /* Inside session: drop ALL opaque backgrounds, let passthrough show */
      'body[data-xr-active="1"]{background:transparent !important}' +
      'body[data-xr-active="1"] html,body[data-xr-active="1"]::before,body[data-xr-active="1"]::after{background:transparent !important}' +
      'body[data-xr-active="1"] .app,body[data-xr-active="1"] .viewer,body[data-xr-active="1"] .page,' +
      'body[data-xr-active="1"] .viewer__stage{background:transparent !important}' +

      /* Panel transparency tweak — applies to every translucent card.
         Note: .film and .row__link:hover are intentionally NOT in this list —
         those should stay fully transparent in XR (just blur + glow ring),
         not gain a dark fill via the panel-alpha tweak. */
      'body[data-xr-active="1"] .section,body[data-xr-active="1"] .vhdr,body[data-xr-active="1"] .dock,' +
      'body[data-xr-active="1"] .filters,body[data-xr-active="1"] .hdr__readout,body[data-xr-active="1"] .empty,' +
      'body[data-xr-active="1"] .save-bar{' +
        'background:rgba(6,22,42,calc(var(--xr-panel-alpha,0.55))) !important;}' +

      /* Headset preview-only (no live session): keep page chrome visible
         enough to demo, but tint everything to look passthrough-y */
      'body[data-headset="1"]:not([data-xr-active="1"]){--xr-panel-alpha:0.55}' +

      /* The slide-down bar should not appear once a session is running */
      'body[data-xr-active="1"] .xrbar{transform:translateY(-110%) !important}' +

      /* ── Faux-passthrough room (preview only) ────────────────────────
         A stock room photo behind every page so the user can SEE what the
         transparency slider does on desktop. Only renders when preview is
         active — a real WebXR session must never cover the camera feed. */
      '.xr-passthrough-bg{position:fixed;inset:0;z-index:0;pointer-events:none;display:none;' +
        'background-size:cover;background-position:center;' +
        'background-image:url(https://images.unsplash.com/photo-1554995207-c18c203602cb?w=2400&q=82&fit=crop&crop=entropy);' +
        'filter:brightness(0.72) saturate(0.85) contrast(1.05)}' +
      '.xr-passthrough-bg::after{content:"";position:absolute;inset:0;' +
        'background:radial-gradient(ellipse 80% 50% at 50% 30%,rgba(0,30,60,0.25),transparent 70%),' +
        'linear-gradient(180deg,rgba(2,8,18,0.25) 0%,rgba(2,8,18,0.55) 100%)}' +
      'body[data-xr-preview="1"] .xr-passthrough-bg{display:block}' +
      /* Ensure page content paints above the room. */
      'body[data-xr-preview="1"] .app{position:relative;z-index:2}' +
      'body[data-xr-preview="1"] .page{position:relative;z-index:2}' +
      'body[data-xr-preview="1"] .viewer{z-index:2}' +
      'body[data-xr-preview="1"] .xrbar{z-index:99990}' +

      /* ── XR-friendly <select> replacement ─────────────────────────── */
      'select.xrsel__native{position:absolute !important;width:1px !important;height:1px !important;opacity:0 !important;pointer-events:none !important;overflow:hidden !important;border:0 !important;padding:0 !important;margin:-1px !important;clip:rect(0 0 0 0) !important}' +
      '.xrsel{display:inline-flex;align-items:center;gap:8px;font:inherit;font-family:"JetBrains Mono",ui-monospace,monospace;font-size:11px;letter-spacing:0.04em;text-transform:uppercase;' +
        'padding:6px 10px 6px 12px;line-height:1;background:rgba(140,215,255,0.06);color:#86d8ff;border:1px solid rgba(140,215,255,0.32);border-radius:2px;cursor:pointer;transition:border-color 120ms,color 120ms,box-shadow 120ms}' +
      '.xrsel:hover,.xrsel[aria-expanded="true"]{border-color:#86d8ff;box-shadow:0 0 14px -3px rgba(140,215,255,0.55);color:#a8e0ff}' +
      '.xrsel__label{flex:1;text-align:left;white-space:nowrap}' +
      '.xrsel__chev{font-size:9px;opacity:0.7;transition:transform 160ms}' +
      '.xrsel[aria-expanded="true"] .xrsel__chev{transform:rotate(180deg)}' +
      '.xrsel__menu{position:fixed;z-index:99996;display:flex;flex-direction:column;padding:6px;gap:2px;min-width:180px;max-width:min(360px,90vw);max-height:60vh;overflow-y:auto;' +
        'background:rgba(6,18,36,0.92);-webkit-backdrop-filter:blur(20px) saturate(140%);backdrop-filter:blur(20px) saturate(140%);' +
        'border:1px solid rgba(140,215,255,0.36);border-radius:4px;box-shadow:0 24px 60px -16px rgba(0,30,60,0.7),0 0 30px -8px rgba(140,215,255,0.45);' +
        'animation:xrselin 180ms cubic-bezier(.2,.7,.2,1)}' +
      '@keyframes xrselin{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}' +
      '.xrsel__opt{font:inherit;font-family:"JetBrains Mono",ui-monospace,monospace;font-size:12px;letter-spacing:0.04em;text-transform:uppercase;text-align:left;' +
        'background:transparent;border:1px solid transparent;color:#a8e0ff;padding:10px 14px;border-radius:2px;cursor:pointer;transition:background 100ms,color 100ms,border-color 100ms;line-height:1.3;min-height:36px}' +
      '.xrsel__opt:hover{background:rgba(140,215,255,0.12);border-color:rgba(140,215,255,0.32);color:#fff}' +
      '.xrsel__opt.is-on{background:rgba(140,215,255,0.2);color:#fff;border-color:#86d8ff;box-shadow:inset 0 0 0 1px rgba(140,215,255,0.4)}' +
      '.xrsel__opt.is-disabled{opacity:0.4;cursor:default}' +
      '.xrsel__opt.is-disabled:hover{background:transparent;color:#a8e0ff;border-color:transparent}' +

      /* In XR-active mode, all custom dropdowns get ≥60 px hit targets too */
      'body[data-xr-active="1"] .xrsel{min-height:60px;padding:12px 18px;font-size:14px}' +
      'body[data-xr-active="1"] .xrsel__opt{min-height:60px;font-size:14px;padding:18px 18px}' +
      'body[data-xr-active="1"] .xrsel__menu{padding:8px;gap:4px;border-width:1.5px}' +

      /* ≥1280px desktop refinement: keep gallery readable wide */
      '@media (min-width:1280px){.app{max-width:1400px !important}}' +
      '@media (min-width:1600px){.app{max-width:1560px !important}}' +
      ''
    );
    var s = document.createElement('style');
    s.id = 'xr-bridge-css';
    s.textContent = css;
    document.head.appendChild(s);

    // Live panel-opacity from tweaks
    document.documentElement.style.setProperty('--xr-panel-alpha', (state.panelOpacity / 100).toFixed(2));
  }

  /* ── DOM-overlay root ─────────────────────────────────────────────────── */
  function ensureOverlayRoot() {
    if (state.overlayRoot) return state.overlayRoot;
    // The whole <body> is the overlay root — every page is already a flat
    // surface that should be re-projected over passthrough. We don't have to
    // create a wrapper, but we mark the body for the spec.
    state.overlayRoot = document.body;
    state.overlayRoot.classList.add('xr-overlay-root');
    return state.overlayRoot;
  }

  /* ── The slide-down bar ───────────────────────────────────────────────── */
  function buildBar() {
    if (state.barEl) return state.barEl;
    var bar = document.createElement('div');
    bar.className = 'xrbar';
    bar.innerHTML = (
      '<span class="xrbar__live" aria-hidden></span>' +
      '<div class="xrbar__title">' +
        '<span class="xrbar__tag">XR</span>' +
        '<span><b id="xrbar-title-text">Headset detected</b> &middot; <span id="xrbar-subtitle">tap to enter immersive</span></span>' +
      '</div>' +
      '<div class="xrbar__actions">' +
        '<button class="xrbar__btn" type="button" data-xr-enter>Enter</button>' +
        '<button class="xrbar__caret" type="button" aria-label="Expand" data-xr-expand>&#x25BE;</button>' +
        '<button class="xrbar__btn xrbar__btn--ghost" type="button" data-xr-dismiss aria-label="Dismiss">Later</button>' +
      '</div>'
    );
    document.body.appendChild(bar);

    var tray = document.createElement('div');
    tray.className = 'xrbar__tray';
    tray.innerHTML = (
      '<h4>XR Tweaks</h4>' +
      '<div class="xrbar__row"><label for="xr-tw-preview">Headset preview (simulate passthrough on desktop)</label>' +
        '<div class="xrbar__sw' + (previewOn ? ' is-on' : '') + '" data-xr-tw="preview" role="switch" aria-checked="' + previewOn + '"></div></div>' +
      '<div class="xrbar__row"><label for="xr-tw-op">Panel transparency</label>' +
        '<input id="xr-tw-op" type="range" min="20" max="95" step="5" value="' + state.panelOpacity + '" data-xr-tw="opacity"></div>' +
      '<div class="xrbar__row"><label for="xr-tw-reticle">Force pinch reticle visible</label>' +
        '<div class="xrbar__sw' + (state.forceReticle ? ' is-on' : '') + '" data-xr-tw="reticle" role="switch" aria-checked="' + state.forceReticle + '"></div></div>' +
      '<div class="xrbar__row" style="border-top-color:transparent;padding-top:14px">' +
        '<div style="font-size:10px;color:#6cc8ff;letter-spacing:0.1em">SUPPORT</div>' +
        '<div style="display:flex;gap:10px"><span id="xrbar-ar">AR&middot;–</span><span id="xrbar-vr">VR&middot;–</span></div></div>'
    );
    document.body.appendChild(tray);
    state.barEl  = bar;
    state.trayEl = tray;

    bar.querySelector('[data-xr-enter]').addEventListener('click', enterImmersive);
    bar.querySelector('[data-xr-dismiss]').addEventListener('click', function () {
      bar.classList.remove('is-open');
      tray.classList.remove('is-open');
      bar.classList.remove('is-expanded');
      /* Note: NOT persisted — the bar comes back on the next page load.
         Persistent dismissal hides the tweaks tray and there's no other
         entry point, so users couldn't get back to it. */
    });
    bar.querySelector('[data-xr-expand]').addEventListener('click', function () {
      var open = !bar.classList.contains('is-expanded');
      bar.classList.toggle('is-expanded', open);
      tray.classList.toggle('is-open', open);
    });

    /* Tweak controls */
    tray.addEventListener('click', function (e) {
      var sw = e.target.closest('[data-xr-tw]');
      if (!sw || sw.tagName === 'INPUT') return;
      var k = sw.dataset.xrTw;
      if (k === 'preview') {
        previewOn = !previewOn;
        sw.classList.toggle('is-on', previewOn);
        sw.setAttribute('aria-checked', previewOn);
        lsSet(K.preview, previewOn ? '1' : '0');
        applyHeadsetFlag();
        document.documentElement.style.setProperty('--xr-panel-alpha', (state.panelOpacity/100).toFixed(2));
      } else if (k === 'reticle') {
        state.forceReticle = !state.forceReticle;
        sw.classList.toggle('is-on', state.forceReticle);
        sw.setAttribute('aria-checked', state.forceReticle);
        lsSet(K.reticle, state.forceReticle ? '1' : '0');
        if (state.reticleEl) state.reticleEl.classList.toggle('is-on', state.forceReticle || !!state.session);
      }
    });
    tray.addEventListener('input', function (e) {
      var t = e.target.closest('input[data-xr-tw="opacity"]');
      if (!t) return;
      state.panelOpacity = parseInt(t.value, 10);
      lsSet(K.panelOp, String(state.panelOpacity));
      document.documentElement.style.setProperty('--xr-panel-alpha', (state.panelOpacity/100).toFixed(2));
    });

    return bar;
  }

  /* ── Floating exit pill (in DOM, always visible during session) ───────── */
  function ensurePill() {
    if (state.pillEl) return state.pillEl;
    var p = document.createElement('button');
    p.type = 'button';
    p.className = 'xrpill';
    p.innerHTML = '<span class="xrpill__dot"></span> Exit XR';
    p.addEventListener('click', exitImmersive);
    document.body.appendChild(p);
    state.pillEl = p;
    return p;
  }

  /* ── Faux-passthrough background (preview only) ──────────────────────────
     When the user toggles "Headset preview" or clicks Enter without a real
     headset, we layer a stock room photo behind every page so the
     transparency slider has something to peek through. The image is only
     visible when data-xr-preview is set — a real WebXR session must never
     cover the headset's camera feed. */
  function ensurePassthroughBg() {
    if (state.bgEl) return state.bgEl;
    var bg = document.createElement('div');
    bg.className = 'xr-passthrough-bg';
    /* Layered: a minimal-room photo + a subtle cyan wash so the Glass UI
       still reads as belonging to the same world. */
    document.body.appendChild(bg);
    state.bgEl = bg;
    return bg;
  }

  /* ── Gaze reticle (the visual focus indicator that follows the pointer) ─ */
  function ensureReticle() {
    if (state.reticleEl) return state.reticleEl;
    var r = document.createElement('div');
    r.className = 'xrgaze';
    document.body.appendChild(r);
    state.reticleEl = r;
    return r;
  }

  function bindHoverGlow() {
    if (state._boundHover) return;
    state._boundHover = true;
    var selector = 'button:not(:disabled),a[href],select:not(:disabled),input:not(:disabled),[role="button"],[tabindex]:not([tabindex="-1"]),.chip--btn,.dock__pill,.xrsel';
    document.addEventListener('pointermove', function (e) {
      if (!(state.session || state.forceReticle || previewOn)) return;
      var el = e.target;
      if (!el) { hideReticle(); return; }
      var hit = el.closest(selector);
      if (!hit || hit.disabled) { hideReticle(); return; }
      var r = hit.getBoundingClientRect();
      var pad = 6;
      var g = ensureReticle();
      g.style.left   = (r.left - pad) + 'px';
      g.style.top    = (r.top  - pad) + 'px';
      g.style.width  = (r.width  + pad*2) + 'px';
      g.style.height = (r.height + pad*2) + 'px';
      g.classList.add('is-on');
    }, true);
    document.addEventListener('pointerleave', hideReticle, true);
    function hideReticle() {
      if (state.reticleEl) state.reticleEl.classList.remove('is-on');
    }
  }

  /* ── XR-friendly <select> replacement ─────────────────────────────────────
     Native <select> popups don't fire correctly under WebXR transient-pointer
     (confirmed on Quest 2 Meta Browser). We replace each select with a button
     that opens a custom popup of options — same selection semantics, but
     fully DOM-overlay-friendly and ≥60 px tap targets in XR mode. The native
     select stays in the DOM (hidden) so form submission still works. */
  function enhanceSelect(sel) {
    if (!sel || sel.dataset.xrEnhanced === '1' || sel.multiple) return;
    if (sel.dataset.xrNoEnhance === '1') return;
    sel.dataset.xrEnhanced = '1';

    // Hide the native control without removing it from the form.
    sel.classList.add('xrsel__native');

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'xrsel ' + (sel.className || '');
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.setAttribute('aria-expanded', 'false');
    /* Preserve id so labels[for=] still work; move id to the button. */
    if (sel.id) { btn.id = sel.id + '-xr'; }
    /* Match the native select's title attribute for hover hints */
    if (sel.title) btn.title = sel.title;

    function syncLabel() {
      var opt = sel.options[sel.selectedIndex];
      var label = opt ? (opt.dataset.label || opt.text) : '';
      btn.innerHTML = '<span class="xrsel__label"></span><span class="xrsel__chev" aria-hidden>&#x25BE;</span>';
      btn.firstChild.textContent = label;
    }
    syncLabel();

    sel.parentNode.insertBefore(btn, sel.nextSibling);

    function closeAllMenus() {
      document.querySelectorAll('.xrsel__menu').forEach(function (m) { m.remove(); });
      document.querySelectorAll('.xrsel[aria-expanded="true"]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    }

    function openMenu() {
      closeAllMenus();
      var menu = document.createElement('div');
      menu.className = 'xrsel__menu';
      menu.setAttribute('role', 'listbox');
      var rect = btn.getBoundingClientRect();
      menu.style.left  = rect.left + 'px';
      menu.style.top   = (rect.bottom + 6) + 'px';
      menu.style.minWidth = Math.max(rect.width, 180) + 'px';

      Array.prototype.forEach.call(sel.options, function (opt) {
        if (opt.hidden) return;
        var item = document.createElement('button');
        item.type = 'button';
        item.className = 'xrsel__opt' + (opt.selected ? ' is-on' : '') + (opt.disabled ? ' is-disabled' : '');
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', opt.selected);
        item.textContent = opt.text;
        if (opt.disabled) item.disabled = true;
        item.addEventListener('click', function (e) {
          e.preventDefault();
          if (opt.disabled) return;
          sel.value = opt.value;
          /* Some option lists have multiple items with the same value
             (e.g. settings.html "Other"). Setting .value picks the FIRST
             — use selectedIndex to be exact. */
          sel.selectedIndex = Array.prototype.indexOf.call(sel.options, opt);
          syncLabel();
          sel.dispatchEvent(new Event('input',  { bubbles: true }));
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          closeAllMenus();
          btn.setAttribute('aria-expanded', 'false');
        });
        menu.appendChild(item);
      });

      document.body.appendChild(menu);
      btn.setAttribute('aria-expanded', 'true');

      /* Flip menu above if it would overflow the viewport bottom. */
      var mRect = menu.getBoundingClientRect();
      if (mRect.bottom > window.innerHeight - 10) {
        menu.style.top = Math.max(10, rect.top - mRect.height - 6) + 'px';
      }
      if (mRect.right > window.innerWidth - 10) {
        menu.style.left = Math.max(10, window.innerWidth - mRect.width - 10) + 'px';
      }
    }

    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (btn.getAttribute('aria-expanded') === 'true') {
        closeAllMenus();
      } else {
        openMenu();
      }
    });
    btn.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
        e.preventDefault(); openMenu();
      }
    });

    /* External code may set sel.value programmatically — sync our label. */
    sel.addEventListener('change', syncLabel);
    /* If options are added later (e.g. settings camera DB), refresh. */
    new MutationObserver(syncLabel).observe(sel, { childList: true, subtree: true });
  }

  function autoEnhanceSelects(root) {
    var scope = root || document;
    if (!scope || !scope.querySelectorAll) return;
    scope.querySelectorAll('select:not([data-xr-enhanced])').forEach(enhanceSelect);
  }

  function bindSelectAutoEnhance() {
    if (state._boundSelectAutoEnhance) return;
    state._boundSelectAutoEnhance = true;
    autoEnhanceSelects(document);
    /* Re-enhance as new selects mount (gallery re-renders its filter bar
       on every filter change). */
    new MutationObserver(function (records) {
      records.forEach(function (r) {
        r.addedNodes && r.addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) return;
          if (n.matches && n.matches('select')) enhanceSelect(n);
          autoEnhanceSelects(n);
        });
      });
    }).observe(document.body, { childList: true, subtree: true });

    /* Close any open menu when the user clicks outside one. */
    document.addEventListener('click', function (e) {
      if (e.target.closest('.xrsel__menu') || e.target.closest('.xrsel')) return;
      document.querySelectorAll('.xrsel__menu').forEach(function (m) { m.remove(); });
      document.querySelectorAll('.xrsel[aria-expanded="true"]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    });
    /* Close on Escape */
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        document.querySelectorAll('.xrsel__menu').forEach(function (m) { m.remove(); });
        document.querySelectorAll('.xrsel[aria-expanded="true"]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
      }
    });
  }

  /* ── Support probing ──────────────────────────────────────────────────── */
  function probe() {
    return new Promise(function (resolve) {
      if (!navigator.xr) { resolve({ ar:false, vr:false }); return; }
      Promise.all([
        navigator.xr.isSessionSupported('immersive-ar').catch(function(){return false;}),
        navigator.xr.isSessionSupported('immersive-vr').catch(function(){return false;}),
      ]).then(function (r) { resolve({ ar: !!r[0], vr: !!r[1] }); });
    });
  }

  /* ── Enter / exit session ─────────────────────────────────────────────── */
  function pickSessionType() {
    return state.supportAR ? 'immersive-ar' : (state.supportVR ? 'immersive-vr' : null);
  }

  function enterImmersive() {
    if (state.session) return;
    var type = pickSessionType();
    if (!type) {
      // Fall back to preview mode so the bar still does something on desktop.
      previewOn = true;
      lsSet(K.preview, '1');
      applyHeadsetFlag();
      document.body.dataset.xrActive = '1';
      ensurePill().classList.add('is-on');
      forceGlass();
      state.barEl && state.barEl.classList.remove('is-open');
      if (state.onEnter) try { state.onEnter({ preview: true }); } catch(_){}
      return;
    }
    var features = {
      requiredFeatures: [],
      optionalFeatures: ['dom-overlay', 'hand-tracking', 'local-floor', 'bounded-floor'],
      domOverlay: { root: ensureOverlayRoot() },
    };
    navigator.xr.requestSession(type, features).then(function (session) {
      state.session = session;
      state.sessionType = type;
      document.body.dataset.xrActive = '1';
      applyHeadsetFlag();
      forceGlass();
      ensurePill().classList.add('is-on');
      state.barEl && state.barEl.classList.remove('is-open');

      /* The session needs a base layer or it ends immediately. We set up a
         minimal WebGL canvas + XRWebGLLayer here; if a page's own renderer
         (e.g. viewer's Three.js) wants to take over, it can call
         session.updateRenderState({ baseLayer: ... }) which replaces ours. */
      try {
        var canvas = document.createElement('canvas');
        canvas.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:1';
        document.body.appendChild(canvas);
        state.fallbackCanvas = canvas;
        var gl = canvas.getContext('webgl', { xrCompatible: true }) ||
                 canvas.getContext('experimental-webgl', { xrCompatible: true });
        if (gl && gl.makeXRCompatible) {
          Promise.resolve(gl.makeXRCompatible()).then(function () {
            if (state.session !== session) return; // session changed
            session.updateRenderState({ baseLayer: new XRWebGLLayer(session, gl) });
          }).catch(function (e) { console.warn('XR baseLayer setup failed', e); });
        }
      } catch (e) { console.warn('XR fallback canvas failed', e); }

      session.addEventListener('end', onSessionEnd);

      if (state.onEnter) {
        try { state.onEnter({ session: session, type: type }); }
        catch (e) { console.warn('onEnter hook failed', e); }
      }
    }).catch(function (err) {
      console.warn('XR session refused', err);
      // Soft-fall to preview
      previewOn = true;
      lsSet(K.preview, '1');
      applyHeadsetFlag();
      document.body.dataset.xrActive = '1';
      ensurePill().classList.add('is-on');
      forceGlass();
      if (state.onEnter) try { state.onEnter({ preview: true, error: err }); } catch(_){}
    });
  }

  function exitImmersive() {
    if (state.session) {
      try { state.session.end(); } catch (_) {}
      // onSessionEnd will fire and do the rest.
      return;
    }
    // Preview mode exit
    document.body.removeAttribute('data-xr-active');
    state.pillEl && state.pillEl.classList.remove('is-on');
    previewOn = false;
    lsSet(K.preview, '0');
    applyHeadsetFlag();
    restoreAesthetic();
    if (state.onExit) try { state.onExit({ preview: true }); } catch(_){}
  }

  function onSessionEnd() {
    state.session = null;
    state.sessionType = null;
    document.body.removeAttribute('data-xr-active');
    state.pillEl && state.pillEl.classList.remove('is-on');
    if (state.fallbackCanvas) {
      try { state.fallbackCanvas.remove(); } catch(_){}
      state.fallbackCanvas = null;
    }
    applyHeadsetFlag();
    restoreAesthetic();
    if (state.onExit) try { state.onExit({}); } catch(_){}
  }

  /* ── Aesthetic lock ───────────────────────────────────────────────────── */
  function forceGlass() {
    var current = ls(K.ui, document.body.dataset.aesthetic || 'glass');
    if (current !== 'glass') lsSet(K.prevUi, current);
    document.body.dataset.aesthetic = 'glass';
  }
  function restoreAesthetic() {
    var prev = ls(K.prevUi, null);
    if (prev) {
      document.body.dataset.aesthetic = prev;
      lsSet(K.ui, prev);
      lsDel(K.prevUi);
    }
  }

  /* ── Public init ──────────────────────────────────────────────────────── */
  function init(opts) {
    opts = opts || {};
    state.onEnter = opts.onEnter || null;
    state.onExit  = opts.onExit  || null;

    injectCSS();
    bindHoverGlow();
    bindSelectAutoEnhance();
    /* Clear any persisted dismissal from older builds — the bar is now
       transient-dismiss-only. */
    lsDel(K.barDismiss);

    /* Always build the bar so we can show it if support is detected later,
       and so the bar's tweaks tray is reachable for headset-preview testing
       even on non-XR browsers. */
    buildBar();
    ensurePill();
    ensureReticle();
    ensurePassthroughBg();

    if (state.forceReticle || previewOn) state.reticleEl.classList.add('is-on');

    probe().then(function (sup) {
      state.supportAR = sup.ar;
      state.supportVR = sup.vr;
      var arEl = state.trayEl && state.trayEl.querySelector('#xrbar-ar');
      var vrEl = state.trayEl && state.trayEl.querySelector('#xrbar-vr');
      if (arEl) { arEl.textContent = 'AR\u00B7' + (sup.ar ? 'ok' : 'no'); arEl.style.color = sup.ar ? '#86d8ff' : '#456'; }
      if (vrEl) { vrEl.textContent = 'VR\u00B7' + (sup.vr ? 'ok' : 'no'); vrEl.style.color = sup.vr ? '#86d8ff' : '#456'; }

      /* The bar opens on every page load. The dismiss button collapses it
         only for the current view — refreshing brings it back. (Previously
         we persisted dismissal in localStorage which caused the bar to
         disappear permanently if dismissed once; that was the wrong default.) */
      setTimeout(function () { state.barEl.classList.add('is-open'); }, 320);

      var sub = state.barEl.querySelector('#xrbar-subtitle');
      var ttl = state.barEl.querySelector('#xrbar-title-text');
      var btn = state.barEl.querySelector('[data-xr-enter]');
      var tag = state.barEl.querySelector('.xrbar__tag');
      if (sup.ar) {
        ttl && (ttl.textContent = 'Headset detected');
        sub && (sub.textContent = 'enter ar passthrough');
        btn && (btn.textContent = 'Enter AR');
        tag && (tag.textContent = 'AR');
      } else if (sup.vr) {
        ttl && (ttl.textContent = 'Headset detected');
        sub && (sub.textContent = 'enter vr environment');
        btn && (btn.textContent = 'Enter VR');
        tag && (tag.textContent = 'VR');
      } else if (headsetUA) {
        ttl && (ttl.textContent = 'Headset browser');
        sub && (sub.textContent = 'webxr unavailable \u00B7 use preview');
        btn && (btn.textContent = 'Preview');
        tag && (tag.textContent = 'XR');
      } else {
        ttl && (ttl.textContent = 'XR preview available');
        sub && (sub.textContent = 'simulate passthrough \u00B7 expand for tweaks');
        btn && (btn.textContent = 'Preview');
        tag && (tag.textContent = 'XR');
      }
    });
  }

  /* ── Expose ───────────────────────────────────────────────────────────── */
  window.DepthXR = {
    init: init,
    enterImmersive: enterImmersive,
    exitImmersive:  exitImmersive,
    get isInSession() { return !!state.session; },
    get session()     { return state.session; },
    get sessionType() { return state.sessionType; },
    get isHeadset()   { return !!document.body.dataset.headset; },
    get isPreview()   { return previewOn; },
    get supportAR()   { return state.supportAR; },
    get supportVR()   { return state.supportVR; },
  };
}());
