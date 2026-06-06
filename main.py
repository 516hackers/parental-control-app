# ============================================================
# PARENTAL CONTROL — Child Device App (Single File)
# Framework: Kivy + KivyMD
# Build to APK via GitHub Actions (buildozer)
#
# HOW IT WORKS:
#   1. Child opens app → enters the key parent gave them
#   2. App pairs with server, registers device
#   3. App runs in background, polls for commands every 30s
#   4. Parent sends commands from their dashboard (api.php)
# ============================================================

import os
import time
import threading
import requests

# ============================================================
# FIX 1: SSL — point certifi CA bundle before any HTTPS call
# This must happen before Kivy imports touch the network
# ============================================================
try:
    import certifi
    os.environ['SSL_CERT_FILE']    = certifi.where()
    os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
except Exception as _ssl_e:
    print(f"[SSL] certifi setup warning: {_ssl_e}")

# Kivy must be imported before anything else touches the window
from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.core.window import Window
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.graphics import Color, Rectangle, RoundedRectangle
from kivy.utils import platform
from kivy.storage.jsonstore import JsonStore

# ============================================================
# CONFIG  —  change API_BASE to your server URL
# ============================================================
API_BASE   = "https://yourdomain.com/api.php"
POLL_SECS  = 30
STORE_FILE = "buddy_device.json"

# ============================================================
# FIX 2: SSL session factory
# All requests go through this — uses certifi bundle explicitly
# ============================================================
def make_session() -> requests.Session:
    s = requests.Session()
    try:
        import certifi
        s.verify = certifi.where()
    except Exception:
        s.verify = True   # fall back to default
    # Retry adapter — 3 retries on connection errors
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s


# ============================================================
# ANDROID HELPERS  (no-op on desktop)
# ============================================================
if platform == 'android':
    from android.permissions import request_permissions, Permission
    from android import activity
    from jnius import autoclass, PythonJavaClass, java_method

    # ── Java classes ──────────────────────────────────────────
    Context        = autoclass('android.content.Context')
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    NotifManager   = autoclass('android.app.NotificationManager')
    NotifBuilder   = autoclass('android.app.Notification$Builder')
    NotifChannel   = autoclass('android.app.NotificationChannel')
    String         = autoclass('java.lang.String')
    MediaRecorder  = autoclass('android.media.MediaRecorder')
    Camera         = autoclass('android.hardware.Camera')

    # FIX: Build$VERSION is a Java inner class — autoclass separately
    Build        = autoclass('android.os.Build')
    BuildVersion = autoclass('android.os.Build$VERSION')

    # ── Device info ───────────────────────────────────────────
    def get_device_uid() -> str:
        try:
            Settings = autoclass('android.provider.Settings$Secure')
            ctx = PythonActivity.mActivity.getApplicationContext()
            return Settings.getString(ctx.getContentResolver(),
                                      Settings.ANDROID_ID)
        except Exception:
            import uuid
            return str(uuid.uuid4())

    def get_device_info() -> dict:
        return {
            'device_name':     str(Build.MODEL),
            'model':           str(Build.MODEL),
            'android_version': str(BuildVersion.RELEASE),
        }

    # ── Notifications ─────────────────────────────────────────
    def android_notify(title: str, message: str):
        try:
            ctx = PythonActivity.mActivity.getApplicationContext()
            CHANNEL_ID = "parental_ctrl"
            nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE)
            try:
                ch = NotifChannel(CHANNEL_ID,
                                  String("Parental Control"),
                                  NotifManager.IMPORTANCE_HIGH)
                nm.createNotificationChannel(ch)
            except Exception:
                pass
            builder = NotifBuilder(ctx, CHANNEL_ID)
            builder.setSmallIcon(ctx.getApplicationInfo().icon)
            builder.setContentTitle(String(title))
            builder.setContentText(String(message))
            builder.setAutoCancel(True)
            nm.notify(1001, builder.build())
        except Exception as e:
            print(f"[NOTIFY] Error: {e}")

    # ── Screen lock ───────────────────────────────────────────
    def android_lock_screen(minutes: int = 0):
        try:
            ctx = PythonActivity.mActivity.getApplicationContext()
            dpm = ctx.getSystemService(Context.DEVICE_POLICY_SERVICE)
            dpm.lockNow()
        except Exception as e:
            print(f"[LOCK] Error: {e}")

    # ── Camera ────────────────────────────────────────────────
    def android_take_photo(facing: str = 'back') -> bytes | None:
        try:
            cam_id = 1 if facing == 'front' else 0
            cam = Camera.open(cam_id)
            SurfaceTexture = autoclass('android.graphics.SurfaceTexture')
            st = SurfaceTexture(0)
            cam.setPreviewTexture(st)
            cam.startPreview()
            time.sleep(1.5)

            buf = []

            class JpegCB(PythonJavaClass):
                __javainterfaces__ = ['android/hardware/Camera$PictureCallback']
                __javacontext__ = 'app'

                @java_method('([BLandroid/hardware/Camera;)V')
                def onPictureTaken(self, data, camera):
                    if data:
                        buf.append(bytes(data))

            cb = JpegCB()
            cam.takePicture(None, None, cb)
            timeout = time.time() + 5
            while not buf and time.time() < timeout:
                time.sleep(0.2)
            cam.stopPreview()
            cam.release()
            return buf[0] if buf else None
        except Exception as e:
            print(f"[CAMERA] Error: {e}")
            return None

    # ── Audio ─────────────────────────────────────────────────
    def android_record_audio(seconds: int = 10) -> str | None:
        try:
            path = '/sdcard/buddy_audio.mp4'
            rec = MediaRecorder()
            rec.setAudioSource(MediaRecorder.AudioSource.MIC)
            rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            rec.setOutputFile(path)
            rec.prepare()
            rec.start()
            time.sleep(seconds)
            rec.stop()
            rec.release()
            return path
        except Exception as e:
            print(f"[AUDIO] Error: {e}")
            return None

