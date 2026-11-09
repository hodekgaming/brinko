# fx_window.py — borderless Chromium window for BRINKO's visual effects
# (screensaver, camera powers, document builder). The pages live in fx/ and
# are served by BRINKO's own local server (remote_server.py → /fx/...), so the
# camera works (localhost counts as a secure origin) and WebGL/WASM run on
# the GPU.

from __future__ import annotations

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QColor
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView

_OPEN: list["FxWindow"] = []      # keep references alive until closed
_PROFILE = None


def _profile():
    """One named, persistent browser profile for all FX pages, so things like
    'camera allowed' and the chosen webcam are remembered between runs
    (Qt's default profile is off-the-record and forgets everything)."""
    global _PROFILE
    if _PROFILE is None:
        import os
        from pathlib import Path
        # BRINKO_FX_PROFILE: a separate profile folder (tests run next to a live BRINKO)
        base = Path(os.environ.get("BRINKO_FX_PROFILE") or Path(__file__).resolve().parent / "config" / "webdata")
        base.mkdir(parents=True, exist_ok=True)
        _PROFILE = QWebEngineProfile("brinko-fx")
        _PROFILE.setPersistentStoragePath(str(base / "storage"))
        _PROFILE.setCachePath(str(base / "cache"))
    return _PROFILE


def _is_local(url: QUrl) -> bool:
    return url.host() in ("localhost", "127.0.0.1")


class FxPage(QWebEnginePage):
    def __init__(self, parent=None):
        super().__init__(_profile(), parent)
        # Camera/mic for BRINKO's own local pages only.
        try:
            self.permissionRequested.connect(self._on_permission)          # Qt ≥ 6.8
        except Exception:
            pass
        try:
            self.featurePermissionRequested.connect(self._on_feature)      # older Qt
        except Exception:
            pass

    def _on_permission(self, permission):
        try:
            if _is_local(permission.origin()):
                permission.grant()
            else:
                permission.deny()
        except Exception as e:
            print(f"[FX] permission error: {e}")

    def _on_feature(self, origin, feature):
        policy = (QWebEnginePage.PermissionPolicy.PermissionGrantedByUser if _is_local(origin)
                  else QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)
        self.setFeaturePermission(origin, feature, policy)

    def javaScriptConsoleMessage(self, level, message, line, source):
        # errors AND warnings go to the log, so a failing camera/effect is diagnosable
        if level != QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel:
            print(f"[FX js] {message} ({source.rsplit('/', 1)[-1]}:{line})")


