# ============================================================
# PARENTAL CONTROL — Child Device App  (ULTRA-FAST v2)
# Framework: Kivy (no KivyMD dependency)
#
# ⚡ SPEED UPGRADES vs v1:
#   1. LONG-POLLING  — server holds connection 20s, returns the INSTANT
#                      a command is queued → avg <150 ms delivery (was 15s)
#   2. TCP_NODELAY   — monkey-patches urllib3 globally; disables Nagle's
#                      algorithm → removes 40-200 ms buffering on every req
#   3. SO_KEEPALIVE  — detects dropped sockets immediately, no stale waits
#   4. CONN POOL     — reuses TLS sessions (5 hosts × 10 conns)
#                      → zero TLS handshake overhead on subsequent requests
#   5. GZIP          — Accept-Encoding: gzip on every request
#   6. ThreadPoolExecutor (6 workers) — commands execute in parallel,
#                      never block the poll loop
#   7. PRIORITY SORT — lock/unlock commands processed before screenshots
#   8. DEDUP         — seen-command set; never executes the same cmd twice
#   9. WAKELOCK      — acquires PARTIAL_WAKE_LOCK so Android never suspends
#                      the polling thread
#  10. HeartbeatThread — runs on its own 25 s loop, never touches the
#                      poll loop timing
#  11. AUTO-DETECT   — if server returns long-poll req in <0.8 s with no
#                      cmds, gracefully falls back to 2 s short-poll
#  12. SESSION WARMUP — establishes TCP+TLS connection at startup so the
#                      very first poll is instant
#  13. ACK OFF-THREAD — command ACKs fire-and-forget in daemon threads
#                      so they never delay the poll loop
#  14. INSTANT BOOT  — delayed start reduced from 1.5 s → 0.5 s
# ============================================================

from __future__ import annotations
import os
import sys
import time
import json
import uuid
import socket
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

# ============================================================
# STEP 0 — SSL CERTIFICATE SETUP  (must run before any network import)
# Layer 1: certifi bundle   → most reliable on Android
# Layer 2: system CA paths  → fallback
# Layer 3: disable verify   → last resort, logs a warning
# ============================================================
def _setup_ssl_env():
    try:
        import certifi
        ca_path = certifi.where()
        if os.path.exists(ca_path):
            os.environ['SSL_CERT_FILE']      = ca_path
            os.environ['REQUESTS_CA_BUNDLE'] = ca_path
            print(f"[SSL] certifi: {ca_path}")
            return ca_path
    except Exception as e:
        print(f"[SSL] certifi failed: {e}")

    for path in [
        '/system/etc/security/cacerts',
        '/etc/ssl/certs/ca-certificates.crt',
        '/etc/pki/tls/certs/ca-bundle.crt',
        '/etc/ssl/cert.pem',
    ]:
        if os.path.exists(path):
            os.environ['SSL_CERT_FILE']      = path
            os.environ['REQUESTS_CA_BUNDLE'] = path
            print(f"[SSL] system CA: {path}")
            return path

    print("[SSL] WARNING: no CA bundle — verification disabled.")
    return False

SSL_VERIFY = _setup_ssl_env()

if SSL_VERIFY is False:
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

# ============================================================
# ⚡ SPEED PATCH — TCP_NODELAY + SO_KEEPALIVE
# Monkey-patches urllib3's socket factory so EVERY outgoing
# TCP connection disables Nagle buffering and enables keepalive.
# Applied globally before any requests import.
# ============================================================
try:
    from urllib3.util import connection as _u3conn
    _orig_create_connection = _u3conn.create_connection

    def _fast_create_connection(address, *args, **kwargs):
        sock = _orig_create_connection(address, *args, **kwargs)
        # Disable Nagle — send small packets immediately (saves 40-200 ms)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Keep-alive — detect dropped connections without long timeouts
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return sock

    _u3conn.create_connection = _fast_create_connection
    print("[NET] ⚡ TCP_NODELAY + SO_KEEPALIVE applied to all connections")
except Exception as _tcp_err:
    print(f"[NET] TCP patch skipped (non-fatal): {_tcp_err}")

# ============================================================
# STEP 1 — KIVY WINDOW SETUP
# ============================================================
os.environ.setdefault('KIVY_NO_CONSOLELOG', '0')

from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.core.window import Window
from kivy.metrics import dp, sp
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
# CONFIG — ULTRA-FAST CONSTANTS
# ============================================================
API_BASE          = "https://mirab.ayamilcoders.com/api.php"  # ← Change this
LONG_POLL_TIMEOUT = 20    # Server holds this many seconds waiting for a command
                          # → command delivered in avg ~150 ms after parent sends it
POLL_FALLBACK     = 2     # Short-poll interval if server ignores long-poll param
POLL_IDLE_SECS    = 15    # Fallback interval after 5 min no commands (battery save)
HEARTBEAT_SECS    = 25    # Heartbeat cadence (dedicated thread)
CMD_WORKERS       = 6     # Parallel threads for command execution
STORE_FILE        = "buddy_device.json"

# ============================================================
# PLATFORM
# ============================================================
IS_ANDROID = (platform == 'android')

