/* La Cochette Dorée — moteur PWA iPad
 * Fait tourner app.py (Flask) dans le navigateur via Pyodide (Python WASM).
 * - Intercepte fetch('/api/...') -> Flask test_client
 * - Base SQLite persistée dans IndexedDB
 * - Boutons Sauvegarde / Restauration
 */
(function () {
  'use strict';
  window._COCHETTE_PWA = true;
  window._API_BASE = '';

  /* ---------- 1. Interception fetch (AVANT tout autre script) ---------- */
  var nativeFetch = window.fetch.bind(window);
  var readyResolve;
  var ready = new Promise(function (r) { readyResolve = r; });

  window.fetch = function (input, opts) {
    var url = (typeof input === 'string') ? input : (input && input.url) || '';
    var path;
    try { var u = new URL(url, location.href); path = u.pathname + u.search; }
    catch (e) { path = url; }
    if (path.indexOf('/api/') === 0) {
      return ready.then(function () { return apiBridge(path, opts || {}, input); });
    }
    return nativeFetch(input, opts);
  };

  /* ---------- 2. Overlay de chargement ---------- */
  var overlay, statusEl;
  function makeOverlay() {
    overlay = document.createElement('div');
    overlay.id = 'cochette-boot-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:999999;background:#EEF1F5;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:-apple-system,system-ui,sans-serif;color:#19222E';
    overlay.innerHTML =
      '<div style="font-size:64px;line-height:1">🐷</div>' +
      '<div style="font-size:22px;font-weight:800;margin:14px 0 6px">La Cochette Dorée</div>' +
      '<div id="cochette-boot-status" style="font-size:14px;color:#48566A">Démarrage du moteur…</div>' +
      '<div style="margin-top:18px;width:220px;height:6px;background:#DCE2EA;border-radius:3px;overflow:hidden">' +
      '<div id="cochette-boot-bar" style="height:100%;width:5%;background:#C9962B;border-radius:3px;transition:width .4s"></div></div>' +
      '<div style="position:absolute;bottom:24px;font-size:11px;color:#76828F">Premier démarrage : 20–60 s · ensuite ~10 s</div>';
    document.documentElement.appendChild(overlay);
    statusEl = overlay.querySelector('#cochette-boot-status');
  }
  function setStatus(txt, pct) {
    if (statusEl) statusEl.textContent = txt;
    var bar = document.getElementById('cochette-boot-bar');
    if (bar && pct) bar.style.width = pct + '%';
  }
  function killOverlay() { if (overlay) { overlay.remove(); overlay = null; } }
  function bootError(msg) {
    if (!overlay) makeOverlay();
    setStatus('', 0);
    overlay.querySelector('#cochette-boot-status').innerHTML =
      '<div style="color:#B3261E;font-weight:700;max-width:340px;text-align:center">Erreur de démarrage :<br>' + msg +
      '<br><br><button onclick="location.reload()" style="padding:10px 22px;border:0;border-radius:8px;background:#C9962B;color:#fff;font-weight:700;font-size:15px">Réessayer</button></div>';
  }

  /* ---------- 3. IndexedDB (persistance de cochette.db) ---------- */
  var DB_NAME = 'cochette-doree', STORE = 'files', KEY = 'cochette.db';
  function idb() {
    return new Promise(function (res, rej) {
      var rq = indexedDB.open(DB_NAME, 1);
      rq.onupgradeneeded = function () { rq.result.createObjectStore(STORE); };
      rq.onsuccess = function () { res(rq.result); };
      rq.onerror = function () { rej(rq.error); };
    });
  }
  function idbGet(key) {
    return idb().then(function (db) {
      return new Promise(function (res, rej) {
        var rq = db.transaction(STORE).objectStore(STORE).get(key);
        rq.onsuccess = function () { res(rq.result || null); };
        rq.onerror = function () { rej(rq.error); };
      });
    });
  }
  function idbSet(key, val) {
    return idb().then(function (db) {
      return new Promise(function (res, rej) {
        var tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).put(val, key);
        tx.oncomplete = function () { res(); };
        tx.onerror = function () { rej(tx.error); };
      });
    });
  }

  /* ---------- 4. Boot Pyodide + Flask ---------- */
  var py = null, pyBridge = null;
  var saveTimer = null, savePending = false;

  function dbBytes() { return py.FS.readFile('/app/cochette.db'); }

  function persistNow() {
    if (!py) return Promise.resolve();
    savePending = false;
    try {
      var bytes = dbBytes();
      return idbSet(KEY, new Blob([bytes])).catch(function (e) { console.error('persist', e); });
    } catch (e) { console.error('persist', e); return Promise.resolve(); }
  }
  function schedulePersist() {
    savePending = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(persistNow, 1200);
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden' && savePending) persistNow();
  });
  window.addEventListener('pagehide', function () { if (savePending) persistNow(); });

  function apiBridge(path, opts, origInput) {
    var method = ((opts && opts.method) || (origInput && origInput.method) || 'GET').toUpperCase();
    var body = (opts && opts.body != null) ? opts.body : null;
    var ctype = 'application/json';
    if (opts && opts.headers) {
      var h = opts.headers;
      ctype = (h.get ? h.get('Content-Type') : (h['Content-Type'] || h['content-type'])) || ctype;
    }
    return Promise.resolve().then(function () {
      var r = pyBridge(method, path, body === null ? null : String(body), ctype);
      var status = r.get(0), rctype = r.get(1), pyBody = r.get(2);
      var u8 = pyBody.toJs ? pyBody.toJs() : pyBody;
      if (pyBody.destroy) pyBody.destroy();
      r.destroy();
      if (method !== 'GET' && method !== 'HEAD' && status < 500) schedulePersist();
      return new Response(new Blob([u8], { type: rctype }), {
        status: status,
        headers: { 'Content-Type': rctype }
      });
    });
  }

  function base() {
    // dossier contenant index.html (fonctionne aussi en sous-répertoire)
    return location.href.replace(/[#?].*$/, '').replace(/[^/]*$/, '');
  }

  async function boot() {
    makeOverlay();
    try {
      setStatus('Chargement de Python (WASM)…', 15);
      var mod = await import(base() + 'pyodide/pyodide.mjs');
      py = await mod.loadPyodide({ indexURL: base() + 'pyodide/' });

      setStatus('Installation de Flask…', 40);
      var wheels = ['markupsafe-3.0.3-py3-none-any.whl', 'itsdangerous-2.2.0-py3-none-any.whl',
        'blinker-1.9.0-py3-none-any.whl', 'click-8.4.2-py3-none-any.whl',
        'jinja2-3.1.6-py3-none-any.whl', 'werkzeug-3.1.8-py3-none-any.whl',
        'flask-3.1.3-py3-none-any.whl'];
      var SP = py.runPython("import site; site.getsitepackages()[0]");
      for (var i = 0; i < wheels.length; i++) {
        var buf = await (await nativeFetch(base() + 'wheels/' + wheels[i])).arrayBuffer();
        py.unpackArchive(buf, 'wheel', { extractDir: SP });
      }

      setStatus('Chargement de la base de données…', 60);
      py.FS.mkdirTree('/app');
      var appSrc = await (await nativeFetch(base() + 'app.py')).text();
      py.FS.writeFile('/app/app.py', appSrc);

      var stored = await idbGet(KEY).catch(function () { return null; });
      var dbBuf;
      if (stored) {
        dbBuf = await stored.arrayBuffer();
      } else {
        dbBuf = await (await nativeFetch(base() + 'cochette.db')).arrayBuffer();
      }
      py.FS.writeFile('/app/cochette.db', new Uint8Array(dbBuf));

      setStatus('Démarrage du serveur interne…', 80);
      py.runPython(
        "import sys, os, json\n" +
        "os.chdir('/app'); sys.path.insert(0, '/app')\n" +
        "import app as _cochette\n" +
        "_cochette.migrate_db()\n" +
        "_client = _cochette.app.test_client()\n" +
        "def _bridge(method, path, body, ctype):\n" +
        "    kw = {'method': method}\n" +
        "    if body is not None:\n" +
        "        kw['data'] = body\n" +
        "        kw['content_type'] = ctype or 'application/json'\n" +
        "    resp = _client.open(path, **kw)\n" +
        "    return [resp.status_code, resp.headers.get('Content-Type', 'application/json'), resp.get_data()]\n"
      );
      pyBridge = py.globals.get('_bridge');

      // première persistance (si venu du fichier embarqué) + prêt
      if (!stored) persistNow();
      setStatus('Prêt !', 100);
      readyResolve();
      setTimeout(killOverlay, 350);
      addBackupUI();
    } catch (e) {
      console.error(e);
      bootError((e && e.message ? e.message : e) + '');
    }
  }

  /* ---------- 5. Sauvegarde / restauration manuelles ---------- */
  function addBackupUI() {
    var btn = document.createElement('button');
    btn.textContent = '💾';
    btn.title = 'Sauvegarde / restauration de la base';
    btn.style.cssText = 'position:fixed;bottom:14px;right:14px;z-index:99998;width:44px;height:44px;border-radius:50%;border:1px solid #C6CFDA;background:#FFFFFFEE;font-size:20px;box-shadow:0 2px 8px rgba(0,0,0,.15)';
    var panel = null;
    btn.onclick = function () {
      if (panel) { panel.remove(); panel = null; return; }
      panel = document.createElement('div');
      panel.style.cssText = 'position:fixed;bottom:66px;right:14px;z-index:99998;background:#fff;border:1px solid #C6CFDA;border-radius:12px;padding:12px;box-shadow:0 6px 24px rgba(0,0,0,.18);display:flex;flex-direction:column;gap:8px;font-family:-apple-system,system-ui,sans-serif';
      var exp = document.createElement('button');
      exp.textContent = '⬇︎ Exporter la base (.db)';
      exp.style.cssText = 'padding:10px 14px;border:0;border-radius:8px;background:#C9962B;color:#fff;font-weight:700';
      exp.onclick = function () {
        persistNow();
        var blob = new Blob([dbBytes()], { type: 'application/octet-stream' });
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'cochette-' + new Date().toISOString().slice(0, 10) + '.db';
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
      };
      var impLbl = document.createElement('label');
      impLbl.textContent = '⬆︎ Restaurer une base…';
      impLbl.style.cssText = 'padding:10px 14px;border:1px solid #C6CFDA;border-radius:8px;color:#19222E;font-weight:700;text-align:center';
      var inp = document.createElement('input');
      inp.type = 'file'; inp.accept = '.db'; inp.style.display = 'none';
      inp.onchange = function () {
        var f = inp.files[0];
        if (!f) return;
        if (!confirm('Remplacer la base actuelle par « ' + f.name + ' » ?\nLes données actuelles seront écrasées.')) return;
        f.arrayBuffer().then(function (buf) {
          py.FS.writeFile('/app/cochette.db', new Uint8Array(buf));
          return persistNow();
        }).then(function () { location.reload(); });
      };
      impLbl.appendChild(inp);
      panel.appendChild(exp); panel.appendChild(impLbl);
      document.body.appendChild(panel);
    };
    document.body.appendChild(btn);
  }

  /* ---------- 6. Service worker (hors-ligne) ---------- */
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register(base() + 'sw.js').catch(function (e) { console.warn('SW:', e); });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
  // Lancer le boot sans attendre le DOM complet n'est pas sûr (overlay a besoin de <html>) ;
  // l'interception fetch, elle, est déjà active.
})();
