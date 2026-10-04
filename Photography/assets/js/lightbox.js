/**
 * lightbox.js —— 大图 / 视频查看器
 *
 * 两种媒体呈现方式全部由服务端字段决定：
 *   kind=photo           → <img src="srcUrl || imageUrl">
 *   kind=video & 本地文件 → <video src="srcUrl" poster="imageUrl">
 *
 * 外链视频**不进灯箱**：站内只保存链接、封面与「资源是否还在」的结果，
 * 播放交给原站（卡片本身就是指向原站观看页的链接，见 gallery.js）。
 *
 * 只做「打开 / 关闭 / 上下张 / 键盘 / 焦点 / 缩放平移 / 参数渲染」，不做任何解析与格式化。
 * 唯一一处副作用：没有封面的本地视频在第一次播放时，会抓一帧交给后端补封面
 * （`captureCoverAt` → `cover.js`；是否真的写入由那里判断，失败静默）。
 */

import { escapeHtml } from './data.js';
import { readExifRows } from './exif.js';
import { captureCoverFrame } from './cover.js';

const VIDEO = 'video';

/** 播放到这个秒数再抓封面：第 0 秒常常是淡入 / 全黑，抓下来等于没有 */
const COVER_AT_SECONDS = 1;
/** 短片兜底：暂停或播完时只要画面动过，就用当时那一帧 */
const COVER_MIN_SECONDS = 0.15;

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
 * @param {{onCoverCaptured?: (item: object, url: string) => void}} [options]
 *        onCoverCaptured：本地视频第一次播放时抓到封面并已存进后端，回调里可以把网格上的
 *        占位块换成这张图（见 app.js → gallery.js 的 patchCardCover）。
 * @returns {{ open: (items: object[], index: number) => void, close: () => void }}
 */
