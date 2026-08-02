// MAIN-world tracker — must run at document_start so sockets opened by the page are recorded.
(function () {
    if (window.__domCaptureWSTracked) return;
    window.__domCaptureWSTracked = true;
    window.__domCaptureWS = window.__domCaptureWS || [];
    window.__domCaptureES = window.__domCaptureES || [];

    const OrigWS = window.WebSocket;
    window.WebSocket = function (url, protocols) {
        const ws = protocols !== undefined ? new OrigWS(url, protocols) : new OrigWS(url);
        window.__domCaptureWS.push(ws);
        const remove = () => {
            const i = window.__domCaptureWS.indexOf(ws);
            if (i >= 0) window.__domCaptureWS.splice(i, 1);
        };
        ws.addEventListener('close', remove);
        ws.addEventListener('error', remove);
        return ws;
    };
    window.WebSocket.prototype = OrigWS.prototype;
    Object.assign(window.WebSocket, OrigWS);

    if (window.EventSource) {
        const OrigES = window.EventSource;
        window.EventSource = function (url, config) {
            const es = config !== undefined ? new OrigES(url, config) : new OrigES(url);
            window.__domCaptureES.push(es);
            es.addEventListener('error', () => {
                const i = window.__domCaptureES.indexOf(es);
                if (i >= 0) window.__domCaptureES.splice(i, 1);
            });
            return es;
        };
        window.EventSource.prototype = OrigES.prototype;
        Object.assign(window.EventSource, OrigES);
    }
})();