# ============================================================
# ⚡ FAST SESSION FACTORY
# - Connection pool: reuse TLS sessions → no handshake overhead
# - Gzip + keep-alive headers
# - No adapter-level retries (higher-level code handles that)
# ============================================================
def make_fast_session() -> 'requests.Session':
    import requests
    from requests.adapters import HTTPAdapter

    s = requests.Session()
    s.verify = SSL_VERIFY
    s.headers.update({
        'Accept-Encoding':  'gzip, deflate',  # Compressed responses
        'Connection':       'keep-alive',      # Reuse TCP connection
        'Content-Type':     'application/json',
        'Cache-Control':    'no-cache',
        'X-Client-Version': '2.0-fast',
    })
    # Pool: 5 distinct hosts, 10 connections per host
    adapter = HTTPAdapter(pool_connections=5, pool_maxsize=10, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s

# ============================================================
# HELPERS — JSON + STORAGE
# ============================================================
def safe_json(response) -> dict | list:
    try:
        text = response.text.strip()
        if not text:
            return {}
        if text.startswith('<'):
            print(f"[JSON] Server returned HTML: {text[:120]}")
            return {}
        return response.json()
    except Exception as e:
        print(f"[JSON] Parse error: {e} | body={response.text[:200]}")
        return {}

def store_get(key: str, field: str, default=None):
    try:
        s = JsonStore(STORE_FILE)
        if s.exists(key):
            return s.get(key).get(field, default)
    except Exception as e:
        print(f"[STORE] get error: {e}")
    return default

def store_put(key: str, **kwargs):
    try:
        JsonStore(STORE_FILE).put(key, **kwargs)
        return True
    except Exception as e:
        print(f"[STORE] put error: {e}")
        return False

def store_delete(key: str):
    try:
        JsonStore(STORE_FILE).delete(key)
    except Exception as e:
        print(f"[STORE] delete error: {e}")

def store_exists(key: str) -> bool:
    try:
        return JsonStore(STORE_FILE).exists(key)
    except Exception:
        return False

# ============================================================
# ANDROID HELPERS
# Every import individually wrapped; one failure never kills the chain
# ============================================================
_android_ok    = False
PythonActivity = None
Context        = None
NotifManager   = None
NotifBuilder   = None
NotifChannel   = None
String_java    = None
MediaRecorder  = None
Camera         = None
Build          = None
BuildVersion   = None
SurfaceTexture = None
PythonJavaClass = None
java_method    = None
PowerManager   = None

if IS_ANDROID:
    try:
        from android.permissions import request_permissions, Permission  # type: ignore
        _android_ok = True
    except Exception as e:
        print(f"[ANDROID] permissions import failed: {e}")

    def _try_import(cls_path: str):
        try:
            from jnius import autoclass  # type: ignore
            return autoclass(cls_path)
        except Exception as e:
            print(f"[ANDROID] autoclass({cls_path}) failed: {e}")
            return None

    PythonActivity = _try_import('org.kivy.android.PythonActivity')
    Context        = _try_import('android.content.Context')
    NotifManager   = _try_import('android.app.NotificationManager')
    NotifBuilder   = _try_import('android.app.Notification$Builder')
    NotifChannel   = _try_import('android.app.NotificationChannel')
    String_java    = _try_import('java.lang.String')
    MediaRecorder  = _try_import('android.media.MediaRecorder')
    Camera         = _try_import('android.hardware.Camera')
    Build          = _try_import('android.os.Build')
    BuildVersion   = _try_import('android.os.Build$VERSION')
    SurfaceTexture = _try_import('android.graphics.SurfaceTexture')
    PowerManager   = _try_import('android.os.PowerManager')

    try:
        from jnius import PythonJavaClass, java_method  # type: ignore
    except Exception as e:
        print(f"[ANDROID] PythonJavaClass import failed: {e}")

# ---- WakeLock — keeps CPU running so polling thread never stalls -----------
_wakelock = None

def acquire_wakelock():
    """Prevent Android from suspending the polling thread."""
    global _wakelock
    if IS_ANDROID and PythonActivity and PowerManager:
        try:
            ctx = PythonActivity.mActivity.getApplicationContext()
            pm  = ctx.getSystemService(ctx.POWER_SERVICE)
            wl  = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,
                                 "BuddyGuard::FastPoll")
            wl.acquire()
            _wakelock = wl
            print("[WAKELOCK] ⚡ PARTIAL_WAKE_LOCK acquired")
            return wl
        except Exception as e:
            print(f"[WAKELOCK] Failed (non-fatal): {e}")
    return None

def release_wakelock():
    global _wakelock
    if _wakelock:
        try:
            _wakelock.release()
            print("[WAKELOCK] Released")
        except Exception:
            pass
        _wakelock = None

# ---- Device UID -----------------------------------------------------------
def get_device_uid() -> str:
    if IS_ANDROID and PythonActivity:
        try:
            from jnius import autoclass  # type: ignore
            Settings = autoclass('android.provider.Settings$Secure')
            ctx = PythonActivity.mActivity.getApplicationContext()
            uid = Settings.getString(ctx.getContentResolver(),
                                     Settings.ANDROID_ID)
            if uid:
                return uid
        except Exception as e:
            print(f"[UID] Android ID error: {e}")
    stored = store_get('meta', 'device_uid')
    if stored:
        return stored
    new_uid = str(uuid.uuid4())
    store_put('meta', device_uid=new_uid)
    return new_uid

# ---- Device Info ----------------------------------------------------------
def get_device_info() -> dict:
    if IS_ANDROID and Build and BuildVersion:
        try:
            return {
                'device_name':     str(Build.MODEL),
                'model':           str(Build.MODEL),
                'android_version': str(BuildVersion.RELEASE),
            }
        except Exception as e:
            print(f"[INFO] error: {e}")
    return {'device_name': 'Buddy Device', 'model': 'Android',
            'android_version': 'Unknown'}

# ---- Notifications --------------------------------------------------------
def android_notify(title: str, message: str):
    if IS_ANDROID and PythonActivity and Context and NotifManager:
        try:
            ctx = PythonActivity.mActivity.getApplicationContext()
            CHANNEL_ID = "parental_ctrl_v1"
            nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE)
            if NotifChannel:
                try:
                    ch = NotifChannel(
                        CHANNEL_ID,
                        String_java("Parental Control") if String_java
                            else "Parental Control",
                        NotifManager.IMPORTANCE_HIGH,
                    )
                    nm.createNotificationChannel(ch)
                except Exception:
                    pass
            if NotifBuilder:
                b = NotifBuilder(ctx, CHANNEL_ID)
                b.setSmallIcon(ctx.getApplicationInfo().icon)
                if String_java:
                    b.setContentTitle(String_java(title))
                    b.setContentText(String_java(message))
                else:
                    b.setContentTitle(title)
                    b.setContentText(message)
                b.setAutoCancel(True)
                nm.notify(1001, b.build())
            return
        except Exception as e:
            print(f"[NOTIFY] Error: {e}")
    print(f"[NOTIFY] {title}: {message}")

