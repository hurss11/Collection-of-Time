/**
 * data.js —— 数据加载与规范化
 *
 * 数据来源：/data/albums.json、/data/photos.json、/data/videos.json。
 * 照片与视频统一规范化为「媒体条目」（media item），字段结构保持一致，
 * 便于上层的筛选、排序、渲染共用同一套代码。
 *
 * 由于浏览器对 file:// 下的 fetch 有限制，请通过本地服务器访问（见 README）。
 */

const ALBUMS_URL = 'data/albums.json';
const PHOTOS_URL = 'data/photos.json';
const VIDEOS_URL = 'data/videos.json';

/** 媒体类型枚举 */
export const MEDIA_TYPE = {
  PHOTO: 'photo',
  VIDEO: 'video',
};

/** 统一的照片结构（字段缺失时给出安全默认值） */
export function normalizePhoto(raw, index = 0) {
  const tags = Array.isArray(raw.tags) ? raw.tags.filter(Boolean) : [];
  const date = raw.date || raw.exif?.dateTimeOriginal || '';

  return {
    type: MEDIA_TYPE.PHOTO,
    id: raw.id || `photo-${String(index + 1).padStart(3, '0')}`,
    title: raw.title || '未命名作品',
    albumId: raw.album || raw.albumId || 'uncategorized',
    src: raw.src || '',
    thumb: raw.thumb || raw.src || '',
    date,
    year: date ? date.slice(0, 4) : '',
    location: raw.location || '',
    description: raw.description || '',
    tags,
    exif: raw.exif || null,
    /** 搜索用的合并索引，避免每次输入都重新拼接 */
    searchIndex: [raw.title, raw.location, raw.description, raw.exif?.camera, raw.exif?.lens, ...tags]
      .filter(Boolean)
      .join(' ')
      .toLowerCase(),
  };
}

/* ============================================================
   视频：时长解析与来源转换
   ============================================================ */

/**
 * 秒数 → 可读时长文本。
 * @param {number|null} seconds
 * @returns {string} 例如 "00:48"、"12:05"、"1:02:33"
 */
export function formatDuration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '';

  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, '0');

  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** 时长字段归一化：接受 48、"48"、"00:48"、"1:02:33"、null */
export function parseDuration(raw) {
  if (raw === null || raw === undefined || raw === '') return null;
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : null;

  const text = String(raw).trim();
  if (/^\d+(\.\d+)?$/.test(text)) return Number(text);

  const parts = text.split(':').map((piece) => Number(piece));
  if (parts.some((n) => !Number.isFinite(n))) return null;
  return parts.reduce((acc, part) => acc * 60 + part, 0);
}

