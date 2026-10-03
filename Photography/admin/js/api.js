/**
 * api.js —— 后端接口客户端
 *
 * 职责边界（前后端分离的关键）：
 *   - 只负责「发请求 / 收 JSON / 带上凭证与 CSRF / 统一错误」；
 *   - 不碰 DOM，也不持业务状态——DOM 交给 ui.js，业务交给 app.js。
 *
 * 认证方式：HttpOnly Cookie 会话（浏览器自动携带，JS 读不到令牌本身），
 * 写操作额外带上从可读 Cookie 中取出的 CSRF 值做双提交校验。
 */

import { CONFIG, apiUrl, readCookie } from './config.js';

export class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
    this.retryAfter = Number(payload?.retryAfter ?? 0) || 0;
  }
}

/** 未授权（401）时的回调，由 app.js 注入，用来切回登录页 */
let onUnauthorized = () => {};
export function setUnauthorizedHandler(handler) {
  onUnauthorized = typeof handler === 'function' ? handler : () => {};
}

const MUTATING = new Set(['POST', 'PUT', 'DELETE', 'PATCH']);

/**
 * 发起一次 API 请求。
 * @param {string} path 以 /api 开头的路径
 * @param {{method?: string, json?: unknown, form?: FormData, raw?: boolean, retry?: boolean}} options
 */
export async function request(path, options = {}) {
  const { method = 'GET', json, form, raw = false, retry = true } = options;

  const headers = {};
  let body;

  if (form) {
    body = form;                                   // 交给浏览器自动设置 multipart 边界
  } else if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  }

  // 双提交：写操作必须回填 CSRF 值
  if (MUTATING.has(method)) {
    const csrf = readCookie(CONFIG.csrfCookie);
    if (csrf) headers[CONFIG.csrfHeader] = csrf;
  }

  let response;
  try {
    response = await fetch(apiUrl(path), { method, headers, body, credentials: 'same-origin' });
  } catch (error) {
    throw new ApiError('无法连接后端服务，请确认服务已启动', 0, { cause: String(error) });
  }

  if (raw) {
    if (response.status === 401) {
      onUnauthorized();
      throw new ApiError('未登录或会话已过期', 401);
    }
    if (!response.ok) throw new ApiError(`HTTP ${response.status}`, response.status);
    return response;
  }

  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError(`响应不是 JSON（HTTP ${response.status}）`, response.status, { text: text.slice(0, 200) });
  }

  if (response.ok && payload?.ok !== false) return payload;

  const message = payload?.error || `HTTP ${response.status}`;
  const retryAfter = Number(response.headers.get('Retry-After') ?? 0) || 0;

  // CSRF Cookie 过期（例如页面开了很久）：刷新会话后自动重试一次
  if (response.status === 403 && retry && /CSRF/i.test(message)) {
    try {
      await fetch(apiUrl('/api/auth/session'), { credentials: 'same-origin' });
    } catch { /* 刷新失败就让下面的重试去暴露真实错误 */ }
    return request(path, { ...options, retry: false });
  }

  if (response.status === 401) onUnauthorized();

  throw new ApiError(message, response.status, { ...payload, retryAfter });
}

export const api = {
  get: (path, options) => request(path, { ...options, method: 'GET' }),
  post: (path, json, options) => request(path, { ...options, method: 'POST', json }),
  put: (path, json, options) => request(path, { ...options, method: 'PUT', json }),
  del: (path, options) => request(path, { ...options, method: 'DELETE' }),
  upload: (path, form) => request(path, { method: 'POST', form }),
  raw: (path, options) => request(path, { ...options, raw: true }),

  /* ---------- 认证 ---------- */
  session: () => request('/api/auth/session', { method: 'GET' }),
  setup: (payload) => request('/api/auth/setup', { method: 'POST', json: payload }),
  login: (payload) => request('/api/auth/login', { method: 'POST', json: payload }),
  logout: () => request('/api/auth/logout', { method: 'POST', json: {} }),
  changePassword: (payload) => request('/api/auth/password', { method: 'POST', json: payload }),

  /* ---------- 数据 ---------- */
  state: () => request('/api/state', { method: 'GET' }),
  list: (collection) => request(`/api/data/${collection}`, { method: 'GET' }),
  save: (collection, item) => request(`/api/data/${collection}`, { method: 'POST', json: item }),
  replaceAll: (collection, items) =>
    request(`/api/data/${collection}`, { method: 'PUT', json: { items } }),
  remove: (collection, id, withFile = false) =>
    request(`/api/data/${collection}/${encodeURIComponent(id)}${withFile ? '/file' : ''}`, {
      method: 'DELETE',
    }),

  /* ---------- 备份 ---------- */
  backups: () => request('/api/backups', { method: 'GET' }),
  restore: (name) => request(`/api/backups/${encodeURIComponent(name)}/restore`, { method: 'POST', json: {} }),
  exportBundle: () => request('/api/export', { raw: true }),
  importBundle: (form) => request('/api/import', { method: 'POST', form }),
};