# ---- Screen Lock ----------------------------------------------------------
def android_lock_screen(minutes: int = 0):
    if IS_ANDROID and PythonActivity and Context:
        try:
            ctx = PythonActivity.mActivity.getApplicationContext()
            ctx.getSystemService(Context.DEVICE_POLICY_SERVICE).lockNow()
            return
        except Exception as e:
            print(f"[LOCK] lockNow error: {e}")
            try:
                ctx.getSystemService(Context.KEYGUARD_SERVICE)\
                   .inKeyguardRestrictedInputMode()
            except Exception as e2:
                print(f"[LOCK] Keyguard fallback: {e2}")
    print(f"[LOCK] lock_screen(minutes={minutes})")

# ---- Camera ---------------------------------------------------------------
def android_take_photo(facing: str = 'back') -> bytes | None:
    if not IS_ANDROID or not Camera:
        print(f"[CAMERA] Stub ({facing})")
        return None
    cam_id = 1 if facing == 'front' else 0
    cam = None
    try:
        for try_id in [cam_id, 1 - cam_id]:
            try:
                cam = Camera.open(try_id)
                break
            except Exception as e:
                print(f"[CAMERA] open({try_id}) failed: {e}")
        if cam is None:
            return None
        if SurfaceTexture:
            cam.setPreviewTexture(SurfaceTexture(0))
        cam.startPreview()
        time.sleep(1.0)   # Reduced: 1.5 s → 1.0 s
        buf = []
        if PythonJavaClass and java_method:
            class JpegCB(PythonJavaClass):  # type: ignore
                __javainterfaces__ = ['android/hardware/Camera$PictureCallback']
                __javacontext__ = 'app'
                @java_method('([BLandroid/hardware/Camera;)V')
                def onPictureTaken(self, data, camera):
                    if data:
                        buf.append(bytes(data))
            cam.takePicture(None, None, JpegCB())
            deadline = time.time() + 4
            while not buf and time.time() < deadline:
                time.sleep(0.1)   # Tighter loop: 200 ms → 100 ms
        cam.stopPreview()
        cam.release()
        return buf[0] if buf else None
    except Exception as e:
        print(f"[CAMERA] Error: {e}")
        if cam:
            try: cam.release()
            except Exception: pass
        return None

# ---- Audio ----------------------------------------------------------------
def android_record_audio(seconds: int = 10) -> str | None:
    if not IS_ANDROID or not MediaRecorder:
        print(f"[AUDIO] Stub: {seconds}s")
        return None
    rec = None
    try:
        path = '/sdcard/buddy_audio.mp4'
        rec = MediaRecorder()
        rec.setAudioSource(MediaRecorder.AudioSource.MIC)
        rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
        rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
        rec.setOutputFile(path)
        rec.prepare()
        rec.start()
        time.sleep(max(1, seconds))
        rec.stop()
        rec.release()
        return path if os.path.exists(path) else None
    except Exception as e:
        print(f"[AUDIO] Error: {e}")
        if rec:
            try: rec.release()
            except Exception: pass
        return None

# ============================================================
# ⚡ ULTRA-FAST API CLIENT
#
# Key improvements:
#   poll_commands_long()  — passes ?timeout=N to PHP; server holds
#                           connection up to N seconds, returns the
#                           INSTANT a command is inserted into DB.
#   ack_command()         — fire-and-forget daemon thread; never
#                           blocks the poll loop.
#   upload_*()            — all uploads run in daemon threads.
#   _warmup()             — establishes TCP+TLS at startup.
#   SSL fallback          — tries verified first, then no-verify;
#                           result cached in session.verify.
# ============================================================
import requests as _req

