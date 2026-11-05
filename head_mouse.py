# -*- coding: utf-8 -*-
"""
HeadMouse —— 头动控制电脑（实验4 / 体感交互）

用摄像头跟踪头部位置，把头部运动映射成 Windows 光标的位置与点击，
实现"鼠标的移动、单击、双击"，等价于 Enable Viacam，但免安装、可调参、可复现。

一、处理链路
   摄像头 30fps
     -> YuNet 检出人脸框（每 3 帧一次，够快也够稳）
     -> 只在脸框内撒 160 个 Shi-Tomasi 角点
     -> LK 金字塔光流 + 前后向校验，得到每点的帧间位移
     -> 取位移"中位数"作为头部位移（中位数天然抗跑飞点 / 抗背景点）
     -> 与脸框中心做"锚定融合"（防止光流累计误差把光标拖向一个角）
     -> 死区 + 指数平滑（消掉静止时的手指级抖动）
     -> 映射成光标位置（位置模式）或光标速度（速度模式）

二、两种映射律（T 键切换）
   pos 位置映射：offset -> 绝对位置   cursor = origin + k * offset
                 脸偏多少，光标就走多少；脸回中，光标回原点。eViacam 的默认做法。
   vel 速度映射：offset -> 速度       v = VMAX * (|offset| - dead) / full
                 脸偏左则光标一直往左走，直到脸回中才停。适合大屏 / 长时间操作。

三、操作
   空格      开始 / 暂停（开始时把"当前光标位置"记为原点，光标不跳）
   C         重新锁定（脸摆正后按，重撒特征点、清零偏移）
   O         以当前光标位置重新取原点
   T         切换 位置映射 / 速度映射
   X         切换 单击 / 双击
   1/2/3     灵敏度 低 / 中 / 高（位置模式的有效行程）
   F8        暂停 / 继续（全局，窗口失焦也能按）
   F12       立即退出（全局，窗口失焦也能按）
   ESC / Q   退出（需窗口在前台）

四、安全机制（v1.0）
   * 鼠标被人工接管（光标与程序设定的位置相差 > 90px）-> 自动暂停并交还鼠标；
   * 连续 2.5 秒检不到人脸 -> 自动暂停；
   * F8 / F12 走 GetAsyncKeyState，不依赖窗口焦点，杜绝"窗口失焦被锁死"。

五、点击的两道闸
   1) 停留 0.8s 触发一次点击；
   2) 点击后必须再移动 REARM_PX 像素才允许下一次 —— 否则停在原地会连续点击
      （dwell click 的标配防误触，等于鼠标"松开再按"）。

用法：
   python head_mouse.py                 # 正常使用
   python head_mouse.py --cam 1         # 多摄像头时指定序号
   python head_mouse.py --no-mouse      # 只看画面不控制鼠标
   python head_mouse.py --mode vel      # 启动即用速度映射
   python head_mouse.py --frames 60 --no-mouse   # 跑 60 帧自动退出（自测）
   python head_mouse.py --selftest      # 自检：人脸检出率、特征点数、残差
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import time

import cv2
import numpy as np

user32 = ctypes.windll.user32

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "yunet.onnx")

FRAME_W, FRAME_H = 640, 480
ROI_X0, ROI_Y0, ROI_X1, ROI_Y1 = 0.28, 0.16, 0.72, 0.80   # 没检到脸时的兜底跟踪区

# ---- 交互参数 ----
DWELL = 0.8            # 停住多少秒触发点击
STILL_PX = 0.45        # 单帧位移小于它算"没动"
REARM_PX = 90.0        # 点击后光标须离开点击点这么多屏幕像素，才允许下次点击（防连点）
COOLDOWN = 1.2         # 点击后的硬冷却（秒）
LOST_PAUSE = 2.5       # 连续丢脸多少秒自动暂停
HIJACK_PX = 90         # 光标与程序设定值偏差超过它 == 真人接管了鼠标

# ---- 速度映射参数 ----
VEL_DEAD = 6.0         # 速度模式死区（头部像素）
VEL_FULL = 26.0        # 达到这个偏移时给满速
VEL_MAX = 1500.0       # 满速像素/秒

RANGE_TABLE = {ord('1'): 260.0, ord('2'): 160.0, ord('3'): 100.0}


# ============================ Win32 小工具 ============================

def enable_dpi_awareness():
    """必须在取屏幕尺寸之前调用，否则 125%/150% 缩放下坐标会被折算。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def virtual_screen():
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        x, y = 0, 0
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    return x, y, w, h


