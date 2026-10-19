/* Edit version of the document on this page (shared/concurrency.py: stale_edit).
 *
 * The form renders <input type="hidden" id="editVersion" value="N">. Every
 * JSON save this page posts (a URL ending in /save) carries edit_version: N,
 * and every JSON answer that reports an edit_version replaces N before the
 * page's own code sees the answer. This page's saves, approvals and
 * unapprovals keep the number current; a copy opened before another user's
 * save is refused by the server instead of silently overwriting their edit.
 */
(function () {
  var el = document.getElementById('editVersion');
  if (!el || !window.fetch) return;
  var nativeFetch = window.fetch.bind(window);

  window.fetch = function (input, init) {
    init = init || {};
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var method = (init.method || 'GET').toUpperCase();
    if (method === 'POST' && /\/save(\?|$)/.test(url) && typeof init.body === 'string') {
      try {
        var body = JSON.parse(init.body);
        if (body && typeof body === 'object' && !Array.isArray(body) &&
            body.edit_version === undefined) {
          body.edit_version = el.value === '' ? null : Number(el.value);
          init = Object.assign({}, init, { body: JSON.stringify(body) });
        }
      } catch (e) { /* not a JSON body: send it as it is */ }
    }
    return nativeFetch(input, init).then(function (resp) {
      if (method !== 'POST') return resp;
      return resp.clone().json().then(function (data) {
        if (data && data.edit_version !== undefined && data.edit_version !== null) {
          el.value = data.edit_version;
        }
        return resp;
      }, function () { return resp; });
    });
  };
})();