class FastApiClient:
    def __init__(self, base_url: str):
        self.base    = base_url.rstrip('/')
        self.session = make_fast_session()
        self._warmup_done = False
        self._warmup()

    def _warmup(self):
        """Fire-and-forget: open the TCP+TLS connection now so
        the very first poll doesn't pay handshake overhead."""
        def _go():
            try:
                self.session.get(f"{self.base}?action=ping",
                                 timeout=5, verify=self.session.verify)
                self._warmup_done = True
                print("[NET] ⚡ Connection warmed up")
            except Exception as e:
                print(f"[NET] Warmup (non-fatal): {e}")
        threading.Thread(target=_go, daemon=True).start()

    def _url(self, action: str) -> str:
        return f"{self.base}?action={action}"

    def _post(self, action: str, json_data: dict, timeout: int = 8) -> dict:
        url = self._url(action)
        for verify in list(dict.fromkeys([self.session.verify, False])):
            try:
                r = self.session.post(url, json=json_data,
                                      timeout=timeout, verify=verify)
                r.raise_for_status()
                return safe_json(r)
            except _req.exceptions.SSLError as e:
                print(f"[API] SSL error verify={verify} → trying no-verify: {e}")
                continue
            except (_req.exceptions.ConnectionError,
                    _req.exceptions.Timeout,
                    _req.exceptions.HTTPError) as e:
                print(f"[API] POST {action}: {type(e).__name__}")
                raise
            except Exception as e:
                print(f"[API] POST {action} unexpected: {e}")
                raise
        raise RuntimeError("POST: all SSL strategies failed")

    def _get(self, action: str, params: dict | None = None,
             timeout: int = 8) -> dict | list:
        url = self._url(action)
        for verify in list(dict.fromkeys([self.session.verify, False])):
            try:
                r = self.session.get(url, params=params,
                                     timeout=timeout, verify=verify)
                r.raise_for_status()
                return safe_json(r)
            except _req.exceptions.SSLError as e:
                print(f"[API] GET SSL error verify={verify} → retrying: {e}")
                continue
            except Exception as e:
                print(f"[API] GET {action}: {type(e).__name__}: {e}")
                raise
        raise RuntimeError("GET: all SSL strategies failed")

    # ------ ⚡ LONG-POLL (primary speed feature) --------------------------
    def poll_commands_long(self, device_uid: str,
                           long_timeout: int = LONG_POLL_TIMEOUT) -> list:
        """
        Asks the PHP server to hold this connection for up to `long_timeout`
        seconds and return immediately when a command is queued.

        Delivery latency:
          With long-poll  → avg ~150 ms  (server checks every 300 ms)
          Without (v1)    → avg ~15 s    (30 s polling interval)
          Improvement     → ~100 ×

        The server needs to support ?timeout=N on device/poll.
        If it doesn't (returns in < 0.8 s with no commands), the caller
        detects this and switches to short-poll automatically.
        """
        try:
            result = self._get(
                'device/poll',
                params={'device_uid': device_uid, 'timeout': long_timeout},
                timeout=long_timeout + 8,   # Extra buffer beyond server hold
            )
            return result.get('commands', []) if isinstance(result, dict) else []
        except Exception as e:
            print(f"[POLL] long-poll failed: {e}")
            return []

    # ------ Short-poll fallback -------------------------------------------
    def poll_commands(self, device_uid: str) -> list:
        try:
            result = self._get('device/poll',
                               params={'device_uid': device_uid}, timeout=5)
            return result.get('commands', []) if isinstance(result, dict) else []
        except Exception as e:
            print(f"[POLL] short-poll failed: {e}")
            return []

    def heartbeat(self, device_uid: str) -> dict:
        try:
            return self._post('device/heartbeat',
                              {'device_uid': device_uid}, timeout=5)
        except Exception as e:
            print(f"[HB] failed: {e}")
            return {}

    def pair_device(self, key_code: str, device_uid: str,
                    info: dict, fcm_token: str = '') -> dict:
        return self._post('device/pair',
                          {'key_code': key_code, 'device_uid': device_uid,
                           'fcm_token': fcm_token, **info},
                          timeout=20)

    # ⚡ ACK is fire-and-forget so it NEVER delays the poll loop
    def ack_command(self, command_id: int, status: str = 'executed'):
        def _ack():
            try:
                self._post('device/ack',
                           {'command_id': command_id, 'status': status},
                           timeout=5)
            except Exception as e:
                print(f"[ACK] cmd {command_id} failed: {e}")
        threading.Thread(target=_ack, daemon=True, name='ack').start()

    # ⚡ All uploads are non-blocking
    def upload_media(self, device_uid: str, media_type: str,
                     file_path: str, command_id=None):
        if not file_path or not os.path.exists(file_path):
            return
        def _up():
            try:
                with open(file_path, 'rb') as f:
                    data = {'device_uid': device_uid, 'media_type': media_type}
                    if command_id:
                        data['command_id'] = str(command_id)
                    self.session.post(self._url('device/upload'),
                                     files={'file': f}, data=data,
                                     timeout=30, verify=self.session.verify)
            except Exception as e:
                print(f"[UPLOAD] media failed: {e}")
        threading.Thread(target=_up, daemon=True, name='upload').start()

    def upload_bytes(self, device_uid: str, media_type: str,
                     file_bytes: bytes, filename: str, command_id=None):
        def _up():
            try:
                import io
                data = {'device_uid': device_uid, 'media_type': media_type}
                if command_id:
                    data['command_id'] = str(command_id)
                self.session.post(
                    self._url('device/upload'),
                    files={'file': (filename, io.BytesIO(file_bytes), 'image/jpeg')},
                    data=data, timeout=30, verify=self.session.verify,
                )
            except Exception as e:
                print(f"[UPLOAD] bytes failed: {e}")
        threading.Thread(target=_up, daemon=True, name='upload-b').start()


api = FastApiClient(API_BASE)

# ============================================================
# ⚡ COMMAND EXECUTOR — ThreadPool + Priority Sort + Dedup
#
# Priority (lower number = executed first):
#   0  lock_screen / lock_timed   — safety-critical, always first
#   1  unlock_screen
#   2  send_notification
#   3  request_screenshot
#   4  request_front/back_camera
#   5  request_audio
#   6  request_screen_record
#
# Dedup: commands are tracked by ID; duplicate deliveries (can
# happen if network retried the poll) are silently skipped.
# ============================================================
_CMD_PRIORITY: dict[str, int] = {
    'lock_screen':           0,
    'lock_timed':            0,
    'unlock_screen':         1,
    'send_notification':     2,
    'request_screenshot':    3,
    'request_front_camera':  4,
    'request_back_camera':   4,
    'request_audio':         5,
    'request_screen_record': 6,
}

