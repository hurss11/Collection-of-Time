/**
 * cover.js —— 给「没有封面」的本地视频补一张封面
 *
 * 为什么由浏览器来做：服务端抓帧要 ffmpeg（`admin.py` 的 `_capture_poster` 会直接说
 * 「未安装 ffmpeg，无法抓帧」），而**能播的视频浏览器自己就会解码** —— 第一次播放时把
 * 当前帧画到 canvas 上导出 JPEG，再交给后台已有的 `POST /api/videos/{id}/poster`（字段
 * `file`）落盘。存图、写条目、回收旧封面仍然全在后端，这里只负责取像素。
 *
 * 顺带还比服务端抓第 0 秒更准：延时片开头常是淡入或全黑，抓到的是用户真正看到的那一帧。
 *
 * 只在两个前提下动手，其余一律静默放弃（这是顺手做的事，不能影响播放）：
 *   1. 条目是**站内播放**的本地视频，并且确实没有封面
 *      （外链站内不播、也读不到别的域名的画面，不在范围内）；
 *   2. 当前浏览器带着**管理员会话** —— 公开访客既不该也无法写数据，
 *      一次 `GET /api/auth/session` 探不出来就直接不做了。
 */

import { apiBase } from './data.js';

/** 存下来的封面最长边（像素）：够卡片与灯箱用，又不至于把手机拍的 4K 帧原样传上去 */
const MAX_WIDTH = 1600;
const QUALITY = 0.85;

/** 与 `adminlib/auth.py` / `admin/js/config.js` 保持一致（双提交校验用的可读 Cookie） */
const CSRF_COOKIE = 'cot_csrf';
const CSRF_HEADER = 'X-CSRF-Token';

function readCookie(name) {
  const target = `${name}=`;
  for (const chunk of document.cookie.split(';')) {
    const item = chunk.trim();
    if (item.startsWith(target)) return decodeURIComponent(item.slice(target.length));
  }
  return '';
}

/**
 * 会话探测：没有管理员会话就不做任何事。
 *
 * 只有**确认是管理员**才记住结果 —— 否则作者先以访客身份打开站点、再去后台登录，
 * 回来还得刷新一次页面才生效。反过来，访客每播放一条没封面的视频会多一次
 * `GET /api/auth/session`（有封面的视频完全不触发），代价可以忽略。
 */
let sessionIsAdmin = false;
function hasAdminSession() {
  if (sessionIsAdmin) return Promise.resolve(true);
  return fetch(`${apiBase()}/api/auth/session`, {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
    cache: 'no-store',
  })
    .then((response) => (response.ok ? response.json() : null))
    .then((payload) => {
      sessionIsAdmin = Boolean(payload && payload.authenticated);
      return sessionIsAdmin;
    })
    .catch(() => false);
}

/** 把视频当前帧画成 JPEG（拿不到尺寸或编码失败时返回 null） */
function frameToBlob(video) {
  const width = video.videoWidth;
  const height = video.videoHeight;
  if (!width || !height) return Promise.resolve(null);

  const scale = Math.min(1, MAX_WIDTH / width);
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));

  try {
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  } catch {
    return Promise.resolve(null);
  }

  return new Promise((resolve) => {
    try {
      canvas.toBlob((blob) => resolve(blob), 'image/jpeg', QUALITY);
    } catch {
      resolve(null);
    }
  });
}

/**
 * 抓当前帧并交给后端存成这条视频的封面。
 * @param {HTMLVideoElement} video 正在播放的本地视频
 * @param {{id: string, title?: string}} item 条目
 * @returns {Promise<string|null>} 存好后的封面相对路径；没做或失败时 null
 */
export async function captureCoverFrame(video, item) {
  if (!item || !item.id) return null;
  if (!(await hasAdminSession())) return null;

  const blob = await frameToBlob(video);
  if (!blob) return null;

  const form = new FormData();
  form.append('file', blob, `frame-${item.id}.jpg`);
  // 后端兜底：这条已经有封面就什么都不改（免得盖掉作者自己挑的那张）
  form.append('onlyIfMissing', '1');

  const headers = { Accept: 'application/json' };
  const csrf = readCookie(CSRF_COOKIE);
  if (csrf) headers[CSRF_HEADER] = csrf;

  try {
    const response = await fetch(
      `${apiBase()}/api/videos/${encodeURIComponent(item.id)}/poster`,
      { method: 'POST', headers, body: form, credentials: 'same-origin' },
    );
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload || payload.ok === false) return null;
    return payload.skipped ? null : payload.posterUrl || null;
  } catch {
    return null;
  }
}