else:
    # ── Desktop stubs ─────────────────────────────────────────
    import uuid

    def get_device_uid():
        return str(uuid.uuid4())[:16]

    def get_device_info():
        return {'device_name': 'Test Device', 'model': 'Desktop',
                'android_version': '0'}

    def android_notify(title, message):
        print(f"[NOTIFY] {title}: {message}")

    def android_lock_screen(minutes=0):
        print(f"[LOCK] Lock screen called (minutes={minutes})")

    def android_take_photo(facing='back'):
        print(f"[CAMERA] Take photo ({facing})")
        return None

    def android_record_audio(seconds=10):
        print(f"[AUDIO] Record audio ({seconds}s)")
        return None


# ============================================================
# API CLIENT
# ============================================================
class ApiClient:
    def __init__(self, base_url: str):
        self.base    = base_url.rstrip('/')
        self.session = make_session()   # FIX: use SSL-aware session

    def _url(self, action: str) -> str:
        return f"{self.base}?action={action}"

    def pair_device(self, key_code, device_uid, info, fcm_token='') -> dict:
        data = {'key_code': key_code, 'device_uid': device_uid,
                'fcm_token': fcm_token, **info}
        r = self.session.post(self._url('device/pair'), json=data, timeout=15)
        r.raise_for_status()
        return r.json()

    def heartbeat(self, device_uid: str) -> dict:
        r = self.session.post(self._url('device/heartbeat'),
                              json={'device_uid': device_uid}, timeout=10)
        r.raise_for_status()
        return r.json()

    def poll_commands(self, device_uid: str) -> list:
        r = self.session.get(self._url('device/poll'),
                             params={'device_uid': device_uid}, timeout=10)
        r.raise_for_status()
        return r.json().get('commands', [])

    def ack_command(self, command_id: int, status: str = 'executed'):
        try:
            self.session.post(self._url('device/ack'),
                              json={'command_id': command_id, 'status': status},
                              timeout=10)
        except Exception as e:
            print(f"[ACK] Error: {e}")

    def upload_media(self, device_uid: str, media_type: str,
                     file_path: str, command_id=None):
        with open(file_path, 'rb') as f:
            files = {'file': f}
            data  = {'device_uid': device_uid, 'media_type': media_type}
            if command_id:
                data['command_id'] = str(command_id)
            self.session.post(self._url('device/upload'),
                              files=files, data=data, timeout=30)

    def upload_bytes(self, device_uid: str, media_type: str,
                     file_bytes: bytes, filename: str, command_id=None):
        import io
        files = {'file': (filename, io.BytesIO(file_bytes), 'image/jpeg')}
        data  = {'device_uid': device_uid, 'media_type': media_type}
        if command_id:
            data['command_id'] = str(command_id)
        self.session.post(self._url('device/upload'),
                          files=files, data=data, timeout=30)


api = ApiClient(API_BASE)


