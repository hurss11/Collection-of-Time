/**
 * lightbox.js —— 大图 / 视频查看器
 *
 * 三种媒体呈现方式全部由服务端字段决定：
 *   kind=photo                → <img src="srcUrl || imageUrl">
 *   kind=video & isEmbed      → <iframe src="embedUrl">
 *   kind=video & 本地文件      → <video src="srcUrl" poster="imageUrl">
 *
 * 只做「打开 / 关闭 / 上下张 / 键盘 / 焦点 / 参数渲染」，不做任何解析与格式化。
 */

import { escapeHtml } from './data.js';
import { readExifRows } from './exif.js';

const VIDEO = 'video';

function exifRowsHtml(rows) {
  if (!rows || !rows.length) {
    return '<p class="exif__status">暂无拍摄参数，可尝试「解析原文件 EXIF」。</p>';
  }
  return rows
    .map(
      (row) => `
      <div class="exif__item">
        <span class="exif__key">${escapeHtml(row.label)}</span>
        <span class="exif__val" title="${escapeHtml(row.value)}">${escapeHtml(row.value)}</span>
      </div>`,
    )
    .join('');
}

/**
 * 创建灯箱实例。
 * @param {HTMLElement} root 灯箱根节点（含 [data-lb-*] 控件）
 * @returns {{ open: (items: object[], index: number) => void, close: () => void }}
 */
