// ═══════════════════════════════════════════════
// ANIMEKU.ID — Main JS
// ═══════════════════════════════════════════════

// ── Navbar scroll ──────────────────────────────
const navbar = document.getElementById('navbar');
window.addEventListener('scroll', () => {
  navbar?.classList.toggle('scrolled', window.scrollY > 50);
}, { passive: true });

// ── Hamburger ──────────────────────────────────
const hamburger = document.getElementById('hamburger');
const mobileMenu = document.getElementById('mobileMenu');
hamburger?.addEventListener('click', () => {
  hamburger.classList.toggle('open');
  mobileMenu.classList.toggle('open');
});

// ── Search ─────────────────────────────────────
const searchToggle  = document.getElementById('searchToggle');
const searchBar     = document.getElementById('searchBar');
const searchInput   = document.getElementById('searchInput');
const searchResults = document.getElementById('searchResults');

searchToggle?.addEventListener('click', () => {
  searchBar.classList.toggle('open');
  if (searchBar.classList.contains('open')) searchInput.focus();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { searchBar?.classList.remove('open'); }
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); searchBar?.classList.add('open'); searchInput?.focus(); }
});

let searchTimer;
searchInput?.addEventListener('input', e => {
  const q = e.target.value.trim();
  clearTimeout(searchTimer);
  if (q.length < 2) { searchResults.innerHTML = ''; return; }
  searchTimer = setTimeout(async () => {
    searchResults.innerHTML = '<div style="padding:12px;color:var(--text3);font-size:13px">Mencari...</div>';
    try {
      const res   = await fetch(`/api/search/${encodeURIComponent(q)}`);
      const data  = await res.json();
      const list  = data?.animes || data?.anime_list || [];
      if (!list.length) { searchResults.innerHTML = '<div style="padding:12px;color:var(--text3);font-size:13px">Tidak ditemukan</div>'; return; }
      searchResults.innerHTML = list.slice(0, 6).map(a => `
        <a href="/anime/${a.slug}" class="search-result-item" onclick="searchBar.classList.remove('open')">
          <img src="${a.poster||''}" alt="${a.title}" onerror="this.style.display='none'">
          <div class="search-result-info">
            <div class="title">${a.title}</div>
            <div class="meta">${a.type||''} ${a.episode ? '· '+a.episode : ''}</div>
          </div>
        </a>`).join('');
    } catch { searchResults.innerHTML = '<div style="padding:12px;color:var(--text3);font-size:13px">Gagal memuat</div>'; }
  }, 400);
});

// ── Active nav link ─────────────────────────────
const curPath = window.location.pathname;
document.querySelectorAll('.nav-link').forEach(link => {
  const href = link.getAttribute('href');
  if (href !== '/' && curPath.startsWith(href)) link.classList.add('active');
});

