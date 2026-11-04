// Live departure board: polls the public catalog and flips only the rows that changed.
// Pauses while the tab is hidden, backs off on errors, and keeps the last good board on screen.
(() => {
  'use strict';
  const board = document.querySelector('[data-board-live]');
  if (!board) return;
  const list = board.querySelector('.board-list');
  const limit = Number(board.dataset.limit) || 0;
  const every = Math.max(15, Number(board.dataset.refresh) || 30) * 1000;
  const note = board.querySelector('[data-board-note]');
  const noteText = note?.textContent || '';
  const components = document.querySelector('[data-status-components]');
  const freshness = document.querySelector('[data-status-freshness]');
  const TIMEOUT_MS = 10000;
  let timer = 0;
  let failures = 0;
  let lastRun = Date.now();
  let inFlight = false;
  let latest = 0;

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function row(model) {
    const item = el('li', 'board-row');
    item.dataset.model = model.id;
    const roundel = el('span', 'roundel');
    roundel.dataset.line = model.line?.family || 'other';
    roundel.setAttribute('aria-hidden', 'true');
    roundel.append(el('b', '', model.line?.letter || '?'), el('i', '', model.line?.number || '00'));
    const name = el('div', 'model', model.id);
    name.append(el('small', '', model.name !== model.id ? model.name : model.category));
    const use = el('div', 'use', model.category);
    use.append(el('small', '', model.note || '—'));
    item.append(roundel, name, use, el('span', `lamp ${model.state?.tone || 'off'}`, model.state?.label || '—'));
    return item;
  }

  // Server-rendered and script-built rows differ only in whitespace; compare what a rider sees.
  const look = item => [
    item.textContent.replace(/\s+/g, ''),
    item.querySelector('.lamp')?.className,
    item.querySelector('.roundel')?.dataset.line,
  ].join('|');

  function render(data) {
    const stamp = new Date(data.updated_at);
    if (!Array.isArray(data.models) || typeof data.updated_at !== 'string' || Number.isNaN(stamp.getTime()) || typeof data.status !== 'string') throw new Error('Invalid catalog snapshot');
    // Build the overview before committing any part of this snapshot to the page.
    const cards = document.createDocumentFragment();
    if (components) {
      if (!Array.isArray(data.components) || !data.components.length || !Number.isFinite(data.cache_seconds) || typeof data.note !== 'string') throw new Error('Incomplete status snapshot');
      data.components.forEach(component => {
        if (!component || typeof component.name !== 'string' || typeof component.status !== 'string' || typeof component.detail !== 'string') throw new Error('Invalid status component');
        const tone = component.status === 'operational' ? 'ok' : component.status === 'degraded' ? 'warn' : 'off';
        const label = component.status === 'operational' ? '正常' : component.status === 'degraded' ? '异常' : component.status || '—';
        const card = el('article', `card status-card ${tone}`);
        const top = el('div', 'status-top');
        const lamp = el('span', `signal-lamp ${tone}`);
        lamp.setAttribute('aria-hidden', 'true');
        top.append(lamp, el('span', `badge ${tone}`, label));
        card.append(top, el('h2', '', component.name), el('p', 'muted', component.detail));
        if (component.latency_ms !== null && component.latency_ms !== undefined) {
          const metric = el('p', 'metric-inline');
          metric.append(el('span', 'num', component.latency_ms), ' ms 响应');
          card.append(metric);
        }
        cards.append(card);
      });
    }
    const models = data.models.slice(0, limit || undefined);
    const current = new Map([...list.children].map(item => [item.dataset.model, item]));
    const next = models.map(model => {
      const fresh = row(model);
      const old = current.get(model.id);
      return old && look(old) === look(fresh) ? old : fresh; // a new node plays the flap animation
    });
    [...list.children].forEach(item => { if (!next.includes(item)) item.remove(); });
    next.forEach((item, index) => { if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null); });

    board.querySelector('.board-empty')?.toggleAttribute('hidden', models.length > 0);
    const count = board.querySelector('[data-board-count]');
    if (count) count.textContent = data.model_count ?? models.length;
    board.querySelector('[data-board-more]')?.toggleAttribute('hidden', !data.model_count);
    const clock = board.querySelector('[data-board-clock]');
    if (clock && !Number.isNaN(stamp.getTime())) {
      clock.setAttribute('datetime', data.updated_at);
      clock.textContent = stamp.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    }
    const status = document.querySelector('[data-line-status]');
    if (status) status.textContent = data.status === 'operational' ? '全线正常运行' : '部分线路异常';
    if (components) {
      components.replaceChildren(cards);
      const updated = document.querySelector('[data-status-updated]');
      if (updated) {
        updated.setAttribute('datetime', data.updated_at);
        updated.textContent = stamp.toISOString().slice(0, 16).replace('T', ' ');
      }
      const cache = document.querySelector('[data-status-cache]');
      if (cache) cache.textContent = data.cache_seconds;
      const scope = document.querySelector('[data-status-note]');
      if (scope) scope.textContent = data.note;
    }
    const billable = document.querySelector('[data-line-billable]');
    if (billable && Number.isFinite(data.billable_count)) billable.textContent = data.billable_count;
  }

  function nightService() {
    const hour = new Date().getHours();
    const sub = board.querySelector('[data-board-sub]');
    if (sub) sub.textContent = hour >= 23 || hour < 5 ? 'NIGHT SERVICE · 夜行线运行中' : 'DEPARTURES · MODEL LINES';
  }

  function flash(text, className) {
    if (!note) return;
    note.textContent = text;
    board.classList.remove('is-fresh', 'is-stale');
    if (className) board.classList.add(className);
  }

  // One request at a time; each carries a generation so a late answer can never overwrite a newer one.
  async function refresh() {
    if (inFlight) return;
    const generation = ++latest;
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), TIMEOUT_MS);
    inFlight = true;
    lastRun = Date.now();
    try {
      const response = await fetch(board.dataset.boardLive, { cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: controller.signal });
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json(); // the deadline also covers reading the body
      if (generation !== latest) return;
      render(data);
      failures = 0;
      if (freshness) freshness.hidden = true;
      flash('刚刚更新', 'is-fresh');
      setTimeout(() => { if (!failures) flash(noteText, ''); }, 2400);
    } catch {
      if (generation !== latest) return;
      failures += 1;
      if (freshness) freshness.hidden = false;
      flash('刷新失败 · 数据已陈旧', 'is-stale');
    } finally {
      clearTimeout(deadline);
      inFlight = false;
      nightService();
      schedule();
    }
  }

  function schedule() {
    clearTimeout(timer);
    if (inFlight || document.visibilityState !== 'visible') return;
    const delay = every * Math.min(2 ** failures, 8);
    timer = setTimeout(refresh, Math.max(0, delay - (Date.now() - lastRun)));
  }

  document.addEventListener('visibilitychange', schedule);
  nightService();
  schedule();
})();
