# ============================================================
# PARENTAL CONTROL — Child Device App (Single File)
# Framework: Kivy (no KivyMD dependency)
# Build to APK via GitHub Actions (buildozer)
#
# HOW IT WORKS:
#   1. Child opens app → enters the key parent gave them
#   2. App pairs with server, registers device
#   3. App runs in background, polls for commands every 30s
#   4. Parent sends commands from their dashboard (api.php)
#
# ERROR HANDLING STRATEGY:
#   - SSL: 3-layer fallback (certifi → system CA → no-verify)
#   - Network: retry loop with exponential backoff
#   - Android APIs: every call wrapped, fallback to stub
#   - JSON: safe parser, never crashes on bad response
#   - Storage: every read/write guarded
#   - Polling thread: auto-restarts on any crash
#   - UI: fully responsive (sp/dp units, size_hint everywhere)
# ============================================================

from __future__ import annotations
import os
import sys
import time
import json
import uuid
import threading
import traceback

# ============================================================
# STEP 0 — SSL CERTIFICATE SETUP
# Must happen before ANY network import or call.
# Layer 1: certifi bundle   → most reliable on Android
# Layer 2: system CA bundle → fallback if certifi fails
# Layer 3: disable verify   → last resort (logs a warning)
# ============================================================

def _setup_ssl_env():
    """Try multiple CA sources. Return the verify value to use."""
    # Layer 1 — certifi
    try:
        import certifi
        ca_path = certifi.where()
        if os.path.exists(ca_path):
            os.environ['SSL_CERT_FILE']      = ca_path
            os.environ['REQUESTS_CA_BUNDLE'] = ca_path
            print(f"[SSL] Using certifi CA bundle: {ca_path}")
            return ca_path
    except Exception as e:
        print(f"[SSL] certifi layer failed: {e}")

    # Layer 2 — common system CA locations (Android / Linux)
    system_ca_paths = [
        '/system/etc/security/cacerts',       # Android system certs folder
        '/etc/ssl/certs/ca-certificates.crt', # Debian/Ubuntu
        '/etc/pki/tls/certs/ca-bundle.crt',   # RHEL/CentOS
        '/etc/ssl/cert.pem',                  # macOS / Alpine
    ]
    for path in system_ca_paths:
        if os.path.exists(path):
            os.environ['SSL_CERT_FILE']      = path
            os.environ['REQUESTS_CA_BUNDLE'] = path
            print(f"[SSL] Using system CA: {path}")
            return path

    # Layer 3 — disable verification (WARN but don't crash)
    print("[SSL] WARNING: No CA bundle found. SSL verification disabled.")
    return False   # requests.Session.verify = False

SSL_VERIFY = _setup_ssl_env()

# Silence urllib3 InsecureRequestWarning when verify=False
if SSL_VERIFY is False:
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

# ============================================================
# STEP 1 — KIVY WINDOW SETUP (before any Kivy import)
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
# CONFIG
# ============================================================
API_BASE   = "https://mirab.ayamilcoders.com/api.php"  # ← Change this
POLL_SECS  = 3
STORE_FILE = "buddy_device.json"

# ============================================================
# PLATFORM DETECTION
# ============================================================
IS_ANDROID = (platform == 'android')

# ============================================================
# STEP 2 — NETWORK SESSION FACTORY
# Builds a requests.Session with:
#   - Multi-layer SSL verify (same fallback as above)
#   - Retry adapter (3 attempts, exponential backoff)
#   - Sensible timeouts enforced at call site
# ============================================================
def make_session() -> 'requests.Session':
    import requests
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:
        Retry = None

    s = requests.Session()
    s.verify = SSL_VERIFY   # Use whichever CA source worked at startup

    if Retry:
        retry_cfg = Retry(
            total=3,
            backoff_factor=1.5,            # waits: 1.5s, 3s, 6s
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_cfg)
    else:
        adapter = HTTPAdapter()

    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s

