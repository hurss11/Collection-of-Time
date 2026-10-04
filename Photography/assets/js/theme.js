/* theme.js —— 把主题在首次绘制前落到 <html> 上
 *
 * app.js 是 module（延迟执行），等它跑起来再切主题会先闪一下浅色/深色，
 * 所以这一段单独用同步脚本放在 <head> 里。
 * 默认浅色（方案 B 以暖白纸面为主），只有用户明确选过深色才用深色。
 */
(function () {
  var saved = null;
  try {
    saved = window.localStorage.getItem('cot-theme');
  } catch (error) {
    /* 隐私模式下忽略 */
  }
  document.documentElement.dataset.theme = saved === 'dark' ? 'dark' : 'light';
})();