# ============================================================
# COMMAND EXECUTOR  (runs in background thread)
# ============================================================
class CommandExecutor:
    def __init__(self, device_uid: str):
        self.device_uid = device_uid

    def execute(self, cmd: dict):
        ctype   = cmd['command_type']
        payload = cmd.get('payload') or {}
        cmd_id  = cmd['id']
        print(f"[CMD] Executing: {ctype} | payload={payload}")

        try:
            if ctype == 'lock_screen':
                android_lock_screen()

            elif ctype == 'lock_timed':
                minutes = int(payload.get('minutes', 5))
                android_lock_screen(minutes)
                android_notify("Screen Locked",
                               f"Device locked for {minutes} minute(s) by parent.")

            elif ctype == 'unlock_screen':
                android_notify("Screen Unlocked", "Your device has been unlocked.")

            elif ctype == 'send_notification':
                android_notify(
                    payload.get('title',   'Message from Parent'),
                    payload.get('message', '')
                )

            elif ctype == 'request_screenshot':
                self._capture_screenshot(cmd_id)
                return

            elif ctype in ('request_front_camera', 'request_back_camera'):
                facing = 'front' if ctype == 'request_front_camera' else 'back'
                self._capture_camera(facing, cmd_id)
                return

            elif ctype == 'request_audio':
                seconds = int(payload.get('seconds', 15))
                self._capture_audio(seconds, cmd_id)
                return

            elif ctype == 'request_screen_record':
                android_notify("Screen Sharing",
                               "Parent has requested screen view.")

            api.ack_command(cmd_id, 'executed')

        except Exception as e:
            print(f"[CMD] Error: {e}")
            api.ack_command(cmd_id, 'failed')

    def _capture_screenshot(self, cmd_id):
        try:
            path = '/sdcard/buddy_screen.png'
            app  = App.get_running_app()
            if app and app.root_window:
                app.root_window.screenshot(name=path)
            else:
                path = None
            if path and os.path.exists(path):
                api.upload_media(self.device_uid, 'screenshot', path, cmd_id)
            api.ack_command(cmd_id, 'executed')
        except Exception as e:
            print(f"[SCREEN] {e}")
            api.ack_command(cmd_id, 'failed')

    def _capture_camera(self, facing: str, cmd_id: int):
        try:
            data = android_take_photo(facing)
            if data:
                fname = f"{facing}_cam_{int(time.time())}.jpg"
                mtype = 'front_cam' if facing == 'front' else 'back_cam'
                api.upload_bytes(self.device_uid, mtype, data, fname, cmd_id)
                api.ack_command(cmd_id, 'executed')
            else:
                api.ack_command(cmd_id, 'failed')
        except Exception as e:
            print(f"[CAMERA] {e}")
            api.ack_command(cmd_id, 'failed')

    def _capture_audio(self, seconds: int, cmd_id: int):
        try:
            path = android_record_audio(seconds)
            if path and os.path.exists(path):
                api.upload_media(self.device_uid, 'audio', path, cmd_id)
                api.ack_command(cmd_id, 'executed')
            else:
                api.ack_command(cmd_id, 'failed')
        except Exception as e:
            print(f"[AUDIO] {e}")
            api.ack_command(cmd_id, 'failed')


# ============================================================
# BACKGROUND POLLING THREAD
# ============================================================
class BackgroundPoller(threading.Thread):
    def __init__(self, device_uid: str, app_ref):
        super().__init__(daemon=True)
        self.device_uid = device_uid
        self.app        = app_ref
        self.running    = True
        self.executor   = CommandExecutor(device_uid)

    def run(self):
        while self.running:
            try:
                cmds = api.poll_commands(self.device_uid)
                for cmd in cmds:
                    t = threading.Thread(target=self.executor.execute,
                                         args=(cmd,), daemon=True)
                    t.start()
                api.heartbeat(self.device_uid)
            except Exception as e:
                print(f"[POLL] Error: {e}")
            time.sleep(POLL_SECS)

    def stop(self):
        self.running = False


# ============================================================
# UI — COLORS & SHARED STYLES
# ============================================================
BG_DARK    = (0.07, 0.07, 0.12, 1)
BG_CARD    = (0.12, 0.12, 0.20, 1)
ACCENT     = (0.29, 0.56, 1.00, 1)
TEXT_WHITE = (1, 1, 1, 1)
TEXT_GRAY  = (0.6, 0.6, 0.7, 1)
SUCCESS    = (0.27, 0.80, 0.56, 1)
ERROR      = (1.00, 0.35, 0.35, 1)