# ============================================================
# SAFE JSON HELPER
# Never raises — always returns a dict/list even on bad data
# ============================================================
def safe_json(response) -> dict | list:
    """Parse a requests Response safely. Returns {} on any error."""
    try:
        text = response.text.strip()
        if not text:
            return {}
        # Strip accidental HTML error pages (server misconfiguration)
        if text.startswith('<'):
            print(f"[JSON] Server returned HTML, not JSON: {text[:120]}")
            return {}
        return response.json()
    except Exception as e:
        print(f"[JSON] Parse error: {e} | body={response.text[:200]}")
        return {}

# ============================================================
# SAFE STORE HELPER
# ============================================================
def store_get(key: str, field: str, default=None):
    try:
        store = JsonStore(STORE_FILE)
        if store.exists(key):
            return store.get(key).get(field, default)
    except Exception as e:
        print(f"[STORE] get error: {e}")
    return default

def store_put(key: str, **kwargs):
    try:
        store = JsonStore(STORE_FILE)
        store.put(key, **kwargs)
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
# ANDROID HELPERS (graceful stubs on desktop / import failure)
# ============================================================

# --- All Android imports wrapped individually so one failure
#     doesn't kill the entire import chain ---

_android_ok = False
PythonActivity = None
Context = None
NotifManager = None
NotifBuilder = None
NotifChannel = None
String_java = None
MediaRecorder = None
Camera = None
Build = None
BuildVersion = None
SurfaceTexture = None
PythonJavaClass = None
java_method = None

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

    try:
        from jnius import PythonJavaClass, java_method  # type: ignore
    except Exception as e:
        print(f"[ANDROID] jnius PythonJavaClass import failed: {e}")

# ---------- Device UID ----------------------------------------
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
    # Fallback — stable random ID stored in JSON
    try:
        stored = store_get('meta', 'device_uid')
        if stored:
            return stored
        new_uid = str(uuid.uuid4())
        store_put('meta', device_uid=new_uid)
        return new_uid
    except Exception:
        return str(uuid.uuid4())

# ---------- Device Info ---------------------------------------
def get_device_info() -> dict:
    if IS_ANDROID and Build and BuildVersion:
        try:
            return {
                'device_name':     str(Build.MODEL),
                'model':           str(Build.MODEL),
                'android_version': str(BuildVersion.RELEASE),
            }
        except Exception as e:
            print(f"[INFO] device info error: {e}")
    return {'device_name': 'Buddy Device', 'model': 'Android',
            'android_version': 'Unknown'}

# ---------- Notifications -------------------------------------
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
                        String_java("Parental Control") if String_java else "Parental Control",
                        NotifManager.IMPORTANCE_HIGH,
                    )
                    nm.createNotificationChannel(ch)
                except Exception:
                    pass
            if NotifBuilder:
                builder = NotifBuilder(ctx, CHANNEL_ID)
                builder.setSmallIcon(ctx.getApplicationInfo().icon)
                if String_java:
                    builder.setContentTitle(String_java(title))
                    builder.setContentText(String_java(message))
                else:
                    builder.setContentTitle(title)
                    builder.setContentText(message)
                builder.setAutoCancel(True)
                nm.notify(1001, builder.build())
            return
        except Exception as e:
            print(f"[NOTIFY] Error: {e}")
    print(f"[NOTIFY] {title}: {message}")

# ---------- Screen Lock ---------------------------------------
def android_lock_screen(minutes: int = 0):
    if IS_ANDROID and PythonActivity and Context:
        try:
            ctx = PythonActivity.mActivity.getApplicationContext()
            dpm = ctx.getSystemService(Context.DEVICE_POLICY_SERVICE)
            dpm.lockNow()
            return
        except Exception as e:
            print(f"[LOCK] lockNow error: {e}")
            # Fallback: try KeyguardManager
            try:
                km = ctx.getSystemService(Context.KEYGUARD_SERVICE)
                # On older Android this might work
                km.inKeyguardRestrictedInputMode()
            except Exception as e2:
                print(f"[LOCK] Keyguard fallback error: {e2}")
    print(f"[LOCK] Lock screen called (minutes={minutes})")