def get_cursor():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def set_cursor(x, y):
    user32.SetCursorPos(int(x), int(y))


def double_click_interval():
    """跟随系统双击间隔，避免固定 60ms 在慢机上被识别成两次单击。"""
    t = user32.GetDoubleClickTime() / 1000.0
    return min(max(t * 0.5, 0.05), 0.20)


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class Cursor:
    """封装光标读写，同时记录"我们自己设过的位置"，用于识别人工接管。"""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.origin = (0, 0)
        self.last = None          # 上一次我们设定的坐标
        self.fx = 0.0             # 相对移动的亚像素余量
        self.fy = 0.0

    def capture_origin(self):
        self.origin = get_cursor()
        self.last = self.origin
        return self.origin

    def move_abs(self, x, y):
        x, y = int(round(x)), int(round(y))
        self.last = (x, y)
        if self.enabled:
            set_cursor(x, y)

    def move_rel(self, dx, dy):
        self.fx += dx
        self.fy += dy
        ix, iy = int(self.fx), int(self.fy)
        if ix == 0 and iy == 0:
            return
        self.fx -= ix
        self.fy -= iy
        if self.enabled:
            user32.mouse_event(MOUSEEVENTF_MOVE, ix, iy, 0, 0)
            self.last = get_cursor()
        elif self.last is not None:
            self.last = (self.last[0] + ix, self.last[1] + iy)

    def hijacked(self, tol=HIJACK_PX):
        """真人在用物理鼠标吗？必须在"本帧设新位置之前"调用。"""
        if not self.enabled or self.last is None:
            return False
        x, y = get_cursor()
        return abs(x - self.last[0]) > tol or abs(y - self.last[1]) > tol


class GlobalKeys:
    """基于 GetAsyncKeyState 的全局按键，不依赖窗口焦点。"""

    VK = {"F8": 0x77, "F12": 0x7B}

    def __init__(self):
        self.prev = {}

    def edge(self, name):
        vk = self.VK[name]
        down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        old = self.prev.get(vk, False)
        self.prev[vk] = down
        return down and not old


def mouse_click(double=False):
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if double:
        time.sleep(double_click_interval())
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def fit_frame(frame, tw=FRAME_W, th=FRAME_H):
    h, w = frame.shape[:2]
    s = min(tw / float(w), th / float(h))
    nw, nh = max(1, int(w * s)), max(1, int(h * s))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((th, tw, 3), np.uint8)
    ox, oy = (tw - nw) // 2, (th - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = resized
    return canvas


def ascii_model_path(path):
    """OpenCV 的 dnn 加载器打不开含中文的路径，先把模型复制到纯英文临时目录。"""
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        pass
    import shutil
    import tempfile
    dst = os.path.join(tempfile.gettempdir(), "head_mouse_yunet.onnx")
    try:
        if (not os.path.exists(dst)) or os.path.getsize(dst) != os.path.getsize(path):
            shutil.copy2(path, dst)
        return dst
    except Exception:
        return path


def face_center(box, shape):
    """没检到脸时给一个兜底锚点（跟踪区中心），保证 Mapper 不会拿到 None。"""
    if box is not None:
        return (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)
    h, w = shape[:2]
    return ((ROI_X0 + ROI_X1) * 0.5 * w, (ROI_Y0 + ROI_Y1) * 0.5 * h)


# ============================ 算法模块 ============================