/** 从各种形式的链接中提取视频 ID */
const ID_PATTERNS = {
  youtube: [
    /(?:youtube\.com\/watch\?[^#]*\bv=)([\w-]{6,})/i,
    /(?:youtu\.be\/)([\w-]{6,})/i,
    /(?:youtube\.com\/(?:embed|shorts|live)\/)([\w-]{6,})/i,
  ],
  bilibili: [/\/video\/(BV[\w]{6,})/i, /[?&]bvid=(BV[\w]{6,})/i],
  vimeo: [/vimeo\.com\/(?:video\/)?(\d{5,})/i],
};

/** 把用户填写的链接（或裸 ID）转成「可直接放进 iframe」的播放地址 */
export function toEmbedUrl(provider, raw) {
  const src = String(raw || '').trim();
  if (!src || provider === 'file') return '';

  const patterns = ID_PATTERNS[provider] || [];
  let id = null;
  for (const pattern of patterns) {
    const matched = src.match(pattern);
    if (matched) {
      id = matched[1];
      break;
    }
  }
  // 允许直接填写视频 ID
  if (!id && /^[\w-]{6,}$/.test(src)) id = src;
  if (!id) return src; // 无法识别时原样交给 iframe，由浏览器处理

  switch (provider) {
    case 'youtube':
      return `https://www.youtube.com/embed/${id}?rel=0`;
    case 'bilibili':
      return `https://player.bilibili.com/player.html?bvid=${id}&autoplay=0&danmaku=0&high_quality=1`;
    case 'vimeo':
      return `https://player.vimeo.com/video/${id}`;
    default:
      return src;
  }
}

/** 来源提供方 → 展示名称 */
export const PROVIDER_LABELS = {
  file: '本地视频',
  youtube: 'YouTube',
  bilibili: '哔哩哔哩',
  vimeo: 'Vimeo',
  embed: '外部嵌入',
};

/**
 * 统一的视频结构。
 * JSON 字段：id / title / album / provider / src / poster / posterTime /
 *           duration / resolution / date / location / description / tags / exif
 *
 * provider 取值：'file'（本地文件）| 'youtube' | 'bilibili' | 'vimeo' | 'embed'
 */
export function normalizeVideo(raw, index = 0) {
  const tags = Array.isArray(raw.tags) ? raw.tags.filter(Boolean) : [];
  const date = raw.date || '';
  const provider = String(raw.provider || 'file').toLowerCase();
  const isFile = provider === 'file';
  const source = raw.src || raw.url || '';

  const durationSeconds = parseDuration(raw.duration ?? raw.durationSeconds);

  return {
    type: MEDIA_TYPE.VIDEO,
    id: raw.id || `video-${String(index + 1).padStart(3, '0')}`,
    title: raw.title || '未命名视频',
    albumId: raw.album || raw.albumId || 'uncategorized',

    provider,
    providerLabel: PROVIDER_LABELS[provider] || provider,
    /** 本地视频文件路径；外链视频为空 */
    src: isFile ? source : '',
    /** 外链视频的 iframe 播放地址；本地文件为空 */
    embedUrl: isFile ? '' : toEmbedUrl(provider, source),
    /** 用户原始填写的地址，便于界面提示与排错 */
    sourceUrl: source,

    /** 封面：优先用手动指定或预生成的 poster，缺失时由 poster.js 运行时抓帧 */
    poster: raw.poster || raw.thumb || '',
    /** 抓帧时间点（秒），默认第 2 秒，避开黑场开头 */
    posterTime: Number.isFinite(raw.posterTime) ? raw.posterTime : 2,

    durationSeconds,
    durationText: formatDuration(durationSeconds),
    resolution: raw.resolution || '',

    date,
    year: date ? date.slice(0, 4) : '',
    location: raw.location || '',
    description: raw.description || '',
    tags,
    exif: raw.exif || null,

    searchIndex: [
      raw.title,
      raw.location,
      raw.description,
      raw.resolution,
      PROVIDER_LABELS[provider],
      raw.exif?.camera,
      ...tags,
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase(),
  };
}

/** 统一的相册结构 */
export function normalizeAlbum(raw, index = 0) {
  return {
    id: raw.id || `album-${index + 1}`,
    name: raw.name || raw.title || '未命名相册',
    description: raw.description || '',
    cover: raw.cover || '',
  };
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: 'no-cache' });
  if (!response.ok) throw new Error(`加载 ${url} 失败：HTTP ${response.status}`);
  return response.json();
}

/**
 * 并行加载全部数据，并把照片与视频合并为统一的 items 数组。
 * @returns {Promise<{albums: object[], photos: object[], videos: object[], items: object[], errors: string[]}>}
 */
export async function loadData() {
  const errors = [];
  let albums = [];
  let photos = [];
  let videos = [];

  const [albumsResult, photosResult, videosResult] = await Promise.allSettled([
    fetchJson(ALBUMS_URL),
    fetchJson(PHOTOS_URL),
    fetchJson(VIDEOS_URL),
  ]);

  if (albumsResult.status === 'fulfilled') {
    albums = (albumsResult.value || []).map(normalizeAlbum);
  } else {
    errors.push(albumsResult.reason?.message || '相册数据加载失败');
  }

  if (photosResult.status === 'fulfilled') {
    photos = (photosResult.value || []).map(normalizePhoto);
  } else {
    errors.push(photosResult.reason?.message || '照片数据加载失败');
  }

  if (videosResult.status === 'fulfilled') {
    videos = (videosResult.value || []).map(normalizeVideo);
  } else if (!/HTTP 404/.test(videosResult.reason?.message || '')) {
    // videos.json 允许暂不存在，只有真正的网络 / 解析异常才提示
    errors.push(videosResult.reason?.message || '视频数据加载失败');
  }

  // 统一的媒体列表：按拍摄时间倒序
  const items = [...photos, ...videos].sort(
    (a, b) => (Date.parse(b.date) || 0) - (Date.parse(a.date) || 0),
  );

  return { albums, photos, videos, items, errors };
}

/** 统计每个相册的条目数量 → Map<albumId, number> */
export function countByAlbum(items) {
  return items.reduce((map, item) => {
    map.set(item.albumId, (map.get(item.albumId) || 0) + 1);
    return map;
  }, new Map());
}

/** 按媒体类型计数 → { all, photo, video } */
export function countByType(items) {
  const result = { all: items.length, [MEDIA_TYPE.PHOTO]: 0, [MEDIA_TYPE.VIDEO]: 0 };
  for (const item of items) {
    if (result[item.type] !== undefined) result[item.type] += 1;
  }
  return result;
}

/** 汇总全部标签及其出现次数，按次数倒序 → { tag, count }[] */
export function collectTags(items) {
  const counter = new Map();
  for (const item of items) {
    for (const tag of item.tags) {
      counter.set(tag, (counter.get(tag) || 0) + 1);
    }
  }
  return [...counter.entries()]
    .map(([tag, count]) => ({ tag, count }))
    .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag, 'zh-CN'));
}

/** 常用小工具：HTML 转义，防止数据注入 */
export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[ch]);
}
