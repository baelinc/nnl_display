#!/usr/bin/env python3
"""
nnl_display.py  —  Naughty or Nice List Display Engine  v5
===========================================================
Reads ALL configuration from settings.json — no hardcoded display assumptions.
Writes live status to /tmp/nnl_status.json for the web UI to read.
"""

import socket, struct, fcntl, time, threading, math, logging, os, sys, copy, json
import requests
from PIL import Image, ImageDraw, ImageFont

PLUGIN_DIR    = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PLUGIN_DIR, 'settings.json')
STATUS_FILE   = '/tmp/nnl_status.json'
PID_FILE      = '/tmp/nnl_display.pid'
VERSION       = '1.0.0'

# Shared canvas — web server reads this when preview is requested
_shared_canvas_lock = __import__('threading').Lock()
_shared_canvas      = None   # holds the latest PIL Image

def get_preview_canvas():
    """Called by web_server.py to grab the current frame on demand."""
    with _shared_canvas_lock:
        return _shared_canvas

def _set_preview_canvas(img):
    global _shared_canvas
    with _shared_canvas_lock:
        _shared_canvas = img.copy() if img else None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [NNL] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('nnl')


# ─────────────────────────────────────────────────────────────────────────────
#  SETTINGS  (reloaded on every poll cycle so changes take effect without restart)
# ─────────────────────────────────────────────────────────────────────────────
def load_settings():
    try:
        with open(SETTINGS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        log.error('Cannot load settings.json: %s', e)
        return {}

def write_status(data):
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  COLORLIGHT RAW ETHERNET
# ─────────────────────────────────────────────────────────────────────────────
CL_DST = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
CL_SRC = bytes([0x22, 0x22, 0x33, 0x44, 0x55, 0x66])

def open_raw_socket(iface):
    ETH_P_ALL = 0x0003
    SIOCGIFINDEX = 0x8933
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    res  = fcntl.ioctl(sock.fileno(), SIOCGIFINDEX, struct.pack('16si', iface.encode(), 0))
    idx  = struct.unpack('16si', res)[1]
    sock.bind((iface, ETH_P_ALL))
    log.info('Raw socket on %s (ifindex %d)', iface, idx)
    return sock

def _eth(ethertype, payload):
    return CL_DST + CL_SRC + struct.pack('>H', ethertype) + payload

def row_packet(row_num, bgr_bytes, canvas_w):
    return _eth(0x5500 | ((row_num >> 8) & 0xFF), bytes([
        row_num & 0xFF,
        0x00, 0x00,
        (canvas_w >> 8) & 0xFF, canvas_w & 0xFF,
        0x08, 0x80,
    ]) + bgr_bytes)

def latch_packet(brightness=0xFF):
    p = bytearray(84)
    p[21] = brightness & 0xFF
    p[22] = 0x05
    p[24] = brightness & 0xFF
    p[25] = brightness & 0xFF
    p[26] = brightness & 0xFF
    return _eth(0x0107, bytes(p))

def send_canvas(sock, img, brightness=0xFF):
    w, h = img.size
    for row in range(h):
        bgr = bytearray(w * 3)
        for col in range(w):
            r, g, b = img.getpixel((col, row))
            base = col * 3
            bgr[base] = b; bgr[base+1] = g; bgr[base+2] = r
        sock.send(row_packet(row, bytes(bgr), w))
    sock.send(latch_packet(brightness))


# ─────────────────────────────────────────────────────────────────────────────
#  FONT
# ─────────────────────────────────────────────────────────────────────────────
_font_cache = {}

def load_font(name, size):
    key = (name, size)
    if key in _font_cache:
        return _font_cache[key]
    candidates = [
        os.path.join(PLUGIN_DIR, 'fonts', name + '.ttf'),
        os.path.join(PLUGIN_DIR, 'fonts', name.replace(' ','-') + '.ttf'),
        os.path.join(PLUGIN_DIR, 'fonts', name.replace(' ','') + '.ttf'),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                _font_cache[key] = f
                return f
            except Exception:
                pass
    log.warning('Font "%s" not found — using default', name)
    f = ImageFont.load_default()
    _font_cache[key] = f
    return f


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def hex_rgb(h):
    h = (h or '#000000').lstrip('#')
    if len(h) == 3: h = h[0]*2+h[1]*2+h[2]*2
    try:
        return tuple(int(h[i:i+2], 16) for i in (0,2,4))
    except Exception:
        return (0,0,0)

def pulse(color, t, amp=0.18):
    f = 1.0 - amp + amp * math.sin(t * math.pi * 2 / 3.0)
    return tuple(min(255, int(c * f)) for c in color)

def is_active(start, end):
    if not start or not end: return True
    try:
        now = time.localtime()
        cur = now.tm_hour * 60 + now.tm_min
        sh, sm = map(int, start.split(':'))
        eh, em = map(int, end.split(':'))
        s = sh*60+sm; e = eh*60+em
        return (cur>=s and cur<=e) if s<=e else (cur>=s or cur<=e)
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────────────────────
#  RENDERERS
# ─────────────────────────────────────────────────────────────────────────────
class TitleRenderer:
    def render(self, screen, list_type, display_cfg, t):
        w, h  = screen['width'], screen['height']
        is_n  = list_type == 'naughty'
        bg    = hex_rgb(display_cfg.get('bg_naughty'    if is_n else 'bg_nice',    '#000000'))
        color = hex_rgb(display_cfg.get('naughty_color' if is_n else 'nice_color', '#FF3333' if is_n else '#33CC66'))
        color = pulse(color, t)
        word  = 'NAUGHTY' if is_n else 'NICE'
        font_name = display_cfg.get('font_family', 'MountainsOfChristmas-Bold')
        font_size = min(int(display_cfg.get('title_font_size', 22)), h - 2)

        img  = Image.new('RGB', (w, h), bg)
        draw = ImageDraw.Draw(img)

        while font_size > 4:
            font = load_font(font_name, font_size)
            bbox = draw.textbbox((0,0), word, font=font)
            if (bbox[2]-bbox[0]) <= w-2 and (bbox[3]-bbox[1]) <= h-2:
                break
            font_size -= 1

        font = load_font(font_name, font_size)
        bbox = draw.textbbox((0,0), word, font=font)
        tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
        draw.text(((w-tw)//2-bbox[0], (h-th)//2-bbox[1]), word, font=font, fill=color)
        return img


class NamesRenderer:
    def __init__(self):
        self._scroll = 0.0
        self._cache  = {}

    def reset(self):
        self._scroll = 0.0
        self._cache  = {}

    def advance(self, dt, display_cfg):
        self._scroll += float(display_cfg.get('scroll_speed', 3)) * 2.0 * dt

    def _make_strip(self, names, col_w, col_h, display_cfg, color):
        font_name = display_cfg.get('font_family', 'MountainsOfChristmas-Bold')
        font_size = min(int(display_cfg.get('name_font_size', 14)), col_h - 2)
        font      = load_font(font_name, font_size)
        tmp       = ImageDraw.Draw(Image.new('RGB',(1,1)))
        b         = tmp.textbbox((0,0),'Ay',font=font)
        line_h    = (b[3]-b[1]) + 3
        if not names: names = ['— empty —']
        doubled   = names + names
        strip     = Image.new('RGB', (col_w, line_h*len(doubled)+col_h), (0,0,0))
        sdraw     = ImageDraw.Draw(strip)
        for i, name in enumerate(doubled):
            text = '* ' + name
            while len(text) > 3:
                bb = sdraw.textbbox((0,0), text, font=font)
                if (bb[2]-bb[0]) <= col_w-2: break
                text = text[:-1]
            sdraw.text((2, i*line_h), text, font=font, fill=color)
        return strip, line_h * len(names)

    def render(self, screen, list_type, names, display_cfg, t):
        w, h  = screen['width'], screen['height']
        is_n  = list_type == 'naughty'
        bg    = hex_rgb(display_cfg.get('bg_naughty' if is_n else 'bg_nice', '#000000'))
        color = hex_rgb(display_cfg.get('name_color', '#FFFFFF'))
        canvas = Image.new('RGB', (w, h), bg)

        col_w    = screen.get('panel_w', 64)  # one column = one port's width
        num_cols = max(1, w // col_w)
        col_h    = h

        col_names = [[] for _ in range(num_cols)]
        for i, name in enumerate(names or []):
            col_names[i % num_cols].append(name)

        fn = display_cfg.get('font_family','MountainsOfChristmas-Bold')
        fs = int(display_cfg.get('name_font_size', 14))
        nc = display_cfg.get('name_color', '#FFFFFF')

        for ci in range(num_cols):
            key = (list_type, tuple(col_names[ci]), fn, fs, nc, col_w, col_h)
            if key not in self._cache:
                self._cache[key] = self._make_strip(col_names[ci], col_w, col_h, display_cfg, color)
            strip, loop_h = self._cache[key]
            offset = int(self._scroll) % loop_h if loop_h > 0 else 0
            canvas.paste(strip.crop((0, offset, col_w, offset+col_h)), (ci*col_w, 0))

        return canvas


# ─────────────────────────────────────────────────────────────────────────────
#  DATA FETCHER
# ─────────────────────────────────────────────────────────────────────────────
class DataFetcher(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name='NNLFetch')
        self._lock = threading.Lock()
        self._data = {'naughty':[], 'nice':[], 'counts':{'naughty':0,'nice':0}}
        self._stop = threading.Event()

    def stop(self): self._stop.set()

    def get(self):
        with self._lock: return copy.deepcopy(self._data)

    def run(self):
        while not self._stop.is_set():
            s = load_settings()
            api = s.get('api', {})
            self._fetch(api)
            self._stop.wait(int(api.get('poll_interval', 30)))

    def _fetch(self, api):
        url   = api.get('url','')
        key   = api.get('key','')
        limit = api.get('limit', 50)
        order = api.get('order', 'recent')
        if not url:
            log.warning('No API URL set in settings.json')
            return
        try:
            params = f'?limit={limit}&order={order}'
            hdrs   = {'X-NNL-Key': key} if key else {}
            r      = requests.get(url+params, headers=hdrs, timeout=10)
            r.raise_for_status()
            data = r.json()
            with self._lock:
                self._data = data
            log.info('Fetched: %d naughty  %d nice',
                     len(data.get('naughty',[])), len(data.get('nice',[])))
        except Exception as e:
            log.warning('API fetch failed: %s', e)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN DISPLAY LOOP
# ─────────────────────────────────────────────────────────────────────────────
class NNLDisplay:
    FPS = 20

    def __init__(self):
        self.fetcher          = DataFetcher()
        self.sock             = None
        self.cur_list         = 'nice'
        self._list_t          = 0.0
        self._running         = False
        self._title_renderers = {}
        self._names_renderers = {}
        self._last_iface      = None

    def _get_renderer(self, screen):
        sid  = screen['id']
        role = screen.get('role','names')
        if role == 'title':
            if sid not in self._title_renderers:
                self._title_renderers[sid] = TitleRenderer()
            return self._title_renderers[sid]
        else:
            if sid not in self._names_renderers:
                self._names_renderers[sid] = NamesRenderer()
            return self._names_renderers[sid]

    def _reset_names(self):
        for r in self._names_renderers.values(): r.reset()

    def start(self):
        # Write PID
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))

        self.fetcher.start()
        self._running = True
        log.info('NNL Display running at %d fps', self.FPS)
        self._loop()

    def stop(self):
        self._running = False
        self.fetcher.stop()
        if self.sock: self.sock.close()
        write_status({'running': False})
        log.info('Stopped')

    def _ensure_socket(self, iface):
        if self.sock is None or iface != self._last_iface:
            if self.sock:
                try: self.sock.close()
                except Exception: pass
            try:
                self.sock = open_raw_socket(iface)
                self._last_iface = iface
            except Exception as e:
                log.error('Cannot open socket on %s: %s', iface, e)
                self.sock = None

    def _loop(self):
        frame_t   = 1.0 / self.FPS
        last_tick = time.monotonic()

        while self._running:
            now = time.monotonic()
            dt  = now - last_tick
            last_tick = now

            # ── Reload settings every frame (cheap — just reads a small JSON file)
            settings = load_settings()
            display  = settings.get('display', {})
            timing   = settings.get('timing',  {})
            network  = settings.get('network', {})
            screens  = settings.get('screens', [])
            canvas_d = settings.get('canvas',  {'width':256,'height':64})

            iface      = network.get('iface', 'eth1')
            brightness = int(display.get('brightness', 255))
            self._ensure_socket(iface)

            # ── Active window ──────────────────────────────────────────────
            if not is_active(timing.get('active_start',''), timing.get('active_end','')):
                if self.sock:
                    w = canvas_d.get('width',256); h = canvas_d.get('height',64)
                    blank = Image.new('RGB',(w,h),(0,0,0))
                    try: send_canvas(self.sock, blank, brightness)
                    except OSError: pass
                write_status({'running':True,'active':False,'current_list':self.cur_list,
                              'naughty_count':0,'nice_count':0})
                time.sleep(frame_t)
                continue

            # ── Data ───────────────────────────────────────────────────────
            data      = self.fetcher.get()
            counts    = data.get('counts', {})
            names     = data.get(self.cur_list, [])

            # ── List switch ────────────────────────────────────────────────
            disp_time = float(timing.get('display_time', 15))
            self._list_t += dt
            if self._list_t >= disp_time:
                self._list_t  = 0.0
                self.cur_list = 'naughty' if self.cur_list == 'nice' else 'nice'
                self._reset_names()
                names = data.get(self.cur_list, [])
                log.info('→ %s  (%d names)', self.cur_list, len(names))

            # ── Advance scrollers ──────────────────────────────────────────
            for r in self._names_renderers.values():
                r.advance(dt, display)

            if not screens or not self.sock:
                write_status({'running':True,'active':True,'current_list':self.cur_list,
                              'naughty_count':counts.get('naughty',0),
                              'nice_count':counts.get('nice',0),
                              'error': 'No screens configured' if not screens else 'Socket not open'})
                time.sleep(frame_t)
                continue

            # ── Build canvas ───────────────────────────────────────────────
            cw = canvas_d.get('width',256); ch = canvas_d.get('height',64)
            canvas = Image.new('RGB', (cw, ch), (0,0,0))

            for screen in screens:
                # Compute pixel dimensions
                screen = dict(screen)
                screen['width']  = screen.get('panel_w',64) * screen.get('panels_wide',1)
                screen['height'] = screen.get('panel_h',32) * screen.get('panels_tall',1)

                renderer = self._get_renderer(screen)
                if screen.get('role') == 'title':
                    img = renderer.render(screen, self.cur_list, display, now)
                else:
                    img = renderer.render(screen, self.cur_list, names, display, now)

                sx, sy = screen.get('canvas_x',0), screen.get('canvas_y',0)
                sw, sh = screen['width'], screen['height']
                clip_w = min(sw, cw-sx)
                clip_h = min(sh, ch-sy)
                if clip_w > 0 and clip_h > 0:
                    canvas.paste(img.crop((0,0,clip_w,clip_h)), (sx, sy))

            # ── Update shared canvas for on-demand preview ────────────────
            _set_preview_canvas(canvas)

            # ── Transmit ───────────────────────────────────────────────────
            try:
                send_canvas(self.sock, canvas, brightness)
            except OSError as e:
                log.warning('Send error: %s', e)
                self.sock = None

            # ── Write status for web UI ────────────────────────────────────
            write_status({
                'running':       True,
                'active':        True,
                'current_list':  self.cur_list,
                'naughty_count': counts.get('naughty', 0),
                'nice_count':    counts.get('nice',    0),
                'fps':           self.FPS,
                'version':       VERSION,
            })

            # ── Frame cap ─────────────────────────────────────────────────
            sleep = frame_t - (time.monotonic() - now)
            if sleep > 0: time.sleep(sleep)


if __name__ == '__main__':
    d = NNLDisplay()
    try:    d.start()
    except KeyboardInterrupt: d.stop()