def make_bg(widget, color):
    with widget.canvas.before:
        Color(*color)
        widget._bg_rect = Rectangle(pos=widget.pos, size=widget.size)
    widget.bind(pos=lambda w, v: setattr(w._bg_rect, 'pos', v),
                size=lambda w, v: setattr(w._bg_rect, 'size', v))


def styled_btn(text: str, bg=ACCENT, fg=TEXT_WHITE, height=52, radius=14) -> Button:
    btn = Button(
        text=text,
        size_hint_y=None,
        height=height,
        background_normal='',
        background_color=(0, 0, 0, 0),
        color=fg,
        font_size='16sp',
        bold=True,
    )
    with btn.canvas.before:
        Color(*bg)
        btn._rect = RoundedRectangle(pos=btn.pos, size=btn.size, radius=[radius])
    btn.bind(pos=lambda w, v: setattr(w._rect, 'pos', v),
             size=lambda w, v: setattr(w._rect, 'size', v))
    return btn


def styled_input(hint: str, password: bool = False) -> TextInput:
    return TextInput(
        hint_text=hint,
        multiline=False,
        password=password,
        size_hint_y=None,
        height=50,
        background_color=(0.15, 0.15, 0.24, 1),
        foreground_color=TEXT_WHITE,
        hint_text_color=TEXT_GRAY,
        cursor_color=ACCENT,
        padding=[14, 12],
        font_size='15sp',
    )


# ============================================================
# SCREEN: KEY ENTRY (first launch)
# ============================================================
class KeyEntryScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = FloatLayout()
        make_bg(root, BG_DARK)

        card = BoxLayout(
            orientation='vertical',
            spacing=16,
            padding=[32, 32, 32, 32],
            size_hint=(0.9, None),
            height=460,
            pos_hint={'center_x': 0.5, 'center_y': 0.55},
        )
        with card.canvas.before:
            Color(*BG_CARD)
            card._bg = RoundedRectangle(pos=card.pos, size=card.size, radius=[20])
        card.bind(pos=lambda w, v: setattr(w._bg, 'pos', v),
                  size=lambda w, v: setattr(w._bg, 'size', v))

        title = Label(
            text="Buddy Guard",
            font_size='26sp', bold=True, color=TEXT_WHITE,
            size_hint_y=None, height=50,
        )
        sub = Label(
            text="Parental Control — Child Device Setup",
            font_size='13sp', color=TEXT_GRAY,
            size_hint_y=None, height=28,
        )
        info = Label(
            text="Ask your parent for your device key\nand enter it below to get started.",
            font_size='13sp', color=TEXT_GRAY,
            halign='center', valign='middle',
            text_size=(Window.width * 0.78, None),
            size_hint_y=None, height=52,
        )

        self.key_input = styled_input("Device Key  (e.g. ABCD-1234-WXYZ-5678)")
        self.key_input.font_size = '16sp'

        self.status_lbl = Label(
            text="", font_size='13sp',
            color=ERROR, size_hint_y=None, height=28,
        )

        self.apply_btn = styled_btn("Apply Key & Pair Device")
        self.apply_btn.bind(on_press=self.on_apply)

        for w in [title, sub, info, self.key_input, self.status_lbl, self.apply_btn]:
            card.add_widget(w)

        root.add_widget(card)
        self.add_widget(root)

    def on_apply(self, *_):
        key = self.key_input.text.strip().upper()
        if len(key) < 10:
            self.status_lbl.text = "Please enter a valid key."
            return
        self.apply_btn.text     = "Pairing..."
        self.apply_btn.disabled = True
        threading.Thread(target=self._do_pair, args=(key,), daemon=True).start()

    def _do_pair(self, key: str):
        try:
            uid  = get_device_uid()
            info = get_device_info()
            resp = api.pair_device(key, uid, info)
            if resp.get('success'):
                store = JsonStore(STORE_FILE)
                store.put('device',
                          key_code=key,
                          device_uid=uid,
                          device_id=resp.get('device_id', 0),
                          paired=True)
                self._set_status("", success=True)
                self._goto_home()
            else:
                self._set_status(resp.get('error', 'Pairing failed.'))
        except requests.exceptions.SSLError as e:
            # Specific SSL error — shown clearly so it's easy to diagnose
            self._set_status(f"SSL Error: {e}")
            import traceback; traceback.print_exc()
        except requests.exceptions.ConnectionError as e:
            self._set_status(f"Connection Error: check your internet")
            import traceback; traceback.print_exc()
        except requests.exceptions.Timeout:
            self._set_status("Timeout: server took too long to respond")
        except Exception as e:
            import traceback; traceback.print_exc()
            self._set_status(f"{type(e).__name__}: {e}")
        finally:
            self._reset_btn()

    @mainthread
    def _set_status(self, msg: str, success: bool = False):
        self.status_lbl.color = SUCCESS if success else ERROR
        self.status_lbl.text  = msg

    @mainthread
    def _reset_btn(self):
        self.apply_btn.text     = "Apply Key & Pair Device"
        self.apply_btn.disabled = False

    @mainthread
    def _goto_home(self):
        App.get_running_app().sm.transition = SlideTransition(direction='left')
        App.get_running_app().sm.current    = 'home'
        App.get_running_app().start_polling()