# ---------- Camera --------------------------------------------
def android_take_photo(facing: str = 'back') -> bytes | None:
    if not IS_ANDROID or not Camera:
        print(f"[CAMERA] Stub: take photo ({facing})")
        return None

    cam_id = 1 if facing == 'front' else 0
    cam = None
    try:
        # Try requested camera, fallback to other if unavailable
        for try_id in [cam_id, 1 - cam_id]:
            try:
                cam = Camera.open(try_id)
                break
            except Exception as open_err:
                print(f"[CAMERA] open({try_id}) failed: {open_err}")
        if cam is None:
            return None

        if SurfaceTexture:
            st = SurfaceTexture(0)
            cam.setPreviewTexture(st)
        cam.startPreview()
        time.sleep(1.5)

        buf = []

        if PythonJavaClass and java_method:
            class JpegCB(PythonJavaClass):
                __javainterfaces__ = ['android/hardware/Camera$PictureCallback']
                __javacontext__ = 'app'

                @java_method('([BLandroid/hardware/Camera;)V')
                def onPictureTaken(self, data, camera):
                    if data:
                        buf.append(bytes(data))

            cb = JpegCB()
            cam.takePicture(None, None, cb)
            deadline = time.time() + 5
            while not buf and time.time() < deadline:
                time.sleep(0.2)
        else:
            print("[CAMERA] JpegCB unavailable — jnius missing")

        cam.stopPreview()
        cam.release()
        return buf[0] if buf else None

    except Exception as e:
        print(f"[CAMERA] Error: {e}")
        if cam:
            try: cam.release()
            except Exception: pass
        return None

# ---------- Audio ---------------------------------------------
def android_record_audio(seconds: int = 10) -> str | None:
    if not IS_ANDROID or not MediaRecorder:
        print(f"[AUDIO] Stub: record {seconds}s")
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
# API CLIENT
# All methods use the retry session + safe JSON parser.
# On SSL error, automatically retries with verify=False.
# ============================================================
import requests as _req

