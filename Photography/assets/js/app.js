/**
 * app.js —— 应用入口
 *
 * 数据流：fetchSite() → 渲染统计 / 系列专题块 / chips / 排序项
 *         fetchGallery(筛选 + 分页) → 渲染作品流（等高拼接）→ 灯箱
 *
 * 筛选、排序、分页、统计、缺失标记、EXIF、宽高比全部由服务端算好，
 * 前端只做三件事：取值 → 拼 HTML → 绑事件。
 */

import { fetchSite, fetchGallery, escapeHtml } from './data.js';
import {
  renderCards,
  renderAlbums,
  renderChips,
  renderSkeleton,
  renderCountText,
  syncAlbums,
  syncChips,
  resolveCardId,
  patchCardCover,
} from './gallery.js';
import { createLightbox } from './lightbox.js';
import { layoutRows, watchLayout, watchRatios } from './justify.js';

const PAGE_SIZE = 24;

/* ---------- DOM ---------- */
const el = {
  gallery: document.getElementById('gallery-grid'),
  albums: document.getElementById('album-grid'),
  typeFilter: document.getElementById('type-filter'),
  albumFilter: document.getElementById('album-filter'),
  tagFilter: document.getElementById('tag-filter'),
  search: document.getElementById('search-input'),
  searchClear: document.getElementById('search-clear'),
  sort: document.getElementById('sort-select'),
  reset: document.getElementById('filter-reset'),
  filtersToggle: document.getElementById('filters-toggle'),
  filtersPanel: document.getElementById('filters-panel'),
  filtersCount: document.getElementById('filters-count'),
  loadMore: document.getElementById('load-more'),
  count: document.getElementById('result-count'),
  empty: document.getElementById('empty-state'),
  themeToggle: document.getElementById('theme-toggle'),
  themeLabel: document.getElementById('theme-label'),
  year: document.getElementById('year'),
  hero: {
    figure: document.getElementById('hero-figure'),
    image: document.getElementById('hero-image'),
    caption: document.getElementById('hero-caption'),
  },
  stats: {
    works: document.querySelector('[data-stat="works"]'),
    albums: document.querySelector('[data-stat="albums"]'),
    tags: document.querySelector('[data-stat="tags"]'),
  },
};

/* ---------- 状态：只存「当前请求条件」与服务端回显 ---------- */
const state = {
  filters: { q: '', type: 'all', album: '', tags: [], sort: 'date-desc' },
  defaultSort: 'date-desc',
  items: [],
  meta: null,
  active: null,
  page: 1,
  token: 0,
  heroReady: false,
};

const lightbox = createLightbox(document.getElementById('lightbox'), {
  // 灯箱里第一次播放「没有封面的本地视频」时抓到的那一帧已经存进后端了，
  // 顺手把作品流上的占位块换成它，免得要刷新页面才看得到（见 cover.js）。
  onCoverCaptured: (item, url) => {
    patchCardCover(el.gallery, item, url);
  },
});

/** 重置按钮的可用性直接取服务端回显的 active */
function apiHasActive(active) {
  if (!active) return false;
  const tags = active.tags || [];
  return Boolean(active.q) || active.type !== 'all' || Boolean(active.album) || tags.length > 0;
}

/** 生效中的筛选条件条数：折在「筛选」按钮上，收起时也知道自己筛过什么 */
function activeCount(active) {
  if (!active) return 0;
  return (
    (active.q ? 1 : 0) +
    (active.type && active.type !== 'all' ? 1 : 0) +
    (active.album ? 1 : 0) +
    ((active.tags || []).length)
  );
}

/* ---------- 渲染：首屏 ---------- */

