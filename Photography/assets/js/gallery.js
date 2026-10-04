/**
 * gallery.js —— 作品流 / 系列专题块 / 筛选 chips 的渲染
 *
 * 只把 API 已经算好的字段拼成 HTML，并同步选中态；
 * 不含筛选、排序、计数、格式化等任何业务逻辑。
 *
 * 作品流的行结构由 justify.js 决定，这里只保证每个 .shot 带上：
 *   data-ar  —— 宽高比（服务端按原始尺寸算好，用于分行）
 *   data-id / [data-open] —— 灯箱与封面补丁的挂点
 */

import { escapeHtml } from './data.js';

const VIDEO = 'video';
const FALLBACK_AR = 1.5;

/* ---------- 作品流 ---------- */

/** 宽高比：优先用服务端的原始像素数，取不到就 3:2 */
function aspectOf(item) {
  const width = Number(item.width) || 0;
  const height = Number(item.height) || 0;
  if (width > 0 && height > 0) return width / height;
  return FALLBACK_AR;
}

/** 媒体区：服务端给的封面 / 缩略图地址；为空时用占位块。 */
function mediaMarkup(item) {
  if (!item.imageUrl) return '<span class="shot__placeholder" aria-hidden="true"></span>';
  return `<img src="${escapeHtml(item.imageUrl)}" alt="${escapeHtml(item.title)}"
               loading="lazy" decoding="async" data-lazy />`;
}

/** 服务端标记的缺失角标（封面缺失 / 视频文件缺失 / 外链已失效） */
function flagsMarkup(item) {
  const flags = [];
  if (item.imageMissing) flags.push('图缺失');
  if (item.kind === VIDEO && item.srcMissing) flags.push('文件缺失');
  if (item.kind === VIDEO && item.linkStatus === 'gone') flags.push('链接已失效');
  if (!flags.length) return '';
  return `<span class="shot__flags">${flags
    .map((text) => `<span class="shot__flag">${escapeHtml(text)}</span>`)
    .join('')}</span>`;
}

/** 图注第二行：地点 + 拍摄参数，等宽小字 */
function exifLine(item) {
  return [item.location, item.exifSummary].filter(Boolean).join(' · ');
}

/**
 * 一件作品：一个盒子（比例 = 照片比例，所以不裁切）+ 两行图注。
 * @param {object} item 服务端条目
 * @param {number} number 序号，从 1 开始，跨分页连续
 */
function shotMarkup(item, number) {
  const isVideo = item.kind === VIDEO;
  // 外链视频站内不播放：整块就是一个指向原站观看页的链接，点了新标签打开。
  const external = isVideo && item.watchUrl;

  const trigger = external
    ? `<a class="shot__trigger" href="${escapeHtml(item.watchUrl)}"
          target="_blank" rel="noopener noreferrer"
          aria-label="在${escapeHtml(item.providerLabel || '原站')}打开《${escapeHtml(item.title)}》">`
    : `<button class="shot__trigger" type="button" data-open="${escapeHtml(item.id)}"
          aria-label="${isVideo ? '播放' : '查看'}《${escapeHtml(item.title)}》">`;
  const closer = external ? '</a>' : '</button>';

  const play = isVideo
    ? `<span class="shot__play${external ? ' shot__play--out' : ''}" aria-hidden="true"></span>`
    : '';
  const album = item.albumName
    ? `<span class="shot__album">${escapeHtml(item.albumName)}</span>`
    : '';
  const duration = isVideo && item.durationText
    ? `<span class="shot__duration">${escapeHtml(item.durationText)}</span>`
    : '';
  const exif = exifLine(item);

  return `
    <figure class="shot" role="listitem" data-id="${escapeHtml(item.id)}" data-ar="${aspectOf(item).toFixed(4)}">
      ${trigger}
        <span class="shot__frame">
          ${mediaMarkup(item)}
          ${album}
          ${play}
          ${duration}
          ${flagsMarkup(item)}
        </span>
      ${closer}
      <figcaption class="shot__cap">
        <span class="shot__title"><span class="shot__no">${String(number).padStart(2, '0')}</span>${escapeHtml(item.title)}</span>
        ${exif ? `<span class="shot__exif">${escapeHtml(exif)}</span>` : ''}
      </figcaption>
    </figure>`;
}

/** 图片加载完成后淡入，避免「白块」闪烁 */
function attachLazyFade(root) {
  root.querySelectorAll('img[data-lazy]').forEach((img) => {
    const done = () => img.classList.add('is-loaded');
    if (img.complete) done();
    else {
      img.addEventListener('load', done, { once: true });
      img.addEventListener('error', done, { once: true });
    }
  });
}

/**
 * 渲染作品流（事件委托由 app.js 统一绑定）。
 * 序号从容器里已有的作品数接着往下排，所以「加载更多」不会从 01 重来。
 * @param {HTMLElement} container
 * @param {object[]} items 服务端返回的作品条目
 * @param {{append?: boolean}} [options] append=true 时追加（分页「加载更多」）
 */