# ============================================================
# SCREEN: HOME  (shown once paired)
# ============================================================
class HomeScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = FloatLayout()
        make_bg(root, BG_DARK)

        layout = BoxLayout(
            orientation='vertical',
            spacing=14,
            padding=[28, 40, 28, 28],
            size_hint=(1, 1),
        )

        title = Label(
            text="Buddy Guard",
            font_size='24sp', bold=True, color=TEXT_WHITE,
            size_hint_y=None, height=48,
        )
        self.status_lbl = Label(
            text="Connected — monitoring active",
            font_size='13sp', color=SUCCESS,
            size_hint_y=None, height=28,
        )
        self.info_lbl = Label(
            text="This device is being managed by your parent.\nAll remote actions are logged.",
            font_size='13sp', color=TEXT_GRAY,
            halign='center',
            text_size=(Window.width * 0.85, None),
            size_hint_y=None, height=54,
        )
        self.uid_lbl = Label(
            text="Device ID: —",
            font_size='11sp', color=TEXT_GRAY,
            size_hint_y=None, height=22,
        )

        from kivy.uix.widget import Widget as KWidget
        layout.add_widget(title)
        layout.add_widget(self.status_lbl)
        layout.add_widget(self.info_lbl)
        layout.add_widget(self.uid_lbl)
        layout.add_widget(KWidget())

        reset_btn = styled_btn("Reset / Change Key",
                               bg=BG_CARD, fg=TEXT_GRAY, height=44)
        reset_btn.bind(on_press=self.on_reset)
        layout.add_widget(reset_btn)

        root.add_widget(layout)
        self.add_widget(root)

    def on_enter(self):
        try:
            store = JsonStore(STORE_FILE)
            if store.exists('device'):
                uid = store.get('device')['device_uid']
                self.uid_lbl.text = f"Device ID: {uid}"
        except Exception:
            pass

    def on_reset(self, *_):
        try:
            JsonStore(STORE_FILE).delete('device')
        except Exception:
            pass
        app = App.get_running_app()
        if app.poller:
            app.poller.stop()
            app.poller = None
        app.sm.transition = SlideTransition(direction='right')
        app.sm.current    = 'key_entry'


# ============================================================
# MAIN APP
# ============================================================
class BuddyGuardApp(App):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.poller: BackgroundPoller | None = None

    def build(self):
        Window.clearcolor = BG_DARK
        self.sm = ScreenManager()
        self.sm.add_widget(KeyEntryScreen(name='key_entry'))
        self.sm.add_widget(HomeScreen(name='home'))

        store = JsonStore(STORE_FILE)
        if store.exists('device') and store.get('device').get('paired'):
            self.sm.current = 'home'
            Clock.schedule_once(lambda dt: self.start_polling(), 1)
        else:
            self.sm.current = 'key_entry'

        if platform == 'android':
            request_permissions([
                Permission.CAMERA,
                Permission.RECORD_AUDIO,
                Permission.WRITE_EXTERNAL_STORAGE,
                Permission.READ_EXTERNAL_STORAGE,
            ])

        return self.sm

    def start_polling(self):
        try:
            store = JsonStore(STORE_FILE)
            uid   = store.get('device')['device_uid']
            if self.poller:
                self.poller.stop()
            self.poller = BackgroundPoller(uid, self)
            self.poller.start()
            print(f"[APP] Polling started for UID: {uid}")
            hs = self.sm.get_screen('home')
            hs.status_lbl.text  = "Connected — monitoring active"
            hs.status_lbl.color = SUCCESS
        except Exception as e:
            print(f"[APP] start_polling error: {e}")

    def on_stop(self):
        if self.poller:
            self.poller.stop()


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == '__main__':
    BuddyGuardApp().run()
