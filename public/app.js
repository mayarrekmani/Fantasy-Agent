(() => {
  document.documentElement.classList.add('js');
  const $ = (sel) => document.querySelector(sel);
  const form = $('#form'), go = $('#go'), statusEl = $('#status');
  const results = $('#results'), tabsEl = $('#tabs'), panelsEl = $('#panels');
  let runId = 0;

  $('#dl-date').textContent = new Date().toLocaleDateString(undefined,
    { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  try {
    const saved = localStorage.getItem('sleeperUsername'); if (saved) $('#username').value = saved;
    const code = localStorage.getItem('ownerCode');
    if (code) { $('#ownercode').value = code; $('details.key').open = true; }
  } catch (_) {}

  function setStatus(message, kind, spinning) {
    statusEl.className = 'status' + (kind ? ' ' + kind : '');
    statusEl.textContent = '';
    if (spinning) { const s = document.createElement('span'); s.className = 'spin'; s.setAttribute('aria-hidden', 'true'); statusEl.append(s); }
    statusEl.append(document.createTextNode(message || ''));
  }

  async function api(path, params, headers) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 295000);
    try {
      const r = await fetch(path + '?' + new URLSearchParams(params), { headers: headers || {}, signal: ctl.signal });
      const text = await r.text();
      let data = null;
      try { data = JSON.parse(text); } catch (_) {}
      if (!r.ok) throw new Error((data && data.error) || (r.status === 504
        ? 'This took longer than the server allows. Please try again; it is usually faster the second time.'
        : 'The server returned an error (' + r.status + '). Please try again.'));
      if (!data) throw new Error('The server sent an unexpected reply. Please try again.');
      return data;
    } catch (e) {
      if (e.name === 'AbortError') throw new Error('This took too long. Please try again.');
      throw e;
    } finally { clearTimeout(timer); }
  }

  function select(id) {
    tabsEl.querySelectorAll('[role=tab]').forEach((t) => {
      const on = t.dataset.target === id;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    panelsEl.querySelectorAll('.panel').forEach((p) => { p.hidden = p.id !== id; });
  }

  function addLeague(lg, first) {
    const id = 'lg-' + lg.id;
    const tab = document.createElement('button');
    tab.className = 'tab'; tab.type = 'button'; tab.setAttribute('role', 'tab');
    tab.dataset.target = id; tab.dataset.league = lg.id;
    tab.append(document.createTextNode(lg.name));
    const st = document.createElement('small'); st.className = 'state'; st.textContent = 'Waiting';
    tab.append(st);
    tab.addEventListener('click', () => select(id));
    tab.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const all = [...tabsEl.querySelectorAll('[role=tab]')];
      const n = all[(all.indexOf(tab) + (e.key === 'ArrowRight' ? 1 : all.length - 1)) % all.length];
      n.focus(); select(n.dataset.target);
    });
    tabsEl.append(tab);
    const panel = document.createElement('section');
    panel.className = 'panel'; panel.id = id; panel.setAttribute('role', 'tabpanel'); panel.hidden = !first;
    const h = document.createElement('h2'); h.textContent = lg.name;
    panel.append(h);
    const pend = document.createElement('p'); pend.className = 'pending';
    panel.append(pend);
    panelsEl.append(panel);
    return { tab, state: st, panel, pend };
  }

  function setPending(ui, text) {
    ui.pend.textContent = '';
    const s = document.createElement('span'); s.className = 'spin'; s.setAttribute('aria-hidden', 'true');
    ui.pend.append(s, document.createTextNode(text));
  }

  async function analyze(ui, lg, username, apiKey, myRun) {
    ui.state.textContent = 'Analyzing…';
    setPending(ui, 'Analyzing this league. The first one can take a minute while the NFL data loads; the rest are faster.');
    const headers = {};
    const owner = $('#ownercode').value.trim();
    if (owner) headers['X-Owner-Code'] = owner;
    else if (apiKey) headers['X-Anthropic-Key'] = apiKey;
    try {
      const data = await api('/api/lineup', { username, league_id: lg.id }, headers);
      if (myRun !== runId) return;
      if (data.skipped) { ui.state.textContent = 'Skipped'; ui.pend.textContent = data.skipped; return; }
      const tpl = document.createElement('template');
      tpl.innerHTML = data.html;                      // built and escaped by the server
      const fresh = tpl.content.firstElementChild;
      fresh.hidden = ui.panel.hidden;
      ui.panel.replaceWith(fresh);
      ui.panel = fresh;
      ui.state.textContent = 'Record ' + data.record;
    } catch (e) {
      if (myRun !== runId) return;
      ui.state.textContent = 'Needs retry';
      ui.pend.textContent = '';
      const box = document.createElement('div'); box.className = 'failed';
      box.append(document.createTextNode(e.message));
      box.append(document.createElement('br'));
      const b = document.createElement('button'); b.type = 'button'; b.textContent = 'Try this league again';
      b.addEventListener('click', () => { box.remove(); analyze(ui, lg, username, apiKey, myRun); });
      box.append(b);
      ui.panel.append(box);
    }
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const username = $('#username').value.trim();
    const apiKey = $('#apikey').value.trim();
    if (!/^[A-Za-z0-9_.\-]{2,40}$/.test(username)) {
      setStatus('Enter your Sleeper username: letters, numbers, dots, dashes or underscores.', 'error'); return;
    }
    if (apiKey && !/^sk-ant-[A-Za-z0-9_\-]{20,}$/.test(apiKey)) {
      setStatus('That does not look like an Anthropic API key. It should start with sk-ant-. Leave it blank to skip Claude.', 'error'); return;
    }
    try {
      localStorage.setItem('sleeperUsername', username);
      const oc = $('#ownercode').value.trim();
      if (oc) localStorage.setItem('ownerCode', oc); else localStorage.removeItem('ownerCode');
    } catch (_) {}
    const myRun = ++runId;
    go.disabled = true;
    results.hidden = true; tabsEl.textContent = ''; panelsEl.textContent = '';
    setStatus('Looking up your leagues…', '', true);
    try {
      const info = await api('/api/leagues', { username });
      if (myRun !== runId) return;
      $('#dl-week').textContent = 'Week ' + info.week + ' edition';
      const active = info.leagues.filter((l) => l.active);
      const waiting = info.leagues.filter((l) => !l.active);
      if (!info.leagues.length) { setStatus('No ' + info.season + ' NFL leagues were found for ' + info.user.display_name + '.', 'error'); return; }
      if (!active.length) { setStatus('None of your leagues have finished drafting yet, so there is nothing to lineup yet.', 'error'); return; }
      results.hidden = false;
      const uis = active.map((lg, i) => addLeague(lg, i === 0));
      select('lg-' + active[0].id);
      const note = waiting.length ? ' (' + waiting.length + ' league' + (waiting.length > 1 ? 's' : '') + ' skipped: draft not finished.)' : '';
      for (let i = 0; i < active.length; i++) {
        setStatus('Analyzing league ' + (i + 1) + ' of ' + active.length + '…' + note, '', true);
        await analyze(uis[i], active[i], username, apiKey, myRun);
        if (myRun !== runId) return;
      }
      setStatus('Done. ' + active.length + ' league' + (active.length > 1 ? 's' : '') + ' analyzed for ' + info.user.display_name + '.' + note, '');
    } catch (e) {
      if (myRun === runId) setStatus(e.message, 'error');
    } finally {
      if (myRun === runId) go.disabled = false;
    }
  });
})();
