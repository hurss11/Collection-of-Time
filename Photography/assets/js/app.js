/**
 * app.js —— 应用入口
 *
 * 数据流：fetchSite() → 渲染统计 / 相册 / chips / 排序项
 *         fetchGallery(筛选 + 分页) → 渲染作品网格 → 灯箱
 *
 * 筛选、排序、分页、统计、缺失标记、EXIF 全部由服务端算好，
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
  loadMore: document.getElementById('load-more'),
  count: document.getElementById('result-count'),
  empty: document.getElementById('empty-state'),
  themeToggle: document.getElementById('theme-toggle'),
  year: document.getElementById('year'),
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
};

const lightbox = createLightbox(document.getElementById('lightbox'), {
  // 灯箱里第一次播放「没有封面的本地视频」时抓到的那一帧已经存进后端了，
  // 顺手把网格上的占位块换成它，免得要刷新页面才看得到（见 cover.js）。
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

/* ---------- 渲染：首屏 ---------- */

function renderSortOptions(sorts) {
  el.sort.innerHTML = sorts
    .map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`)
    .join('');
  el.sort.value = state.filters.sort;
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

/* ---------- 渲染：作品列表 ---------- */

function renderMetaView() {
  const shown = state.items.length;
  const total = state.meta ? state.meta.total : shown;

  el.count.textContent = renderCountText(shown, total);
  el.empty.hidden = shown > 0;
  el.loadMore.hidden = !(state.meta && state.meta.hasMore);
  el.loadMore.disabled = false;
  el.reset.disabled = !apiHasActive(state.active);
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

  // 相册卡片 → 筛选该相册
  el.albums.addEventListener('click', (event) => {
    const card = event.target.closest('[data-album]');
    if (!card) return;
    setFilter({ album: card.dataset.album });
    document.getElementById('gallery')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // 作品网格（事件委托）→ 打开灯箱
  // 外链视频的卡片是 <a target=_blank>（站内不播放，点了直接去原站），
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

  // 主题切换（记忆到 localStorage）
  el.themeToggle.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
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
  renderSkeleton(el.gallery, 6);
  bindEvents();

  try {
    renderSite(await fetchSite());
  } catch (error) {
    showError(error.message);
    return;
  }
  refresh();
}

bootstrap();
