(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  // Mobile navigation
  const toggle = $('.menu-toggle');
  const closeMenu = () => { document.body.classList.remove('nav-open'); toggle?.setAttribute('aria-expanded', 'false'); };
  toggle?.addEventListener('click', () => { const open = document.body.classList.toggle('nav-open'); toggle.setAttribute('aria-expanded', String(open)); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && document.body.classList.contains('nav-open')) { closeMenu(); toggle?.focus(); } });
  $$('.site-nav a').forEach(a => a.addEventListener('click', closeMenu));
  matchMedia('(min-width: 901px)').addEventListener('change', closeMenu);

  // Theme: follow the system until the visitor picks one; remember the pick when storage allows.
  const dark = matchMedia('(prefers-color-scheme: dark)');
  const current = () => document.documentElement.dataset.theme || (dark.matches ? 'dark' : 'light');
  const labelTheme = () => $$('.theme-toggle').forEach(button => button.setAttribute('aria-label', current() === 'dark' ? '切换到日间主题（站台）' : '切换到夜间主题（夜行线）'));
  $$('.theme-toggle').forEach(button => button.addEventListener('click', () => {
    const next = current() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('luomoapi-theme', next); } catch {}
    labelTheme();
  }));
  dark.addEventListener('change', labelTheme);
  labelTheme();

  // Toast + clipboard
  let toastTimer;
  function notify(message) {
    const toast = $('.toast'); if (!toast) return;
    toast.textContent = message; toast.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.hidden = true; }, 2600);
  }
  async function copy(text) {
    if (navigator.clipboard && window.isSecureContext) { try { await navigator.clipboard.writeText(text); return; } catch {} }
    const focused = document.activeElement;
    const area = document.createElement('textarea');
    area.value = text; area.setAttribute('readonly', ''); area.className = 'sr-only';
    document.body.append(area); area.select();
    const copied = document.execCommand('copy'); area.remove(); focused?.focus();
    if (!copied) throw new Error('Clipboard unavailable');
  }
  function copySource(button) {
    if (button.dataset.copyTarget) return document.getElementById(button.dataset.copyTarget);
    if (button.hasAttribute('data-copy-visible')) return $('pre:not([hidden])', button.closest('.screen'));
    return null;
  }
  $$('[data-copy-target], [data-copy-visible]').forEach(button => button.addEventListener('click', async () => {
    const source = copySource(button); if (!source) return;
    const original = button.textContent;
    try {
      await copy(source.value ?? source.textContent);
      button.textContent = '已复制'; notify('已复制，可以直接粘贴');
      setTimeout(() => { button.textContent = original; }, 1600);
    } catch { notify('复制未成功，请选中文本手动复制'); }
  }));

  // Destructive forms ask once before submitting.
  $$('form[data-confirm]').forEach(form => form.addEventListener('submit', event => { if (!confirm(form.dataset.confirm)) event.preventDefault(); }));

  // Model catalog filter chips (all / callable / image input).
  const modelChips = $$('[data-model-filter]');
  const modelCards = $$('.line-card[data-callable]');
  modelChips.forEach(chip => chip.addEventListener('click', () => {
    const mode = chip.dataset.modelFilter; let shown = 0;
    modelChips.forEach(item => item.setAttribute('aria-pressed', String(item === chip)));
    modelCards.forEach(card => {
      const visible = mode === 'all' || (mode === 'callable' && card.dataset.callable === '1') || (mode === 'vision' && card.dataset.vision === '1');
      card.hidden = !visible; if (visible) shown++;
    });
    const count = $('#model-count'); if (count) count.textContent = mode === 'all' ? '' : `显示 ${shown} 条线路`;
  }));

  // Busy submit buttons: label + disable after the browser has captured the form data, so a double click
  // cannot send the same form twice. Restored if the page comes back from the back/forward cache.
  document.addEventListener('submit', event => {
    const button = event.submitter;
    if (event.defaultPrevented || !button || !button.dataset.busy) return;
    button.dataset.idle = button.innerHTML;
    setTimeout(() => { button.disabled = true; button.textContent = button.dataset.busy; }, 0);
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    $$('[data-busy][data-idle]').forEach(button => { button.disabled = false; button.innerHTML = button.dataset.idle; });
  });

  // Password visibility
  $$('.password-toggle').forEach(button => button.addEventListener('click', () => {
    const input = document.getElementById(button.getAttribute('aria-controls')); if (!input) return;
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.textContent = show ? '隐藏' : '显示';
    button.setAttribute('aria-pressed', String(show));
  }));

  // Tabs (code screens): buttons [role=tab][data-tab] switch [data-panel] blocks in the same group.
  $$('[data-tabs]').forEach(group => {
    const tabs = $$('[role="tab"]', group);
    const select = tab => {
      tabs.forEach(item => { const on = item === tab; item.setAttribute('aria-selected', String(on)); item.tabIndex = on ? 0 : -1; });
      $$('[data-panel]', group).forEach(panel => { panel.hidden = panel.dataset.panel !== tab.dataset.tab; });
    };
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => select(tab));
      tab.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
        next.focus(); select(next);
      });
    });
  });

  // Docs route search + filter
  const search = $('#route-search');
  const routes = $$('.endpoint[data-status]');
  const filters = $$('[data-route-filter]');
  let filter = 'all';
  function applyFilter() {
    const query = (search?.value || '').trim().toLowerCase(); let count = 0;
    routes.forEach(route => {
      const visible = (filter === 'all' || route.dataset.status === filter) && (route.dataset.search || '').toLowerCase().includes(query);
      route.hidden = !visible; if (visible) count++;
    });
    const summary = $('#route-count'); if (summary) summary.textContent = `显示 ${count} / ${routes.length} 个接口`;
    const empty = $('#route-empty'); if (empty) empty.hidden = count > 0;
  }
  search?.addEventListener('input', applyFilter);
  filters.forEach(button => button.addEventListener('click', () => {
    filter = button.dataset.routeFilter;
    filters.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    applyFilter();
  }));
  function revealRoute(hash) {
    let id;
    try { id = decodeURIComponent(hash.slice(1)); } catch { return; }
    const target = document.getElementById(id);
    if (!routes.includes(target)) return;
    const query = (search?.value || '').trim().toLowerCase();
    if (search && !(target.dataset.search || '').toLowerCase().includes(query)) search.value = '';
    if (filter !== 'all' && filter !== target.dataset.status) {
      filter = 'all';
      filters.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.routeFilter === filter)));
    }
    applyFilter();
    if (!target.hasAttribute('tabindex')) target.tabIndex = -1;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: 'start' });
  }
  $('.docs-nav')?.addEventListener('click', event => {
    const link = event.target.closest('a[href^="#endpoint-"]');
    if (link && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) revealRoute(link.hash);
  });
  if (search) {
    applyFilter();
    revealRoute(location.hash);
    window.addEventListener('hashchange', () => revealRoute(location.hash));
  }

  // Fare estimator: credits per 1M tokens come from the server-rendered price options.
  const estimator = $('#estimator');
  if (estimator) {
    const [model, input, cached, output] = ['est-model', 'est-input', 'est-cached', 'est-output'].map(id => document.getElementById(id));
    const format = (value, prefix = '') => {
      if (!Number.isFinite(value)) return '—';
      if (value > 0 && value < 0.000001) return `<${prefix}0.000001`;
      return prefix + value.toLocaleString('zh-CN', { maximumFractionDigits: 6 });
    };
    const update = () => {
      const option = model.selectedOptions[0]; if (!option) return;
      const read = field => Math.max(0, Number(field.value) || 0);
      const inTokens = read(input); const cachedTokens = Math.min(read(cached), inTokens); const outTokens = read(output);
      // Billing switches the whole request to the long-context tier once input exceeds the threshold.
      const threshold = Number(option.dataset.longThreshold) || 0;
      const tier = threshold && inTokens > threshold ? 'long' : '';
      const key = name => tier ? tier + name[0].toUpperCase() + name.slice(1) : name;
      const rate = name => Number(option.dataset[key(name)]);
      const cachedRate = option.dataset[key('cached')] ? rate('cached') : rate('input');
      const credits = ((inTokens - cachedTokens) * rate('input') + cachedTokens * cachedRate + outTokens * rate('output')) / 1e6;
      $('#est-credits').textContent = format(credits);
      $('#est-usd').textContent = format(credits / 720, '$');
      $('#est-cny').textContent = format(credits / 100, '¥');
    };
    estimator.addEventListener('input', update);
    estimator.addEventListener('submit', event => event.preventDefault());
    update();
  }

  // Show UTC timestamps in the visitor's local time where marked.
  $$('time[data-local]').forEach(node => {
    const date = new Date(node.getAttribute('datetime'));
    if (!Number.isNaN(date.getTime())) node.textContent = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  });
})();
