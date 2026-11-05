# -*- coding: utf-8 -*-
"""
头动控制鼠标 —— 等价于 Enable Viacam 的实现（实验4：头动控制电脑，实现鼠标的移动、单击、双击）

原理（与 eViacam 相同，但更贴合本机环境）：
  1) YuNet 神经网络检出人脸框；
  2) 只在脸框内播撒特征点，LK 光流（带前后向校验）跟踪这些纹理点；
  3) 取位移的"中位数"作为本帧头部微位移——中位数天然抗背景点、抗跑飞点，
     不会像平均值那样被大量静止的背景拉慢；
  4) 自动零点校准 + 死区：把光流的系统性微小偏差学掉，静止时鼠标不漂；
  5) 累计位移按比例映射成鼠标的绝对位置：头偏 180px 约等于鼠标横穿全屏。

操作：
  空格   开始 / 暂停（开始时把当前头部位置记为原点，鼠标不跳）
  C      重新锁定（头摆正后按，重撒特征点并清零偏移）
  X      切换 单击 / 双击
  1/2/3  灵敏度 低 / 中 / 高
  ESC    退出

用法：
  python head_mouse.py             # 正常使用
  python head_mouse.py --cam 1     # 多摄像头时指定序号
  python head_mouse.py --no-mouse  # 只看画面不控制鼠标
  python head_mouse.py --selftest  # 自检：测人脸检出率、特征点数、静置漂移
"""
import argparse
import ctypes
import ctypes.wintypes
import os
import time

import cv2
import numpy as np

user32 = ctypes.windll.user32

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "yunet.onnx")

FRAME_W, FRAME_H = 640, 480
ROI_X0, ROI_Y0, ROI_X1, ROI_Y1 = 0.28, 0.16, 0.72, 0.80   # 没检到脸时的兜底框

DWELL = 0.8        # 停留多少秒触发点击
STILL_PX = 0.45    # 单帧位移小于它算"没动"
MOVE_REARM = 16.0  # 点击后必须再移动这么多像素，才允许下一次停留点击


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def set_cursor(x, y):
    user32.SetCursorPos(int(x), int(y))


def fit_frame(frame, tw=FRAME_W, th=FRAME_H):
    h, w = frame.shape[:2]
    s = min(tw / float(w), th / float(h))
    nw, nh = max(1, int(w * s)), max(1, int(h * s))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((th, tw, 3), np.uint8)
    ox, oy = (tw - nw) // 2, (th - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = resized
    return canvas