function renderSortOptions(sorts) {
  el.sort.innerHTML = sorts
    .map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`)
    .join('');
  el.sort.value = state.filters.sort;
}

/**
 * 首屏的图：用第一件「照片」当封面，配一行等宽图注。
 * 只在第一次加载时落一次，之后筛选不再替换首屏（否则翻着翻着就跳一下）。
 */
function renderHero(items) {
  if (state.heroReady || !el.hero.figure || !items) return;
  const item = items.find((entry) => entry.kind === 'photo' && entry.imageUrl) ||
    items.find((entry) => entry.imageUrl);
  if (!item) return;

  state.heroReady = true;
  el.hero.image.src = item.imageUrl;
  el.hero.image.alt = item.title;
  el.hero.image.loading = 'eager';
  el.hero.figure.hidden = false;
  el.hero.caption.textContent = [
    `01 — ${item.title}`,
    item.subtitle,
    (item.exifSummary || '').split(' · ')[0],
  ].filter(Boolean).join(' / ');
}

function renderSite(site) {
  state.defaultSort = site.sorts.length ? site.sorts[0].value : 'date-desc';
  el.stats.works.textContent = String(site.stats.photos + site.stats.videos);
  el.stats.albums.textContent = String(site.stats.albums);
  el.stats.tags.textContent = String(site.stats.tags);

  renderSortOptions(site.sorts);
  renderAlbums(el.albums, site.albums);

  const typeCounts = {
    all: site.stats.photos + site.stats.videos,
    photo: site.stats.photos,
    video: site.stats.videos,
  };
  renderChips(
    el.typeFilter,
    'type',
    site.types.map((item) => ({ ...item, count: typeCounts[item.value] ?? 0 })),
  );
  renderChips(el.albumFilter, 'album', site.facets.albums);
  renderChips(el.tagFilter, 'tag', site.facets.tags.map((item) => ({ ...item, prefix: '#' })));
}

/* ---------- 渲染：作品流 ---------- */

function renderMetaView() {
  const shown = state.items.length;
  const total = state.meta ? state.meta.total : shown;
  const active = activeCount(state.active);

  el.count.textContent = renderCountText(shown, total);
  el.empty.hidden = shown > 0;
  el.loadMore.hidden = !(state.meta && state.meta.hasMore);
  el.loadMore.disabled = false;
  el.reset.disabled = !apiHasActive(state.active);
  if (el.filtersCount) {
    el.filtersCount.textContent = active ? String(active) : '';
    el.filtersCount.hidden = !active;
  }
  if (el.filtersToggle) {
    el.filtersToggle.setAttribute('aria-label', active ? `筛选（已选 ${active} 项）` : '筛选');
  }
}

function showError(message) {
  el.gallery.innerHTML = '';
  el.empty.hidden = false;
  el.empty.innerHTML = `<span class="empty-state__glyph">!</span>${escapeHtml(message)}`;
  el.count.textContent = '';
  el.loadMore.hidden = true;
}

async function refresh(options) {
  const append = Boolean(options && options.append);
  const token = ++state.token;

  try {
    const payload = await fetchGallery({
      q: state.filters.q,
      type: state.filters.type,
      album: state.filters.album,
      tags: state.filters.tags.join(','),
      sort: state.filters.sort,
      page: state.page,
      pageSize: PAGE_SIZE,
    });
    if (token !== state.token) return; // 只采用最后一次请求的结果

    state.meta = payload.meta;
    state.active = payload.active;
    state.items = append ? state.items.concat(payload.items) : payload.items;

    renderCards(el.gallery, payload.items, { append });
    layoutRows(el.gallery);
    renderHero(state.items);
    syncChips({ type: el.typeFilter, album: el.albumFilter, tag: el.tagFilter }, state.active);
    syncAlbums(el.albums, state.active.album);
    renderMetaView();
  } catch (error) {
    if (token !== state.token) return;
    showError(error.message);
  }
}

/** 修改筛选条件：回到第 1 页重新请求 */
function setFilter(patch) {
  state.filters = { ...state.filters, ...patch };
  state.page = 1;
  refresh();
}

/* ---------- 交互 ---------- */

/** 主题按钮显示的是「点了会切到哪一边」 */
function syncThemeLabel() {
  const isDark = document.documentElement.dataset.theme === 'dark';
  if (el.themeLabel) el.themeLabel.textContent = isDark ? '日间' : '夜间';
  if (el.themeToggle) {
    el.themeToggle.setAttribute('aria-label', isDark ? '切换到日间模式' : '切换到夜间模式');
  }
}

function bindEvents() {
  // 搜索（防抖 250ms，服务端负责匹配）
  let timer = 0;
  el.search.addEventListener('input', () => {
    el.searchClear.hidden = !el.search.value;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => setFilter({ q: el.search.value }), 250);
  });

  el.searchClear.addEventListener('click', () => {
    el.search.value = '';
    el.searchClear.hidden = true;
    setFilter({ q: '' });
    el.search.focus();
  });

  el.sort.addEventListener('change', () => setFilter({ sort: el.sort.value }));

  // 筛选抽屉：默认收起，工具栏因此只有一行
  el.filtersToggle.addEventListener('click', () => {
    const open = el.filtersToggle.getAttribute('aria-expanded') === 'true';
    el.filtersToggle.setAttribute('aria-expanded', String(!open));
    el.filtersPanel.hidden = open;
  });

  el.reset.addEventListener('click', () => {
    state.filters = {
      q: '',
      type: 'all',
      album: '',
      tags: [],
      sort: state.defaultSort,
    };
    el.search.value = '';
    el.searchClear.hidden = true;
    el.sort.value = state.defaultSort;
    state.page = 1;
    refresh();
  });

  // 类型 chips（单选）
  el.typeFilter.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-chip="type"]');
    if (chip) setFilter({ type: chip.dataset.value });
  });

  // 相册 chips（再次点击取消）
  el.albumFilter.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-chip="album"]');
    if (!chip) return;
    const value = chip.dataset.value;
    setFilter({ album: state.filters.album === value ? '' : value });
  });

  // 标签 chips（多选，服务端为「与」关系）
  el.tagFilter.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-chip="tag"]');
    if (!chip) return;
    const value = chip.dataset.value;
    const next = new Set(state.filters.tags);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    setFilter({ tags: [...next] });
  });

  // 系列专题块 → 筛选该系列
  el.albums.addEventListener('click', (event) => {
    const card = event.target.closest('[data-album]');
    if (!card) return;
    setFilter({ album: card.dataset.album });
    document.getElementById('gallery')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // 作品流（事件委托）→ 打开灯箱
  // 外链视频是 <a target=_blank>（站内不播放，点了直接去原站），
  // 它们不带 data-open，所以下面这段自然会跳过它们；灯箱里也只用「站内能放」的条目。
  el.gallery.addEventListener('click', (event) => {
    const id = resolveCardId(event);
    if (!id) return;
    const viewable = state.items.filter((item) => !item.isEmbed);
    const at = viewable.findIndex((item) => item.id === id);
    if (at >= 0) lightbox.open(viewable, at);
  });

  // 分页：加载下一页（页码由服务端 meta 驱动）
  el.loadMore.addEventListener('click', () => {
    if (!state.meta || !state.meta.hasMore) return;
    state.page += 1;
    el.loadMore.disabled = true;
    refresh({ append: true });
  });

  // 主题切换（记忆到 localStorage，首屏由 theme.js 提前落好）
  el.themeToggle.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    syncThemeLabel();
    try {
      localStorage.setItem('cot-theme', next);
    } catch {
      /* 隐私模式下忽略 */
    }
  });

  // 快捷键：/ 聚焦搜索框
  window.addEventListener('keydown', (event) => {
    if (event.key === '/' && document.activeElement !== el.search) {
      event.preventDefault();
      el.search.focus();
    }
  });
}

/* ---------- 启动 ---------- */

async function bootstrap() {
  el.year.textContent = String(new Date().getFullYear());
  syncThemeLabel();
  renderSkeleton(el.gallery, 6);
  layoutRows(el.gallery);
  bindEvents();

  // 作品流是等高拼接：宽度一变就重新分行；图片解码后发现比例不对也要重排
  watchLayout(el.gallery);
  watchRatios(el.gallery, layoutRows);

  try {
    renderSite(await fetchSite());
  } catch (error) {
    showError(error.message);
    return;
  }
  refresh();
}

bootstrap();
