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


/**
 * 交易时段判断。看板的分时卡和自选盯盘都要靠它决定"要不要继续轮询",
 * 写第二份的时候就该提上来(这个文件的存在理由就是 esc() 曾经被抄了 8 份)。
 */
var SPMarket = (function () {
  // 北京时间:交易所按北京时间走,而访客的机器可能在任何时区。用本地时间判
  // "现在是不是盘中",时差用户要么永远不刷新、要么半夜狂刷。
  function beijingNow() {
    var d = new Date();
    return new Date(d.getTime() + (d.getTimezoneOffset() + 480) * 60000);
  }

  function fmtDate(d) {
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  function beijingToday() { return fmtDate(beijingNow()); }

  // 窗口放宽到 09:15~15:10:早盘集合竞价 09:15 就有数据,收盘后源站还会补几笔。
  // 节假日在前端判不了(没有交易日历),靠调用方那条"数据日期不是今天就不轮询"
  // 兜底 —— 节假日拿到的是上一个交易日,那份数据不会再变。
  function inTradingWindow() {
    var b = beijingNow();
    var dow = b.getDay();
    if (dow === 0 || dow === 6) return false;
    var m = b.getHours() * 60 + b.getMinutes();
    return m >= 9 * 60 + 15 && m <= 15 * 60 + 10;
  }

  return {
    beijingNow: beijingNow,
    beijingToday: beijingToday,
    inTradingWindow: inTradingWindow,
    fmtDate: fmtDate,
  };
})();
