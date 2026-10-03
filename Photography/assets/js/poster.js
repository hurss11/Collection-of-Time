/**
 * poster.js —— 视频自动封面
 *
 * 两种封面来源：
 *   1. 预先指定的 `poster` 字段（推荐：用 tools/make_posters.py 批量生成静态图，加载最快）；
 *   2. 本模块的「运行时抓帧」：卡片进入视口后，把 <video> 定位到指定时间点，
 *      由浏览器直接显示该帧，无需 canvas、不产生额外图片请求。
 *
 * 对于外链嵌入（YouTube / 哔哩哔哩等），浏览器不允许跨域读帧，
 * 因此必须依赖 `poster` 字段，或者由平台自身的缩略图接口提供。
 */

/** 抓帧的时间点兜底值（秒）：避开开头常见的黑场 */
const DEFAULT_POSTER_TIME = 2;
const SEEK_TIMEOUT = 8000;

/**
 * 让单个 video 元素定位到指定帧。
 * @param {HTMLVideoElement} video
 * @returns {Promise<void>}
 */
function primePoster(video) {
  const src = video.dataset.posterSrc;
  if (!src) return Promise.resolve();

  const time = Number(video.dataset.posterTime);
  const target = Number.isFinite(time) && time >= 0 ? time : DEFAULT_POSTER_TIME;

  video.preload = 'metadata';
  video.src = src;

  return new Promise((resolve) => {
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      video.classList.toggle('is-ready', ok);
      if (!ok) video.classList.add('is-missing');
      resolve();
    };

    // 视频文件缺失（例如示例数据里只有占位 poster）时给出降级样式
    video.addEventListener('error', () => finish(false), { once: true });
    video.addEventListener(
      'loadeddata',
      () => {
        try {
          video.currentTime = Math.min(target, video.duration || target);
        } catch {
          finish(false);
        }
      },
      { once: true },
    );
    video.addEventListener('seeked', () => finish(true), { once: true });

    const timer = window.setTimeout(() => finish(false), SEEK_TIMEOUT);
  });
}

/**
 * 为一组 video 元素挂上「进入视口再抓帧」的懒加载逻辑。
 * @param {HTMLVideoElement[]} videos
 */
export function attachVideoPosters(videos) {
  if (!videos.length) return;

  // 不支持 IntersectionObserver 时直接全部加载
  if (!('IntersectionObserver' in window)) {
    videos.forEach(primePoster);
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        observer.unobserve(entry.target);
        primePoster(entry.target);
      }
    },
    { rootMargin: '300px 0px' },
  );

  videos.forEach((video) => observer.observe(video));
}

/**
 * 把当前帧导出为 data URL。
 * 用于「手动挑一帧当封面」或调试；同源视频才可用（跨域会被 canvas 污染）。
 * @param {HTMLVideoElement} video
 * @param {string} [mime]
 * @param {number} [quality]
 * @returns {string} data URL
 */
export function capturePosterDataUrl(video, mime = 'image/jpeg', quality = 0.8) {
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth || 640;
  canvas.height = video.videoHeight || 360;

  const ctx = canvas.getContext('2d');
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL(mime, quality);
}