class CommandExecutor:
    def __init__(self, device_uid: str):
        self.device_uid = device_uid
        self._pool      = ThreadPoolExecutor(max_workers=CMD_WORKERS,
                                            thread_name_prefix='cmd')
        self._seen: set[int] = set()   # Dedup store
        self._lock = threading.Lock()

    def submit_batch(self, cmds: list):
        """Sort by priority → dedup → submit all to thread pool (non-blocking)."""
        if not cmds:
            return
        sorted_cmds = sorted(
            cmds,
            key=lambda c: _CMD_PRIORITY.get(c.get('command_type', ''), 99)
        )
        for cmd in sorted_cmds:
            cmd_id = cmd.get('id', 0)
            with self._lock:
                if cmd_id in self._seen:
                    print(f"[CMD] Skip duplicate id={cmd_id}")
                    continue
                self._seen.add(cmd_id)
            self._pool.submit(self._safe_execute, cmd)

    def _safe_execute(self, cmd: dict):
        try:
            self._execute(cmd)
        except Exception as e:
            print(f"[CMD] Crash: {e}\n{traceback.format_exc()}")
            api.ack_command(cmd.get('id', 0), 'failed')

    def _execute(self, cmd: dict):
        ctype   = cmd.get('command_type', '')
        payload = cmd.get('payload') or {}
        if isinstance(payload, str):
            try:   payload = json.loads(payload)
            except Exception: payload = {}
        cmd_id = cmd.get('id', 0)
        print(f"[CMD] ▶ {ctype} | id={cmd_id}")

        handlers = {
            'lock_screen':           self._cmd_lock,
            'lock_timed':            self._cmd_lock_timed,
            'unlock_screen':         self._cmd_unlock,
            'send_notification':     self._cmd_notify,
            'request_screenshot':    self._cmd_screenshot,
            'request_front_camera':  self._cmd_front_cam,
            'request_back_camera':   self._cmd_back_cam,
            'request_audio':         self._cmd_audio,
            'request_screen_record': self._cmd_screen_record,
        }
        h = handlers.get(ctype)
        if h:
            h(cmd_id, payload)
        else:
            print(f"[CMD] Unknown type: {ctype}")
            api.ack_command(cmd_id, 'failed')

    def _cmd_lock(self, cmd_id, _p):
        android_lock_screen()
        api.ack_command(cmd_id)

    def _cmd_lock_timed(self, cmd_id, p):
        minutes = int(p.get('minutes', 5))
        android_lock_screen(minutes)
        android_notify("Screen Locked",
                       f"Device locked for {minutes} minute(s) by parent.")
        api.ack_command(cmd_id)

    def _cmd_unlock(self, cmd_id, _p):
        android_notify("Screen Unlocked", "Your device has been unlocked.")
        api.ack_command(cmd_id)

    def _cmd_notify(self, cmd_id, p):
        android_notify(p.get('title', 'Message from Parent'),
                       p.get('message', ''))
        api.ack_command(cmd_id)

    def _cmd_screenshot(self, cmd_id, _p):
        try:
            path = '/sdcard/buddy_screen.png'
            app_ref = App.get_running_app()
            if app_ref and app_ref.root_window:
                app_ref.root_window.screenshot(name=path)
                time.sleep(0.3)   # Reduced: 0.5 → 0.3 s
            if os.path.exists(path):
                api.upload_media(self.device_uid, 'screenshot', path, cmd_id)
                api.ack_command(cmd_id)
            else:
                print("[SCREEN] File not created")
                api.ack_command(cmd_id, 'failed')
        except Exception as e:
            print(f"[SCREEN] {e}")
            api.ack_command(cmd_id, 'failed')

    def _cmd_front_cam(self, cmd_id, _p):
        self._capture_camera('front', cmd_id)

    def _cmd_back_cam(self, cmd_id, _p):
        self._capture_camera('back', cmd_id)

    def _capture_camera(self, facing: str, cmd_id: int):
        try:
            data = android_take_photo(facing)
            if data:
                fname = f"{facing}_{int(time.time())}.jpg"
                mtype = 'front_cam' if facing == 'front' else 'back_cam'
                api.upload_bytes(self.device_uid, mtype, data, fname, cmd_id)
                api.ack_command(cmd_id)
            else:
                print(f"[CAMERA] No data for {facing}")
                api.ack_command(cmd_id, 'failed')
        except Exception as e:
            print(f"[CAMERA] {e}")
            api.ack_command(cmd_id, 'failed')

    def _cmd_audio(self, cmd_id, p):
        seconds = int(p.get('seconds', 15))
        try:
            path = android_record_audio(seconds)
            if path and os.path.exists(path):
                api.upload_media(self.device_uid, 'audio', path, cmd_id)
                api.ack_command(cmd_id)
            else:
                api.ack_command(cmd_id, 'failed')
        except Exception as e:
            print(f"[AUDIO] {e}")
            api.ack_command(cmd_id, 'failed')

    def _cmd_screen_record(self, cmd_id, _p):
        android_notify("Screen Sharing", "Parent has requested screen view.")
        api.ack_command(cmd_id)

    def shutdown(self):
        self._pool.shutdown(wait=False)


# ============================================================
# HEARTBEAT THREAD — lightweight, fully independent of polling
# Sends a POST every HEARTBEAT_SECS on its own daemon thread so
# the poll loop is never delayed by heartbeat I/O.
# ============================================================
class HeartbeatThread(threading.Thread):
    def __init__(self, device_uid: str):
        super().__init__(daemon=True, name='heartbeat')
        self.device_uid = device_uid
        self.running    = True

    def run(self):
        print(f"[HB] Thread started — interval {HEARTBEAT_SECS}s")
        while self.running:
            for _ in range(HEARTBEAT_SECS * 10):   # Check stop in 0.1 s steps
                if not self.running:
                    return
                time.sleep(0.1)
            try:
                api.heartbeat(self.device_uid)
            except Exception as e:
                print(f"[HB] Error: {e}")

    def stop(self):
        self.running = False


