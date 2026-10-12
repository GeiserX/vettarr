// The layout's behaviour: toasts, the confirm dialog, the theme toggle, keys and swipe,
// the slide-over and the selection. The rest is htmx; this file stays under 150 lines.
(function () {
  'use strict';
  const root = document.documentElement;

  // --- toasts ---------------------------------------------------------------
  // showToast(text, 'ok' | 'error'). The server asks for one with the HX-Trigger header
  // ({"toast": {"text": ..., "kind": ...}}); an error answer (409 busy, 404 gone, 403 refused)
  // is one by itself, its body the text. An open slide-over carries its own region.
  function showToast(text, kind) {
    const region = document.querySelector('dialog[open] .toasts') || document.getElementById('toast');
    if (!region) return;
    const error = kind === 'error';
    const toast = document.createElement('div');
    toast.className = 'toast' + (error ? ' toast-error' : '');
    toast.setAttribute('role', error ? 'alert' : 'status');
    const label = document.createElement('span');
    label.className = 'sr-only';
    label.textContent = error ? 'Error: ' : 'Done: ';
    const body = document.createElement('span');
    body.textContent = text || 'Something went wrong.';
    toast.append(label, body);
    region.append(toast);
    setTimeout(() => toast.remove(), error ? 8000 : 5000);
  }
  window.showToast = showToast;
  document.addEventListener('toast', (e) => {
    const d = e.detail || {};
    showToast(d.text || d.value, d.kind);
  });
  document.addEventListener('htmx:responseError', (e) => {
    const text = e.detail.xhr.responseText || '';
    // A proxy's or the server's own error page is not a sentence to show.
    showToast(text.length > 300 || text.trimStart().startsWith('<') ? '' : text, 'error');
  });
  document.addEventListener('htmx:sendError', () => {
    showToast('The server did not answer. Check the connection and try again.', 'error');
  });

  // --- the confirm dialog -----------------------------------------------------
  // Any control that destroys something carries data-confirm="what happens", and optionally
  // data-confirm-title and data-confirm-action (the button's word). Only that button is a yes.
  function confirmDialog(opts) {
    const dialog = document.getElementById('confirm');
    if (!dialog || !dialog.showModal) return Promise.resolve(window.confirm(opts.text));
    dialog.querySelector('[data-confirm-title]').textContent = opts.title || 'Are you sure?';
    dialog.querySelector('[data-confirm-text]').textContent = opts.text || '';
    dialog.querySelector('[value="yes"]').textContent = opts.action || 'Confirm';
    dialog.returnValue = '';
    dialog.showModal();
    return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'yes'), { once: true }));
  }
  window.confirmDialog = confirmDialog;
  function asked(el) {
    return confirmDialog({ title: el.dataset.confirmTitle, text: el.dataset.confirm, action: el.dataset.confirmAction });
  }
  // htmx asks before every request it sends; hold the ones that need a yes.
  document.addEventListener('htmx:confirm', (e) => {
    const el = e.detail.elt;
    if (!el.dataset || !el.dataset.confirm) return;
    e.preventDefault();
    asked(el).then((yes) => yes && e.detail.issueRequest(true));
  });
  // A plain form (no htmx) with data-confirm.
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!form.dataset.confirm || form.dataset.confirmed) return;
    if (form.matches('[hx-post],[hx-put],[hx-patch],[hx-delete],[hx-get]')) return;
    e.preventDefault();
    asked(form).then((yes) => {
      if (!yes) return;
      form.dataset.confirmed = '1';
      form.requestSubmit(e.submitter || undefined);
    });
  });

  // --- theme ------------------------------------------------------------------
  // No stored choice: the system's. The toggle stores the other one (<head> sets it before paint).
  function dark() {
    const stored = root.dataset.theme;
    return stored ? stored === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  function label() {
    const button = document.getElementById('theme-toggle');
    if (!button) return;
    button.setAttribute('aria-pressed', String(dark()));
    button.title = dark() ? 'Switch to the light theme' : 'Switch to the dark theme';
  }
  document.addEventListener('click', (e) => {
    if (!e.target.closest || !e.target.closest('#theme-toggle')) return;
    root.dataset.theme = dark() ? 'light' : 'dark';
    try {
      localStorage.setItem('theme', root.dataset.theme);
    } catch (_) { /* private mode: the choice lasts for this page */ }
    label();
  });
  label();

  // --- keys and swipe ---------------------------------------------------------
  // A control with data-key="a" is clicked by that key, never in a field or under an open
  // dialog. A held key acts once: its repeats are dropped, Enter's on a focused control too,
  // or holding A would approve one item per repeat. A swipe on [data-swipe] is J or K.
  const modal = () => document.querySelector('dialog[open]');
  function press(key) {
    const el = document.querySelector('[data-key="' + CSS.escape(key) + '"]:not(:disabled)');
    if (el) el.click();
  }
  document.addEventListener('keydown', (e) => {
    if (e.repeat && e.key === 'Enter' && e.target.closest('button, a, summary')) e.preventDefault();
    if (e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.key.length !== 1 || modal()) return;
    if (!e.target.closest('input, textarea, select, [contenteditable]')) press(e.key.toLowerCase());
  });
  let from = null;
  document.addEventListener('touchstart', (e) => {
    const on = window.innerWidth < 640 && e.target.closest('[data-swipe]') && !modal();
    from = on ? [e.touches[0].clientX, e.touches[0].clientY] : null;
  }, { passive: true });
  document.addEventListener('touchend', (e) => {
    const to = e.changedTouches[0];
    const dx = from ? to.clientX - from[0] : 0;
    if (Math.abs(dx) > 60 && Math.abs(dx) > 2 * Math.abs(to.clientY - from[1])) press(dx < 0 ? 'j' : 'k');
    from = null;
  }, { passive: true });

  // --- the slide-over and the selection -----------------------------------------
  // htmx fills #drawer-body and the dialog opens; closing empties it (`close` does not bubble:
  // caught on the way down). [data-select-all] ticks every box; #bulk-bar shows while one is.
  document.addEventListener('close', (e) => {
    if (e.target.id === 'drawer') document.getElementById('drawer-body').replaceChildren();
  }, true);
  function selection(e) {
    const all = e.type === 'change' && e.target.closest('[data-select-all]');
    const boxes = [...document.querySelectorAll('input[name="ids"]')];
    if (all) boxes.forEach((box) => { box.checked = all.checked; });
    const bar = document.getElementById('bulk-bar');
    if (!bar) return;
    const ticked = boxes.filter((box) => box.checked).length;
    bar.hidden = !ticked;
    bar.querySelector('[data-count]').textContent = ticked + ' selected';
    bar.querySelector('[data-confirm]').dataset.confirmTitle = 'Reject ' + ticked + (ticked === 1 ? ' item?' : ' items?');
    const every = document.querySelector('[data-select-all]');
    if (every && !all) every.checked = ticked > 0 && ticked === boxes.length;
  }
  document.addEventListener('change', selection);
  document.addEventListener('htmx:afterSettle', (e) => {
    if (e.detail.target.id === 'drawer-body' && !modal()) document.getElementById('drawer').showModal();
    selection(e);
  });
})();
