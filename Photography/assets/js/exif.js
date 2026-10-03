/**
 * exif.js —— 「解析原文件 EXIF」按钮的服务端入口
 *
 * 原先 200 多行的 JPEG APP1 解析器已移到服务端：
 *   GET /api/public/exif?path=<相对路径> → { ok, path, exif: [{ label, value }], raw }
 * 前端只负责请求、渲染，并如实展示服务端返回的错误文案。
 */

import { fetchExif } from './data.js';

/**
 * @param {string} path 相对仓库根目录的图片路径（需位于 assets/img/photos/ 下）
 * @returns {Promise<{label: string, value: string}[]>} 服务端算好的 EXIF 行
 */
export function readExifRows(path) {
  return fetchExif(path);
}