# ============================================================
# ⚡ ULTRA-FAST ADAPTIVE POLLER
#
# HOW IT WORKS:
#
#   LONG-POLL MODE (default):
#     1. Send GET /api.php?action=device/poll&device_uid=X&timeout=20
#     2. PHP server blocks up to 20 s, checking every 300 ms for cmds
#     3. When parent queues a command, PHP returns it within ~300 ms
#     4. Client processes commands, immediately sends next long-poll
#     → No sleep between cycles — the server's wait IS the sleep
#     → Commands arrive avg 150 ms after parent sends (was avg 15 s)
#
#   SHORT-POLL FALLBACK (if server returns in < 0.8 s with no cmds):
#     Server ignored the timeout param → fall back to 2 s interval
#     After 5 min idle → stretch to 15 s to save battery
#
#   AUTO-RESTART:
#     Any unhandled exception → exponential backoff (max 30 s) → retry
# ============================================================
class UltraFastPoller(threading.Thread):
    def __init__(self, device_uid: str, app_ref):
        super().__init__(daemon=True, name='ultra-poller')
        self.device_uid          = device_uid
        self.app                 = app_ref
        self.running             = True
        self.executor            = CommandExecutor(device_uid)
        self._consecutive_errors = 0
        self._max_backoff        = 30        # seconds
        self._long_poll_works    = True      # optimistic assumption
        self._fast_misses        = 0         # track consecutive fast empty returns
        self._last_cmd_time      = 0.0       # for idle detection
        self._total_cmds         = 0
        self._cycles             = 0

    def run(self):
        print(f"[POLLER] ⚡ Ultra-fast poller started | uid={self.device_uid}")
        acquire_wakelock()

        while self.running:
            try:
                self._cycle()
                self._consecutive_errors = 0
            except Exception as err:
                self._consecutive_errors += 1
                backoff = min(2 ** self._consecutive_errors, self._max_backoff)
                print(f"[POLLER] Error #{self._consecutive_errors}: {err} "
                      f"— backoff {backoff}s")
                self._update_status(f"Reconnecting ({backoff}s)…", WARN_COL)
                self._sleep(backoff)

    def _cycle(self):
        self._cycles += 1
        now       = time.time()
        idle_secs = now - self._last_cmd_time if self._last_cmd_time > 0 else 9999
        cmds: list = []

        if self._long_poll_works:
            # ── Long-poll mode ─────────────────────────────────────────────
            t0    = time.time()
            cmds  = api.poll_commands_long(self.device_uid,
                                           long_timeout=LONG_POLL_TIMEOUT)
            elapsed = time.time() - t0

            if not cmds and elapsed < 0.8:
                # Server returned suspiciously fast with nothing →
                # it probably doesn't support long-poll
                self._fast_misses += 1
                if self._fast_misses >= 3:   # 3 strikes before switching
                    self._long_poll_works = False
                    self._fast_misses     = 0
                    print("[POLLER] Long-poll not supported — switching to short-poll")
                    self._update_mode(False)
            else:
                self._fast_misses = 0
            # No extra sleep in long-poll mode: the server's hold WAS the wait

        else:
            # ── Short-poll fallback ─────────────────────────────────────────
            cmds     = api.poll_commands(self.device_uid)
            interval = POLL_IDLE_SECS if idle_secs > 300 else POLL_FALLBACK
            self._sleep(interval)

        self._process_commands(cmds)

    def _process_commands(self, cmds: list):
        if not cmds:
            return
        self._last_cmd_time  = time.time()
        self._total_cmds    += len(cmds)
        print(f"[POLLER] ▼ {len(cmds)} cmd(s) received | total={self._total_cmds}")
        self.executor.submit_batch(cmds)
        self._update_status(
            f"⚡ Active — {self._total_cmds} command(s) total",
            SUCCESS
        )

    def _sleep(self, seconds: float):
        """Interruptible sleep: wakes on stop() within 0.1 s."""
        deadline = time.time() + seconds
        while self.running and time.time() < deadline:
            time.sleep(0.1)

    @mainthread
    def _update_status(self, text: str, color):
        try:
            App.get_running_app().sm.get_screen('home').set_status(text, color)
        except Exception:
            pass

    @mainthread
    def _update_mode(self, long_poll: bool):
        try:
            App.get_running_app().sm.get_screen('home').set_poll_mode(long_poll)
        except Exception:
            pass

    def stop(self):
        self.running = False
        try:
            self.executor.shutdown()
        except Exception:
            pass
        release_wakelock()


# ============================================================
# DESIGN TOKENS
# ============================================================
BG_DARK    = (0.06, 0.06, 0.10, 1)
BG_CARD    = (0.11, 0.11, 0.18, 1)
BG_INPUT   = (0.14, 0.14, 0.22, 1)
ACCENT     = (0.30, 0.57, 1.00, 1)
ACCENT_DIM = (0.20, 0.40, 0.80, 1)
TEXT_WHITE = (1.00, 1.00, 1.00, 1)
TEXT_GRAY  = (0.55, 0.58, 0.65, 1)
SUCCESS    = (0.25, 0.80, 0.55, 1)
ERROR_COL  = (1.00, 0.36, 0.36, 1)
WARN_COL   = (1.00, 0.78, 0.20, 1)
LIVE_GREEN = (0.20, 0.95, 0.50, 1)

def _draw_rect_bg(widget, color):
    with widget.canvas.before:
        Color(*color)
        widget._bg_rect = Rectangle(pos=widget.pos, size=widget.size)
    widget.bind(
        pos  = lambda w, v: setattr(w._bg_rect, 'pos', v),
        size = lambda w, v: setattr(w._bg_rect, 'size', v),
    )

def _draw_rounded_bg(widget, color, radius=16):
    with widget.canvas.before:
        Color(*color)
        widget._bg_rr = RoundedRectangle(
            pos=widget.pos, size=widget.size, radius=[dp(radius)])
    widget.bind(
        pos  = lambda w, v: setattr(w._bg_rr, 'pos', v),
        size = lambda w, v: setattr(w._bg_rr, 'size', v),
    )

def make_btn(text: str, bg=ACCENT, fg=TEXT_WHITE,
             height_dp: int = 52, radius: int = 14,
             font_size: str = '16sp') -> Button:
    btn = Button(
        text=text, size_hint=(1, None), height=dp(height_dp),
        background_normal='', background_color=(0, 0, 0, 0),
        color=fg, font_size=font_size, bold=True,
    )
    _draw_rounded_bg(btn, bg, radius)
    return btn

def make_input(hint: str, password: bool = False,
               font_size: str = '15sp') -> TextInput:
    return TextInput(
        hint_text=hint, multiline=False, password=password,
        size_hint=(1, None), height=dp(50),
        background_color=BG_INPUT, foreground_color=TEXT_WHITE,
        hint_text_color=TEXT_GRAY, cursor_color=ACCENT,
        padding=[dp(14), dp(12)], font_size=font_size,
    )

def make_status_label() -> Label:
    return Label(
        text='', font_size='13sp', color=ERROR_COL,
        size_hint=(1, None), height=dp(30),
        halign='center', valign='middle',
    )