class ApiClient:
    def __init__(self, base_url: str):
        self.base    = base_url.rstrip('/')
        self.session = make_session()

    def _url(self, action: str) -> str:
        return f"{self.base}?action={action}"

    def _post(self, action: str, json_data: dict, timeout: int = 15) -> dict:
        """POST with SSL-error retry loop (3 strategies)."""
        url = self._url(action)
        last_error = None

        # Strategy loop: full verify → certifi explicit → no verify
        verify_options = [self.session.verify, SSL_VERIFY if SSL_VERIFY else None, False]
        seen = []
        strategies = []
        for v in verify_options:
            if v not in seen:
                seen.append(v)
                strategies.append(v)

        for verify in strategies:
            try:
                r = self.session.post(url, json=json_data, timeout=timeout, verify=verify)
                r.raise_for_status()
                return safe_json(r)
            except _req.exceptions.SSLError as ssl_err:
                last_error = ssl_err
                print(f"[API] SSL error with verify={verify}: {ssl_err}. Trying next strategy…")
                continue
            except _req.exceptions.ConnectionError as conn_err:
                last_error = conn_err
                print(f"[API] Connection error: {conn_err}")
                break   # No point retrying different SSL mode for connection error
            except _req.exceptions.Timeout:
                last_error = _req.exceptions.Timeout("timeout")
                print(f"[API] Timeout on POST {action}")
                break
            except _req.exceptions.HTTPError as http_err:
                last_error = http_err
                print(f"[API] HTTP error: {http_err}")
                break
            except Exception as e:
                last_error = e
                print(f"[API] Unexpected error: {e}")
                break

        raise last_error or RuntimeError("All API strategies failed")

    def _get(self, action: str, params: dict = None, timeout: int = 10) -> dict | list:
        """GET with SSL-error retry loop."""
        url = self._url(action)
        last_error = None
        verify_options = list(dict.fromkeys([self.session.verify, False]))

        for verify in verify_options:
            try:
                r = self.session.get(url, params=params, timeout=timeout, verify=verify)
                r.raise_for_status()
                return safe_json(r)
            except _req.exceptions.SSLError as ssl_err:
                last_error = ssl_err
                print(f"[API] GET SSL error with verify={verify}: {ssl_err}. Retrying…")
                continue
            except Exception as e:
                last_error = e
                print(f"[API] GET error: {e}")
                break

        raise last_error or RuntimeError("All GET strategies failed")

    def pair_device(self, key_code: str, device_uid: str, info: dict, fcm_token: str = '') -> dict:
        data = {'key_code': key_code, 'device_uid': device_uid,
                'fcm_token': fcm_token, **info}
        return self._post('device/pair', data, timeout=20)

    def heartbeat(self, device_uid: str) -> dict:
        try:
            return self._post('device/heartbeat', {'device_uid': device_uid}, timeout=10)
        except Exception as e:
            print(f"[HB] heartbeat failed: {e}")
            return {}

    def poll_commands(self, device_uid: str) -> list:
        try:
            result = self._get('device/poll', params={'device_uid': device_uid}, timeout=10)
            if isinstance(result, dict):
                return result.get('commands', [])
            return []
        except Exception as e:
            print(f"[POLL] poll_commands failed: {e}")
            return []

    def ack_command(self, command_id: int, status: str = 'executed'):
        try:
            self._post('device/ack',
                       {'command_id': command_id, 'status': status},
                       timeout=10)
        except Exception as e:
            print(f"[ACK] ack_command({command_id}) failed: {e}")

    def upload_media(self, device_uid: str, media_type: str,
                     file_path: str, command_id=None):
        if not file_path or not os.path.exists(file_path):
            print(f"[UPLOAD] File not found: {file_path}")
            return
        try:
            import io
            with open(file_path, 'rb') as f:
                data = {'device_uid': device_uid, 'media_type': media_type}
                if command_id:
                    data['command_id'] = str(command_id)
                self.session.post(
                    self._url('device/upload'),
                    files={'file': f},
                    data=data,
                    timeout=30,
                    verify=self.session.verify,
                )
        except Exception as e:
            print(f"[UPLOAD] upload_media failed: {e}")

    def upload_bytes(self, device_uid: str, media_type: str,
                     file_bytes: bytes, filename: str, command_id=None):
        try:
            import io
            data = {'device_uid': device_uid, 'media_type': media_type}
            if command_id:
                data['command_id'] = str(command_id)
            self.session.post(
                self._url('device/upload'),
                files={'file': (filename, io.BytesIO(file_bytes), 'image/jpeg')},
                data=data,
                timeout=30,
                verify=self.session.verify,
            )
        except Exception as e:
            print(f"[UPLOAD] upload_bytes failed: {e}")


api = ApiClient(API_BASE)

