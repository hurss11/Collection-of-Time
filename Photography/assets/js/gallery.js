/**
 * gallery.js —— 作品网格与相册卡片的渲染
 *
 * 只负责「渲染 DOM + 绑定回调」，不持有状态（状态由 app.js 管理）。
 */

import { escapeHtml, MEDIA_TYPE } from './data.js';

/** 照片：相机 + 曝光参数摘要 */
function photoSummary(item) {
  const e = item.exif || {};
  return [e.camera, e.focalLength, e.aperture, e.shutter, e.iso].filter(Boolean).join(' · ');
}

/** 视频：来源 + 分辨率 + 帧率摘要 */
function videoSummary(item) {
  const e = item.exif || {};
  return [item.providerLabel, item.resolution, e.fps].filter(Boolean).join(' · ');
}

function summaryOf(item) {
  return item.type === MEDIA_TYPE.VIDEO ? videoSummary(item) : photoSummary(item);
}

/**
 * 媒体区域：照片用 <img>，视频优先用封面图；
 * 没有封面图的本地视频交给 poster.js 抓帧（先渲染 <video> 占位）。
 */
function mediaMarkup(item) {
  const isVideo = item.type === MEDIA_TYPE.VIDEO;

  if (isVideo && !item.poster && item.src) {
    return `<video class="card__video" data-poster-src="${escapeHtml(item.src)}"
                   data-poster-time="${item.posterTime}" muted playsinline preload="none"
                   aria-hidden="true"></video>`;
  }

  const source = isVideo ? item.poster || item.src : item.thumb;
  if (!source) return '<span class="card__placeholder" aria-hidden="true"></span>';

  return `<img src="${escapeHtml(source)}" alt="${escapeHtml(item.title)}"
               loading="lazy" decoding="async" data-lazy />`;
}

function cardMarkup(item) {
  const isVideo = item.type === MEDIA_TYPE.VIDEO;
  const summary = summaryOf(item);
  const meta = [item.location, item.date].filter(Boolean).join(' · ');

  return `
    <article class="card${isVideo ? ' card--video' : ''}" role="listitem" data-id="${escapeHtml(item.id)}">
      <button class="card__trigger" type="button" data-open="${escapeHtml(item.id)}"
              aria-label="${isVideo ? '播放' : '查看'}《${escapeHtml(item.title)}》">
        <div class="card__media">
          ${mediaMarkup(item)}
          <span class="card__badge">${escapeHtml(item.albumName || item.albumId)}</span>
          ${isVideo ? '<span class="play-badge" aria-hidden="true"></span>' : ''}
          ${isVideo && item.durationText ? `<span class="card__duration">${escapeHtml(item.durationText)}</span>` : ''}
        </div>
        <div class="card__body">
          <span class="card__title">${escapeHtml(item.title)}</span>
          ${meta ? `<span class="card__meta">${escapeHtml(meta)}</span>` : ''}
          ${summary ? `<span class="card__meta">${escapeHtml(summary)}</span>` : ''}
          ${
            item.tags.length
              ? `<span class="card__tags">${item.tags
                  .slice(0, 4)
                  .map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`)
                  .join('')}</span>`
              : ''
          }
        </div>
      </button>
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
 * 渲染媒体网格（事件委托由 app.js 统一绑定，避免重复注册）。
 * @param {HTMLElement} container
 * @param {object[]} items 照片与视频的混合数组
 * @returns {HTMLVideoElement[]} 需要抓取封面的视频元素，交给 poster.js 处理
 */
export function renderGallery(container, items) {
  container.innerHTML = items.map(cardMarkup).join('');
  attachLazyFade(container);
  return [...container.querySelectorAll('video[data-poster-src]')];
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

/**
 * 渲染相册总览。
 * @param {HTMLElement} container
 * @param {object[]} albums
 * @param {Map<string, number>} counts
 * @param {(albumId: string) => void} onSelect
 */
export function renderAlbums(container, albums, counts, onSelect) {
  if (!albums.length) {
    container.innerHTML = '';
    return;
  }

  container.innerHTML = albums
    .map((album) => {
      const total = counts.get(album.id) || 0;
      return `
        <button class="album-card" type="button" data-album="${escapeHtml(album.id)}"
                aria-pressed="false" title="${escapeHtml(album.description)}">
          <span class="album-card__media">
            ${album.cover ? `<img src="${escapeHtml(album.cover)}" alt="" loading="lazy" decoding="async" />` : ''}
          </span>
          <span class="album-card__body">
            <span class="album-card__name">${escapeHtml(album.name)}</span>
            <span class="album-card__meta">${total} 项作品</span>
          </span>
        </button>`;
    })
    .join('');

  container.addEventListener('click', (event) => {
    const card = event.target.closest('[data-album]');
    if (card) onSelect(card.dataset.album);
  });
}

/** 同步相册卡片与筛选 chips 的选中态 */
export function syncSelected(container, activeId) {
  container.querySelectorAll('[data-album]').forEach((el) => {
    el.setAttribute('aria-pressed', String(el.dataset.album === activeId));
  });
}

/** 更新结果计数文案（照片与视频混合计数） */
export function renderCount(el, shown, total) {
  el.textContent = shown === total ? `共 ${total} 项作品` : `匹配 ${shown} / ${total} 项作品`;
}