class FaceBox:
    """YuNet 人脸检测。抓不到脸时逐帧找，抓到后每 N 帧更新一次。"""

    def __init__(self, model_path=MODEL, every=3):
        self.every = every
        self.det = None
        self.box = None
        self.score = 0.0
        self.frame_id = 0
        if os.path.exists(model_path):
            try:
                self.det = cv2.FaceDetectorYN.create(
                    ascii_model_path(model_path), "", (FRAME_W, FRAME_H), 0.6, 0.3, 5000)
                self.det.setInputSize((FRAME_W, FRAME_H))
            except Exception as e:
                print("[警告] 人脸模型加载失败，退回固定框跟踪：%s" % e)
                self.det = None

    @property
    def enabled(self):
        return self.det is not None

    def update(self, frame_bgr):
        self.frame_id += 1
        if self.det is None:
            return self.box
        if self.box is not None and self.frame_id % self.every != 0:
            return self.box
        try:
            _, faces = self.det.detect(frame_bgr)
        except Exception:
            faces = None
        if faces is not None and len(faces) > 0:
            f = max(faces, key=lambda a: a[2] * a[3])
            self.box = (float(f[0]), float(f[1]), float(f[2]), float(f[3]))
            self.score = float(f[-1])
        return self.box

    def mask(self, shape):
        h, w = shape[:2]
        m = np.zeros((h, w), np.uint8)
        if self.box is None:
            m[int(h * ROI_Y0):int(h * ROI_Y1), int(w * ROI_X0):int(w * ROI_X1)] = 255
            return m
        x, y, bw, bh = self.box
        mx, my = bw * 0.10, bh * 0.08
        x0 = int(max(2, x + mx)); y0 = int(max(2, y + my))
        x1 = int(min(w - 2, x + bw - mx)); y1 = int(min(h - 2, y + bh - my))
        if x1 - x0 < 12 or y1 - y0 < 12:
            m[int(h * ROI_Y0):int(h * ROI_Y1), int(w * ROI_X0):int(w * ROI_X1)] = 255
        else:
            m[y0:y1, x0:x1] = 255
        return m


class FaceTracker:
    """脸框内特征点 + LK 光流（前后向校验），返回中位位移。"""

    LK = dict(winSize=(21, 21), maxLevel=3,
              criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03))

    def __init__(self, facebox):
        self.fb = facebox
        self.prev = None
        self.pts = None

    def reseed(self, gray):
        mask = self.fb.mask(gray.shape)
        pts = None
        for q in (0.01, 0.005, 0.001):
            pts = cv2.goodFeaturesToTrack(
                gray, maxCorners=160, qualityLevel=q, minDistance=4,
                blockSize=7, mask=mask)
            if pts is not None and len(pts) >= 12:
                break
        self.pts = pts
        self.prev = gray.copy()

    def update(self, gray):
        if self.pts is None or self.prev is None or len(self.pts) < 8:
            self.reseed(gray)
            return 0.0, 0.0, 0

        p1, st1, _ = cv2.calcOpticalFlowPyrLK(self.prev, gray, self.pts, None, **self.LK)
        if p1 is None or st1 is None:
            self.reseed(gray)
            return 0.0, 0.0, 0
        p0r, st2, _ = cv2.calcOpticalFlowPyrLK(gray, self.prev, p1, None, **self.LK)
        if p0r is None or st2 is None:
            self.reseed(gray)
            return 0.0, 0.0, 0

        a = self.pts.reshape(-1, 2)
        b = p1.reshape(-1, 2)
        back = p0r.reshape(-1, 2)
        good = ((st1.reshape(-1) == 1) & (st2.reshape(-1) == 1)
                & (np.linalg.norm(back - a, axis=1) < 1.5))
        if good.sum() < 8:
            self.reseed(gray)
            return 0.0, 0.0, 0

        delta = b[good] - a[good]
        dx = float(np.median(delta[:, 0]))
        dy = float(np.median(delta[:, 1]))
        spread = float(np.percentile(np.linalg.norm(delta - [dx, dy], axis=1), 75))

        if good.sum() < 20 or spread > 6.0:
            self.reseed(gray)
        else:
            self.pts = b[good].reshape(-1, 1, 2).astype(np.float32)
            self.prev = gray.copy()
        return dx, dy, int(good.sum())

    def points(self):
        return [] if self.pts is None else self.pts.reshape(-1, 2)


