/**
 * gallery.js —— 作品网格 / 相册总览 / 筛选 chips 的渲染
 *
 * 只把 API 已经算好的字段拼成 HTML，并同步选中态；
 * 不含筛选、排序、计数、格式化等任何业务逻辑。
 */

import { escapeHtml } from './data.js';

const VIDEO = 'video';

/* ---------- 作品网格 ---------- */

/** 媒体区：服务端给的封面 / 缩略图地址；为空时用占位块。 */
function mediaMarkup(item) {
  if (!item.imageUrl) return '<span class="card__placeholder" aria-hidden="true"></span>';
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
  return `<span class="card__flags">${flags
    .map((text) => `<span class="card__flag">${escapeHtml(text)}</span>`)
    .join('')}</span>`;
}

function tagsMarkup(tags) {
  if (!tags || !tags.length) return '';
  return `<span class="card__tags">${tags
    .slice(0, 4)
    .map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`)
    .join('')}</span>`;
}

function cardMarkup(item) {
  const isVideo = item.kind === VIDEO;
  const subtitle = item.subtitle
    ? `<span class="card__meta">${escapeHtml(item.subtitle)}</span>`
    : '';
  const summary = item.exifSummary
    ? `<span class="card__meta">${escapeHtml(item.exifSummary)}</span>`
    : '';
  const date = item.dateText
    ? `<span class="card__meta card__meta--date">${escapeHtml(item.dateText)}</span>`
    : '';

  // 外链视频站内不播放：整张卡片就是一个指向原站观看页的链接，点了新标签打开。
  // index.html 的 CSP 里 frame-src 还留着，但页面已经不再嵌任何 iframe。
  const link = isVideo && item.watchUrl
    ? `<a class="card__trigger" href="${escapeHtml(item.watchUrl)}"
          target="_blank" rel="noopener noreferrer"
          aria-label="在${escapeHtml(item.providerLabel || '原站')}打开《${escapeHtml(item.title)}》">`
    : `<button class="card__trigger" type="button" data-open="${escapeHtml(item.id)}"
          aria-label="${isVideo ? '播放' : '查看'}《${escapeHtml(item.title)}》">`;
  const closer = isVideo && item.watchUrl ? '</a>' : '</button>';
  const mediaInside = isVideo && item.watchUrl
    ? '<span class="play-badge play-badge--out" aria-hidden="true"></span>'
    : (isVideo ? '<span class="play-badge" aria-hidden="true"></span>' : '');

  return `
    <article class="card${isVideo ? ' card--video' : ''}" role="listitem" data-id="${escapeHtml(item.id)}">
      ${link}
        <div class="card__media">
          ${mediaMarkup(item)}
          ${item.albumName ? `<span class="card__badge">${escapeHtml(item.albumName)}</span>` : ''}
          ${mediaInside}
          ${isVideo && item.durationText ? `<span class="card__duration">${escapeHtml(item.durationText)}</span>` : ''}
          ${flagsMarkup(item)}
        </div>
        <div class="card__body">
          <span class="card__title">${escapeHtml(item.title)}</span>
          ${subtitle}
          ${summary}
          ${date}
          ${tagsMarkup(item.tags)}
          ${isVideo && item.watchUrl ? `<span class="card__meta card__meta--out">${escapeHtml(item.providerLabel || '原站')}打开 ↗</span>` : ''}
        </div>
      ${closer}
    </article>`;
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
 * 渲染作品网格（事件委托由 app.js 统一绑定）。
 * @param {HTMLElement} container
 * @param {object[]} items 服务端返回的作品条目
 * @param {{append?: boolean}} [options] append=true 时追加（分页「加载更多」）
 */
export function renderCards(container, items, options) {
  const html = items.map(cardMarkup).join('');
  if (options && options.append) container.insertAdjacentHTML('beforeend', html);
  else container.innerHTML = html;
  attachLazyFade(container);
}

/** 从点击事件中解析出媒体 id，若无则返回 null */
export function resolveCardId(event) {
  const trigger = event.target.closest('[data-open]');
  return trigger ? trigger.dataset.open : null;
}

/** 加载中的骨架屏 */
export function renderSkeleton(container, count = 6) {
  const template = document.getElementById('skeleton-template');
  if (!template) return;
  container.innerHTML = Array.from({ length: count }, () => template.innerHTML).join('');
}

/* ---------- 相册总览 ---------- */

function albumMarkup(album) {
  const cover = album.cover && !album.coverMissing
    ? `<img src="${escapeHtml(album.cover)}" alt="" loading="lazy" decoding="async" />`
    : '<span class="album-card__placeholder" aria-hidden="true"></span>';
  return `
    <button class="album-card" type="button" data-album="${escapeHtml(album.id)}"
            aria-pressed="false" title="${escapeHtml(album.description)}">
      <span class="album-card__media">${cover}</span>
      <span class="album-card__body">
        <span class="album-card__name">${escapeHtml(album.name)}</span>
        <span class="album-card__meta">${album.count} 项作品</span>
      </span>
    </button>`;
}

/** @param {object[]} albums 服务端 albums 卡片（id / name / cover / coverMissing / count） */
export function renderAlbums(container, albums) {
  container.innerHTML = albums.map(albumMarkup).join('');
}

/** 同步相册卡片的选中态 */
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
  return shown >= total ? `共 ${total} 项作品` : `已显示 ${shown} / ${total} 项作品`;
}
