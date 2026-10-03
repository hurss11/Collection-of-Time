/**
 * search.js —— 搜索、标签筛选与排序（纯函数，便于单测）
 */

const SORTERS = {
  'date-desc': (a, b) => compareDate(b, a),
  'date-asc': (a, b) => compareDate(a, b),
  'title-asc': (a, b) => a.title.localeCompare(b.title, 'zh-CN'),
  album: (a, b) => a.albumId.localeCompare(b.albumId, 'zh-CN') || compareDate(b, a),
};

function compareDate(a, b) {
  const ta = Date.parse(a.date) || 0;
  const tb = Date.parse(b.date) || 0;
  return ta - tb;
}

/** 把查询串拆成多个关键词（空白分隔，全部命中才算匹配） */
function toTerms(query) {
  return String(query || '')
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
}

/**
 * 依条件过滤 + 排序。
 * @param {object[]} items 规范化后的媒体条目数组（照片与视频混合）
 * @param {{query?: string, tags?: string[], albumId?: string|null,
 *          type?: 'all'|'photo'|'video', sort?: string}} criteria
 *        tags 之间为「与」关系；albumId 为 null 表示不限相册；type 为 'all' 表示不限类型
 * @returns {object[]}
 */
export function filterMedia(items, criteria = {}) {
  const { query = '', tags = [], albumId = null, type = 'all', sort = 'date-desc' } = criteria;
  const terms = toTerms(query);

  const result = items.filter((item) => {
    if (type !== 'all' && item.type !== type) return false;
    if (albumId && item.albumId !== albumId) return false;
    if (tags.length && !tags.every((tag) => item.tags.includes(tag))) return false;
    if (terms.length && !terms.every((term) => item.searchIndex.includes(term))) return false;
    return true;
  });

  const sorter = SORTERS[sort] || SORTERS['date-desc'];
  return result.sort(sorter);
}

/** 判断是否存在任何生效中的筛选条件 */
export function hasActiveFilters({ query = '', tags = [], albumId = null, type = 'all' } = {}) {
  return Boolean(query.trim()) || tags.length > 0 || Boolean(albumId) || type !== 'all';
}
