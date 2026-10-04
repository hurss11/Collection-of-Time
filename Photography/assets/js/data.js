/**
 * data.js —— 公开只读 API 客户端
 *
 * 搜索、筛选、排序、分页、统计、EXIF 摘要与解析全部由服务端完成，
 * 本模块只负责发请求并把响应原样交回渲染层。
 *
 * 接口：
 *   GET /api/public/site                 → 统计 / 相册总览 / 筛选项 / 排序项
 *   GET /api/public/gallery              → 当前页作品（含 meta / active / facets）
 *   GET /api/public/exif?path=...        → 原文件 EXIF 行
 */

/** 同源为默认；若页面提供 <meta name="api-base"> 则以其为前缀。 */
const API_BASE = (
  document.querySelector('meta[name="api-base"]')?.content || ''
).replace(/\/+$/, '');

/** 接口根地址（空串 = 同源）。给需要自己发请求的模块用（见 cover.js）。 */
export function apiBase() {
  return API_BASE;
}

async function getJson(path, params) {
  const query = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    query.set(key, String(value));
  });
  const suffix = query.toString() ? `?${query}` : '';

  const response = await fetch(`${API_BASE}${path}${suffix}`, {
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  });

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok || !payload || payload.ok === false) {
    const message = (payload && payload.error) || `请求失败（HTTP ${response.status}）`;
    throw new Error(message);
  }
  return payload;
}

/** 首屏数据：stats / albums / facets / sorts / types */
export function fetchSite() {
  return getJson('/api/public/site');
}

/**
 * 作品列表（服务端已完成搜索 → 筛选 → 排序 → 分页）。
 * @param {{q?: string, type?: string, album?: string, tags?: string,
 *          sort?: string, page?: number, pageSize?: number}} params
 */
export function fetchGallery(params) {
  return getJson('/api/public/gallery', params);
}

/** 解析原文件 EXIF → [{ label, value }, ...] */
export async function fetchExif(path) {
  const payload = await getJson('/api/public/exif', { path });
  return payload.exif || [];
}

/** HTML 转义，防止数据注入 */
export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[ch]);
}