class FxWindow(QWebEngineView):
    """kind: 'fullscreen' (screensaver, camera) or 'panel' (doc builder)."""

    def __init__(self, url: str, kind: str = "fullscreen", size=(1100, 900),
                 on_title=None, on_loaded=None, on_closed=None, stay_on_top: bool = True):
        super().__init__()
        self._on_title, self._on_loaded, self._on_closed = on_title, on_loaded, on_closed
        page = FxPage(self)
        self.setPage(page)
        s = self.settings()
        for attr in ("WebGLEnabled", "Accelerated2dCanvasEnabled", "LocalStorageEnabled"):
            try:
                s.setAttribute(getattr(QWebEngineSettings.WebAttribute, attr), True)
            except Exception:
                pass
        try:
            s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        except Exception:
            pass
        page.setBackgroundColor(QColor("#07000d"))
        page.windowCloseRequested.connect(self.close)          # window.close() in the page
        page.titleChanged.connect(self._title)
        page.loadFinished.connect(self._loaded)

        flags = Qt.WindowType.FramelessWindowHint
        if stay_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setWindowTitle("BRINKO FX")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.load(QUrl(url))
        if kind == "fullscreen":
            self.showFullScreen()
        else:
            self.resize(*size)
            scr = self.screen().availableGeometry()
            self.move(scr.x() + (scr.width() - size[0]) // 2, scr.y() + (scr.height() - size[1]) // 2)
            self.show()
        self.raise_()
        self.activateWindow()
        _OPEN.append(self)

    def run_js(self, code: str, callback=None):
        if callback:
            self.page().runJavaScript(code, callback)
        else:
            self.page().runJavaScript(code)

    def _title(self, title: str):
        if self._on_title:
            try:
                self._on_title(self, title)
            except Exception as e:
                print(f"[FX] title handler: {e}")

    def _loaded(self, ok: bool):
        if self._on_loaded:
            try:
                self._on_loaded(self, ok)
            except Exception as e:
                print(f"[FX] load handler: {e}")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        # stop the camera etc. before the page goes away
        try:
            self.page().runJavaScript("window.__brinkoStop && window.__brinkoStop()")
        except Exception:
            pass
        if self in _OPEN:
            _OPEN.remove(self)
        if self._on_closed:
            try:
                self._on_closed(self)
            except Exception:
                pass
        super().closeEvent(e)


def close_all(prefix: str = ""):
    for w in list(_OPEN):
        if not prefix or w.url().path().startswith(prefix):
            w.close()


def is_open(prefix: str) -> bool:
    return any(w.url().path().startswith(prefix) for w in _OPEN)


# ── Thread-safe host: tools (worker threads) ask, the GUI thread opens windows ──
import threading as _threading
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, QMarginsF
from PyQt6.QtGui import QPageLayout, QPageSize


class FxHost(QObject):
    _open_sig = pyqtSignal(str, str)
    _doc_start_sig = pyqtSignal(str)
    _doc_feed_sig = pyqtSignal(str, str, str)
    _doc_error_sig = pyqtSignal(str, str)
    _close_sig = pyqtSignal(str)
    _js_sig = pyqtSignal(str, str)

    def __init__(self, url_fn):
        super().__init__()
        self._url = url_fn                       # url_fn(page, query) -> str
        self._docs: dict[str, dict] = {}
        self._open_sig.connect(self._on_open)
        self._doc_start_sig.connect(self._on_doc_start)
        self._doc_feed_sig.connect(self._on_doc_feed)
        self._doc_error_sig.connect(self._on_doc_error)
        self._close_sig.connect(lambda prefix: close_all(prefix))
        self._js_sig.connect(self._on_js)

    # ── API for any thread ──
    def open(self, page: str, query: str = ""):
        self._open_sig.emit(page, query)

    def close(self, page_prefix: str = ""):
        self._close_sig.emit(page_prefix)

    def is_open(self, page_prefix: str) -> bool:
        return is_open(page_prefix)

    def js(self, page_prefix: str, code: str):
        """Run JavaScript in every open FX window whose path starts with page_prefix."""
        self._js_sig.emit(page_prefix, code)

    def doc_start(self, key: str):
        self._docs[key] = {"event": _threading.Event(), "ok": False, "msg": "", "win": None,
                           "ready": False, "pending": None}
        self._doc_start_sig.emit(key)

    def doc_feed(self, key: str, doc_json: str, pdf_path: str, done_label: str = ""):
        self._docs[key]["label"] = done_label
        self._doc_feed_sig.emit(key, doc_json, pdf_path)

    def doc_error(self, key: str, msg: str):
        self._doc_error_sig.emit(key, msg)

    def doc_wait(self, key: str, timeout: float = 150.0) -> tuple[bool, str]:
        d = self._docs.get(key)
        if not d:
            return False, "no builder"
        if not d["event"].wait(timeout):
            return False, "The build animation timed out."
        return d["ok"], d["msg"]

    # ── GUI thread ──
    def _on_js(self, prefix: str, code: str):
        for w in list(_OPEN):
            if w.url().path().startswith(prefix):
                w.run_js(code)

    def _on_open(self, page: str, query: str):
        close_all("/fx/" + page)
        FxWindow(self._url(page, query), kind="fullscreen")

    def _on_doc_start(self, key: str):
        d = self._docs[key]

        def on_title(win, title):
            if title == "BRINKO:READY":
                d["ready"] = True
                if d["pending"]:
                    self._inject(key)
            elif title == "BRINKO:BUILT":
                self._finish(key)

        def on_closed(win):
            if not d["event"].is_set():
                d["msg"] = d["msg"] or "Builder window was closed."
                d["event"].set()

        d["win"] = FxWindow(self._url("docbuilder.html", ""), kind="panel", size=(1040, 880),
                            on_title=on_title, on_closed=on_closed)

    def _on_doc_feed(self, key: str, doc_json: str, pdf_path: str):
        d = self._docs.get(key)
        if not d:
            return
        d["pending"] = (doc_json, pdf_path)
        if d["ready"]:
            self._inject(key)

    def _inject(self, key: str):
        d = self._docs[key]
        doc_json, _ = d["pending"]
        import json as _json
        d["win"].run_js(f"window.brinkoBuild({_json.dumps(doc_json)})")

    def _finish(self, key: str):
        d = self._docs[key]
        _, pdf_path = d["pending"]
        win = d["win"]

        def done(ok: bool, msg: str):
            d["ok"], d["msg"] = ok, msg
            import json as _json
            win.run_js(f"window.{'brinkoDone' if ok else 'brinkoError'}({_json.dumps(d.get('label') or msg)})")
            QTimer.singleShot(2600, win.close)
            d["event"].set()

        if not pdf_path:
            done(True, "built")
            return

        def printed(path, success):
            try:
                win.page().pdfPrintingFinished.disconnect(printed)
            except Exception:
                pass
            done(bool(success), path if success else "PDF export failed")

        win.page().pdfPrintingFinished.connect(printed)
        layout = QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0))
        win.page().printToPdf(pdf_path, layout)

    def _on_doc_error(self, key: str, msg: str):
        d = self._docs.get(key)
        if d and d["win"]:
            import json as _json
            d["win"].run_js(f"window.brinkoError({_json.dumps(msg)})")
            QTimer.singleShot(3500, d["win"].close)