export function renderCards(container, items, options) {
  const append = Boolean(options && options.append);
  const start = append ? container.querySelectorAll('.shot').length : 0;
  const html = items.map((item, index) => shotMarkup(item, start + index + 1)).join('');

  if (append) container.insertAdjacentHTML('beforeend', html);
  else container.innerHTML = html;
  attachLazyFade(container);
}

/** 从点击事件中解析出媒体 id，若无则返回 null */
export function resolveCardId(event) {
  const trigger = event.target.closest('[data-open]');
  return trigger ? trigger.dataset.open : null;
}

/**
 * 补上封面后同步作品：把占位块换成真图。
 *
 * 用在「本地视频第一次播放时抓的那一帧」（见 cover.js）：封面是灯箱里补的，
 * 作品流上的占位块得跟着换掉，否则要刷新页面才看得到。
 * @returns {boolean} 是否真的换掉了
 */
export function patchCardCover(container, item, url) {
  if (!container || !item || !url) return false;

  const shot = [...container.querySelectorAll('.shot[data-id]')]
    .find((node) => node.dataset.id === String(item.id));
  const placeholder = shot?.querySelector('.shot__frame .shot__placeholder');
  if (!placeholder) return false;

  placeholder.insertAdjacentHTML('afterend', mediaMarkup({ imageUrl: url, title: item.title }));
  placeholder.remove();
  attachLazyFade(placeholder.parentElement || shot);
  return true;
}

/** 加载中的骨架屏：走同一套行布局，所以不会先跳一下再变成作品 */
export function renderSkeleton(container, count = 6) {
  const template = document.getElementById('skeleton-template');
  if (!template) return;
  container.innerHTML = Array.from({ length: count }, () => template.innerHTML).join('');
}

/* ---------- 系列专题块 ---------- */

function stripMarkup(album, index) {
  const cover = album.cover && !album.coverMissing
    ? `<img src="${escapeHtml(album.cover)}" alt="" loading="lazy" decoding="async" />`
    : '<span class="strip__placeholder" aria-hidden="true"></span>';
  const description = album.description
    ? `<span class="strip__desc">${escapeHtml(album.description)}</span>`
    : '';

  return `
    <button class="strip" type="button" data-album="${escapeHtml(album.id)}"
            aria-pressed="false" title="${escapeHtml(album.description)}">
      <span class="strip__media">${cover}</span>
      <span class="strip__body">
        <span class="strip__name">${escapeHtml(album.name)}</span>
        ${description}
        <span class="strip__meta">系列 ${String(index + 1).padStart(2, '0')} · ${album.count} 项作品</span>
      </span>
    </button>`;
}

/** @param {object[]} albums 服务端 albums 卡片（id / name / cover / coverMissing / count / description） */
export function renderAlbums(container, albums) {
  container.innerHTML = albums.map(stripMarkup).join('');
}

/** 同步系列专题块的选中态 */
export function syncAlbums(container, albumId) {
  container.querySelectorAll('[data-album]').forEach((node) => {
    node.setAttribute('aria-pressed', String(node.dataset.album === albumId));
  });
}

/* ---------- 筛选 chips ---------- */

function chipMarkup(role, chip) {
  const prefix = chip.prefix || '';
  return `<button class="chip" type="button" data-chip="${role}" data-value="${escapeHtml(chip.value)}"
                  aria-pressed="${chip.pressed ? 'true' : 'false'}">${prefix}${escapeHtml(chip.label)}<span class="chip__count">${chip.count}</span></button>`;
}

/**
 * 用服务端 facets / types 重建某一组 chips（保留分组标题）。
 * @param {HTMLElement} container 含 .chipgroup__label 与 chips 的容器
 * @param {string} role 'type' | 'album' | 'tag'
 * @param {object[]} chips [{ value, label, count, prefix?, pressed? }]
 */
export function renderChips(container, role, chips) {
  container.querySelectorAll('.chip').forEach((node) => node.remove());
  container.insertAdjacentHTML('beforeend', chips.map((chip) => chipMarkup(role, chip)).join(''));
}

/** 用服务端回显的 active 同步 chips 选中态 */
export function syncChips(containers, active) {
  const tags = (active && active.tags) || [];
  containers.type.querySelectorAll('[data-chip="type"]').forEach((chip) => {
    chip.setAttribute('aria-pressed', String(chip.dataset.value === (active && active.type)));
  });
  containers.album.querySelectorAll('[data-chip="album"]').forEach((chip) => {
    chip.setAttribute('aria-pressed', String(chip.dataset.value === (active && active.album)));
  });
  containers.tag.querySelectorAll('[data-chip="tag"]').forEach((chip) => {
    chip.setAttribute('aria-pressed', String(tags.includes(chip.dataset.value)));
  });
}

/** 结果计数文案：数字全部来自服务端 meta */
export function renderCountText(shown, total) {
  return shown >= total ? `共 ${total} 项` : `已显示 ${shown} / ${total} 项`;
}
