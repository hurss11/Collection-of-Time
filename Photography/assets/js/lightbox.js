/**
 * lightbox.js —— 大图 / 视频查看器
 *
 * 支持三种媒体：
 *   photo  → <img>
 *   video  + provider 'file'  → <video controls>
 *   video  + 外链 provider    → <iframe> 嵌入播放器
 *
 * 功能：打开 / 关闭、上一张 / 下一张、键盘操作、参数面板、焦点管理。
 */

import { escapeHtml, MEDIA_TYPE } from './data.js';
import { readExifFromUrl, formatExif } from './exif.js';

/** 把照片 JSON 里的 exif 字段转成统一的展示列表 */
function photoExifItems(photo) {
  const e = photo.exif;
  if (!e) return [];

  return [
    ['相机', e.camera],
    ['镜头', e.lens],
    ['焦距', e.focalLength],
    ['光圈', e.aperture],
    ['快门', e.shutter],
    ['ISO', e.iso],
    ['拍摄时间', e.dateTimeOriginal || photo.date],
    ['尺寸', e.dimensions],
    ['地点', photo.location],
  ]
    .filter(([, value]) => Boolean(value))
    .map(([label, value]) => ({ key: label, label, value: String(value) }));
}

/** 视频参数列表（复用 EXIF 面板的渲染） */
function videoInfoItems(video) {
  const e = video.exif || {};

  return [
    ['来源', video.providerLabel],
    ['时长', video.durationText],
    ['分辨率', video.resolution],
    ['帧率', e.fps],
    ['编码', e.codec],
    ['设备', e.camera],
    ['镜头', e.lens],
    ['拍摄时间', video.date],
    ['地点', video.location],
  ]
    .filter(([, value]) => Boolean(value))
    .map(([label, value]) => ({ key: label, label, value: String(value) }));
}

function infoItems(item) {
  return item.type === MEDIA_TYPE.VIDEO ? videoInfoItems(item) : photoExifItems(item);
}

function exifHtml(items) {
  if (!items.length) {
    return '<p class="exif__status">暂无拍摄参数，可尝试「解析原文件 EXIF」。</p>';
  }

  return items
    .map(
      (item) => `
      <div class="exif__item">
        <span class="exif__key">${escapeHtml(item.label)}</span>
        <span class="exif__val" title="${escapeHtml(item.value)}">${escapeHtml(item.value)}</span>
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
  const exifEl = root.querySelector('#lb-exif');
  const counterEl = root.querySelector('#lb-counter');
  const parseBtn = root.querySelector('#lb-exif-parse');
  const stageEl = root.querySelector('.lightbox__stage');

  /** @type {object[]} */ let items = [];
  let index = 0;
  let lastFocused = null;

  const isOpen = () => !root.hidden;

  /** 停止并卸载所有播放器，避免关闭后仍在后台播放 */
  function resetMedia() {
    videoEl.pause();
    videoEl.removeAttribute('src');
    videoEl.load();
    embedEl.removeAttribute('src');
  }

  /** 切换当前媒体的呈现方式 */
  function showMedia(item) {
    const isVideo = item.type === MEDIA_TYPE.VIDEO;
    const isEmbed = isVideo && Boolean(item.embedUrl);

    imageEl.hidden = isVideo;
    videoEl.hidden = !isVideo || isEmbed;
    embedEl.hidden = !isEmbed;
    stageEl.classList.toggle('is-video', isVideo);

    if (!isVideo) {
      imageEl.src = item.src || item.thumb;
      imageEl.alt = item.title;
      return;
    }

    if (isEmbed) {
      embedEl.src = item.embedUrl;
      embedEl.title = item.title;
    } else if (item.src) {
      videoEl.src = item.src;
      videoEl.setAttribute('poster', item.poster || '');
    } else {
      // 既没有文件也没有可用嵌入地址
      videoEl.hidden = true;
    }
  }

  /** 视频文件缺失时的友好提示（示例数据常见） */
  function handleVideoError() {
    const item = items[index];
    if (!isOpen() || !item || item.type !== MEDIA_TYPE.VIDEO) return;
    if (videoEl.hidden || !videoEl.getAttribute('src')) return; // 卸载过程中的空 src，忽略
    exifEl.innerHTML = `<p class="exif__status">未找到视频文件：${escapeHtml(
      item.src || item.sourceUrl,
    )}<br />请把视频放入 <code>assets/video/</code>，或改用 <code>provider: "embed"</code> 的外链方式。</p>`;
  }

  function render() {
    const item = items[index];
    if (!item) return;

    resetMedia();
    showMedia(item);

    titleEl.textContent = item.title;
    subtitleEl.textContent = [item.description, item.location, item.date].filter(Boolean).join(' · ');
    counterEl.textContent = `${index + 1} / ${items.length}`;

    const isVideo = item.type === MEDIA_TYPE.VIDEO;
    exifEl.innerHTML = exifHtml(infoItems(item));

    // 「解析原文件 EXIF」只对 JPEG 照片有意义
    if (parseBtn) {
      parseBtn.hidden = isVideo;
      parseBtn.disabled = false;
      parseBtn.textContent = '解析原文件 EXIF';
    }

    // 预加载相邻的图片 / 视频，翻页更顺滑
    [items[index + 1], items[index - 1]].forEach((neighbor) => {
      if (!neighbor) return;
      if (neighbor.type === MEDIA_TYPE.VIDEO) {
        if (neighbor.poster) new Image().src = neighbor.poster;
      } else if (neighbor.src) {
        new Image().src = neighbor.src;
      }
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

  // 视频文件缺失 / 解码失败 → 给出可操作的提示
  videoEl.addEventListener('error', handleVideoError);

  // 点击图片左右两侧区域翻页（视频区域留给播放器自身交互）
  root.querySelector('.lightbox__viewport')?.addEventListener('click', (event) => {
    if (items[index]?.type === MEDIA_TYPE.VIDEO) return;
    if (event.target !== imageEl) return;

    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    if (ratio < 0.28) go(-1);
    else if (ratio > 0.72) go(1);
  });

  // 按需解析原图 EXIF（需要经 HTTP 访问，file:// 下会被浏览器拦截）
  parseBtn?.addEventListener('click', async () => {
    const photo = items[index];
    if (!photo || photo.type === MEDIA_TYPE.VIDEO) return;

    parseBtn.disabled = true;
    parseBtn.textContent = '解析中…';

    try {
      const raw = await readExifFromUrl(photo.src || photo.thumb);
      const items2 = formatExif(raw);
      if (!items2.length) throw new Error('未读取到可用的 EXIF 字段');
      exifEl.innerHTML = exifHtml(items2);
      parseBtn.textContent = '已从原文件解析';
    } catch (error) {
      exifEl.innerHTML = `<p class="exif__status">解析失败：${escapeHtml(error.message)}</p>`;
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