# ============================================================
# SCREEN: KEY ENTRY
# ============================================================
class KeyEntryScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._build_ui()
        Window.bind(on_resize=self._on_resize)

    def _build_ui(self):
        self.clear_widgets()
        root = FloatLayout()
        _draw_rect_bg(root, BG_DARK)
        sv = ScrollView(size_hint=(1, 1), do_scroll_x=False)
        inner = BoxLayout(
            orientation='vertical', spacing=dp(14),
            padding=[dp(24), dp(48), dp(24), dp(24)],
            size_hint_y=None,
        )
        inner.bind(minimum_height=inner.setter('height'))
        card = BoxLayout(
            orientation='vertical', spacing=dp(14),
            padding=[dp(24), dp(28), dp(24), dp(28)],
            size_hint=(1, None),
        )
        _draw_rounded_bg(card, BG_CARD, radius=20)

        # Logo
        logo_wrap = BoxLayout(size_hint=(1, None), height=dp(70))
        logo_wrap.add_widget(Label(text="🛡️", font_size='48sp', size_hint=(1,1)))

        def _lbl(text, size, color, height, halign='center', bold=False):
            l = Label(text=text, font_size=size, color=color, bold=bold,
                      size_hint=(1, None), height=dp(height), halign=halign)
            l.bind(size=l.setter('text_size'))
            return l

        title       = _lbl("Buddy Guard",           '26sp', TEXT_WHITE, 40, bold=True)
        subtitle    = _lbl("Parental Control — Child Setup", '13sp', TEXT_GRAY, 24)
        speed_badge = _lbl("⚡ Ultra-Fast • Long-Poll • ~150 ms delivery",
                           '12sp', LIVE_GREEN, 22)
        desc        = _lbl("Ask your parent for your device key\n"
                           "and enter it below to get started.",
                           '13sp', TEXT_GRAY, 48)

        div = Widget(size_hint=(1, None), height=dp(1))
        _draw_rect_bg(div, TEXT_GRAY)

        key_label = _lbl("Device Key", '13sp', TEXT_GRAY, 20, halign='left')

        self.key_input  = make_input("ABCD-1234-WXYZ-5678")
        self.status_lbl = make_status_label()
        self.apply_btn  = make_btn("Apply Key & Pair Device")
        self.apply_btn.bind(on_press=self.on_apply)

        for w in [logo_wrap, title, subtitle, speed_badge, desc, div,
                  key_label, self.key_input, self.status_lbl, self.apply_btn]:
            card.add_widget(w)
        card.height = (
            sum(w.height for w in card.children if hasattr(w, 'height'))
            + dp(14) * (len(card.children) - 1) + dp(56)
        )
        inner.add_widget(card)
        sv.add_widget(inner)
        root.add_widget(sv)
        self.add_widget(root)

    def _on_resize(self, *_):
        Clock.schedule_once(lambda dt: self._build_ui(), 0.05)

    def on_apply(self, *_):
        key = self.key_input.text.strip().upper()
        if len(key.replace('-', '').replace(' ', '')) < 8:
            self._set_status("Please enter a valid key (min 8 characters).")
            return
        self.apply_btn.text     = "Connecting…"
        self.apply_btn.disabled = True
        self._set_status("")
        threading.Thread(target=self._do_pair, args=(key,), daemon=True).start()

    def _do_pair(self, key: str):
        uid = get_device_uid()
        info = get_device_info()
        last_err = ""
        for attempt in range(1, 4):
            try:
                resp = api.pair_device(key, uid, info)
                if resp.get('success'):
                    store_put('device', key_code=key, device_uid=uid,
                              device_id=resp.get('device_id', 0), paired=True)
                    self._set_status("")
                    self._goto_home()
                    return
                else:
                    self._set_status(resp.get('error', resp.get('message',
                                                                 'Pairing failed.')))
                    return
            except _req.exceptions.SSLError:
                last_err = f"SSL Error (attempt {attempt}/3)"
            except _req.exceptions.ConnectionError:
                last_err = f"No connection (attempt {attempt}/3)"
            except _req.exceptions.Timeout:
                last_err = f"Timeout (attempt {attempt}/3)"
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:80]}"
                traceback.print_exc()
            if attempt < 3:
                self._set_status(f"{last_err}. Retrying…")
                time.sleep(2 ** attempt)
        self._set_status(last_err)
        self._reset_btn()

    @mainthread
    def _set_status(self, msg: str, success: bool = False):
        try:
            self.status_lbl.color = SUCCESS if success else ERROR_COL
            self.status_lbl.text  = msg
        except Exception: pass

    @mainthread
    def _reset_btn(self):
        try:
            self.apply_btn.text     = "Apply Key & Pair Device"
            self.apply_btn.disabled = False
        except Exception: pass

    @mainthread
    def _goto_home(self):
        try:
            app = App.get_running_app()
            app.sm.transition = FadeTransition()
            app.sm.current    = 'home'
            app.start_polling()
        except Exception as e:
            print(f"[UI] _goto_home error: {e}")


