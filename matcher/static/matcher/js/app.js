(function () {
  // ---- privacy heartbeat: the resume lives only while this page is open ----
  var ping = document.body.dataset.pingUrl, leave = document.body.dataset.leaveUrl;
  if (!ping) return;
  var tokenEl = document.querySelector('meta[name=csrf-token]');
  var token = tokenEl ? tokenEl.content : '';
  function beat() {
    fetch(ping, { method: 'POST', headers: { 'X-CSRFToken': token }, credentials: 'same-origin', keepalive: true })
      .then(function (r) { if (r.status === 404) location.href = '/'; })   // already deleted -> back to start
      .catch(function () {});
  }
  beat();
  setInterval(beat, 30000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) beat(); });
  // Tab closed / navigated away: start a short deletion countdown. The next page of the same site pings again and cancels it.
  window.addEventListener('pagehide', function () {
    var fd = new FormData(); fd.append('csrfmiddlewaretoken', token);
    if (navigator.sendBeacon) navigator.sendBeacon(leave, fd);
  });
})();
(function () {
  // ---- drag & drop upload ----
  var dz = document.getElementById('dropzone');
  if (dz) {
    var input = document.getElementById('resumeInput');
    var title = document.getElementById('dropTitle');
    var hint = document.getElementById('dropHint');
    function show() {
      if (input.files.length) {
        var f = input.files[0];
        title.textContent = f.name;
        hint.textContent = (f.size / 1024 / 1024).toFixed(2) + ' MB · ready to analyse';
        dz.classList.add('has-file');
      }
    }
    input.addEventListener('change', show);
    ['dragenter', 'dragover'].forEach(function (e) {
      dz.addEventListener(e, function (ev) { ev.preventDefault(); dz.classList.add('over'); });
    });
    ['dragleave', 'drop'].forEach(function (e) {
      dz.addEventListener(e, function (ev) { ev.preventDefault(); dz.classList.remove('over'); });
    });
    dz.addEventListener('drop', function (ev) {
      if (ev.dataTransfer.files.length) { input.files = ev.dataTransfer.files; show(); }
    });
    document.getElementById('uploadForm').addEventListener('submit', function () {
      var b = document.getElementById('uploadBtn');
      b.disabled = true; b.textContent = 'Reading your resume…';
    });
  }

  // ---- background job search with live progress ----
  var sf = document.getElementById('searchForm');
  if (sf) sf.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var overlay = document.getElementById('loading'), bar = document.getElementById('loadBar'),
        text = document.getElementById('loadText'), title = document.getElementById('loadTitle');
    overlay.hidden = false;
    var csrf = sf.querySelector('[name=csrfmiddlewaretoken]').value;
    fetch(sf.action, { method: 'POST', headers: { 'X-CSRFToken': csrf }, body: new FormData(sf), credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (info) {
        (function poll() {
          fetch(info.status_url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (st) {
            if (st.state === 'done') { bar.style.width = '100%'; location.href = info.results_url; return; }
            if (st.state === 'error') { title.textContent = 'Something went wrong'; text.textContent = 'Please go back and try again.'; return; }
            if (st.total) {
              bar.style.width = Math.max(4, Math.round(st.done / st.total * 90)) + '%';
              text.textContent = st.done < st.total ? 'Checked ' + st.done + ' of ' + st.total + ' sources…' : 'Scoring matches against your resume…';
            }
            setTimeout(poll, 800);
          }).catch(function () { setTimeout(poll, 1500); });
        })();
      })
      .catch(function () { sf.submit(); });   // fallback: classic form post
  });

  // ---- auto-delete countdown ----
  var timer = document.querySelector('.timer[data-seconds]');
  if (timer) {
    var left = parseInt(timer.dataset.seconds, 10);
    var out = timer.querySelector('b');
    (function tick() {
      if (left <= 0) { out.textContent = 'deleted'; setTimeout(function () { location.href = '/'; }, 1200); return; }
      var m = Math.floor(left / 60), s = left % 60;
      out.textContent = String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
      timer.classList.toggle('low', left < 300);
      left -= 1; setTimeout(tick, 1000);
    })();
  }

  // ---- relative posted dates ----
  document.querySelectorAll('time[data-posted]').forEach(function (t) {
    var d = new Date(t.dataset.posted); if (isNaN(d)) return;
    var days = Math.floor((Date.now() - d) / 864e5);
    t.textContent = days <= 0 ? 'posted today' : days === 1 ? 'posted yesterday'
      : days < 31 ? 'posted ' + days + ' days ago' : 'posted ' + Math.floor(days / 30) + ' mo ago';
  });

  // ---- result filters ----
  document.querySelectorAll('.pill[data-filter]').forEach(function (p) {
    p.addEventListener('click', function () {
      document.querySelectorAll('.pill').forEach(function (x) { x.classList.remove('active'); });
      p.classList.add('active');
      var f = p.dataset.filter;
      document.querySelectorAll('.job').forEach(function (j) {
        j.hidden = !(f === 'all' || j.dataset.verdict === f);
      });
    });
  });
})();

// ---- LaTeX editor: compile + preview ----
(function () {
  var form = document.getElementById('edForm');
  if (!form) return;
  var btn = document.getElementById('compileBtn'), src = document.getElementById('src'),
      err = document.getElementById('edError'), frame = document.getElementById('preview'),
      empty = document.getElementById('edEmpty'), dl = document.getElementById('dlPdf'),
      status = document.getElementById('edStatus'), blobUrl = null;

  function compile() {
    btn.disabled = true; btn.textContent = 'Compiling…'; status.textContent = 'compiling…';
    var body = new FormData(form);
    fetch(form.dataset.compileUrl, { method: 'POST', body: body, credentials: 'same-origin' })
      .then(function (r) {
        if (r.ok) return r.blob().then(function (b) { return { ok: true, blob: b }; });
        return r.json().then(function (j) { return { ok: false, msg: j.error }; });
      })
      .then(function (res) {
        if (res.ok) {
          if (blobUrl) URL.revokeObjectURL(blobUrl);
          blobUrl = URL.createObjectURL(res.blob);
          frame.src = blobUrl + '#toolbar=0&navpanes=0'; frame.hidden = false; empty.hidden = true;
          dl.href = blobUrl; dl.hidden = false; err.hidden = true; status.textContent = 'compiled ✓';
        } else {
          err.textContent = res.msg; err.hidden = false; status.textContent = 'error';
        }
      })
      .catch(function () { err.textContent = 'Could not reach the server. Please try again.'; err.hidden = false; status.textContent = 'error'; })
      .finally(function () { btn.disabled = false; btn.textContent = 'Compile PDF'; });
  }
  btn.addEventListener('click', compile);
  src.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); compile(); }
    if (e.key === 'Tab') {                                  // real tabs/indent in the editor
      e.preventDefault(); var s = src.selectionStart;
      src.value = src.value.slice(0, s) + '  ' + src.value.slice(src.selectionEnd);
      src.selectionStart = src.selectionEnd = s + 2;
    }
  });
  src.addEventListener('input', function () { status.textContent = 'edited'; });
  if (!btn.disabled) compile();                              // show a PDF immediately
})();
