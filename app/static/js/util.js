/**
 * 全站共用的极小工具函数集,必须在其它页面脚本之前加载。
 * 之前 esc() 在 8 个页面脚本里各自重复了一份、写法还互相不一致(有的漏转义
 * 引号),统一到这一处,新页面也不用再抄一遍。
 */
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
