/* 界面偏好的浏览器缓存键。按工作目录隔开，避免 A/B 串用。 */
'use strict';

function uiCacheKey(workspace, name) {
  return 'wb:' + encodeURIComponent(String(workspace || '')) + ':' + name;
}

function uiSaveMessage(ok) {
  return ok ? '' : '界面偏好未写入工作目录，仅在当前工作区临时生效';
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { uiCacheKey: uiCacheKey, uiSaveMessage: uiSaveMessage };
}