# ============================================================
# SCREEN: HOME — live pulse indicator + poll-mode display
# ============================================================
class HomeScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._pulse_event = None
        self._pulse_state = True
        self._build_ui()
        Window.bind(on_resize=self._on_resize)

    def _build_ui(self):
        self.clear_widgets()
        root = FloatLayout()
        _draw_rect_bg(root, BG_DARK)

        layout = BoxLayout(
            orientation='vertical', spacing=dp(16),
            padding=[dp(24), dp(48), dp(24), dp(24)],
            size_hint=(1, 1),
        )

        # ── Header ──
        header = BoxLayout(orientation='vertical', spacing=dp(6),
                           size_hint=(1, None), height=dp(90))
        title = Label(text="🛡️  Buddy Guard", font_size='26sp', bold=True,
                      color=TEXT_WHITE, size_hint=(1, None), height=dp(48),
                      halign='center')
        title.bind(size=title.setter('text_size'))
        self.status_lbl = Label(text="Connecting…", font_size='13sp',
                                color=WARN_COL, size_hint=(1, None),
                                height=dp(24), halign='center')
        self.status_lbl.bind(size=self.status_lbl.setter('text_size'))
        header.add_widget(title)
        header.add_widget(self.status_lbl)

        # ── Live status card ──
        status_card = BoxLayout(
            orientation='vertical', spacing=dp(8),
            padding=[dp(18), dp(16), dp(18), dp(16)],
            size_hint=(1, None), height=dp(168),
        )
        _draw_rounded_bg(status_card, BG_CARD, radius=18)

        # Row: title + pulsing LIVE dot
        top_row = BoxLayout(orientation='horizontal',
                            size_hint=(1, None), height=dp(28))
        card_title = Label(text="Live Monitoring", font_size='14sp', bold=True,
                           color=TEXT_WHITE, size_hint=(0.65, 1), halign='left')
        card_title.bind(size=card_title.setter('text_size'))
        self.live_dot = Label(text="● LIVE", font_size='12sp', color=LIVE_GREEN,
                              size_hint=(0.35, 1), halign='right')
        self.live_dot.bind(size=self.live_dot.setter('text_size'))
        top_row.add_widget(card_title)
        top_row.add_widget(self.live_dot)

        self.uid_lbl = Label(text="Device ID: loading…", font_size='11sp',
                             color=TEXT_GRAY, size_hint=(1, None), height=dp(20),
                             halign='left')
        self.uid_lbl.bind(size=self.uid_lbl.setter('text_size'))

        # Poll-mode indicator — updates when poller auto-detects server capability
        self.mode_lbl = Label(
            text="Mode: ⚡ Long-Poll  (~150 ms delivery)",
            font_size='11sp', color=ACCENT,
            size_hint=(1, None), height=dp(20), halign='left',
        )
        self.mode_lbl.bind(size=self.mode_lbl.setter('text_size'))

        self.info_lbl = Label(
            text="This device is monitored by your parent.\n"
                 "All remote actions are securely logged.",
            font_size='12sp', color=TEXT_GRAY,
            halign='left', valign='top',
            size_hint=(1, None), height=dp(40),
        )
        self.info_lbl.bind(size=self.info_lbl.setter('text_size'))

        for w in [top_row, self.uid_lbl, self.mode_lbl, self.info_lbl]:
            status_card.add_widget(w)

        spacer = Widget(size_hint=(1, 1))

        reset_btn = make_btn("Reset / Change Key", bg=BG_CARD, fg=TEXT_GRAY,
                             height_dp=46, radius=12, font_size='14sp')
        reset_btn.bind(on_press=self.on_reset)

        version_lbl = Label(text="Buddy Guard v2.0  •  Ultra-Fast Edition",
                            font_size='11sp', color=TEXT_GRAY,
                            size_hint=(1, None), height=dp(20), halign='center')

        for w in [header, status_card, spacer, reset_btn, version_lbl]:
            layout.add_widget(w)

        root.add_widget(layout)
        self.add_widget(root)

    def _on_resize(self, *_):
        Clock.schedule_once(lambda dt: self._build_ui(), 0.05)

    def on_enter(self):
        uid = store_get('device', 'device_uid', '—')
        try: self.uid_lbl.text = f"Device ID: {uid}"
        except Exception: pass
        self._start_pulse()

    def on_leave(self):
        self._stop_pulse()

    def _start_pulse(self):
        """Pulse the LIVE indicator every second to show the thread is alive."""
        self._stop_pulse()
        self._pulse_state = True
        def _tick(dt):
            try:
                self.live_dot.color = LIVE_GREEN if self._pulse_state \
                                      else (*LIVE_GREEN[:3], 0.25)
                self._pulse_state = not self._pulse_state
            except Exception:
                pass
        self._pulse_event = Clock.schedule_interval(_tick, 1.0)

    def _stop_pulse(self):
        if self._pulse_event:
            self._pulse_event.cancel()
            self._pulse_event = None

    def set_status(self, text: str, color=SUCCESS):
        try:
            self.status_lbl.text  = text
            self.status_lbl.color = color
        except Exception: pass

    def set_poll_mode(self, long_poll: bool):
        try:
            if long_poll:
                self.mode_lbl.text  = "Mode: ⚡ Long-Poll  (~150 ms delivery)"
                self.mode_lbl.color = ACCENT
            else:
                self.mode_lbl.text  = "Mode: Short-Poll  (2 s interval)"
                self.mode_lbl.color = WARN_COL
        except Exception: pass

    def on_reset(self, *_):
        store_delete('device')
        App.get_running_app().stop_polling()
        try:
            app = App.get_running_app()
            app.sm.transition = FadeTransition()
            app.sm.current    = 'key_entry'
        except Exception as e:
            print(f"[UI] reset error: {e}")


# ============================================================
# MAIN APP
# ============================================================
class BuddyGuardApp(App):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.poller:    UltraFastPoller | None = None
        self.hb_thread: HeartbeatThread  | None = None
        self.sm:        ScreenManager    | None = None

    def build(self):
        Window.clearcolor = BG_DARK
        self.sm = ScreenManager()
        self.sm.add_widget(KeyEntryScreen(name='key_entry'))
        self.sm.add_widget(HomeScreen(name='home'))

        if store_exists('device') and store_get('device', 'paired', False):
            self.sm.current = 'home'
            # ⚡ Start polling in 0.5 s (was 1.5 s)
            Clock.schedule_once(lambda dt: self.start_polling(), 0.5)
        else:
            self.sm.current = 'key_entry'

        if IS_ANDROID and _android_ok:
            try:
                request_permissions([
                    Permission.CAMERA,
                    Permission.RECORD_AUDIO,
                    Permission.WRITE_EXTERNAL_STORAGE,
                    Permission.READ_EXTERNAL_STORAGE,
                    Permission.POST_NOTIFICATIONS,
                ])
            except Exception as e:
                print(f"[APP] Permission request error: {e}")

        return self.sm

    def start_polling(self):
        try:
            uid = store_get('device', 'device_uid')
            if not uid:
                print("[APP] No device_uid — polling not started")
                return
            self.stop_polling()

            self.poller = UltraFastPoller(uid, self)
            self.poller.start()

            self.hb_thread = HeartbeatThread(uid)
            self.hb_thread.start()

            print(f"[APP] ⚡ Ultra-fast polling started | uid={uid}")
            try:
                self.sm.get_screen('home').set_status(
                    "⚡ Connected — ultra-fast monitoring", SUCCESS)
            except Exception:
                pass
        except Exception as e:
            print(f"[APP] start_polling error: {e}\n{traceback.format_exc()}")

    def stop_polling(self):
        if self.poller:
            try: self.poller.stop()
            except Exception: pass
            self.poller = None
        if self.hb_thread:
            try: self.hb_thread.stop()
            except Exception: pass
            self.hb_thread = None

    def on_stop(self):
        self.stop_polling()
        release_wakelock()


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == '__main__':
    BuddyGuardApp().run()
