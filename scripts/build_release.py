#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键构建 Windows 发布包。

    python scripts/build_release.py --version 1.2

产出（都在 dist/ 下）：
    HeadMouse-v1.2-win64.exe            单文件版，双击即用（启动稍慢，杀软误报概率高）
    HeadMouse-portable/                 目录版，解压即用（启动快，推荐发给同学）
    HeadMouse-v1.2-win64-portable.zip   上面那个目录的压缩包

为什么要两个版本：PyInstaller 单文件版每次运行都要把几百 MB 依赖解到 %TEMP%，
冷启动 2~5 秒，且不少杀软会把"自解压 exe"当成可疑程序；目录版没有这两个问题。
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
MODEL = os.path.join("models", "yunet.onnx")
ICON = os.path.join("assets", "app.ico")


def run(args):
    print("[build] " + " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def pyinstaller(name, onefile, extra=()):
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--onefile" if onefile else "--onedir",
            "--name", name,
            "--icon", ICON,
            "--add-data", "%s;models" % MODEL,
            "--distpath", DIST, "--workpath", BUILD]
    args += list(extra)
    args.append("head_mouse.py")
    run(args)


def zip_dir(src_dir, zip_path):
    base = os.path.dirname(src_dir)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for folder, _, files in os.walk(src_dir):
            for f in files:
                full = os.path.join(folder, f)
                z.write(full, os.path.relpath(full, base))
    return zip_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="1.2")
    ap.add_argument("--skip-onefile", action="store_true", help="只出目录版（更快）")
    a = ap.parse_args()
    ver = a.version

    os.makedirs(DIST, exist_ok=True)

    # 1) 目录版
    pyinstaller("HeadMouse", onefile=False)
    portable = os.path.join(DIST, "HeadMouse-portable")
    if os.path.isdir(portable):
        shutil.rmtree(portable)
    shutil.move(os.path.join(DIST, "HeadMouse"), portable)
    # 随包附带面向同学的纸质说明
    readme_txt = os.path.join(ROOT, "docs", "同学速用说明.txt")
    if os.path.exists(readme_txt):
        shutil.copy(readme_txt, os.path.join(portable, "使用说明.txt"))
    z = zip_dir(portable, os.path.join(DIST, "HeadMouse-v%s-win64-portable.zip" % ver))
    print("[build] 目录版 -> %s" % portable)
    print("[build] 压缩包 -> %s (%.1f MB)" % (z, os.path.getsize(z) / 1048576))

    # 2) 单文件版
    if not a.skip_onefile:
        pyinstaller("HeadMouse", onefile=True)
        exe = os.path.join(DIST, "HeadMouse.exe")
        target = os.path.join(DIST, "HeadMouse-v%s-win64.exe" % ver)
        if os.path.exists(target):
            os.remove(target)
        shutil.move(exe, target)
        print("[build] 单文件 -> %s (%.1f MB)" % (target, os.path.getsize(target) / 1048576))

    print("[build] 完成。发布前建议在本机双击跑一遍，确认能看到摄像头画面。", flush=True)


if __name__ == "__main__":
    main()