// ── Dark / Light mode ───────────────────────────
const themeBtn = document.getElementById('themeToggle');
function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('theme', t);
  if (!themeBtn) return;
  themeBtn.innerHTML = t === 'dark'
    ? `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>`
    : `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;
}
setTheme(localStorage.getItem('theme') || 'dark');
themeBtn?.addEventListener('click', () => {
  setTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
});

// ── Back to top ─────────────────────────────────
const backTop = document.getElementById('backTop');
window.addEventListener('scroll', () => backTop?.classList.toggle('visible', window.scrollY > 400), { passive: true });
backTop?.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));

// ── Synopsis read more ──────────────────────────
const synopsisEl  = document.querySelector('.synopsis-text');
const readMoreBtn = document.querySelector('.read-more');
readMoreBtn?.addEventListener('click', () => {
  synopsisEl.classList.toggle('clamped');
  readMoreBtn.textContent = synopsisEl.classList.contains('clamped') ? 'Baca selengkapnya ↓' : 'Sembunyikan ↑';
});

// ════════════════════════════════════════════════
// localStorage Helpers
// ════════════════════════════════════════════════
const WL_KEY   = 'animeku_watchlist';
const HIST_KEY = 'animeku_history';
const PROG_KEY = 'animeku_progress';

function lsGet(key, def) { try { return JSON.parse(localStorage.getItem(key)) ?? def; } catch { return def; } }
function lsSet(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch {} }

// ── Toast ───────────────────────────────────────
function showToast(msg) {
  document.querySelector('.toast')?.remove();
  const t = Object.assign(document.createElement('div'), { className: 'toast', textContent: msg });
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 2500);
}

// ── Watchlist ───────────────────────────────────
function toggleWatchlist(slug, title, poster, type) {
  let list = lsGet(WL_KEY, []);
  const idx = list.findIndex(a => a.slug === slug);
  if (idx >= 0) {
    list.splice(idx, 1);
    showToast('Dihapus dari watchlist');
  } else {
    list.unshift({ slug, title, poster, type, addedAt: Date.now() });
    showToast('Ditambahkan ke watchlist ❤️');
  }
  lsSet(WL_KEY, list);
  _syncWatchlistBtn(slug, list);
}

function _syncWatchlistBtn(slug, list) {
  const inList = list.some(a => a.slug === slug);
  const btn    = document.getElementById('watchlistBtn');
  const txt    = document.getElementById('watchlistBtnText');
  if (!btn) return;
  btn.classList.toggle('active', inList);
  if (txt) txt.textContent = inList ? '✓ Di Watchlist' : '+ Watchlist';
}

// ── Save progress ────────────────────────────────
function saveEpisodeProgress(animeSlug, animeTitle, animePoster, epSlug, epName) {
  if (!animeSlug) return;
  // history
  let hist = lsGet(HIST_KEY, []);
  hist = hist.filter(h => h.epSlug !== epSlug);
  hist.unshift({ animeSlug, animeTitle, animePoster, epSlug, epName, watchedAt: Date.now() });
  lsSet(HIST_KEY, hist.slice(0, 50));
  // progress
  const prog = lsGet(PROG_KEY, {});
  prog[animeSlug] = { epSlug, epName, savedAt: Date.now() };
  lsSet(PROG_KEY, prog);
}

// ── Render watchlist ─────────────────────────────
function renderWatchlist() {
  const el = document.getElementById('watchlistContainer');
  if (!el) return;
  const list = lsGet(WL_KEY, []);
  if (!list.length) {
    el.innerHTML = `<div class="empty-state">
      <i class="fas fa-fire" style="font-size:48px;opacity:0.2;margin:0 auto 16px;display:block;text-align:center"></i>
      <h3>Watchlist kosong</h3><p>Tambahkan anime favorit dari halaman detail!</p></div>`;
    return;
  }
  el.innerHTML = `<div class="anime-grid lg">${list.map(a => `
    <a href="/anime/${a.slug}" class="anime-card">
      <div class="card-poster">
        <img src="${a.poster||''}" alt="${a.title}" loading="lazy" onerror="this.src='/static/img/placeholder.svg'">
        <div class="card-overlay"><div class="play-btn"><svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M5 3l14 9-14 9V3z"/></svg></div></div>
        ${a.type ? `<span class="card-badge type">${a.type}</span>` : ''}
        <button class="wl-delete-btn" onclick="event.stopPropagation();removeWatchlistItem('${a.slug}')" title="Hapus">
          <i class="fas fa-times"></i>
        </button>
      </div>
      <div class="card-info">
        <div class="card-title">${a.title}</div>
        <div class="card-meta"><span class="card-status" style="font-size:11px;color:var(--text3)">${new Date(a.addedAt).toLocaleDateString('id-ID')}</span></div>
      </div>
    </a>`).join('')}</div>`;
}

function removeWatchlistItem(slug) {
  let list = lsGet(WL_KEY, []);
  list = list.filter(a => a.slug !== slug);
  lsSet(WL_KEY, list);
  showToast('Dihapus dari watchlist');
  renderWatchlist();
}

function clearWatchlist() {
  if (!confirm('Hapus semua watchlist?')) return;
  lsSet(WL_KEY, []);
  showToast('Watchlist dikosongkan');
  renderWatchlist();
}

// ── Render history ────────────────────────────────
function renderHistory() {
  const el = document.getElementById('historyContainer');
  if (!el) return;
  const hist = lsGet(HIST_KEY, []);
  if (!hist.length) {
    el.innerHTML = `<div class="empty-state">
      <i class="fas fa-fire" style="font-size:48px;opacity:0.2;margin:0 auto 16px;display:block;text-align:center"></i>
      <h3>Belum ada riwayat</h3><p>Mulai nonton anime dulu!</p></div>`;
    return;
  }
  function timeAgo(ts) {
    const d = (Date.now()-ts)/1000;
    if (d<60) return 'Baru saja';
    if (d<3600) return `${Math.floor(d/60)} menit lalu`;
    if (d<86400) return `${Math.floor(d/3600)} jam lalu`;
    return `${Math.floor(d/86400)} hari lalu`;
  }
  el.innerHTML = `<div style="display:flex;flex-direction:column;gap:10px">${hist.map(h => `
    <div class="history-item">
      <a href="/episode/${h.epSlug}?anime=${h.animeSlug}" style="display:contents;text-decoration:none;color:inherit">
        <img src="${h.animePoster||''}" alt="${h.animeTitle}" onerror="this.src='/static/img/placeholder.svg'">
        <div class="history-info">
          <div class="history-title">${h.animeTitle}</div>
          <div class="history-ep">${h.epName}</div>
          <div class="history-time">${timeAgo(h.watchedAt)}</div>
        </div>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" style="color:var(--text3);flex-shrink:0"><path d="M5 3l14 9-14 9V3z"/></svg>
      </a>
      <button class="wl-delete-btn" onclick="removeHistoryItem('${h.epSlug}')" title="Hapus riwayat" style="flex-shrink:0">
        <i class="fas fa-times"></i>
      </button>
    </div>`).join('')}</div>`;
}

function removeHistoryItem(epSlug) {
  let hist = lsGet(HIST_KEY, []);
  hist = hist.filter(h => h.epSlug !== epSlug);
  lsSet(HIST_KEY, hist);
  showToast('Riwayat dihapus');
  renderHistory();
}

function clearHistory() {
  if (!confirm('Hapus semua riwayat?')) return;
  lsSet(HIST_KEY, []);
  lsSet(PROG_KEY, {});
  showToast('Riwayat dikosongkan');
  renderHistory();
}

// Hapus semua sesuai tab aktif
function clearActive() {
  const isHistory = document.getElementById('tab-history')?.classList.contains('active');
  if (isHistory) clearHistory();
  else clearWatchlist();
}

// ── Init ──────────────────────────────────────────
function _init() {
  // Sync watchlist btn on detail page + pasang event listener
  const detailSlug = document.getElementById('detailHero')?.dataset.animeSlug;
  if (detailSlug) {
    _syncWatchlistBtn(detailSlug, lsGet(WL_KEY, []));

    // Pasang click listener langsung (tidak perlu onclick di HTML)
    const btn = document.getElementById('watchlistBtn');
    if (btn) {
      btn.removeAttribute('onclick');
      btn.addEventListener('click', function() {
        const hero   = document.getElementById('detailHero');
        const slug   = hero?.dataset.animeSlug   || '';
        const title  = hero?.dataset.animeTitle  || '';
        const poster = hero?.dataset.animePoster || '';
        const type   = hero?.dataset.animeType   || '';
        toggleWatchlist(slug, title, poster, type);
      });
    }
  }

  // Render koleksi page
  renderWatchlist();
  renderHistory();
}

// Jalankan segera jika DOM sudah siap, atau tunggu event-nya
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _init);
} else {
  _init();
}
