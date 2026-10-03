/**
 * config.js —— 运行时配置
 *
 * 前后端分离时的接口地址入口。同源部署留空即可（默认）。
 * 需要指向另一台服务器时，任选一种方式覆盖：
 *   1. 在 index.html 的 <meta name="api-base" content="https://api.example.com">；
 *   2. 在加载本文件前设置 window.__API_BASE__ = 'https://api.example.com'。
 */

function resolveApiBase() {
  if (typeof window !== 'undefined' && typeof window.__API_BASE__ === 'string') {
    return window.__API_BASE__.replace(/\/+$/, '');
  }

  const meta = document.querySelector('meta[name="api-base"]');
  if (meta && meta.content && meta.content !== 'auto') {
    return meta.content.replace(/\/+$/, '');
  }

  return '';   // 空字符串 = 同源，请求走相对路径
}

export const CONFIG = {
  /** 接口根地址，例如 '' 或 'https://api.example.com' */
  apiBase: resolveApiBase(),

  /**
   * 媒体资源（缩略图 / 封面）的根地址。
   * 图片视频由后端以静态资源提供，因此默认与接口同源；
   * 若前端把资源挪到了 CDN，在这里单独改。
   */
  assetBase: resolveApiBase(),

  /** CSRF Cookie / 请求头名称，需与后端 adminlib/auth.py 保持一致 */
  csrfCookie: 'cot_csrf',
  csrfHeader: 'X-CSRF-Token',

  /** 前端版本号，显示在登录页页脚，便于确认部署的是哪一版 */
  version: '1.1.0',
};

/** 拼接接口地址 */
export function apiUrl(path) {
  return `${CONFIG.apiBase}${path}`;
}

/** 拼接静态资源地址（外部链接原样返回） */
export function assetUrl(path) {
  const value = String(path || '');
  if (!value) return '';
  if (/^https?:\/\//i.test(value)) return value;
  return `${CONFIG.assetBase}/${value.replace(/^\/+/, '')}`;
}

/** 读取 Cookie（只用于读非 HttpOnly 的 CSRF 值） */
export function readCookie(name) {
  const target = `${name}=`;
  for (const chunk of document.cookie.split(';')) {
    const item = chunk.trim();
    if (item.startsWith(target)) return decodeURIComponent(item.slice(target.length));
  }
  return '';
}