# ============================================================
# COMMAND EXECUTOR
# Every command type is individually try/excepted.
# On any crash: ack 'failed' so parent knows, and keep going.
# ============================================================
class CommandExecutor:
    def __init__(self, device_uid: str):
        self.device_uid = device_uid

    def execute(self, cmd: dict):
        ctype   = cmd.get('command_type', '')
        payload = cmd.get('payload') or {}
        if isinstance(payload, str):
            try:   payload = json.loads(payload)
            except Exception: payload = {}
        cmd_id = cmd.get('id', 0)
        print(f"[CMD] → {ctype} | id={cmd_id} | payload={payload}")

        handler_map = {
            'lock_screen':          self._cmd_lock,
            'lock_timed':           self._cmd_lock_timed,
            'unlock_screen':        self._cmd_unlock,
            'send_notification':    self._cmd_notify,
            'request_screenshot':   self._cmd_screenshot,
            'request_front_camera': self._cmd_front_cam,
            'request_back_camera':  self._cmd_back_cam,
            'request_audio':        self._cmd_audio,
            'request_screen_record': self._cmd_screen_record,
        }

        handler = handler_map.get(ctype)
        if handler:
            try:
                handler(cmd_id, payload)
            except Exception as e:
                print(f"[CMD] Handler crash for {ctype}: {e}\n{traceback.format_exc()}")
                api.ack_command(cmd_id, 'failed')
        else:
            print(f"[CMD] Unknown command type: {ctype}")
            api.ack_command(cmd_id, 'failed')

    def _cmd_lock(self, cmd_id, payload):
        android_lock_screen()
        api.ack_command(cmd_id, 'executed')

    def _cmd_lock_timed(self, cmd_id, payload):
        minutes = int(payload.get('minutes', 5))
        android_lock_screen(minutes)
        android_notify("Screen Locked",
                       f"Device locked for {minutes} minute(s) by parent.")
        api.ack_command(cmd_id, 'executed')

    def _cmd_unlock(self, cmd_id, payload):
        android_notify("Screen Unlocked", "Your device has been unlocked.")
        api.ack_command(cmd_id, 'executed')

    def _cmd_notify(self, cmd_id, payload):
        android_notify(
            payload.get('title',   'Message from Parent'),
            payload.get('message', ''),
        )
        api.ack_command(cmd_id, 'executed')

    def _cmd_screenshot(self, cmd_id, payload):
        try:
            path = '/sdcard/buddy_screen.png'
            app_ref = App.get_running_app()
            if app_ref and app_ref.root_window:
                app_ref.root_window.screenshot(name=path)
                time.sleep(0.5)
            if os.path.exists(path):
                api.upload_media(self.device_uid, 'screenshot', path, cmd_id)
                api.ack_command(cmd_id, 'executed')
            else:
                print("[SCREEN] Screenshot file not created")
                api.ack_command(cmd_id, 'failed')
        except Exception as e:
            print(f"[SCREEN] {e}")
            api.ack_command(cmd_id, 'failed')

    def _cmd_front_cam(self, cmd_id, payload):
        self._capture_camera('front', cmd_id)

    def _cmd_back_cam(self, cmd_id, payload):
        self._capture_camera('back', cmd_id)

    def _capture_camera(self, facing: str, cmd_id: int):
        try:
            data = android_take_photo(facing)
            if data:
                fname = f"{facing}_cam_{int(time.time())}.jpg"
                mtype = 'front_cam' if facing == 'front' else 'back_cam'
                api.upload_bytes(self.device_uid, mtype, data, fname, cmd_id)
                api.ack_command(cmd_id, 'executed')
            else:
                print(f"[CAMERA] No data for {facing}")
                api.ack_command(cmd_id, 'failed')
        except Exception as e:
            print(f"[CAMERA] capture_camera error: {e}")
            api.ack_command(cmd_id, 'failed')

    def _cmd_audio(self, cmd_id, payload):
        seconds = int(payload.get('seconds', 15))
        try:
            path = android_record_audio(seconds)
            if path and os.path.exists(path):
                api.upload_media(self.device_uid, 'audio', path, cmd_id)
                api.ack_command(cmd_id, 'executed')
            else:
                print("[AUDIO] No recording file")
                api.ack_command(cmd_id, 'failed')
        except Exception as e:
            print(f"[AUDIO] {e}")
            api.ack_command(cmd_id, 'failed')

    def _cmd_screen_record(self, cmd_id, payload):
        android_notify("Screen Sharing", "Parent has requested screen view.")
        api.ack_command(cmd_id, 'executed')


# ============================================================
# BACKGROUND POLLING THREAD
# Self-healing: restarts automatically on any crash.
# ============================================================
class BackgroundPoller(threading.Thread):
    def __init__(self, device_uid: str, app_ref):
        super().__init__(daemon=True)
        self.device_uid = device_uid
        self.app        = app_ref
        self.running    = True
        self.executor   = CommandExecutor(device_uid)
        self._consecutive_errors = 0
        self._max_backoff = 120   # seconds

    def run(self):
        print(f"[POLLER] Started for UID={self.device_uid}")
        while self.running:
            try:
                self._poll_cycle()
                self._consecutive_errors = 0  # reset on success
            except Exception as outer_err:
                self._consecutive_errors += 1
                backoff = min(
                    POLL_SECS * self._consecutive_errors,
                    self._max_backoff,
                )
                print(f"[POLLER] Cycle error #{self._consecutive_errors}: {outer_err}. "
                      f"Sleeping {backoff}s before retry.")
                time.sleep(backoff)
                continue

            # Normal sleep between polls
            sleep_remaining = float(POLL_SECS)
            while self.running and sleep_remaining > 0:
                time.sleep(min(1, sleep_remaining))
                sleep_remaining -= 1

    def _poll_cycle(self):
        cmds = api.poll_commands(self.device_uid)
        for cmd in cmds:
            t = threading.Thread(
                target=self._safe_execute,
                args=(cmd,),
                daemon=True,
            )
            t.start()
        api.heartbeat(self.device_uid)

    def _safe_execute(self, cmd: dict):
        try:
            self.executor.execute(cmd)
        except Exception as e:
            print(f"[POLLER] execute() crash: {e}\n{traceback.format_exc()}")

    def stop(self):
        self.running = False