class Mapper:
    """把光流微位移和脸框绝对位置融合成"头部偏移"。

      * 光流负责帧间的高频、亚像素级位移（快，不卡）；
      * 脸框中心是绝对参照，用它把累计位移慢慢"拉回来"（BETA 每帧只拉一小步），
        光流的系统性偏差就不会一路累加成"只往左上跑"；
      * 死区 + 轻平滑，去掉静止时的手指级抖动。
    """

    BETA = 0.12       # 每帧向脸框中心回拉的比例（越大越稳，过大会拖慢真实动作）
    ANCHOR_LR = 0.20  # 脸框中心本身的平滑，压低检测抖动
    DEAD = 1.6        # 位置模式的偏移死区（像素）
    SMOOTH = 0.30

    def __init__(self):
        self.reset()

    def reset(self):
        self.v = None          # 融合后的头部位置（跟踪坐标系）
        self.ref = None        # 锁定时的原点
        self.anchor = None     # 平滑后的脸框中心
        self.off = np.zeros(2)
        self.out = np.zeros(2)

    def set_anchor(self, center):
        if center is None:
            return
        c = np.array(center, float)
        if self.anchor is None:
            self.anchor = c.copy()
        else:
            self.anchor = (1 - self.ANCHOR_LR) * self.anchor + self.ANCHOR_LR * c
        if self.v is None:
            self.v = self.anchor.copy()
            self.ref = self.anchor.copy()

    def lock(self, center):
        """重新锁定：位置 = 当前脸框中心，原点也在这里，偏移清零。"""
        self.reset()
        if center is not None:
            self.anchor = np.array(center, float)
            self.v = self.anchor.copy()
            self.ref = self.anchor.copy()
        self.off = np.zeros(2)
        self.out = np.zeros(2)

    def step(self, dx, dy, center=None):
        self.set_anchor(center)
        if self.v is None:
            self.set_anchor((0.0, 0.0))
        self.v = self.v + np.array([dx, dy], float)
        if self.anchor is not None:
            self.v = self.v + self.BETA * (self.anchor - self.v)
        off = self.v - self.ref
        # 死区：死区外按比例平移，避免"过冲"
        mag = float(np.hypot(off[0], off[1]))
        if mag <= self.DEAD:
            off_eff = np.zeros(2)
        else:
            off_eff = off * (1.0 - self.DEAD / mag)
        self.off = off
        self.out = (1 - self.SMOOTH) * self.out + self.SMOOTH * off_eff
        return self.out

    @property
    def offset(self):
        return float(self.out[0]), float(self.out[1])

    @property
    def residual(self):
        """融合位置与脸框中心的差距，越小说明锚定越健康。"""
        if self.anchor is None or self.v is None:
            return 0.0
        return float(np.hypot(*(self.v - self.anchor)))


# ============================ 自检 ============================

def selftest(cap, frames):
    fb = FaceBox()
    tr = FaceTracker(fb)
    mp = Mapper()
    _, _, SW, SH = virtual_screen()
    t0 = time.time()
    got = hits = last = 0
    seeded = False
    for i in range(frames):
        ok, frame = cap.read()
        if not ok:
            continue
        got += 1
        img = cv2.flip(fit_frame(frame), 1)
        box = fb.update(img)
        if box is not None:
            hits += 1
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if box is not None and not seeded:
            tr.reseed(g)
            seeded = True
        dx, dy, last = tr.update(g)
        mp.step(dx, dy, face_center(box, g.shape))
        if i == 0:
            print("首帧 %dx%d，脸框 %s，初始特征点 %d 个"
                  % (img.shape[1], img.shape[0],
                     "(%.0f,%.0f,%.0fx%.0f)" % box if box else "无", len(tr.points())))
    cap.release()
    if got == 0:
        print("[失败] 读不到任何画面")
        return 1
    el = time.time() - t0
    ox, oy = mp.offset
    kx, ky = (SW * 0.5) / 160.0, (SH * 0.5) / 160.0
    print("自检完成：%d 帧 / 人脸检出 %d 帧 / 特征点 %d 个 / 用时 %.1fs（%.0f fps）"
          % (got, hits, last, el, got / max(el, 1e-3)))
    print("末态头部偏移：%+.1f, %+.1f px  ->  光标 %+.0f, %+.0f px"
          "（这段里你自己的头也在动，数值约等于你实际动的那一下）" % (ox, oy, ox * kx, oy * ky))
    print("锚定残差 %.1f px（融合位置与脸框中心的差距，<10 说明没有跑偏）" % mp.residual)
    return 0


