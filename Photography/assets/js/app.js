/**
 * app.js —— 应用入口：装配状态、渲染与交互
 *
 * 数据流：
 *   loadData() → 全局 state.items → filterMedia() → renderGallery() → attachVideoPosters()
 */

import { loadData, collectTags, countByAlbum, countByType, escapeHtml, MEDIA_TYPE } from './data.js';
import { filterMedia, hasActiveFilters } from './search.js';
import { renderGallery, renderAlbums, renderSkeleton, renderCount, resolveCardId } from './gallery.js';
import { createLightbox } from './lightbox.js';
import { attachVideoPosters } from './poster.js';

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
  count: document.getElementById('result-count'),
  empty: document.getElementById('empty-state'),
  themeToggle: document.getElementById('theme-toggle'),
  year: document.getElementById('year'),
  stats: {
    photos: document.querySelector('[data-stat="photos"]'),
    albums: document.querySelector('[data-stat="albums"]'),
    tags: document.querySelector('[data-stat="tags"]'),
  },
};

/** 类型筛选的可选项 */
const TYPE_OPTIONS = [
  { value: 'all', label: '全部' },
  { value: MEDIA_TYPE.PHOTO, label: '照片' },
  { value: MEDIA_TYPE.VIDEO, label: '视频' },
];

/* ---------- 状态 ---------- */
const state = {
  albums: [],
  /** 照片 + 视频的混合列表 */
  items: [],
  tags: [],
  typeCounts: { all: 0, photo: 0, video: 0 },
  filters: { query: '', tags: [], albumId: null, type: 'all', sort: 'date-desc' },
  visible: [],
};

const lightbox = createLightbox(document.getElementById('lightbox'));

/* ---------- 渲染 ---------- */

function currentVisible() {
  return filterMedia(state.items, state.filters);
}

function render() {
  state.visible = currentVisible();

  const pendingPosters = renderGallery(el.gallery, state.visible);
  attachVideoPosters(pendingPosters);

  renderCount(el.count, state.visible.length, state.items.length);
  el.empty.hidden = state.visible.length > 0;
  el.reset.disabled = !hasActiveFilters(state.filters);

  // 同步相册选中态
  el.albums.querySelectorAll('[data-album]').forEach((node) => {
    node.setAttribute('aria-pressed', String(node.dataset.album === state.filters.albumId));
  });
}

/** 渲染筛选 chips（类型 / 相册 / 标签） */
function renderFilters() {
  const albumCounts = countByAlbum(state.items);

  el.typeFilter.querySelectorAll('.chip').forEach((n) => n.remove());
  el.typeFilter.insertAdjacentHTML(
    'beforeend',
    TYPE_OPTIONS.map(
      (option) => `<button class="chip" type="button" data-chip="type" data-value="${option.value}"
                    aria-pressed="${option.value === 'all'}">${option.label}<span class="chip__count">${
                      state.typeCounts[option.value] || 0
                    }</span></button>`,
    ).join(''),
  );

  el.albumFilter.querySelectorAll('.chip').forEach((n) => n.remove());
  el.albumFilter.insertAdjacentHTML(
    'beforeend',
    state.albums
      .map(
        (album) => `<button class="chip" type="button" data-chip="album" data-value="${escapeHtml(album.id)}"
                    aria-pressed="false">${escapeHtml(album.name)}<span class="chip__count">${
                      albumCounts.get(album.id) || 0
                    }</span></button>`,
      )
      .join(''),
  );

  el.tagFilter.querySelectorAll('.chip').forEach((n) => n.remove());
  el.tagFilter.insertAdjacentHTML(
    'beforeend',
    state.tags
      .slice(0, 12)
      .map(
        (item) => `<button class="chip" type="button" data-chip="tag" data-value="${escapeHtml(item.tag)}"
                    aria-pressed="false">#${escapeHtml(item.tag)}<span class="chip__count">${
                      item.count
                    }</span></button>`,
      )
      .join(''),
  );
}