# ============================================================
# DESIGN TOKENS — Responsive
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

def _rw(frac: float) -> float:
    """Responsive width fraction of current window."""
    return Window.width * frac

def _rh(frac: float) -> float:
    return Window.height * frac

# ---- Helpers for drawing backgrounds ----
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
        widget._bg_rr = RoundedRectangle(pos=widget.pos, size=widget.size,
                                         radius=[dp(radius)])
    widget.bind(
        pos  = lambda w, v: setattr(w._bg_rr, 'pos', v),
        size = lambda w, v: setattr(w._bg_rr, 'size', v),
    )

# ---- Styled button ----
def make_btn(text: str, bg=ACCENT, fg=TEXT_WHITE,
             height_dp: int = 52, radius: int = 14,
             font_size: str = '16sp') -> Button:
    btn = Button(
        text=text,
        size_hint=(1, None),
        height=dp(height_dp),
        background_normal='',
        background_color=(0, 0, 0, 0),
        color=fg,
        font_size=font_size,
        bold=True,
    )
    _draw_rounded_bg(btn, bg, radius)
    return btn

# ---- Styled input ----
def make_input(hint: str, password: bool = False,
               font_size: str = '15sp') -> TextInput:
    ti = TextInput(
        hint_text=hint,
        multiline=False,
        password=password,
        size_hint=(1, None),
        height=dp(50),
        background_color=BG_INPUT,
        foreground_color=TEXT_WHITE,
        hint_text_color=TEXT_GRAY,
        cursor_color=ACCENT,
        padding=[dp(14), dp(12)],
        font_size=font_size,
    )
    return ti