export function createLightbox(root, options = {}) {
  const onCoverCaptured = typeof options.onCoverCaptured === 'function'
    ? options.onCoverCaptured
    : null;
  const imageEl = root.querySelector('#lb-image');
  const videoEl = root.querySelector('#lb-video');
  const titleEl = root.querySelector('#lb-title');
  const subtitleEl = root.querySelector('#lb-subtitle');
  const descEl = root.querySelector('#lb-description');
  const metaEl = root.querySelector('#lb-meta');
  const statusEl = root.querySelector('#lb-status');
  const exifEl = root.querySelector('#lb-exif');
  const counterEl = root.querySelector('#lb-counter');
  const parseBtn = root.querySelector('#lb-exif-parse');
  const stageEl = root.querySelector('.lightbox__stage');
  const viewportEl = root.querySelector('.lightbox__viewport');
  const zoomEl = root.querySelector('#lb-zoom');
  const zoomLevelBtn = root.querySelector('[data-lb-zoom-reset]');
  const zoomOutBtn = root.querySelector('[data-lb-zoom-out]');
  const zoomInBtn = root.querySelector('[data-lb-zoom-in]');
  const vbarEl = root.querySelector('#lb-vbar');
  const vseekEl = root.querySelector('#lb-vseek');
  const vtimeEl = root.querySelector('#lb-vtime');
  const vplayBtn = root.querySelector('[data-lb-vplay]');
  const vfitBtn = root.querySelector('[data-lb-vfit]');

  /** @type {object[]} */ let items = [];
  let index = 0;
  let lastFocused = null;

  /** 「没封面 + 本地视频」的条目 id：播放到一定进度就抓一帧补封面（见 cover.js） */
  let coverCaptureId = null;
  /** 本页已经试过的条目：同一条只试一次，反复打开不重复上传 */
  const coverAttempted = new Set();

  /* ---------- 缩放状态（scale = 1 表示「适应窗口」） ---------- */
  const MIN_SCALE = 0.25;
  const MAX_SCALE = 8;
  const ZOOM_STEP = 1.4;
  const NATIVE_CONTROLS_PX = 48; // 视频底部原生控件条高度（元素坐标，会随缩放放大）
  let scale = 1;
  let tx = 0;
  let ty = 0;
  let suppressClick = false;
  let wheelTimer = 0;
  let seeking = false;

  const isOpen = () => !root.hidden;
  const current = () => items[index];

  function setStatus(html) {
    if (!statusEl) return;
    statusEl.hidden = !html;
    statusEl.innerHTML = html || '';
  }

  /** 停止并卸载播放器，避免关闭后仍在后台播放 */
  function resetMedia() {
    coverCaptureId = null;
    videoEl.pause();
    videoEl.removeAttribute('src');
    videoEl.load();
  }

  /** 切换当前媒体的呈现方式（字段全部来自服务端） */
  function showMedia(item) {
    const isVideo = item.kind === VIDEO;

    imageEl.hidden = isVideo;
    videoEl.hidden = !isVideo;
    stageEl.classList.toggle('is-video', isVideo);

    // 站内能播、又还没有封面的本地视频：播放一会儿就顺手抓一帧当封面
    coverCaptureId = isVideo && !item.isEmbed && !item.imageUrl && item.srcUrl
      && !coverAttempted.has(item.id)
      ? item.id
      : null;

    if (!isVideo) {
      imageEl.src = item.srcUrl || item.imageUrl;
      imageEl.alt = item.title || '';
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

  /**
   * 没封面的本地视频：播放到一定进度就抓当前帧，交给后端存成封面。
   *
   * 服务端抓帧要 ffmpeg（缺 ffmpeg 的部署上，封面此前只能靠人工上传），而浏览器
   * 既然能播就一定能解码 —— 抓到的还是用户真正看到的那一帧。是否真的写入由
   * cover.js 决定（只在浏览器带管理员会话时才做），这里只挑时机：
   * 「进度」用第 1 秒（避开淡入 / 黑场），「短片」用暂停或播完时的那一帧兜底。
   */
  async function captureCoverAt(reason) {
    const item = current();
    if (!coverCaptureId || !item || item.id !== coverCaptureId) return;

    const at = videoEl.currentTime;
    const threshold = reason === 'progress' ? COVER_AT_SECONDS : COVER_MIN_SECONDS;
    if (!(at >= threshold)) return;

    coverCaptureId = null; // 一次机会，成败都不再重复
    coverAttempted.add(item.id);

    const url = await captureCoverFrame(videoEl, item);
    if (!url) return;

    item.imageUrl = url; // 同页再打开这条时不用再靠 `#t=0.1` 顶替封面
    if (isOpen() && current() === item) videoEl.setAttribute('poster', url);
    if (onCoverCaptured) onCoverCaptured(item, url);
  }

  /* ---------- 缩放 / 平移 ---------- */

  /** 当前可缩放的媒体；外链 iframe（跨域）不参与缩放 */
  function activeMedia() {
    if (!videoEl.hidden) return videoEl;
    if (!imageEl.hidden) return imageEl;
    return null;
  }

  /** 「适应窗口」相当于原始尺寸的多少倍；拿不到原始尺寸时返回 0 */
  function fitScale() {
    const el = activeMedia();
    if (!el) return 0;
    const natural = el instanceof HTMLVideoElement ? el.videoWidth : el.naturalWidth;
    return natural > 0 && el.offsetWidth > 0 ? el.offsetWidth / natural : 0;
  }

  /** 平移量不超过「放大后溢出的部分」，媒体不会被拖出画面 */
  function clampOffsets() {
    const el = activeMedia();
    if (!el) return;
    const box = viewportEl.getBoundingClientRect();
    const maxX = Math.max(0, (el.offsetWidth * scale - box.width) / 2);
    const maxY = Math.max(0, (el.offsetHeight * scale - box.height) / 2);
    tx = Math.min(maxX, Math.max(-maxX, tx));
    ty = Math.min(maxY, Math.max(-maxY, ty));
  }

  function applyZoom() {
    const el = activeMedia();
    if (!el) return;
    clampOffsets();
    const idle = Math.abs(scale - 1) < 1e-3 && Math.abs(tx) < 0.5 && Math.abs(ty) < 0.5;
    el.style.transform = idle
      ? ''
      : `translate3d(${tx.toFixed(1)}px, ${ty.toFixed(1)}px, 0) scale(${scale.toFixed(4)})`;
    stageEl.classList.toggle('is-zoomed', scale > 1.001);
    syncPlaybackBar();
    updateZoomLabel();
  }

  function updateZoomLabel() {
    const fit = fitScale();
    if (zoomLevelBtn) {
      zoomLevelBtn.textContent =
        Math.abs(scale - 1) < 1e-3
          ? '适应'
          : `${Math.round((fit > 0 ? fit * scale : scale) * 100)}%`;
    }
    if (zoomOutBtn) zoomOutBtn.disabled = scale <= MIN_SCALE + 1e-3;
    if (zoomInBtn) zoomInBtn.disabled = scale >= MAX_SCALE - 1e-3;
  }

  /** 视频的原生控件条贴在画面底部：缩放以「画面底部中心」为不动点，
      这样放大后进度条仍在可视区里（否则放大就等于把控件推出屏幕）。 */
  function defaultAnchor() {
    if (videoEl.hidden) return null;
    const rect = videoEl.getBoundingClientRect();
    const box = viewportEl.getBoundingClientRect();
    return { x: box.left + box.width / 2, y: rect.bottom };
  }

  /** 以 anchor（客户端坐标）为不动点缩放；照片默认以画面中心为不动点 */
  function setZoom(next, anchor) {
    if (!activeMedia()) return;
    const target = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next));
    if (Math.abs(target - scale) < 1e-6) return;

    const box = viewportEl.getBoundingClientRect();
    const point = anchor ?? defaultAnchor() ?? { x: box.left + box.width / 2, y: box.top + box.height / 2 };
    const ax = point.x - (box.left + box.width / 2);
    const ay = point.y - (box.top + box.height / 2);
    const k = target / scale;
    tx = ax - k * (ax - tx);
    ty = ay - k * (ay - ty);
    scale = target;
    applyZoom();
  }

  function resetZoom() {
    scale = 1;
    tx = 0;
    ty = 0;
    suppressClick = false;
    imageEl.style.transform = '';
    videoEl.style.transform = '';
    stageEl.classList.remove('is-zoomed', 'is-panning');
    if (vbarEl) vbarEl.hidden = true;
    videoEl.controls = true;
    updateZoomLabel();
  }

  /** 适应窗口 ⇄ 原始像素 1:1；原始尺寸拿不到时退化成 2 倍 */
  function toggleZoom() {
    if (Math.abs(scale - 1) > 1e-3) {
      resetZoom();
      return;
    }
    const fit = fitScale();
    setZoom(fit > 0 && fit < 0.999 ? 1 / fit : 2);
  }

  /** 缩放控件只在照片 / 本地视频时显示 */
  function syncZoomUI() {
    if (!zoomEl) return;
    zoomEl.hidden = !activeMedia();
    updateZoomLabel();
  }

  /** 视频底部是浏览器原生控件条，那里的指针事件留给播放器 */
  function onNativeControls(event) {
    if (videoEl.hidden) return false;
    const rect = videoEl.getBoundingClientRect();
    return event.clientY > rect.bottom - NATIVE_CONTROLS_PX * scale;
  }

  /* ---------- 放大态的播放条 ---------- */

  /** 秒 → m:ss（超过一小时补小时） */
  function clockText(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const mm = h ? String(m).padStart(2, '0') : String(m);
    return h ? `${h}:${mm}:${String(s).padStart(2, '0')}` : `${mm}:${String(s).padStart(2, '0')}`;
  }

  const videoDuration = () => (Number.isFinite(videoEl.duration) ? videoEl.duration : 0);

  function syncVBar() {
    if (!vbarEl || vbarEl.hidden) return;
    const duration = videoDuration();
    if (!seeking && vseekEl) {
      vseekEl.value = duration ? String(Math.round((videoEl.currentTime / duration) * 1000)) : '0';
    }
    if (vtimeEl) {
      vtimeEl.textContent = `${clockText(videoEl.currentTime)} / ${clockText(duration)}`;
    }
  }

  function syncPlayButton() {
    if (!vplayBtn) return;
    vplayBtn.textContent = videoEl.paused ? '▶' : '▮▮';
    vplayBtn.setAttribute('aria-label', videoEl.paused ? '播放' : '暂停');
  }

  /** 视频放大后原生控件条会被放大 / 推出可视区，改用独立控制条 */
  function syncPlaybackBar() {
    if (!vbarEl) return;
    const zoomed = scale > 1.001 && !videoEl.hidden;
    vbarEl.hidden = !zoomed;
    if (!videoEl.hidden) videoEl.controls = !zoomed;
    if (zoomed) {
      syncVBar();
      syncPlayButton();
    }
  }

  function render() {
    const item = current();
    if (!item) return;

    setStatus('');
    resetMedia();
    resetZoom();
    showMedia(item);
    syncZoomUI();

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
    resetZoom();
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
  viewportEl?.addEventListener('click', (event) => {
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    if (current()?.kind === VIDEO) return;
    if (scale > 1.001) return; // 放大后左右两区留给平移，翻页交给箭头 / 键盘
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
      '+': () => setZoom(scale * ZOOM_STEP),
      '=': () => setZoom(scale * ZOOM_STEP),
      '-': () => setZoom(scale / ZOOM_STEP),
      _: () => setZoom(scale / ZOOM_STEP),
      '0': () => resetZoom(),
    };
    const action = actions[event.key];
    if (action) {
      event.preventDefault();
      action();
    }
  });

  /* ---------- 缩放交互：按钮 / 滚轮 / 双击 / 拖拽 / 双指 ---------- */

  const pointers = new Map();
  let panStart = null; // { x, y, tx, ty }
  let pinchPrev = null; // { dist, mid }
  let dragDistance = 0;
  let pinched = false;

  /**
   * 只在「真的开始拖动」时才捕获指针：capture 会把后续的 click / dblclick
   * 一并重定向到容器，普通单击（点两侧翻页、双击缩放）就收不到正确 target 了。
   */
  function capturePointer(id) {
    try {
      viewportEl.setPointerCapture(id);
    } catch {
      /* 指针已经释放，忽略 */
    }
  }

  /** 手势进行中先关掉 transition，否则拖拽会「发飘」 */
  function markGesture() {
    stageEl.classList.add('is-panning');
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => {
      wheelTimer = 0;
      if (!pointers.size) stageEl.classList.remove('is-panning');
    }, 160);
  }

  zoomInBtn?.addEventListener('click', () => setZoom(scale * ZOOM_STEP));
  zoomOutBtn?.addEventListener('click', () => setZoom(scale / ZOOM_STEP));
  zoomLevelBtn?.addEventListener('click', () => toggleZoom());

  // 放大态播放条
  vplayBtn?.addEventListener('click', () => {
    if (videoEl.paused) videoEl.play();
    else videoEl.pause();
  });
  vfitBtn?.addEventListener('click', () => resetZoom());
  vseekEl?.addEventListener('pointerdown', () => {
    seeking = true;
  });
  vseekEl?.addEventListener('input', () => {
    const duration = videoDuration();
    if (duration) videoEl.currentTime = (Number(vseekEl.value) / 1000) * duration;
  });
  vseekEl?.addEventListener('change', () => {
    seeking = false;
  });
  videoEl.addEventListener('timeupdate', syncVBar);
  videoEl.addEventListener('durationchange', syncVBar);
  videoEl.addEventListener('play', syncPlayButton);
  videoEl.addEventListener('pause', syncPlayButton);

  // 顺手补封面（只在「没封面的本地视频 + 带管理员会话的浏览器」时才会真发请求）
  videoEl.addEventListener('timeupdate', () => captureCoverAt('progress'));
  videoEl.addEventListener('pause', () => captureCoverAt('settle'));
  videoEl.addEventListener('ended', () => captureCoverAt('settle'));

  viewportEl?.addEventListener('dblclick', (event) => {
    if (!activeMedia()) return;
    if (event.target !== imageEl && event.target !== videoEl) return;
    if (onNativeControls(event)) return;
    toggleZoom();
  });

  viewportEl?.addEventListener(
    'wheel',
    (event) => {
      if (!activeMedia()) return;
      event.preventDefault();
      markGesture();
      const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
      // 照片以光标为不动点；视频用默认锚点（底部中心），保住原生控件条
      const anchor = videoEl.hidden ? { x: event.clientX, y: event.clientY } : null;
      setZoom(scale * Math.pow(1.0015, -delta), anchor);
    },
    { passive: false },
  );

  viewportEl?.addEventListener('pointerdown', (event) => {
    if (!activeMedia()) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    if (event.target.closest?.('.lightbox__vbar')) return; // 播放条自己处理
    if (onNativeControls(event)) return; // 让原生控件条先拿到事件

    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (pointers.size === 1) {
      panStart = { x: event.clientX, y: event.clientY, tx, ty };
      pinchPrev = null;
      dragDistance = 0;
    } else {
      panStart = null;
      pinchPrev = null;
      pinched = true;
    }
  });

  /** 双指：按指距缩放，并让双指中点跟着走 */
  function pinchMove() {
    const [a, b] = [...pointers.values()];
    if (!a || !b) return;

    const dist = Math.hypot(a.x - b.x, a.y - b.y);
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    if (!pinchPrev || pinchPrev.dist < 8 || dist < 8) {
      pinchPrev = { dist, mid };
      return;
    }

    const box = viewportEl.getBoundingClientRect();
    const target = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale * (dist / pinchPrev.dist)));
    const k = target / scale; // 实际生效的倍数（可能被上限截断）
    const ax = mid.x - (box.left + box.width / 2);
    const ay = mid.y - (box.top + box.height / 2);

    tx = ax - k * (pinchPrev.mid.x - (box.left + box.width / 2) - tx);
    ty = ay - k * (pinchPrev.mid.y - (box.top + box.height / 2) - ty);
    scale = target;
    [...pointers.keys()].forEach(capturePointer);
    markGesture();
    applyZoom();
    pinchPrev = { dist, mid };
  }

  viewportEl?.addEventListener('pointermove', (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (pointers.size >= 2) {
      pinchMove();
      return;
    }
    if (!panStart || scale <= 1.001) return; // 没放大就不拖，免得抢掉「点两侧翻页」

    const dx = event.clientX - panStart.x;
    const dy = event.clientY - panStart.y;
    dragDistance = Math.max(dragDistance, Math.hypot(dx, dy));
    if (dragDistance > 2) markGesture();
    if (dragDistance > 4) capturePointer(event.pointerId);
    tx = panStart.tx + dx;
    ty = panStart.ty + dy;
    applyZoom();
  });

  function endPointer(event) {
    if (!pointers.has(event.pointerId)) return;
    pointers.delete(event.pointerId);
    if (viewportEl.hasPointerCapture?.(event.pointerId)) {
      viewportEl.releasePointerCapture(event.pointerId);
    }
    pinchPrev = null;

    if (pointers.size === 1) {
      const [rest] = [...pointers.values()];
      panStart = { x: rest.x, y: rest.y, tx, ty };
      dragDistance = 0;
      return;
    }
    if (pointers.size > 1) return;

    panStart = null;
    if (!wheelTimer) stageEl.classList.remove('is-panning');
    if (dragDistance > 4 || pinched) {
      // 拖完 / 捏完的那一下不应该被当成「点两侧翻页」
      suppressClick = true;
      setTimeout(() => {
        suppressClick = false;
      }, 0);
    }
    dragDistance = 0;
    pinched = false;
  }

  viewportEl?.addEventListener('pointerup', endPointer);
  viewportEl?.addEventListener('pointercancel', endPointer);

  // 图片 / 视频的原始尺寸是异步才知道的，知道了就刷新百分比
  imageEl.addEventListener('load', updateZoomLabel);
  videoEl.addEventListener('loadedmetadata', () => {
    updateZoomLabel();
    syncVBar();
    syncPlayButton();
  });

  window.addEventListener('resize', () => {
    if (!isOpen()) return;
    if (scale > 1.001) applyZoom();
    else updateZoomLabel();
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