function syncFilterChips() {
  el.typeFilter.querySelectorAll('[data-chip="type"]').forEach((chip) => {
    chip.setAttribute('aria-pressed', String(chip.dataset.value === state.filters.type));
  });
  el.albumFilter.querySelectorAll('[data-chip="album"]').forEach((chip) => {
    chip.setAttribute('aria-pressed', String(chip.dataset.value === state.filters.albumId));
  });
  el.tagFilter.querySelectorAll('[data-chip="tag"]').forEach((chip) => {
    chip.setAttribute('aria-pressed', String(state.filters.tags.includes(chip.dataset.value)));
  });
}

/* ---------- 交互 ---------- */

function bindEvents() {
  // 搜索（带 200ms 防抖）
  let timer = 0;
  el.search.addEventListener('input', () => {
    el.searchClear.hidden = !el.search.value;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      state.filters.query = el.search.value;
      render();
    }, 200);
  });

  el.searchClear.addEventListener('click', () => {
    el.search.value = '';
    el.searchClear.hidden = true;
    state.filters.query = '';
    render();
    el.search.focus();
  });

  el.sort.addEventListener('change', () => {
    state.filters.sort = el.sort.value;
    render();
  });

  el.reset.addEventListener('click', () => {
    state.filters = { query: '', tags: [], albumId: null, type: 'all', sort: el.sort.value };
    el.search.value = '';
    el.searchClear.hidden = true;
    syncFilterChips();
    render();
  });

  // 类型 chips（全部 / 照片 / 视频，单选）
  el.typeFilter.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-chip="type"]');
    if (!chip) return;
    state.filters.type = chip.dataset.value;
    syncFilterChips();
    render();
  });

  // 相册 chips（再次点击取消选中）
  el.albumFilter.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-chip="album"]');
    if (!chip) return;
    const value = chip.dataset.value;
    state.filters.albumId = state.filters.albumId === value ? null : value;
    syncFilterChips();
    render();
  });

  // 标签 chips（多选，「与」关系）
  el.tagFilter.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-chip="tag"]');
    if (!chip) return;
    const value = chip.dataset.value;
    const set = new Set(state.filters.tags);
    if (set.has(value)) set.delete(value);
    else set.add(value);
    state.filters.tags = [...set];
    syncFilterChips();
    render();
  });

  // 点击相册卡片 → 直接筛选该相册
  el.albums.addEventListener('click', (event) => {
    const card = event.target.closest('[data-album]');
    if (!card) return;
    state.filters.albumId = card.dataset.album;
    syncFilterChips();
    render();
    document.getElementById('gallery')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // 作品网格（事件委托）→ 打开灯箱
  el.gallery.addEventListener('click', (event) => {
    const id = resolveCardId(event);
    if (!id) return;
    const at = state.visible.findIndex((photo) => photo.id === id);
    if (at >= 0) lightbox.open(state.visible, at);
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

  const { albums, items, errors } = await loadData();

  if (errors.length) {
    el.gallery.innerHTML = '';
    el.empty.hidden = false;
    el.empty.innerHTML = `<span class="empty-state__glyph">!</span>${errors
      .map(escapeHtml)
      .join('<br />')}<br />请通过本地服务器访问（<code>python serve.py</code>）。`;
    return;
  }

  state.albums = albums;
  // 把相册名称挂到条目上，卡片角标即可直接展示中文名
  const albumNames = new Map(albums.map((album) => [album.id, album.name]));
  state.items = items.map((item) => ({
    ...item,
    albumName: albumNames.get(item.albumId) || item.albumId,
  }));
  state.tags = collectTags(state.items);
  state.typeCounts = countByType(state.items);

  el.stats.photos.textContent = String(state.items.length);
  el.stats.albums.textContent = String(albums.length);
  el.stats.tags.textContent = String(state.tags.length);

  renderAlbums(el.albums, albums, countByAlbum(state.items));
  renderFilters();
  render();
}

bootstrap();