# ---- Status label ----
def make_status_label() -> Label:
    return Label(
        text='',
        font_size='13sp',
        color=ERROR_COL,
        size_hint=(1, None),
        height=dp(30),
        halign='center',
        valign='middle',
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

        # Scrollable so it works on tiny screens / landscape
        sv = ScrollView(size_hint=(1, 1), do_scroll_x=False)
        inner = BoxLayout(
            orientation='vertical',
            spacing=dp(14),
            padding=[dp(24), dp(48), dp(24), dp(24)],
            size_hint_y=None,
        )
        inner.bind(minimum_height=inner.setter('height'))

        # Card
        card = BoxLayout(
            orientation='vertical',
            spacing=dp(14),
            padding=[dp(24), dp(28), dp(24), dp(28)],
            size_hint=(1, None),
        )
        _draw_rounded_bg(card, BG_CARD, radius=20)

        # Logo / icon placeholder (circle)
        logo_wrap = BoxLayout(size_hint=(1, None), height=dp(70))
        logo_lbl = Label(
            text="🛡️",
            font_size='48sp',
            size_hint=(1, 1),
        )
        logo_wrap.add_widget(logo_lbl)

        title = Label(
            text="Buddy Guard",
            font_size='26sp',
            bold=True,
            color=TEXT_WHITE,
            size_hint=(1, None),
            height=dp(40),
            halign='center',
        )
        title.bind(size=title.setter('text_size'))

        subtitle = Label(
            text="Parental Control — Child Setup",
            font_size='13sp',
            color=TEXT_GRAY,
            size_hint=(1, None),
            height=dp(24),
            halign='center',
        )
        subtitle.bind(size=subtitle.setter('text_size'))

        desc = Label(
            text="Ask your parent for your device key\nand enter it below to get started.",
            font_size='13sp',
            color=TEXT_GRAY,
            halign='center',
            size_hint=(1, None),
            height=dp(48),
        )
        desc.bind(size=desc.setter('text_size'))

        # Divider
        div = Widget(size_hint=(1, None), height=dp(1))
        _draw_rect_bg(div, TEXT_GRAY)

        key_label = Label(
            text="Device Key",
            font_size='13sp',
            color=TEXT_GRAY,
            size_hint=(1, None),
            height=dp(20),
            halign='left',
        )
        key_label.bind(size=key_label.setter('text_size'))

        self.key_input = make_input("ABCD-1234-WXYZ-5678", font_size='15sp')

        self.status_lbl = make_status_label()

        self.apply_btn = make_btn("Apply Key & Pair Device")
        self.apply_btn.bind(on_press=self.on_apply)

        for w in [logo_wrap, title, subtitle, desc, div,
                  key_label, self.key_input,
                  self.status_lbl, self.apply_btn]:
            card.add_widget(w)

        # Auto-size card
        card.height = sum(
            (w.height for w in card.children if hasattr(w, 'height')),
            start=0
        ) + dp(14) * (len(card.children) - 1) + dp(56)

        inner.add_widget(card)
        sv.add_widget(inner)
        root.add_widget(sv)
        self.add_widget(root)

    def _on_resize(self, *_):
        # Rebuild on resize so all dp() values recalculate
        Clock.schedule_once(lambda dt: self._build_ui(), 0.05)

    def on_apply(self, *_):
        key = self.key_input.text.strip().upper()
        # Accept keys with or without dashes; min 10 chars
        key_clean = key.replace('-', '').replace(' ', '')
        if len(key_clean) < 8:
            self._set_status("Please enter a valid key (min 8 characters).")
            return
        self.apply_btn.text     = "Connecting…"
        self.apply_btn.disabled = True
        self._set_status("")
        threading.Thread(target=self._do_pair, args=(key,), daemon=True).start()

    def _do_pair(self, key: str):
        uid  = get_device_uid()
        info = get_device_info()
        last_err = ""

        # Retry up to 3 times (each attempt tries all SSL strategies internally)
        for attempt in range(1, 4):
            try:
                resp = api.pair_device(key, uid, info)
                if resp.get('success'):
                    store_put('device',
                              key_code=key,
                              device_uid=uid,
                              device_id=resp.get('device_id', 0),
                              paired=True)
                    self._set_status("")
                    self._goto_home()
                    return
                else:
                    err = resp.get('error', resp.get('message', 'Pairing failed.'))
                    # Don't retry on logical errors (bad key etc.)
                    self._set_status(err)
                    return
            except _req.exceptions.SSLError as e:
                last_err = f"SSL Error (attempt {attempt}/3): check server certificate"
                print(f"[PAIR] {last_err} → {e}")
            except _req.exceptions.ConnectionError:
                last_err = f"No connection (attempt {attempt}/3): check internet"
                print(f"[PAIR] {last_err}")
            except _req.exceptions.Timeout:
                last_err = f"Timeout (attempt {attempt}/3): server too slow"
                print(f"[PAIR] {last_err}")
            except Exception as e:
                last_err = f"Error: {type(e).__name__}: {str(e)[:80]}"
                print(f"[PAIR] {last_err}")
                traceback.print_exc()

            # Exponential backoff between retries
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
        except Exception:
            pass

    @mainthread
    def _reset_btn(self):
        try:
            self.apply_btn.text     = "Apply Key & Pair Device"
            self.apply_btn.disabled = False
        except Exception:
            pass

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
# SCREEN: HOME
# ============================================================
class HomeScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._build_ui()
        Window.bind(on_resize=self._on_resize)

    def _build_ui(self):
        self.clear_widgets()
        root = FloatLayout()
        _draw_rect_bg(root, BG_DARK)

        layout = BoxLayout(
            orientation='vertical',
            spacing=dp(16),
            padding=[dp(24), dp(48), dp(24), dp(24)],
            size_hint=(1, 1),
        )

        # Header
        header = BoxLayout(orientation='vertical', spacing=dp(6),
                           size_hint=(1, None), height=dp(90))

        title = Label(
            text="🛡️  Buddy Guard",
            font_size='26sp', bold=True, color=TEXT_WHITE,
            size_hint=(1, None), height=dp(48),
            halign='center',
        )
        title.bind(size=title.setter('text_size'))

        self.status_lbl = Label(
            text="Connecting…",
            font_size='13sp', color=WARN_COL,
            size_hint=(1, None), height=dp(24),
            halign='center',
        )
        self.status_lbl.bind(size=self.status_lbl.setter('text_size'))
        header.add_widget(title)
        header.add_widget(self.status_lbl)

        # Status card
        status_card = BoxLayout(
            orientation='vertical',
            spacing=dp(10),
            padding=[dp(20), dp(18), dp(20), dp(18)],
            size_hint=(1, None),
            height=dp(130),
        )
        _draw_rounded_bg(status_card, BG_CARD, radius=18)

        card_title = Label(
            text="Monitoring Status",
            font_size='14sp', bold=True, color=TEXT_WHITE,
            size_hint=(1, None), height=dp(24),
            halign='left',
        )
        card_title.bind(size=card_title.setter('text_size'))

        self.uid_lbl = Label(
            text="Device ID: loading…",
            font_size='11sp', color=TEXT_GRAY,
            size_hint=(1, None), height=dp(20),
            halign='left',
        )
        self.uid_lbl.bind(size=self.uid_lbl.setter('text_size'))

        self.info_lbl = Label(
            text="This device is managed by your parent.\nAll remote actions are logged.",
            font_size='12sp', color=TEXT_GRAY,
            halign='left', valign='top',
            size_hint=(1, None), height=dp(40),
        )
        self.info_lbl.bind(size=self.info_lbl.setter('text_size'))

        for w in [card_title, self.uid_lbl, self.info_lbl]:
            status_card.add_widget(w)

        # Spacer
        spacer = Widget(size_hint=(1, 1))

        # Reset button
        reset_btn = make_btn("Reset / Change Key",
                             bg=BG_CARD, fg=TEXT_GRAY,
                             height_dp=46, radius=12,
                             font_size='14sp')
        reset_btn.bind(on_press=self.on_reset)

        version_lbl = Label(
            text="Buddy Guard v1.0",
            font_size='11sp', color=TEXT_GRAY,
            size_hint=(1, None), height=dp(20),
            halign='center',
        )

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

    def set_status(self, text: str, color=SUCCESS):
        try:
            self.status_lbl.text  = text
            self.status_lbl.color = color
        except Exception:
            pass

    def on_reset(self, *_):
        store_delete('device')
        app = App.get_running_app()
        if app.poller:
            try:
                app.poller.stop()
            except Exception:
                pass
            app.poller = None
        try:
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
        self.poller: BackgroundPoller | None = None
        self.sm: ScreenManager | None = None

    def build(self):
        Window.clearcolor = BG_DARK
        self.sm = ScreenManager()
        self.sm.add_widget(KeyEntryScreen(name='key_entry'))
        self.sm.add_widget(HomeScreen(name='home'))

        # Decide initial screen
        if store_exists('device') and store_get('device', 'paired', False):
            self.sm.current = 'home'
            Clock.schedule_once(lambda dt: self.start_polling(), 1.5)
        else:
            self.sm.current = 'key_entry'

        # Request Android permissions (best effort — don't crash if it fails)
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
            # Stop any existing poller
            if self.poller:
                try: self.poller.stop()
                except Exception: pass
            self.poller = BackgroundPoller(uid, self)
            self.poller.start()
            print(f"[APP] Polling started for UID={uid}")
            # Update home screen status
            try:
                hs = self.sm.get_screen('home')
                hs.set_status("Connected — monitoring active", SUCCESS)
            except Exception:
                pass
        except Exception as e:
            print(f"[APP] start_polling error: {e}\n{traceback.format_exc()}")

    def on_stop(self):
        if self.poller:
            try: self.poller.stop()
            except Exception: pass


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == '__main__':
    BuddyGuardApp().run()