export function createLightbox(root) {
  const imageEl = root.querySelector('#lb-image');
  const videoEl = root.querySelector('#lb-video');
  const embedEl = root.querySelector('#lb-embed');
  const titleEl = root.querySelector('#lb-title');
  const subtitleEl = root.querySelector('#lb-subtitle');
  const descEl = root.querySelector('#lb-description');
  const metaEl = root.querySelector('#lb-meta');
  const statusEl = root.querySelector('#lb-status');
  const exifEl = root.querySelector('#lb-exif');
  const counterEl = root.querySelector('#lb-counter');
  const parseBtn = root.querySelector('#lb-exif-parse');
  const stageEl = root.querySelector('.lightbox__stage');

  /** @type {object[]} */ let items = [];
  let index = 0;
  let lastFocused = null;

  const isOpen = () => !root.hidden;
  const current = () => items[index];

  function setStatus(html) {
    if (!statusEl) return;
    statusEl.hidden = !html;
    statusEl.innerHTML = html || '';
  }

  /** 停止并卸载所有播放器，避免关闭后仍在后台播放 */
  function resetMedia() {
    videoEl.pause();
    videoEl.removeAttribute('src');
    videoEl.load();
    embedEl.removeAttribute('src');
  }

  /** 切换当前媒体的呈现方式（字段全部来自服务端） */
  function showMedia(item) {
    const isVideo = item.kind === VIDEO;
    const isEmbed = isVideo && Boolean(item.embedUrl);

    imageEl.hidden = isVideo;
    videoEl.hidden = !isVideo || isEmbed;
    embedEl.hidden = !isEmbed;
    stageEl.classList.toggle('is-video', isVideo);

    if (!isVideo) {
      imageEl.src = item.srcUrl || item.imageUrl;
      imageEl.alt = item.title || '';
      return;
    }

    if (isEmbed) {
      embedEl.src = item.embedUrl;
      embedEl.title = item.title || '';
      return;
    }

    videoEl.setAttribute('poster', item.imageUrl || '');
    if (item.srcUrl) {
      // 没有封面（后台没装 ffmpeg 时抓不了帧）就带 `#t=0.1` 让浏览器直接显示
      // 开头一帧：否则播放器是一块纯黑，用户看不到任何预览。
      videoEl.src = item.imageUrl ? item.srcUrl : `${item.srcUrl}#t=0.1`;
      return;
    }

    videoEl.hidden = true;
    setStatus(
      `未找到视频文件：<code>${escapeHtml(item.srcMissing ? '（文件缺失）' : '（未配置 src）')}</code>`,
    );
  }

  function render() {
    const item = current();
    if (!item) return;

    setStatus('');
    resetMedia();
    showMedia(item);

    titleEl.textContent = item.title || '';
    subtitleEl.textContent = item.subtitle || '';
    descEl.textContent = item.description || '';
    metaEl.textContent = item.dateText || '';
    counterEl.textContent = `${index + 1} / ${items.length}`;
    exifEl.innerHTML = exifRowsHtml(item.exif);

    // 「解析原文件 EXIF」只对照片有意义，且必须有可解析的路径
    if (parseBtn) {
      parseBtn.hidden = item.kind === VIDEO || !(item.srcUrl || item.imageUrl);
      parseBtn.disabled = false;
      parseBtn.textContent = '解析原文件 EXIF';
    }

    // 预加载相邻媒体，翻页更顺滑
    [items[index + 1], items[index - 1]].forEach((neighbor) => {
      if (!neighbor) return;
      const url = neighbor.srcUrl || neighbor.imageUrl;
      if (url) new Image().src = url;
    });
  }

  function go(step) {
    if (!items.length) return;
    index = (index + step + items.length) % items.length;
    render();
  }

  function open(list, startIndex) {
    items = list;
    index = Math.max(0, Math.min(startIndex, list.length - 1));
    lastFocused = document.activeElement;

    root.hidden = false;
    document.body.style.overflow = 'hidden';
    render();
    root.querySelector('.lightbox__close')?.focus();
  }

  function close() {
    if (!isOpen()) return;
    resetMedia();
    root.hidden = true;
    document.body.style.overflow = '';
    imageEl.removeAttribute('src');
    lastFocused?.focus?.();
  }

  /* ---------- 事件绑定 ---------- */

  root.querySelectorAll('[data-lb-close]').forEach((el) => el.addEventListener('click', close));
  root.querySelector('[data-lb-prev]')?.addEventListener('click', () => go(-1));
  root.querySelector('[data-lb-next]')?.addEventListener('click', () => go(1));

  // 本地视频文件缺失 / 解码失败 → 如实提示（地址来自服务端）
  videoEl.addEventListener('error', () => {
    const item = current();
    if (!isOpen() || !item || item.kind !== VIDEO || videoEl.hidden) return;
    const src = videoEl.getAttribute('src');
    if (!src || src !== item.srcUrl) return; // 卸载过程或上一张的残留事件，忽略
    setStatus(`视频无法播放：<code>${escapeHtml(src)}</code>`);
  });

  // 点击图片左右两侧区域翻页（视频区域留给播放器自身交互）
  root.querySelector('.lightbox__viewport')?.addEventListener('click', (event) => {
    if (current()?.kind === VIDEO) return;
    if (event.target !== imageEl) return;

    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    if (ratio < 0.28) go(-1);
    else if (ratio > 0.72) go(1);
  });

  // 按需解析原图 EXIF（服务端解析，只允许 assets/img/photos/ 下的文件）
  parseBtn?.addEventListener('click', async () => {
    const item = current();
    if (!item || item.kind === VIDEO) return;

    parseBtn.disabled = true;
    parseBtn.textContent = '解析中…';
    setStatus('');

    try {
      const rows = await readExifRows(item.srcUrl || item.imageUrl);
      if (!rows.length) throw new Error('未读取到可用的 EXIF 字段');
      exifEl.innerHTML = exifRowsHtml(rows);
      parseBtn.textContent = '已从原文件解析';
    } catch (error) {
      setStatus(`解析失败：${escapeHtml(error.message)}`);
      parseBtn.textContent = '重试解析';
      parseBtn.disabled = false;
    }
  });

  window.addEventListener('keydown', (event) => {
    if (!isOpen()) return;

    // Esc 任何时候都可用；方向键在播放器 / 表单控件获得焦点时让给控件自身
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }

    if (event.target.closest?.('video, iframe, input, select, textarea')) return;

    const actions = {
      ArrowLeft: () => go(-1),
      ArrowRight: () => go(1),
    };
    const action = actions[event.key];
    if (action) {
      event.preventDefault();
      action();
    }
  });

  // 简单焦点陷阱：Tab 在灯箱内部循环
  root.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const focusables = [...root.querySelectorAll('button:not([disabled]), [href]')];
    if (!focusables.length) return;

    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  return { open, close };
}
