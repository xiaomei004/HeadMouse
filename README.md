# HeadMouse · 头动控制鼠标

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)
![OpenCV](https://img.shields.io/badge/OpenCV-4.8%2B-green.svg)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)

> 人机交互实验 4「体感操控」的自主实现：一颗普通摄像头 + 一台没有装任何驱动的电脑，
> 用头部运动代替鼠标完成**移动、单击、双击**。等价于 Enable Viacam，但只有一个 Python 文件。

- 纯 Python，依赖只有 `opencv-python` 与 `numpy`
- 640×480 @ 29 fps，静置 8 秒漂移 ≈ 17 px（脸宽的一半都不到）
- 位置映射 / 速度映射两种操作律，可在运行时切换
- 光标外面套一圈高对比度琥珀准星（系统指针只有 32px，动起来很难找）
- 三重安全兜底：全局急停键、人工接管检测、丢脸冻结

## 下载即用（同学 / 老师不用装 Python）

到 [Releases](https://github.com/xiaomei004/HeadMouse/releases/latest) 下载现成的 Windows 包：

| 文件 | 说明 |
|---|---|
| `HeadMouse-v1.2-win64-portable.zip`（61 MB） | **推荐**。解压整个文件夹，双击 `HeadMouse.exe`，启动约 3 秒 |
| `HeadMouse-v1.2-win64.exe`（61 MB） | 单文件版，双击即用；每次启动要解压依赖，约 5 秒，且更容易被杀软误报 |

> 为什么两个都提供：单文件版每次运行会把依赖解到 `%TEMP%`，冷启动慢、体积大；
> 目录版没有这两个问题。功能完全一样。

首次运行三件事：

1. 弹出蓝色「Windows 已保护你的电脑」→ **更多信息 → 仍要运行**（未做数字签名的程序都会这样）；
2. `设置 → 隐私和安全性 → 相机` 里允许**桌面应用访问相机**；
3. 看到画面里出现**黄色人脸框 + FACE 0.9x** 再按空格。

压缩包内附 `使用说明.txt`（按键表、常见问题、手感调节）。杀软若报毒，把整个文件夹加入白名单
——PyInstaller 产物被误判很常见，源码就在本仓库，可自行核对或直接 `python head_mouse.py` 跑。

## 为什么不用 eViacam

课程指定安装 eViacam，但这台机器上它一启动就崩（`APPCRASH 0xc0000005`），换英文路径、
改兼容模式、重装 CRT 都没救（排查过程见 [docs/02-问题排查记录.md](docs/02-问题排查记录.md)）。
于是按同一套人机交互思路自己写了一个：摄像头 → 人脸 → 头部微位移 → 光标 + 停留点击。

## 快速开始

```bash
pip install -r requirements.txt
python head_mouse.py            # 或双击 启动头控.cmd
```

1. 等摄像头窗口弹出，画面里出现**黄色人脸框**（说明脸被抓到了）
2. 让光标停在你想要的起点（默认以**当前光标位置**为原点，不会跳到屏幕中心）
3. 按 `空格`（或 `F8`）开始；头往哪偏，光标往哪走
4. 光标停住 0.8 秒 = 一次单击；按 `X` 切到双击模式

## 快捷键

| 键 | 作用 | 是否需要窗口焦点 |
|---|---|---|
| `空格` | 开始 / 暂停 | 是 |
| `F8` | 开始 / 暂停 | **否（全局）** |
| `F12` | 立即退出，交还鼠标 | **否（全局）** |
| `ESC` / `Q` | 退出 | 是 |
| `C` | 重新锁定（脸摆正后按，重撒特征点、偏移清零） | 是 |
| `O` | 以当前光标位置重新取原点 | 是 |
| `T` | 位置映射 ↔ 速度映射 | 是 |
| `X` | 单击 ↔ 双击 | 是 |
| `H` | 开 / 关 光标准星光环 | 是 |
| `1` `2` `3` | 灵敏度 低 / 中 / 高（有效行程 260 / 160 / 100 px） | 是 |

> 窗口失去焦点时 `cv2.waitKey` 收不到按键，所以急停键走的是 `GetAsyncKeyState` 轮询（全局生效）。

## 两种映射律

| | 位置映射 `pos`（默认） | 速度映射 `vel` |
|---|---|---|
| 公式 | `光标 = 原点 + k · 偏移` | `v = VMAX · clamp((|偏移| − 7) / 45, 0, 1)^1.5` |
| 手感 | 脸偏多少光标走多少，脸回中光标回原点 | 脸持续偏左，光标持续左移，回中才停 |
| 适合 | 小范围精确定位，和 eViacam 默认一致 | 大屏、长距离移动、不需要频繁回中 |
| 缺点 | 大屏要靠加大灵敏度，手抖会放大 | 容易过冲，停不住时会一直撞边 |
| `1/2/3` | 行程 260 / 160 / 100 px | 满速 380 / 650 / 1000 px/s |

`T` 键随时切换，两个都可以写进实验报告做对比。

## 工作原理

```
摄像头 30 fps
  └─ YuNet 人脸检测（每 3 帧一次）           → 脸框 (x, y, w, h)
      └─ 只在脸框内撒 160 个 Shi-Tomasi 角点   → 免疫背景静止纹理
          └─ LK 金字塔光流 + 前后向校验        → 每点帧间位移
              └─ 取位移「中位数」              → 抗跑飞点、抗遮挡
                  └─ 与脸框中心的锚定融合       → 消掉光流累计误差
                      └─ 死区 + 指数平滑        → 静止不抖
                          ├─ 位置/速度映射      → SetCursorPos / mouse_event(MOVE)
                          └─ 停留 0.8s + 净位移 ≥ 90px → 单击 / 双击
```

为什么是中位数而不是平均值、锚定融合的公式怎么来的、参数怎么定的，都在
[docs/01-技术方案与原理.md](docs/01-技术方案与原理.md)。

## 安全设计（重点）

| 风险 | 兜底 |
|---|---|
| 程序一直抢光标，人抢不回来 | 光标与程序设定值相差 > 90 px → 判定人工接管，**自动暂停并交还鼠标** |
| 窗口失焦后按不了 ESC | `F8` / `F12` 全局键，`GetAsyncKeyState` 轮询，任何前台状态下都能停 |
| 人离开 / 转头，跟踪点跑到背景上 | 检不到脸 **2.5 秒**自动暂停；检不到脸时**光标冻结**不跟背景乱飞 |
| 停在原地被连续点击 | 每次点击后必须让光标**净位移 ≥ 90 px** 才能再点（UI 显示 `LOCKED / ARMED`） |
| 按下空格瞬间误点 | 开始后同样要先动一下才允许第一次点击 |

## 命令行参数

```
--cam N          指定摄像头序号（默认 0）
--no-mouse       只显示画面，不控制鼠标（演示 / 截图）
--mode pos|vel   启动时的映射律（默认 pos）
--vel-max N      速度模式满速像素/秒（默认 650，越小越稳）
--no-halo        不显示光标准星
--origin cursor|center  开始时光标原点（默认 cursor=当前光标位置）
--selftest       自检：人脸检出率、特征点数、锚定残差、帧率
--frames N       跑满 N 帧自动退出（自测 / CI）
--autostart      启动即开始
--snapshot PATH  第 25 帧存一张带叠层的截图
```

主要可调参数在文件头部：`DWELL`（停留时间）、`REARM_PX`（重新武装距离）、
`STILL_PX`（静止判定）、`VEL_*`（速度映射：`VEL_DEAD` 死区、`VEL_FULL` 满速区、
`VEL_CURVE` 曲线指数、`VEL_MAX`/`VEL_TABLE` 满速），以及 `Mapper` 里的 `BETA` / `DEAD` / `SMOOTH`。

## 目录结构

```
HeadMouse/
├── head_mouse.py          主程序（单文件，含检测/跟踪/映射/UI/安全逻辑）
├── models/yunet.onnx      YuNet 人脸检测模型（232 KB，OpenCV Zoo）
├── 启动头控.cmd           一键启动（GBK 编码，检查依赖后运行）
├── requirements.txt
└── docs/
    ├── 01-技术方案与原理.md
    ├── 02-问题排查记录.md
    ├── 03-版本演进.md
    └── 04-实验报告对照.md
```

## 已知限制

- 只支持 Windows（`SetCursorPos` / `mouse_event` / `GetAsyncKeyState` 全是 Win32）
- 强逆光、脸只露一半、戴大口罩时会丢脸，此时光标冻结并自动暂停
- 头部**旋转**（点头、转头）会产生光流位移，被当成平移，必要时按 `C` 重新锁定
- 位置模式下光标回中 = 头回正，长时间使用颈部容易累，可切到速度映射
- 准星是置顶窗口，个别全屏独占游戏（DirectX 独占全屏）下不会显示

## 参考

- [OpenCV Zoo · YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
- Baker & Matthews, *Lucas-Kanade 20 Years On: A Unifying Framework*, IJCV 2004
- Enable Viacam（Cream Software）——本项目的功能参照