def mouse_click(double=False):
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if double:
        time.sleep(0.06)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


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

    思路：
      * 光流负责帧间的高频、亚像素级位移（快，不卡）；
      * 脸框中心是绝对参照，用它把累计位移"拉回来"（慢速锚定，BETA 每帧只拉一小步），
        这样光流的系统性偏差不会一路累加成"只往左上跑"；
      * 死区 + 轻平滑，去掉静止时的手指级抖动。
    """

    BETA = 0.12       # 每帧向脸框中心回拉的比例（越大越稳，过大会拖慢真实动作）
    ANCHOR_LR = 0.20  # 脸框中心本身的平滑，压低检测抖动
    DEAD = 1.6        # 偏移死区（像素），低于它视为没动
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--no-mouse", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--frames", type=int, default=90, help="自检读取的帧数")
    args = ap.parse_args()

    enable_dpi_awareness()
    SW, SH = screen_size()

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

    fb = FaceBox()
    tr = FaceTracker(fb)
    mp = Mapper()

    print("[模型] 人脸检测：%s" % ("YuNet 已加载" if fb.enabled else "不可用，使用固定框"))
    print("[屏幕] %dx%d" % (SW, SH))

    # ---------------- 自检 ----------------
    if args.selftest:
        t0 = time.time()
        got = hits = last = 0
        seeded = False
        for i in range(args.frames):
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
            center = None
            if box is not None:
                center = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)
            off = mp.step(dx, dy, center)
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
        kx = (SW * 0.5) / 160.0
        ky = (SH * 0.5) / 160.0
        print("自检完成：%d 帧 / 人脸检出 %d 帧 / 特征点 %d 个 / 用时 %.1fs（%.0f fps）"
              % (got, hits, last, el, got / max(el, 1e-3)))
        print("末态头部偏移：%+.1f, %+.1f px  →  鼠标 %+.0f, %+.0f px（这段里你自己的头也在动，"
              "数值≈你实际动的那一下）" % (ox, oy, ox * kx, oy * ky))
        print("锚定残差 %.1f px（融合位置与脸框中心的差距，<10 说明没有跑偏）" % mp.residual)
        return 0

    # ---------------- 主循环 ----------------
    running = False
    double_mode = False
    range_px = 160.0
    still_t = 0.0
    cooldown = 0.0
    moved_since_click = 1e9
    clicks = 0
    npts = 0
    seeded_on_face = False
    last_reseed = 0.0
    prev_t = time.time()

    win = "Head Mouse  [SPACE] start/pause  [C] relock  [X] click/double  [1-3] speed  [ESC] quit"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 760, 570)
    print("[启动] 摄像头已打开，窗口已弹出（标题 Head Mouse）。")
    print("       按 空格 开始，头往哪偏鼠标往哪走；停住 0.8 秒单击。ESC 退出。", flush=True)

    while True:
        ok, frame = cap.read()
        if not ok:
            print("[错误] 读不到画面，可能被其他程序占用")
            break
        frame = cv2.flip(fit_frame(frame), 1)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]

        now = time.time()
        dt = max(now - prev_t, 1e-3)
        prev_t = now

        box = fb.update(frame)
        # 一旦抓到脸就把点重撒到脸框内：这是解决"动得慢、只往一个方向跑"的关键
        if box is not None and (not seeded_on_face or now - last_reseed > 2.5):
            tr.reseed(gray)
            seeded_on_face = True
            last_reseed = now

        dx, dy, npts = tr.update(gray)

        center = None
        if box is not None:
            center = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)

        if running:
            mp.step(dx, dy, center)
            sx, sy = mp.offset
            kx, ky = (SW * 0.5) / range_px, (SH * 0.5) / range_px
            tx = max(0, min(SW - 1, SW * 0.5 + sx * kx))
            ty = max(0, min(SH - 1, SH * 0.5 + sy * ky))
            if not args.no_mouse:
                set_cursor(tx, ty)

            moved_since_click += abs(dx) + abs(dy)
            cooldown = max(0.0, cooldown - dt)
            if abs(dx) > STILL_PX or abs(dy) > STILL_PX:
                still_t = 0.0
            else:
                still_t += dt
                if (still_t >= DWELL and cooldown <= 0 and not args.no_mouse
                        and npts >= 10 and moved_since_click >= MOVE_REARM):
                    mouse_click(double=double_mode)
                    clicks += 1
                    still_t = 0.0
                    cooldown = 1.2
                    moved_since_click = 0.0
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
            tip, color = "PAUSED  -  press SPACE to start", (0, 200, 255)
        elif npts < 12:
            tip, color = "TRACKING WEAK  -  press C to relock", (0, 0, 255)
        else:
            tip = "TRACKING  -  %s  -  hold still to click" % (
                "DOUBLE-CLICK" if double_mode else "CLICK")
            color = (0, 255, 0)
        cv2.putText(frame, tip, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.56, color, 2, cv2.LINE_AA)

        sx, sy = mp.offset
        cv2.putText(frame, "pts:%d  head:%+.0f,%+.0f px  range:%.0f  clicks:%d"
                    % (npts, sx, sy, range_px, clicks),
                    (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 240, 240), 1, cv2.LINE_AA)
        prog = min(1.0, still_t / DWELL)
        if prog > 0:
            cv2.rectangle(frame, (0, h - 7), (int(w * prog), h), (0, 255, 0), -1)

        # 右上角小地图：黄点表示光标大致位置
        mx0, my0, mw, mh = w - 86, 46, 78, 44
        cv2.rectangle(frame, (mx0, my0), (mx0 + mw, my0 + mh), (60, 60, 60), 1)
        px = int(mx0 + mw * min(max(0.5 + (sx * ((SW * 0.5) / range_px)) / SW, 0), 1))
        py = int(my0 + mh * min(max(0.5 + (sy * ((SH * 0.5) / range_px)) / SH, 0), 1))
        cv2.circle(frame, (px, py), 3, (0, 255, 255), -1)

        cv2.imshow(win, frame)

        k = cv2.waitKey(1) & 0xFF
        if k in (27, ord('q')):
            break
        elif k == 32:
            if not running:
                tr.reseed(gray)
                mp.reset()
                mp.set_anchor(center)
                still_t = 0.0
                moved_since_click = 0.0
                running = True
                print("[开始] 已锁定头部；头往哪偏鼠标往哪走，空格暂停")
            else:
                running = False
                print("[暂停] 鼠标已交还")
        elif k in (ord('c'), ord('C')):
            tr.reseed(gray)
            mp.reset()
            mp.set_anchor(center)
            last_reseed = time.time()
            seeded_on_face = True
            still_t = 0.0
            moved_since_click = 0.0
            print("[重新锁定] 特征点已重撒，偏移清零")
        elif k in (ord('x'), ord('X')):
            double_mode = not double_mode
            print("[模式]", "双击" if double_mode else "单击")
        elif k == ord('1'):
            range_px = 260.0
        elif k == ord('2'):
            range_px = 160.0
        elif k == ord('3'):
            range_px = 100.0

    cap.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