# ============================ 主程序 ============================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--no-mouse", action="store_true")
    ap.add_argument("--mode", choices=("pos", "vel"), default="pos",
                    help="pos=位置映射（默认，eViacam 风格）；vel=速度映射")
    ap.add_argument("--origin", choices=("cursor", "center"), default="cursor",
                    help="开始时光标原点取哪里：cursor=当前光标位置（默认），center=屏幕中心")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--autostart", action="store_true", help="启动即开始（演示/自测用）")
    ap.add_argument("--snapshot", default="", help="第 25 帧存一张带叠层的截图（写文档用）")
    ap.add_argument("--frames", type=int, default=0, help="跑满 N 帧后自动退出（自测用）")
    args = ap.parse_args()

    enable_dpi_awareness()
    VX, VY, VW, VH = virtual_screen()

    cap = cv2.VideoCapture(args.cam, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(args.cam)
    if not cap.isOpened():
        print("[错误] 打不开摄像头（序号 %d），可试 --cam 1 或 --cam 2" % args.cam)
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    cap.set(cv2.CAP_PROP_FPS, 30)
    for _ in range(6):
        cap.read()

    if args.selftest:
        return selftest(cap, args.frames or 90)

    fb = FaceBox()
    tr = FaceTracker(fb)
    mp = Mapper()
    cursor = Cursor(enabled=not args.no_mouse)
    gk = GlobalKeys()

    print("[模型] 人脸检测：%s" % ("YuNet 已加载" if fb.enabled else "不可用，使用固定框"))
    print("[屏幕] 虚拟桌面 %dx%d @ (%d,%d)" % (VW, VH, VX, VY))

    running = False
    mode = args.mode
    double_mode = False
    range_px = 160.0
    still_t = 0.0
    cooldown = 0.0
    click_pos = None        # 上一次点击时光标所在的位置（用"净位移"判断是否重新武装）
    armed = False           # 是否允许下一次停留点击
    clicks_single = 0
    clicks_double = 0
    npts = 0
    lost_t = 0.0
    seeded_on_face = False
    last_reseed = 0.0
    prev_t = time.time()
    pause_reason = "未开始"
    frame_count = 0
    dist = 0.0
    gray = None
    center = None

    def start():
        """开始/继续：原点取当前光标位置，光标不会跳到屏幕中心。"""
        nonlocal click_pos, armed, still_t
        tr.reseed(gray)
        mp.lock(center)
        if args.origin == "center":
            cursor.move_abs(VX + VW // 2, VY + VH // 2)
        else:
            cursor.capture_origin()
        click_pos = cursor.last
        armed = False       # 开始后先动一下才允许点击，避免按空格的瞬间误点
        still_t = 0.0

    win = ("HeadMouse  [SPACE]start/pause  [C]relock  [O]origin  "
           "[T]mapping  [X]click/double  [1-3]speed  [F8]pause  [F12]quit")
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 760, 570)
    print("[启动] 摄像头已打开，窗口已弹出（标题 HeadMouse）。", flush=True)
    print("       空格 开始；F8 暂停（全局）；F12 退出（全局）；ESC 退出。", flush=True)
    print("       位置模式：头偏多少光标走多少，脸回中光标回原点。", flush=True)
    print("       速度模式：头偏左光标持续左移，脸回中才停（T 键切换）。", flush=True)

    while True:
        ok, frame = cap.read()
        if not ok:
            print("[错误] 读不到画面，可能被其他程序占用")
            break
        frame = cv2.flip(fit_frame(frame), 1)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]

        now = time.time()
        dt = clamp(now - prev_t, 1e-3, 0.10)
        prev_t = now
        frame_count += 1

        box = fb.update(frame)
        # 一旦抓到脸就把点重撒到脸框内：这是解决"动得慢、只往一个方向跑"的关键
        if box is not None and (not seeded_on_face or now - last_reseed > 2.5):
            tr.reseed(gray)
            seeded_on_face = True
            last_reseed = now

        dx, dy, npts = tr.update(gray)
        center = face_center(box, gray.shape)

        if args.autostart and not running and frame_count == 2:
            start()
            running = True
            pause_reason = ""
            print("[自动开始] --autostart")

        # ---------- 全局急停（不依赖窗口焦点） ----------
        if gk.edge("F12"):
            print("[F12] 全局退出")
            break
        if gk.edge("F8"):
            if running:
                running = False
                pause_reason = "F8 暂停"
                print("[F8] 已暂停，鼠标交还")
            else:
                start()
                running = True
                pause_reason = ""
                print("[F8] 已开始")

        if running:
            # 安全 1：连续丢脸 -> 自动暂停
            if box is None:
                lost_t += dt
            else:
                lost_t = 0.0
            if lost_t > LOST_PAUSE:
                running = False
                pause_reason = "丢脸 %.1fs 自动暂停" % lost_t
                print("[自动暂停] 连续 %.1f 秒没检到人脸，已交出鼠标" % lost_t)
            # 安全 2：真人抓鼠标 -> 自动暂停（本帧还没设新位置，比对才有意义）
            elif cursor.hijacked():
                running = False
                pause_reason = "检测到手动接管"
                print("[自动暂停] 光标被手动移动，判定人工接管，已交出鼠标（F8 恢复）")

        # 人脸模型可用却检不到脸时，冻结光标：此时跟踪点在背景上，一动就是乱飞
        face_lost = (box is None and fb.enabled)

        if running and not face_lost:
            mp.step(dx, dy, center)
            ox, oy = mp.offset

            if mode == "pos":
                kx = (VW * 0.5) / range_px
                ky = (VH * 0.5) / range_px
                tx = clamp(cursor.origin[0] + ox * kx, VX + 1, VX + VW - 2)
                ty = clamp(cursor.origin[1] + oy * ky, VY + 1, VY + VH - 2)
                cursor.move_abs(tx, ty)
            else:
                m = float(np.hypot(ox, oy))
                if m > VEL_DEAD:
                    gain = min(1.0, (m - VEL_DEAD) / VEL_FULL)
                    speed = VEL_MAX * gain
                    cursor.move_rel(ox / m * speed * dt, oy / m * speed * dt)

            # 重新武装用"净位移"而不是"累计路程"：抖动来回抵消，不会把自己抖成可点击
            if click_pos is None:
                dist = REARM_PX
            elif cursor.last is None:
                dist = 0.0
            else:
                dist = float(np.hypot(cursor.last[0] - click_pos[0],
                                      cursor.last[1] - click_pos[1]))
            armed = dist >= REARM_PX
            cooldown = max(0.0, cooldown - dt)

            head_moved = abs(dx) > STILL_PX or abs(dy) > STILL_PX
            if head_moved:
                still_t = 0.0
            else:
                still_t += dt
                if (still_t >= DWELL and cooldown <= 0 and armed
                        and not args.no_mouse and npts >= 10):
                    mouse_click(double=double_mode)
                    if double_mode:
                        clicks_double += 1
                    else:
                        clicks_single += 1
                    still_t = 0.0
                    cooldown = COOLDOWN
                    click_pos = cursor.last
                    armed = False
                    print("[点击] %s  单击:%d  双击:%d"
                          % ("双击" if double_mode else "单击", clicks_single, clicks_double))
        else:
            still_t = 0.0

        # ---------- 叠层 ----------
        if box is not None:
            x, y, bw, bh = [int(v) for v in box]
            cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 200, 255), 2)
            cv2.putText(frame, "FACE %.2f" % fb.score, (x, max(12, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 200, 255), 1, cv2.LINE_AA)
        else:
            cv2.rectangle(frame, (int(w * ROI_X0), int(h * ROI_Y0)),
                          (int(w * ROI_X1), int(h * ROI_Y1)), (0, 0, 255), 1)
            cv2.putText(frame, "NO FACE - press C", (int(w * ROI_X0), int(h * ROI_Y0) - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
        for p in tr.points():
            cv2.circle(frame, (int(p[0]), int(p[1])), 2, (0, 255, 0), -1)

        cv2.rectangle(frame, (0, 0), (w, 36), (0, 0, 0), -1)
        if not running:
            tip, color = "PAUSED (%s)  -  SPACE / F8 to start" % pause_reason, (0, 200, 255)
        elif face_lost:
            tip, color = "FACE LOST  -  cursor frozen, press C to relock", (0, 0, 255)
        elif npts < 12:
            tip, color = "TRACKING WEAK  -  press C to relock", (0, 0, 255)
        elif not armed:
            tip, color = "RECLICK LOCKED  -  move %.0f px to re-arm" % REARM_PX, (0, 160, 255)
        else:
            tip = "TRACKING  -  %s  -  hold still %.1fs to %s" % (
                "POSITION" if mode == "pos" else "VELOCITY",
                DWELL, "DOUBLE-CLICK" if double_mode else "CLICK")
            color = (0, 255, 0)
        cv2.putText(frame, tip, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.52, color, 2, cv2.LINE_AA)

        ox, oy = mp.offset
        total = clicks_single + clicks_double * 2
        cv2.rectangle(frame, (0, h - 46), (w, h), (0, 0, 0), -1)
        cv2.putText(frame, "pts:%d  head:%+.0f,%+.0f px  %s  clicks:%d (S%d/D%d)"
                    % (npts, ox, oy,
                       ("range %.0f" % range_px) if mode == "pos" else "vmax %.0f" % VEL_MAX,
                       total, clicks_single, clicks_double),
                    (10, h - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (240, 240, 240), 1, cv2.LINE_AA)
        cv2.putText(frame, "move:%.0f/%.0f px  %s  %s"
                    % (dist, REARM_PX, "ARMED" if armed else "LOCKED",
                       "FACE LOST" if face_lost else "TRACKING"),
                    (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.46,
                    (0, 255, 0) if armed else (0, 160, 255), 1, cv2.LINE_AA)

        prog = min(1.0, still_t / DWELL)
        if prog > 0 and running:
            cv2.rectangle(frame, (0, h - 52), (int(w * prog), h - 47), (0, 255, 0), -1)

        # 右上角小地图（仅位置模式有意义）
        if mode == "pos":
            mx0, my0, mw, mh = w - 86, 46, 78, 44
            cv2.rectangle(frame, (mx0, my0), (mx0 + mw, my0 + mh), (60, 60, 60), 1)
            px = int(clamp(mx0 + mw * (0.5 + ox * ((VW * 0.5) / range_px) / VW), mx0, mx0 + mw))
            py = int(clamp(my0 + mh * (0.5 + oy * ((VH * 0.5) / range_px) / VH), my0, my0 + mh))
            cv2.circle(frame, (px, py), 3, (0, 255, 255), -1)

        cv2.imshow(win, frame)

        if args.snapshot and frame_count == 25:
            ok2, buf = cv2.imencode(".png", frame)
            if ok2:
                with open(args.snapshot, "wb") as fp:
                    fp.write(buf.tobytes())
                print("[快照] 已保存 %s" % args.snapshot)

        k = cv2.waitKey(1) & 0xFF
        if k in (27, ord('q')):
            break
        elif k == 32:
            if not running:
                start()
                running = True
                pause_reason = ""
                print("[开始] 光标原点 = 当前光标位置；头往哪偏光标往哪走")
            else:
                running = False
                pause_reason = "空格暂停"
                print("[暂停] 鼠标已交还")
        elif k in (ord('c'), ord('C')):
            tr.reseed(gray)
            if running:
                mp.lock(center)
            else:
                mp.reset()
            last_reseed = time.time()
            seeded_on_face = True
            still_t = 0.0
            click_pos = cursor.last
            armed = False
            print("[重新锁定] 特征点已重撒，偏移清零")
        elif k in (ord('o'), ord('O')):
            cursor.capture_origin()
            if running:
                mp.lock(center)
            print("[原点] 已取当前光标位置 %s" % (cursor.origin,))
        elif k in (ord('t'), ord('T')):
            mode = "vel" if mode == "pos" else "pos"
            click_pos = cursor.last
            armed = False
            print("[映射]", "速度映射（脸偏左则持续左移）" if mode == "vel"
                  else "位置映射（脸偏多少光标走多少）")
        elif k in (ord('x'), ord('X')):
            double_mode = not double_mode
            print("[模式]", "双击" if double_mode else "单击")
        elif k in RANGE_TABLE:
            range_px = RANGE_TABLE[k]
            print("[灵敏度] 有效行程 %.0f px" % range_px)

        if args.frames and frame_count >= args.frames:
            print("[自测] 已跑满 %d 帧，退出" % frame_count)
            break

    cap.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
