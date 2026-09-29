#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bTool — ESP32 flashing / file management / REPL tool
bTool — ESP32 图形化烧写 / 文件管理 / REPL 工具

Features / 功能:
  1. Auto-detect serial ports, pick from dropdown / 串口自动检测, 下拉选择
  2. Chip selection: ESP32 / ESP32-S3 / ESP32-C3 / ESP32-C6
  3. Flash one or more .bin files (address auto-guessed), progress bar, optional erase
     烧写本地 .bin (可多文件+多地址), 带进度条, 可选先擦除
  4. Local <-> device VFS transfer: upload / download / delete / rename
     本地文件 ↔ 设备 VFS: 上传 / 下载 / 删除 / 重命名
  5. REPL terminal: Ctrl+C to interrupt, raw REPL toggle
     底部 REPL 终端: Ctrl+C 中断, raw REPL 切换

Dependencies / 依赖:
  pip install pyserial esptool

Run / 运行:
  python btool.py
"""

import base64
import ctypes
import json
import os
import re
import queue
import sys
import threading
import time
import traceback
from contextlib import contextmanager

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("Missing pyserial / 缺少 pyserial:  pip install pyserial")

try:
    import esptool
    import esptool.cmds
    from esptool.logger import EsptoolLogger
except ImportError:
    esptool = None
    EsptoolLogger = None


APP_NAME = "bTool"


# ======================================================================
# DPI 感知 / DPI awareness
# ======================================================================
# ★ 必须在 **tk.Tk() 之前**跑 —— Windows 是在进程第一次建窗口时登记 DPI 感知级别的,
#   登记之后再调就晚了 (窗口已按 unaware 画过一轮)。
#
# 不开的后果: 系统缩放 125% 时, Windows 把整个窗口按 96 DPI 画进离屏位图, 再整块
#   拉伸到 125% —— 文字和线条全是插值出来的, 发虚。这就是"界面看着不锐利"的根源,
#   跟哪个控件没关系。(实测本机 120 DPI 缩放, 本工具原先的进程 DPI 感知 = 0/UNAWARE)
#
# 代价: 开了之后写死的像素**不再被放大** ⇒ 界面整体缩 25%。所以尺寸都要过 px(),
#   把那 25% 补回来 (见 px() 的注释)。
def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 2 = PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()        # 老系统 / 无 shcore 时的退路
    except Exception:
        pass


def _query_scale():
    """本机 DPI 缩放系数 (96 DPI = 1.0)。拿不到就当 1.0, 不能因此起不来。"""
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if 48 <= dpi <= 480:            # 0.5x ~ 5x, 离谱的值当没读到
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


_enable_dpi_awareness()
_SCALE = _query_scale()


def px(n):
    """逻辑像素 → 物理像素。

    ⚠ 每个**写死**的尺寸都要过这里, 否则在 125% 缩放的屏上会比改前小一圈 ——
      改前那些像素是被 Windows 拉伸放大的 (糊, 但尺寸对), 开了 DPI 感知后不再
      放大, 不补回来就成了"清楚了但也变小了"。

    只对**像素**生效。字符数 (Entry 的 width=) 和行数 (Text 的 height=) 不用管,
    它们本来就跟着字号走。
    """
    return max(1, int(round(n * _SCALE)))


def work_area():
    """桌面可用区域 (左, 上, 宽, 高), 物理像素 —— **已排除任务栏**。

    ⚠ 不能用 winfo_screenwidth/height: 那两个给的是整块屏幕, 不含任务栏。
      写死窗口尺寸时差的就是这一条 —— 实测在 1920x1080 / 125% 的机器上,
      窗口按屏幕高 1080 算出来 1099 高, 而工作区只有 1020, 底部直接出屏。
    拿不到就返回 None, 由调用方退回屏幕尺寸。
    """
    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    try:
        r = _RECT()
        # SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            if r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        pass
    return None


# ======================================================================
# 视觉规范 / design tokens
# ======================================================================
# 界面里**只允许**用这里的值。改之前: 间距散落 11 种 (2,3,4,6,8,10,12,14,18,20,30)、
# 字号 5 种、按钮宽度 6 档、颜色 9 个 —— 同样一句"留点间隔", 在不同地方是 4px 还是
# 6px 全凭当时手写了多少, 这就是"排布随意"的来源。收敛成刻度之后, 整体调只改这里。

# ---- 间距刻度 (逻辑px, 用的时候过 px()) ----
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 12, 16, 24

# ---- 字号: 5 种收敛到 4 种 ----
FONT_UI      = ("Microsoft YaHei UI", 9)          # 正文 (与 tk 默认字体一致, 显式写出来免得随主题漂)
FONT_UI_BOLD = ("Microsoft YaHei UI", 9, "bold")  # 正文加粗 (仅强调用, 不算新字号)
FONT_SMALL   = ("Microsoft YaHei UI", 8)          # 提示 / 次要说明
FONT_MONO    = ("Consolas", 10)                   # **所有**等宽输出统一 (原先终端 11 / 日志 9)
FONT_H1      = ("Microsoft YaHei UI", 12, "bold") # 对话框标题

# ---- 控件宽度 (字符) ----
W_S, W_M, W_L = 8, 12, 20

# ---- 调色板: **原值**取自 Arduino IDE 2.x 浅色主题 ----
# 来源文件 (arduino/arduino-ide):
#   arduino-ide-extension/src/browser/data/default.color-theme.json
# 每个 token 后面标的就是它在那里对应的键名 —— 要改色请**照键名去查**,
# 别再自己配 (之前凭印象配过一轮, 结果 #00979D / #434F54 / #ECECEC 全是
# 旧版 rc 或别的键的值, 整套偏色)。
# ★ 中性色都是**纯中性**(白 / #f7f9f9 / #ecf1f1 / #dae3e3), 青色只用在强调上 ——
#   之前我把所有中性色都染了青, 看着"颜色莫名其妙"就是这个原因。
#
# --- 中性 ---
C_FIELD     = "#FFFFFF"   # editor.background / input.background / dropdown.background
C_WIDGET    = "#F7F9F9"   # editorWidget.background / sideBar.background
C_CHROME    = "#ECF1F1"   # editorGroupHeader.tabsBackground / activityBar.background / tab.inactiveBackground
C_BORDER    = "#DAE3E3"   # dropdown.border / tree.indentGuidesStroke / list.inactiveSelectionBackground
C_BORDER_2  = "#B5C8C9"   # arduino.branding.secondary —— **强一档**的边框, 只在需要真边界时用
                          #   (#DAE3E3 太淡, 用来分隔页签会"糊在一起" —— 实测反馈)
C_MUTED     = "#BDC7C7"   # activityBar.inactiveForeground —— 次要文字/不可用
# --- 文字 ---
C_TEXT      = "#4E5B61"   # foreground / editor.foreground / dropdown.foreground
C_TEXT_HI   = "#212121"   # menu.selectionForeground —— 需要更重时用
# --- 品牌 (青色只在这些地方出现) ---
C_ACCENT    = "#008184"   # arduino.branding.primary = button.background
C_ACCENT_DK = "#005C5F"   # button.hoverBackground / progressBar.background
C_BAR       = "#006D70"   # statusBar.background / titleBar.activeBackground (深 teal)
C_ACCENT_LT = "#7FCBCD"   # focusBorder / toolbar.button.background / dropdown.borderActive
C_ACCENT_T2 = "#B5E0E1"   # ↑掺白 50% 的淡版 —— **不是 Arduino 原值**, 是为了让文字按钮
                          #   (120px 宽) 别像图标按钮那样一大片。悬停时回到 #7FCBCD。
C_ACCENT_2  = "#1DA086"   # toolbar.dropdown.iconSelected
C_SEL       = "#CCE6E6"   # list.activeSelectionBackground(#00818433) 的白底等效色 —— Tk 不支持透明度
C_HILITE    = "#DAE3E3"   # menu.selectionBackground / 悬停底
# --- 语义 ---
C_DANGER    = "#DF7365"   # errorForeground
C_WARN      = "#F1C40F"   # toolbar.toggleBackground —— 借来当警告黄
C_OK        = "#1DA086"
# --- 深色区: Arduino 的输出面板与终端**都是纯黑** ---
C_OUT_BG, C_OUT_FG = "#000000", "#FFFFFF"   # arduino.output.background / .foreground
C_TERM_BG, C_TERM_FG, C_TERM_SEL = "#000000", "#FFFFFF", "#7FCBCD"
# --- 顶部工具栏 (Arduino 的工具栏条) ---
# ⚠ 底色是 **#006D70** 而不是 #008184 —— 实拍 Arduino IDE 2.3.10 逐像素采样确认。
#   它就是 statusBar.background / titleBar.activeBackground, 同一个深 teal。
#   早先用 #008184 (branding.primary) 偏亮, 跟圆按钮底色 #7FCBCD 对比不足。
C_TOPBAR    = "#006D70"   # 工具栏底色
C_TOPBAR_LN = "#005C5F"   # 工具栏下沿 1px (压深一档, 跟内容区划清界线)
# --- 左侧页面竖栏 (Arduino 的 activityBar, 实拍取值) ---
#   实拍: 栏宽 60px / 底色 #ECF1F1 / 未选中图标 #BDC7C7 / 选中图标 #4E5B61 /
#         选中标记 = **左沿 2px 竖条 #008184**, 背景不变 (不是"整块变色")
C_RAIL_BG   = "#ECF1F1"   # activityBar.background
C_RAIL_W    = 48          # 竖栏宽度 (逻辑px)。Arduino 实拍 60 —— 它图标也大;
                          #   bTool 图标 28px, 48 已经足够疏朗又不浪费横向空间
C_RAIL_FG   = "#4E5B61"   # activityBar.foreground        —— 选中项图标
C_RAIL_DIM  = "#BDC7C7"   # activityBar.inactiveForeground —— 未选中图标
C_RAIL_MARK = "#008184"   # activityBar.activeBorder      —— 选中项左沿竖条
C_RAIL_HOV  = "#E0F1F1"   # 悬停底 (实拍里出现过的一层极浅青)

# ---- 圆形工具按钮 (Arduino 工具栏规格, 源码原值) ----
#   border-radius 14px + 28px 方块 = 正圆; 底色 toolbar.button.background;
#   圆内图标色 = titleBar.activeBackground (#006D70, Arduino 的图标就是这个色)
C_TOOL_BG    = "#7FCBCD"
C_TOOL_FG    = "#006D70"
C_TOOL_HOVER = "#F7F9F9"   # toolbar.button.hoverBackground
C_TOOL_DIM   = "#5E9FA0"   # 禁用态圆底 (比 C_TOOL_BG 暗, 表示不可点)


# ======================================================================
# 圆角外观 / rounded corners
# ======================================================================
# Tk 的 ttk 控件**画不出圆角** —— 按钮形状由主题的 element 决定, 只有方角。
# 想做圆角只有三条路, 这里走第二条:
#   ① 保持直角                    —— 零成本, 但用户明确要圆角
#   ② 九宫格图 + ttk image element —— 还是**真 ttk 按钮**, 键盘 Tab 导航/禁用态/
#                                     焦点环全部保留, 只是换了张"脸"  ← 本文件采用
#   ③ 自绘 Canvas 按钮             —— 最灵活, 但 Canvas 不进焦点环, 会丢掉 Tab 导航
#
# 图是**运行时纯 Python 逐像素画的** (PPM P6, 带 4×4 超采样抗锯齿), 不进源码、
# 不依赖 Pillow —— bTool 的运行期依赖只有 pyserial + esptool, 不能因为圆角就多一个。
_ROUND_IMGS = []      # ★★ 必须留引用: PhotoImage 被 GC 掉, Tk 那边的图像就没了,
                      #    渲染出来是**纯黑块** (实测踩过)。别删这个列表。


def _seg_dist(px, py, ax, ay, bx, by):
    """点 (px,py) 到线段 AB 的距离 —— 画下拉箭头的两条斜线用。"""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    ex, ey = ax + t * dx, ay + t * dy
    return ((px - ex) ** 2 + (py - ey) ** 2) ** 0.5


# ---- 线描图标 / line icons ----
# Arduino 的图标全是**线描**风格 (stroke, 统一线宽, 端点圆整)。之前我用实心三角/方块
# 画, 形状是够用但糙 —— 这里改成同一套线描画法。
# 坐标一律用 **0~1 归一化**, 由 box 映射到实际像素, 所以同一个图标能在任意尺寸下复用。
# 图元:
#   ("line", x0, y0, x1, y1)          线段
#   ("rect", x0, y0, x1, y1)          矩形**框** (只画边)
#   ("box",  x0, y0, x1, y1)          实心矩形
_ICONS = {
    # 连接: 箭头插进一根竖条 (plug in)
    # ⚠ 箭头那两笔**必须短**。笔画本身有宽度, 两笔在尖端夹出的实心三角会糊成一坨 ——
    #   实拍 Arduino 圆里的 ✓ / → 都是细笔画 + 小箭头, 视觉重量全在这儿。
    "connect": [("line", 0.08, 0.50, 0.60, 0.50),
                ("line", 0.46, 0.34, 0.62, 0.50),
                ("line", 0.46, 0.66, 0.62, 0.50),
                ("line", 0.88, 0.16, 0.88, 0.84)],
    # 断开: 向上抽出的箭头 + 底线 (拔出)
    "disconnect": [("line", 0.50, 0.84, 0.50, 0.28),
                   ("line", 0.36, 0.44, 0.50, 0.26),
                   ("line", 0.64, 0.44, 0.50, 0.26),
                   ("line", 0.16, 0.92, 0.84, 0.92)],
    # 已连接: 对勾
    "check": [("line", 0.12, 0.52, 0.38, 0.80), ("line", 0.38, 0.80, 0.88, 0.20)],
    # 未连接: 叉
    "cross": [("line", 0.18, 0.18, 0.82, 0.82), ("line", 0.18, 0.82, 0.82, 0.18)],
    # --- 左侧竖栏的页面图标 ---
    # 烧写: 向下箭头 + 底线 (download)
    "ic_flash": [("line", 0.50, 0.08, 0.50, 0.60),
                 ("line", 0.30, 0.40, 0.50, 0.62),
                 ("line", 0.70, 0.40, 0.50, 0.62),
                 ("line", 0.18, 0.90, 0.82, 0.90)],
    # 终端: > 加下划线。下划线要**长**、要压在 > 的下端同一水平线上, 才像命令行
    "ic_term": [("line", 0.14, 0.24, 0.44, 0.50),
                ("line", 0.44, 0.50, 0.14, 0.76),
                ("line", 0.56, 0.78, 0.90, 0.78)],
    # 文件: 一个**文件夹** (Arduino 的 SKETCHBOOK 就是个文件夹)。
    #   早先画"一张纸 + 三条内容线", 在 26px 下三条线糊成一个实心块 ——
    #   看着就是个黑方块, 完全认不出是什么。少即是多。
    "ic_file": [("line", 0.10, 0.24, 0.42, 0.24),
                ("line", 0.42, 0.24, 0.42, 0.34),
                ("line", 0.42, 0.34, 0.90, 0.34),
                ("line", 0.90, 0.34, 0.90, 0.82),
                ("line", 0.90, 0.82, 0.10, 0.82),
                ("line", 0.10, 0.82, 0.10, 0.24)],
    # 芯片: 方框 + 四脚
    "ic_chip": [("rect", 0.26, 0.26, 0.74, 0.74),
                ("line", 0.50, 0.10, 0.50, 0.26),
                ("line", 0.50, 0.74, 0.50, 0.90),
                ("line", 0.10, 0.50, 0.26, 0.50),
                ("line", 0.74, 0.50, 0.90, 0.50)],
}


def _icon_hit(x, y, box, name, th):
    """点 (x,y) 是否落在名为 name 的线描图标上 (坐标归一化, 由 box 映射)。"""
    x0, y0, x1, y1 = box
    W, H = x1 - x0, y1 - y0
    if W <= 0 or H <= 0:
        return False
    for st in _ICONS.get(name, ()):
        k = st[0]
        a, b = x0 + st[1] * W, y0 + st[2] * H
        c, d = x0 + st[3] * W, y0 + st[4] * H
        if k == "line":
            if _seg_dist(x, y, a, b, c, d) <= th:
                return True
        elif k == "rect":
            if a - th <= x <= c + th and b - th <= y <= d + th                     and not (a + th <= x <= c - th and b + th <= y <= d - th):
                return True
        elif k == "box":
            if a <= x <= c and b <= y <= d:
                return True
    return False



def _icon_ppm(n, name, fg, bg, ss=4, th=None):
    """线描图标 → n×n 的 PPM(P6) 原始字节 (线条 fg, 背景 bg)。

    ⚠ 背景色是**烤进图里的** —— PPM 没有 alpha 通道, 所以必须传入图标所在
      容器的实际底色, 否则图标四周会带一个异色方块。

    坐标走 _ICONS 的归一化表, 所以同一个图标名在任意尺寸下都成立
    (竖栏 26px、圆按钮里 14px, 都是同一份定义)。
    """
    th = th if th is not None else max(1.0, n * 0.062)   # 线宽 ≈ 图标尺寸的 6.2%
    box = (n * 0.12, n * 0.12, n * 0.88, n * 0.88)       # 图形占中间 76%
    fr, fgn, fb = _rgb(fg)
    br, bgn, bb = _rgb(bg)
    NL = chr(10)
    buf = bytearray(("P6" + NL + "%d %d" % (n, n) + NL + "255" + NL).encode())
    inv = 1.0 / (ss * ss)
    for y in range(n):
        for x in range(n):
            hit = 0
            for j in range(ss):
                yy = y + (j + 0.5) / ss
                for i in range(ss):
                    if _icon_hit(x + (i + 0.5) / ss, yy, box, name, th):
                        hit += 1
            a = hit * inv
            buf.append(int(br + (fr - br) * a))
            buf.append(int(bgn + (fgn - bgn) * a))
            buf.append(int(bb + (fb - bb) * a))
    return bytes(buf)


def _mark_hit(x, y, box, shape):
    """图标覆盖判定: 点 (x,y) 是否落在图标内。box = (x0, y0, x1, y1) 图像像素。

    shape 直接取 _ICONS 里的**线描图标名** (connect / disconnect / check /
    cross / ic_flash ...) —— 圆按钮里的图形和左侧竖栏的图标因此共用同一套
    画法, 不用各写一份覆盖判定 (早先这里是 play/stop/check/cross 四个硬编码
    形状, 而且 play 是个实心三角, 视觉重量跟别处对不上)。
    """
    x0, y0, x1, y1 = box
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    # 笔画粗细取短边的 6% —— 跟 _icon_ppm 的默认线宽同量级, 免得同一个图标
    # 挂在圆按钮里和在竖栏里看着一粗一细。
    # ⚠ 这个数**调大过就回不去了**: 实拍对比过 Arduino 圆里的 ✓ / →, 它的笔画
    #   只占直径的 6% 左右; 我这里一度是 9%, 放大看箭头直接糊成一个实心三角。
    return _icon_hit(x, y, box, shape, max(1.0, min(x1 - x0, y1 - y0) * 0.06))


def _rr_ppm(w, h, r, fill, border, outside, ss=4, chevron=None, bw=1.0,
            mark=None):
    """圆角矩形 → PPM(P6) 原始字节 (tk.PhotoImage 直接吃 bytes, **不能**用 base64)。

    三层合成: 外部色 outside → 边框色 border → 填充色 fill, 各带覆盖率。
    outside 是**按钮所在容器的底色** —— 把容器色烤进四角, 就不用真透明通道
    (PPM 没有 alpha), 边缘抗锯齿也自然过渡到容器色。
    """
    def cov(x, y, inset):
        n = ss; hit = 0
        for i in range(n):
            for j in range(n):
                px, py = x + (i + 0.5) / n - inset, y + (j + 0.5) / n - inset
                dx = max(r - px, px - (w - r), 0.0)
                dy = max(r - py, py - (h - r), 0.0)
                if dx * dx + dy * dy <= r * r:
                    hit += 1
        return hit / (n * n)
    NL = chr(10)
    buf = bytearray(("P6" + NL + "%d %d" % (w, h) + NL + "255" + NL).encode())
    # ★ 边框宽度必须是 **1px**。早先写成 r*0.32 (半径 7 时 ≈2.2px), 深色描边包浅色填充
    #   ⇒ 看着像倒角/浮雕, 而第一级(描边=填充, 看不见边)看着是平面的 —— 两级不在
    #   一个语汇里。Arduino 的按钮**压根没有描边**, 全是纯平铺色块。
    # chevron: (中心x, 中心y, 半宽, 半高, 线粗, 颜色) —— 下拉箭头直接画进图里。
    #   为什么不单用 Combobox.downarrow 元素: 它跟圆角 field 并存时要么被 field
    #   挤掉宽度、要么取不到 arrowcolor 而根本不画 (两种都实测过, 都没出来)。
    #   画进图里最稳 —— 而且整个下拉框本来就可点, 不靠那个元素响应。
    if mark:
        mkn, _mkc, mk_pad = mark       # (形状, 颜色, 边距)
        _mk = _rgb(_mkc)
        mk_box = (mk_pad, mk_pad, w - mk_pad, h - mk_pad)
    cxs = cy = cx = None
    if chevron:
        cx, cy, hw, hh, ctk, _cc = chevron
        creg = (cx - hw - ctk, cy - hh - ctk, cx + hw + ctk, cy + hh + ctk)
    for y in range(h):
        for x in range(w):
            ao = cov(x, y, 0.0)
            ai = cov(x, y, bw)
            rgb = [outside[k] * (1.0 - ao) + border[k] * max(0.0, ao - ai)
                   + fill[k] * ai for k in range(3)]
            # mark: 圆里画一个图标 —— 直接画进图里, 不用字体字符
            #   (U+25B6 这类字符在部分字体下会被渲染成彩色 emoji, 不可控)
            if mark and mk_box[0] <= x <= mk_box[2] and mk_box[1] <= y <= mk_box[3]:
                hit = 0
                for i in range(ss):
                    for j in range(ss):
                        sx, sy = x + (i + 0.5) / ss, y + (j + 0.5) / ss
                        if _mark_hit(sx, sy, mk_box, mkn):
                            hit += 1
                m_a = hit / (ss * ss)
                if m_a > 0:
                    for k in range(3):
                        rgb[k] = rgb[k] * (1 - m_a) + _mk[k] * m_a
            if chevron and creg[0] <= x <= creg[2] and creg[1] <= y <= creg[3]:
                hit = 0
                for i in range(ss):
                    for j in range(ss):
                        sx, sy = x + (i + 0.5) / ss, y + (j + 0.5) / ss
                        d = min(_seg_dist(sx, sy, cx - hw, cy - hh, cx, cy),
                                _seg_dist(sx, sy, cx, cy, cx + hw, cy - hh))
                        if d <= ctk / 2.0:
                            hit += 1
                c_a = hit / (ss * ss)
                if c_a > 0:
                    for k in range(3):
                        rgb[k] = rgb[k] * (1 - c_a) + _cc[k] * c_a
            for k in range(3):
                buf.append(max(0, min(255, int(round(rgb[k])))))
    return bytes(buf)


def _rgb(hexstr):
    """'#RRGGBB' -> (r, g, b)。"""
    s = hexstr.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


_STYLED = set()       # element_create 每个名字只能建一次; 切语言会重建 UI, 要挡住重复


def _round_element(st, el, radius, outside, faces, chevron=False):
    """建一个九宫格图元素, 返回元素名 (可反复调用, 同名只建一次)。

    faces   —— {状态: (填充色, 边框色)}, 必须有一个 "" 作默认图
    chevron —— 是否在右端画一个下拉箭头 (下拉框用); 会同时把右边切片加宽
    """
    if el in _STYLED:
        return el
    R = radius + 2                     # 九宫格的角盒边长
    CH = px(15) if chevron else 0      # 右端留给箭头的宽度
    W, H = 2 * R + 8 + CH, 2 * R + 8   # 中间 8px 会被拉伸
    out = _rgb(outside)
    args = []
    for state, (fill, edge) in faces.items():
        ch = None
        if chevron:
            # 箭头画在**右切片**里 —— 那一列不会被拉伸, 所以箭头尺寸固定不变形
            ch = (W - R - CH / 2.0, H / 2.0, px(4), px(2), max(1.0, px(1.4)),
                  _rgb(C_TEXT))
        im = tk.PhotoImage(data=_rr_ppm(W, H, R, _rgb(fill), _rgb(edge), out,
                                        chevron=ch))
        _ROUND_IMGS.append(im)
        args.append(im if state == "" else (state, im))
    # border 支持四元组 ⇒ 右边切片能比左边宽, 箭头因此有地方待
    st.element_create(el, "image", *args,
                      border=(R, R, R + CH, R) if chevron else R, sticky="nswe")
    _STYLED.add(el)
    return el


def install_round_button(st, style, radius, outside, faces, pad, fg, font=None):
    """把一个 ttk 按钮样式换成圆角外观。

    style   —— 样式名, 如 "TButton" / "Chrome.TButton"
    outside —— 按钮所在**容器**的底色 ('#RRGGBB'), 会被烤进四角
    """
    el = _round_element(st, "Rnd" + style.replace(".", "_"), radius, outside, faces)
    st.layout(style, [
        (el, {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [
                ("Button.label", {"sticky": "nswe"}),
            ]}),
        ]}),
    ])
    st.configure(style, padding=pad, foreground=fg, borderwidth=0,
                 relief="flat", font=font or FONT_UI)


def install_round_field(st, style, radius, outside, faces, text_el,
                        tail=(), chevron=False):
    """把输入框的 field 元素换成圆角 —— TEntry 与 TCombobox 共用。

    text_el —— "Entry.textarea" 或 "Combobox.textarea"
    tail    —— 放在 field **之后**的元素 (绘制在 field 之上)。
    ⚠ TCombobox 的 `Combobox.downarrow` **必须**在这里带上 —— 这个函数是替换
      **整个 layout**, 漏掉谁谁就消失 (实测: 漏了 downarrow, 下拉箭头直接没了,
      输入框看着像个普通文本框)。
    """
    el = _round_element(st, "Rnd" + style.replace(".", "_") + "Field",
                        radius, outside, faces, chevron=chevron)
    # ★ 顺序要紧: 带 -side 的元素必须排在**前**, 无 -side 的那个才会去填"剩下的"空间。
    #   反过来的话无 -side 的元素会吃掉整块, 后面的分不到尺寸 —— 实测下拉箭头就是这样
    #   消失的 (元素还在 layout 里, 但宽度为 0, 什么也画不出来)。
    layout = list(tail)
    layout.append(
        (el, {"sticky": "nswe", "children": [
            (text_el.replace(".textarea", ".padding"),
             {"sticky": "nswe", "children": [(text_el, {"sticky": "nswe"})]}),
        ]}),
    )
    st.layout(style, layout)


# ---- 关于 / about ----
# 改版本号 / 作者就改这里。
APP_VERSION = "1.0"
APP_AUTHOR = "bobyuhit"


def build_stamp():
    """这份程序是什么时候编的。

    工具改得勤, 光有版本号不如有个**日期**实在 —— "我手上这份是不是最新的"
    一眼就看出来了。取的是 exe (打包后) 或 btool.py (源码跑) 的修改时间。
    """
    try:
        p = sys.executable if getattr(sys, "frozen", False) else __file__
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    except Exception:
        return ""
DEFAULT_BAUD = 115200
REPL_BAUD = 115200

# 按文件名猜烧写地址 (bPuppy / ESP-IDF 常见布局)
# Flash address guessed from the filename (common bPuppy / ESP-IDF layout)
ADDR_GUESS = (
    ("bootloader", 0x0),
    ("partition-table", 0x8000),
    ("partition_table", 0x8000),
    ("boot_app0", 0xE000),
)


# ======================================================================
# 语言 / Language
# ======================================================================

LANG = {
    "zh": {
        "lang_name": "中文",
        "title": "bTool — ESP32 图形化工具",

        # 顶部
        "connection": "连接",
        "port": "串口:",
        "refresh": "刷新",
        "chip": "芯片:",
        "language": "语言",      # 原先是工具栏标签 "语言:" (后面跟下拉框); 那个下拉已删, 现在只给菜单用
        "connect": "连接",
        "disconnect": "断开",

        # 选项卡
        "tab_flash": "  烧写  ",
        "tab_repl": "  REPL 终端  ",
        "tab_files": "  文件管理  ",

        # 烧写页
        "add_fw": "添加固件…",
        "remove_sel": "移除选中",
        "clear": "清空",
        "flash_baud": "烧写波特率:",
        "col_addr": "地址",
        "col_fw": "固件文件",
        "col_size": "大小",
        "erase_warn": ("⚠ 「擦除」会清空整片 Flash —— 包括 NVS 里保存的标定 / 配置数据。\n"
                       "   只想更新程序时, 取消勾选下面的「先擦除」, 直接烧写即可。"),
        "erase_first": "先擦除整片 Flash",
        "erase_and_flash": "擦除并烧写",
        "flash_log": "烧写日志:",
        "idle": "等待操作",

        # 文件页
        "local_files": "本地文件",
        "device_vfs": "设备 VFS",
        "col_name": "名称",
        "upload": "上传 →",
        "download": "← 下载",
        "rename": "重命名",
        "delete": "删除",

        # REPL
        "repl_baud": "REPL 波特率:",
        "repl_baud_hint": "(连接设备用; 改完要重新连接才生效)",
        "term_hint": "直接在上面输入 (回车发送, 回显来自板子)",
        "to_repl": "切回 REPL",
        "menu_copy": "复制",
        "menu_paste": "粘贴",
        "menu_select_all": "全选",
        "board_info": "[板子] {text}",
        "read_chip": "读芯片信息",
        "about": "关于",
        "cd_chip_type": "芯片型号:",
        "cd_features": "特性:",
        "cd_crystal": "晶振频率:",
        "cd_usb": "USB 模式:",
        "cd_mac": "MAC 地址:",
        "about_build": "构建于 {stamp}",
        "about_body": [
            "bTool 是一个面向 ESP 系列芯片的图形化工具，将固件烧写、芯片信息读取、",
            "串口终端与文件传输集成于同一界面。",
            "",
            "#主要功能",
            "  固件烧写    多文件按地址烧写，地址可按文件名自动识别",
            "  芯片信息    经 ROM 下载模式读取型号、修订版、封装、MAC、晶振、",
            "              特性串及 Flash 分区表，不依赖芯片内已烧录的固件",
            "  串口终端    面向 MicroPython 的交互式终端，支持命令行编辑、",
            "              历史回溯与剪贴板操作",
            "  文件传输    主机与设备文件系统之间的双向传输",
            "",
            "#支持范围",
            "  ESP32 / ESP32-S2 / ESP32-S3 / ESP32-C2 / ESP32-C3 / ESP32-C5",
            "  ESP32-C6 / ESP32-C61 / ESP32-H2 / ESP32-H21 / ESP32-H4 / ESP32-P4",
            "  ESP32-E22 / ESP8266",
            "",
            "#运行环境",
            "  Windows 10 / 11（64 位）",
            "  串口驱动：CH343 / CH340 或 CP210x",
            "  串口终端与文件传输需设备端运行 MicroPython",
            "",
            "#使用须知",
            "  · 读取芯片信息会将设备复位 —— 与片内 ROM 通信必须进入下载模式",
            "  · 「先擦除整片 Flash」会一并清除 NVS 中的标定与配置数据",
            "  · 终端中 Ctrl+C 在有选中文本时为复制，无选中时为中断设备",
            "",
            "#文档",
            "  开发说明.md     架构与实现说明",
            "  用户手册.md      使用说明",
            "",
            "© 2026 OSL, Lingnan University",
        ],
        "vfs_no_mpy": "板子上没有 MicroPython —— 文件管理不可用 (这头没有可浏览的文件系统)",
        "vfs_need_conn": "先连接板子才能管理文件",
        "read_chip_ask": "读芯片信息需要把芯片复位进下载模式, 板子上跑的程序会重启。继续吗?",

        "part_info": "[分区] {text}",
        "chip_unknown": "未识别",
        "chip_hint": "[提示] 想看芯片信息, 点「烧写」页的「读芯片信息」按钮 (会把板子复位一下)",

        # 状态 / 提示
        "ready": "就绪",
        "ports_found": "检测到 {n} 个串口",
        "no_ports": "没有可用串口",
        "connecting": "正在连接 {port} ...",
        "connected": "—— 已连接 {port} @{baud} ——",
        "connected_status": "已连接 {port} (REPL)",
        "link_up": "已连接",
        "not_connected": "未连接",
        "linked": "已连接 {port} @{baud}",
        "flash_progress": "烧写进度:",
        "no_prompt": "(没看到 >>> 提示符; 若板子在跑程序请按 Ctrl+C)",
        "disconnected": "已断开",
        "please_connect": "请先连接设备",
        "failed": "失败: {msg}",
        "ui_error": "[UI错误] ",

        "select_local_first": "先在左边选文件",
        "select_dev_first": "先在右边选文件",
        "select_one": "请选中恰好一个文件",
        "confirm_delete": "确认删除设备上的这 {n} 个文件?\n{names}",

        "pick_fw_title": "选择固件 (.bin)",
        "ft_firmware": "固件",
        "ft_all": "所有文件",
        "addr_title": "修改烧写地址",
        "addr_label": "地址 (十六进制, 如 0x10000):",
        "addr_bad": "地址格式不对: {txt}",
        "rename_title": "重命名",
        "new_name": "新名称:",
        "ok": "确定",

        "no_esptool": "缺少 esptool:  pip install esptool",
        "no_fw": "先添加固件文件",
        "file_missing": "文件不存在: {path}",

        "reading_dir": "读取设备目录 {path} ...",
        "dir_items": "设备目录 {path}: {n} 项",
        "uploading": "上传 {name} ({size} 字节)...",
        "uploading_pct": "上传 {name}: {pct:.0f}%",
        "upload_done": "上传完成",
        "downloading": "下载 {name} ...",
        "downloading_pct": "下载 {name}: {pct:.0f}%",
        "download_done": "下载完成",
        "delete_done": "删除完成",
        "local_dir_err": "[本地目录读取失败] {msg}",

        "log_upload_ok": "✓ 上传 {name} → {dst}",
        "log_download_ok": "✓ 下载 {src} → {dst} ({n} 字节)",
        "log_delete_ok": "✓ 删除 {name}",
        "log_delete_fail": "✗ 删除失败 {name}",
        "log_rename_ok": "✓ 重命名 {old} → {new}",
        "log_rename_fail": "✗ 重命名失败",
        "log_ctrl_c": "[已发送 Ctrl+C]",
        "log_to_repl": "[已切回普通 REPL]",

        # 烧写日志
        "fl_release": "[已释放串口供烧写使用]",
        "fl_connect": "连接芯片 @{baud} ...",
        "fl_chip": "芯片: {name}",
        "fl_stub": "已加载 stub flasher",
        "fl_baud_up": "已提速到 {baud} 波特率",
        "fl_baud_rom": "ROM 不支持提速, 保持 {baud} 波特率",
        "fl_failed": "✗ 烧写失败: {msg}",
        "fl_erasing": "擦除整片 Flash ...",
        "fl_erasing_pb": "擦除中…",
        "fl_erase_done": "擦除完成",
        "fl_writing": "烧写 {n} 个文件 ...",
        "fl_verify": "校验 ...",
        "fl_reset": "复位 ...",
        "fl_done": "✓ 烧写完成",
        "fl_done_pb": "烧写完成",
        "fl_reopen": "[串口已重新打开]",
        "fl_reopen_fail": "[串口重开失败: {msg}]",
        "progress_pct": "进度 {pct:.1f}%  ({cur} / {total})",
        # 菜单栏 / menu bar
        "menu_file":     "文件",
        "menu_tools":    "工具",
        "port_menu":     "串口",
        "menu_settings": "设置",
        "menu_help":     "帮助",
        "quit":          "退出",
        "refresh_ports": "刷新串口",
        "clear_fw_list": "清空固件列表",
        "flash_start":   "开始烧写",
        "user_manual":   "用户手册",
        "dev_notes":     "开发说明",
        "about_app":     "关于 bTool",

    },

    "en": {
        "lang_name": "English",
        "title": "bTool — ESP32 Tool",

        "connection": "Connection",
        "port": "Port:",
        "refresh": "Refresh",
        "chip": "Chip:",
        "language": "Language",
        "connect": "Connect",
        "disconnect": "Disconnect",

        "tab_flash": "  Flash  ",
        "tab_repl": "  REPL  ",
        "tab_files": "  Files  ",

        "add_fw": "Add firmware…",
        "remove_sel": "Remove",
        "clear": "Clear",
        "flash_baud": "Flash baud:",
        "col_addr": "Address",
        "col_fw": "Firmware file",
        "col_size": "Size",
        "erase_warn": ("⚠ Erase wipes the ENTIRE flash — including any calibration / "
                       "configuration data stored in NVS.\n"
                       "   To just update the program, leave \"Erase first\" "
                       "unchecked and flash directly."),
        "erase_first": "Erase whole flash first",
        "erase_and_flash": "Erase & Flash",
        "flash_log": "Flash log:",
        "idle": "Idle",

        "local_files": "Local files",
        "device_vfs": "Device VFS",
        "col_name": "Name",
        "upload": "Upload →",
        "download": "← Download",
        "rename": "Rename",
        "delete": "Delete",

        "repl_baud": "REPL baud:",
        "repl_baud_hint": "(used for connecting; reconnect to apply)",
        "term_hint": "Type directly above (Enter sends; echo comes from the board)",
        "to_repl": "Back to REPL",
        "menu_copy": "Copy",
        "menu_paste": "Paste",
        "menu_select_all": "Select All",
        "board_info": "[board] {text}",
        "read_chip": "Read chip info",
        "about": "About",
        "cd_chip_type": "Chip type:",
        "cd_features": "Features:",
        "cd_crystal": "Crystal frequency:",
        "cd_usb": "USB mode:",
        "cd_mac": "MAC:",
        "about_build": "built {stamp}",
        "about_body": [
            "bTool is a graphical tool for the ESP family, bringing firmware flashing,",
            "chip information, a serial terminal and file transfer into one window.",
            "",
            "#Features",
            "  Flashing      Write one or more images by address; addresses can be",
            "                inferred from filenames",
            "  Chip info     Model, revision, package, MAC, crystal, feature string",
            "                and the Flash partition table, read via the ROM download",
            "                mode - independent of the firmware already on the chip",
            "  Terminal      Interactive terminal for MicroPython, with line editing,",
            "                history and clipboard support",
            "  File transfer Two-way transfer between host and device filesystems",
            "",
            "#Supported chips",
            "  ESP32 / ESP32-S2 / ESP32-S3 / ESP32-C2 / ESP32-C3 / ESP32-C5",
            "  ESP32-C6 / ESP32-C61 / ESP32-H2 / ESP32-H21 / ESP32-H4 / ESP32-P4",
            "  ESP32-E22 / ESP8266",
            "",
            "#Requirements",
            "  Windows 10 / 11 (64-bit)",
            "  Serial driver: CH343 / CH340 or CP210x",
            "  Terminal and file transfer require MicroPython on the device",
            "",
            "#Notes",
            "  - Reading chip info resets the device; the on-chip ROM can only be",
            "    reached in download mode",
            "  - 'Erase entire flash first' also wipes calibration and configuration",
            "    data held in NVS",
            "  - In the terminal, Ctrl+C copies when text is selected and interrupts",
            "    the device otherwise",
            "",
            "#Documentation",
            "  开发说明.md     Architecture and implementation notes",
            "  用户手册.md      User guide (Chinese)",
            "",
            "© 2026 OSL, Lingnan University",
        ],
        "vfs_no_mpy": "No MicroPython on the board - file management unavailable",
        "vfs_need_conn": "Connect to the board first",
        "read_chip_ask": "Reading chip info resets the chip into download mode; anything running will restart. Continue?",

        "part_info": "[parts] {text}",
        "chip_unknown": "unknown",
        "chip_hint": "[hint] To read chip info, click \"Read chip info\" on the Flash tab (it resets the board)",

        "ready": "Ready",
        "ports_found": "{n} port(s) found",
        "no_ports": "No serial port available",
        "connecting": "Connecting to {port} ...",
        "connected": "—— Connected {port} @{baud} ——",
        "connected_status": "Connected {port} (REPL)",
        "link_up": "Connected",
        "not_connected": "Not connected",
        "linked": "Connected {port} @{baud}",
        "flash_progress": "Flash progress:",
        "no_prompt": "(no >>> prompt seen; press Ctrl+C if the board is running a program)",
        "disconnected": "Disconnected",
        "please_connect": "Please connect to a device first",
        "failed": "Failed: {msg}",
        "ui_error": "[UI error] ",

        "select_local_first": "Select a file on the left first",
        "select_dev_first": "Select a file on the right first",
        "select_one": "Select exactly one file",
        "confirm_delete": "Delete these {n} file(s) from the device?\n{names}",

        "pick_fw_title": "Select firmware (.bin)",
        "ft_firmware": "Firmware",
        "ft_all": "All files",
        "addr_title": "Edit flash address",
        "addr_label": "Address (hex, e.g. 0x10000):",
        "addr_bad": "Bad address format: {txt}",
        "rename_title": "Rename",
        "new_name": "New name:",
        "ok": "OK",

        "no_esptool": "esptool missing:  pip install esptool",
        "no_fw": "Add a firmware file first",
        "file_missing": "File not found: {path}",

        "reading_dir": "Reading device directory {path} ...",
        "dir_items": "Device directory {path}: {n} item(s)",
        "uploading": "Uploading {name} ({size} bytes)...",
        "uploading_pct": "Upload {name}: {pct:.0f}%",
        "upload_done": "Upload complete",
        "downloading": "Downloading {name} ...",
        "downloading_pct": "Download {name}: {pct:.0f}%",
        "download_done": "Download complete",
        "delete_done": "Delete complete",
        "local_dir_err": "[cannot read local directory] {msg}",

        "log_upload_ok": "✓ Uploaded {name} → {dst}",
        "log_download_ok": "✓ Downloaded {src} → {dst} ({n} bytes)",
        "log_delete_ok": "✓ Deleted {name}",
        "log_delete_fail": "✗ Delete failed {name}",
        "log_rename_ok": "✓ Renamed {old} → {new}",
        "log_rename_fail": "✗ Rename failed",
        "log_ctrl_c": "[Ctrl+C sent]",
        "log_to_repl": "[back to normal REPL]",

        "fl_release": "[serial port released for flashing]",
        "fl_connect": "Connecting @{baud} ...",
        "fl_chip": "Chip: {name}",
        "fl_stub": "Stub flasher loaded",
        "fl_baud_up": "Raised baud to {baud}",
        "fl_baud_rom": "ROM cannot change baud, keeping {baud}",
        "fl_failed": "✗ Flash failed: {msg}",
        "fl_erasing": "Erasing entire flash ...",
        "fl_erasing_pb": "Erasing…",
        "fl_erase_done": "Erase done",
        "fl_writing": "Writing {n} file(s) ...",
        "fl_verify": "Verifying ...",
        "fl_reset": "Resetting ...",
        "fl_done": "✓ Flash complete",
        "fl_done_pb": "Flash complete",
        "fl_reopen": "[serial port reopened]",
        "fl_reopen_fail": "[failed to reopen port: {msg}]",
        "progress_pct": "Progress {pct:.1f}%  ({cur} / {total})",
        # menu bar
        "menu_file":     "File",
        "menu_tools":    "Tools",
        "port_menu":     "Port",
        "menu_settings": "Settings",
        "menu_help":     "Help",
        "quit":          "Quit",
        "refresh_ports": "Refresh Ports",
        "clear_fw_list": "Clear Firmware List",
        "flash_start":   "Flash",
        "user_manual":   "User Manual",
        "dev_notes":     "Developer Notes",
        "about_app":     "About bTool",

    },
}

_LANG_ID = "zh"


def tr(key, **kw):
    """取当前语言下的字符串; 缺键回退中文, 再缺就原样返回 key。"""
    s = LANG.get(_LANG_ID, LANG["zh"]).get(key)
    if s is None:
        s = LANG["zh"].get(key, key)
    if kw:
        try:
            return s.format(**kw)
        except Exception:
            return s
    return s


def set_lang(lang_id):
    global _LANG_ID
    if lang_id in LANG:
        _LANG_ID = lang_id


# ======================================================================
# 配置持久化 / persistent settings
# ======================================================================
# 存用户主目录下的 .btool.json —— 脚本和打包后的 exe 都能用, 也不涉及写权限问题。
# (不放在 exe 旁边: 打包后可能在只读目录里跑)

# ======================================================================
# 从 ROM bootloader 读硬件信息 / chip info from the ROM bootloader
# ======================================================================
# ★ 这条路的能力上限是**芯片**, 不是固件 —— ROM 是出厂烧在硅片里的, 刷什么程序
#   都盖不掉。所以裸机板、MicroPython、出厂空白, 一样读得出来。
#
# 代价: 要跟 ROM 说话, 芯片**必须复位进下载模式**。所以只有两个时机能做:
#       · 烧写时 (反正本来就要进下载模式)
#       · 用户**主动点**「读芯片信息」
#   绝不能在"连接"时偷偷做 —— 那等于每次连板子都把上面的程序重启一遍。

def pad_label(label, width=20):
    """把标签补到指定**显示宽度**, 好让右边那一列对齐。

    ⚠ 不能直接用 `%-20s`: 那个按**字符数**补, 而一个中文字显示占 **2 格** ——
    中文标签(如"芯片型号:")照字符数补完, 实际比英文版宽, 右边的值就错开了。
    """
    w = sum(2 if ord(ch) > 0x2E80 else 1 for ch in label)
    return label + " " * max(1, width - w)


def chip_detail_lines(esp):
    """把 ROM 里读到的硬件信息整理成若干行。

    每个字段单独 try: 不同芯片/ROM 能给的字段不一样, 拿不到就跳过 ——
    一个字段失败不能把整块信息搞没。
    """
    if getattr(esp, "secure_download_mode", False):
        return [pad_label(tr("cd_chip_type")) + esp.CHIP_NAME +
                " (Secure Download Mode)"]

    lines = []

    def add(label, fn):
        try:
            v = fn()
        except Exception:
            return
        if v:
            lines.append(pad_label(label) + str(v))

    add(tr("cd_chip_type"), esp.get_chip_description)
    add(tr("cd_features"), lambda: ", ".join(esp.get_chip_features()))
    add(tr("cd_crystal"), lambda: "%dMHz" % esp.get_crystal_freq())
    add(tr("cd_usb"), esp.get_usb_mode)
    add(tr("cd_mac"),
        lambda: ":".join("%02x" % b for b in esp.read_mac("BASE_MAC")))
    return lines


def read_partitions_rom(esp):
    """从 flash 0x8000 读分区表, 返回 [(label, size), ...]。

    32 字节一项 / 32 bytes per entry:
        magic(2)=0x50AA  type(1) subtype(1) offset(4) size(4) label(16) flags(4)
    读到 magic 对不上为止 (表尾 0xFF 填充)。

    ⚠ 没上 stub 时 esptool 走的是 `read_flash_slow`, 3KB 要好几秒。
      在意速度的话调用方先 `run_stub()`。
    """
    raw = esp.read_flash(0x8000, 0xC00)
    parts = []
    for i in range(0, len(raw) - 31, 32):
        e = raw[i:i + 32]
        if e[0] != 0xAA or e[1] != 0x50:
            break
        size = int.from_bytes(e[8:12], "little")
        # bytes(1) = 单个 0 字节, 写成 bytes(1) 是为了避开转义
        label = bytes(e[12:28]).split(bytes(1))[0].decode("utf-8", "replace")
        if label:
            parts.append((label, size))
    return parts


# ---- 窗口图标 / window icon ----
# 内嵌成 base64, 不读外部文件 —— 打包成单文件 exe 后, 外面那个 bTool.ico
# 不一定会跟着走; 而且 exe 里的图标资源只有资源管理器能用, Tk 要自己再设一次
# (标题栏 / 任务栏)。内嵌的话 btool.py 仍然是**自包含的单个文件**。
#
# 图是画出来的(microchip): 深色圆角底 + 白色芯片轮廓 + 引脚 + 中心绿点。
# 重新生成见 README 的"应用图标"一节。
ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAADA0lEQVR42u1bzU8TQRSf1/QooCJE"
    "LwpqBKHGmJIWrP0gsTQmREIEv07+aZ6kFQyJkmwFdaW4FogmTURLjN8HNKJV0Pt4MF0tZbsz3Znp"
    "dHdeM4d23s68369v3nztA0Qpp2LXMZJYXuRuAY0+kXIgek1q0FaytjQFjggIRK82JfBqItJATUD/"
    "uSuuAF+Wl08yQExAf+Syq8CbJBi3wZaAvsikK8GX5ZUxDZYE9J2dcDV4k4SnM1BFwMmhS54AX5Zi"
    "/g4ghJDf/AV7Cn/lEOgdHPck+vXlWfB7+d83hwAP/At3b1Z8T168Qa1D0oZjAnpCYxghER6AGeiw"
    "tbMnNIb9WJD7k/Rjp8PDVr+w8U/Sj50ODwIwEuQBBP3Y6fCwVXmAigEqBogh4JE25ViHh60+hDFi"
    "XXQtzYVEXUszt9WHMUYsi57NcPUkPZthai90n04S+9Xi/emmWN/HU5NyBMH4yASzthbnZ9gGXBHT"
    "IG7QLpOmXz/fjVCjttmcPCB6frxm/dKDWWEeYGeLHAuhJmi7ITEgMDds+czaqC40vvj+jhdWZbex"
    "WFlqgf9HTn1t11OknAVYbJy4xADj8ZyYcUrwnKHfs6yLJEZpNkPyTYJYYL9yzgJObWr2laBTm7it"
    "BIdiF2rW53OasEFgZwvxNMhya7nbP7GzFFLZmgYVUtm6266nNCQGFEY0b68EZWqbKgasGAvS7AZX"
    "jHnLunAkqc4D3L8bZLYO4Oimq/mHDToOIccEnV1nmDLwbFnnDnBgcBhJey8wEE7wBR9OML4XQOw/"
    "wXCcC/hgOM7cVmF3g8FQrOq356u5mjo769XtsLodVm+IKA9QMYCx+EobRWB7NE5zjG2nw+covFxK"
    "G0UAhBDad6jXk+/K/vi8rt4VNvMF9h484SkWfn55rfIFqlJm2jqPe4KFra9vwDJpqq3jmKtJ2Np8"
    "C7Zpc60dR11JwvbmOyBOnGw90O0qEra/vQfq1NmW9i5XkPDr+wdwlDzd0n4ENyfwj8Ake/x/2bP/"
    "sNRk/C59osL0B6iZ4Kv8T7j9AAAAAElFTkSuQmCC"
)


def set_window_icon(win):
    """给窗口设图标 (标题栏 / 任务栏)。失败就当没这回事, 不该因此起不来。"""
    try:
        # 必须留引用: PhotoImage 被 GC 掉的话图标会变成空白
        win._icon_img = tk.PhotoImage(data=ICON_PNG_B64)
        win.iconphoto(True, win._icon_img)
    except Exception:
        pass


CFG_PATH = os.path.join(os.path.expanduser("~"), ".btool.json")


def cfg_load():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def cfg_save(cfg):
    """写失败就算了 (没权限/磁盘满), 不该因为存个偏好把程序搞崩"""
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ======================================================================
# 按文件名猜烧写地址 / guess flash address from filename
# ======================================================================

def guess_addr(path):
    """猜不出就给 app 区默认值 0x10000 / fall back to 0x10000"""
    name = os.path.basename(path).lower()
    for key, addr in ADDR_GUESS:
        if key in name:
            return addr
    return 0x10000


# ======================================================================
# esptool 进度钩子 / progress hook
# ======================================================================
# esptool 5.x 把日志/进度条收在一个单例 logger 里, 默认直接往 stdout 打。
# 子类化 EsptoolLogger 覆写 print/progress_bar, 把进度送回 Tkinter。
# esptool 5.x funnels logs through a singleton logger; subclass to redirect
# output and progress into the Tkinter UI.

class BridgeLogger(EsptoolLogger if EsptoolLogger else object):
    """把 esptool 的输出和进度转发给回调 (由 UI 层注入)。"""

    sink = None          # callable(text: str)
    progress = None      # callable(cur: int, total: int)

    def __new__(cls, *a, **kw):
        """⚠ 必须绕开 EsptoolLogger.__new__ 的单例逻辑。

        EsptoolLogger.__new__ 是单例: 它返回 cls.instance (= 那个唯一的 logger
        实例本身)。于是 BridgeLogger() 拿到的**还是 EsptoolLogger 实例**,
        再交给 set_logger() 就变成 self.__class__ = 自己, 静默空操作 ——
        set_logger 报"成功", 但 esptool 的日志和进度一个都收不到
        (症状: 烧写进度条不走, 烧写日志空白)。
        """
        return object.__new__(cls)

    def print(self, *args, **kwargs):
        try:
            text = " ".join(str(a) for a in args)
        except Exception:
            return
        if BridgeLogger.sink:
            BridgeLogger.sink(text.rstrip())

    def note(self, message):
        if BridgeLogger.sink:
            BridgeLogger.sink("· " + str(message))

    def warning(self, message):
        if BridgeLogger.sink:
            BridgeLogger.sink("⚠ " + str(message))

    def error(self, message):
        if BridgeLogger.sink:
            BridgeLogger.sink("✗ " + str(message))

    def stage(self, message=None, *a, **kw):
        if BridgeLogger.sink and message:
            BridgeLogger.sink("▶ " + str(message))

    def progress_bar(self, cur_iter, total_iters, prefix="", suffix="", bar_length=30):
        if BridgeLogger.progress and total_iters:
            BridgeLogger.progress(cur_iter, total_iters)


# ======================================================================
# 串口管理 —— 同一时刻只允许一个功能占用
# Serial manager — only one consumer may own the port at a time
# ======================================================================
# 三种用途 / three uses:
#   'repl'  普通 REPL   (交互输入)     normal REPL, interactive
#   'raw'   raw REPL    (文件操作)     raw REPL, programmatic file ops
#   'flash' esptool 烧写               esptool flashing
#
# repl 和 raw 是**同一条串口的两种模式** (Ctrl-A / Ctrl-B 切换), 不用重开;
# 只有烧写要真正独占 —— esptool 自己开串口, 所以烧写前必须先关掉我们的。
# repl and raw are the SAME connection in two modes (Ctrl-A / Ctrl-B), no
# reopen needed.  Only flashing needs exclusive access, so we close first.

class SerialError(Exception):
    pass


class SerialManager:
    def __init__(self):
        self._ser = None
        self._port = None
        self._baud = DEFAULT_BAUD
        self._mode = "none"
        self.lock = threading.RLock()
        # 终端读线程与"请求/响应"类操作的互斥:
        # 终端要**持续**读串口才有实时回显, 但 raw_exec / read_until 这类
        # 一问一答的操作必须独占读取 —— 否则读线程会把响应抢走, 那边就超时。
        self._excl_depth = 0
        self._excl_lock = threading.Lock()      # 只护 _excl_depth (给读线程看)
        # ★ 真正干"互斥"这件事的锁, 必须是**可重入**的:
        #   同一个线程可重入 (raw_exec 内部还会调 enter_raw → read_until),
        #   不同线程互斥。**光靠 _excl_depth 计数是不行的** —— 那只挡得住终端
        #   读线程, 挡不住第二个工作线程: 两边同时发 raw REPL 命令, 串口上就
        #   搅成一团, 谁也别想拿到正确应答。
        self._excl_serial = threading.RLock()

    @property
    def busy_io(self):
        """有请求/响应操作正在进行 → 终端读线程此刻不要读"""
        with self._excl_lock:
            return self._excl_depth > 0

    @contextmanager
    def exclusive(self):
        """独占读取区间。

        **两层含义, 缺一不可**:
        - `_excl_depth > 0` → 终端读线程让路 (`busy_io`)
        - `_excl_serial` (RLock) → **别的线程进不来**

        只做第一层是不够的 —— 深度计数挡不住第二个工作线程, 两边会同时在
        串口上发 raw REPL 命令。RLock 同时满足"同线程可重入"和"跨线程互斥"。
        """
        with self._excl_serial:
            with self._excl_lock:
                self._excl_depth += 1
            try:
                yield
            finally:
                with self._excl_lock:
                    self._excl_depth -= 1

    # ---- 生命周期 / lifecycle ----
    @property
    def is_open(self):
        return self._ser is not None and self._ser.is_open

    @property
    def mode(self):
        return self._mode

    def open(self, port, baud=DEFAULT_BAUD):
        with self.lock:
            self.close()
            try:
                # 打开时**不要**动 DTR/RTS: 它们是复位/BOOT 控制脚,
                # 一动板子就重启, 会把正在跑的程序打断。
                # Do NOT touch DTR/RTS: they drive reset/boot and would
                # reboot the board, killing whatever is running.
                self._ser = serial.Serial(port, baud, timeout=0.2,
                                          write_timeout=3.0)
            except Exception as e:
                raise SerialError(str(e))
            self._port = port
            self._baud = baud
            self._mode = "repl"
            time.sleep(0.15)
            self._ser.reset_input_buffer()
            return self._ser

    def close(self):
        with self.lock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None
            self._port = None
            self._mode = "none"

    # ---- 读写 / I/O ----
    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        with self.lock:
            if not self.is_open:
                raise SerialError("port not open / 串口未连接")
            self._ser.write(data)
            self._ser.flush()

    def read_avail(self):
        with self.lock:
            if not self.is_open:
                return b""
            n = self._ser.in_waiting
            return self._ser.read(n) if n else b""

    def read_until(self, want, timeout=3.0, echo_sink=None):
        """读到出现 want (bytes) 为止。"""
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.read_avail()
            if chunk:
                buf += chunk
                if echo_sink:
                    echo_sink(chunk)
                if want in buf:
                    return buf
            else:
                time.sleep(0.01)
        return buf

    # ---- 模式切换 / mode switching ----
    def interrupt(self, timeout=2.0):
        """Ctrl-C 中断正在运行的程序 / interrupt a running program

        ★ 必须和其它串口操作互斥: `\\x03` 在 **raw REPL 里的含义是"清空当前行"**,
          发在别人的传输中途会把正在传的数据打断。
        """
        # 带超时是为了**不冻住 UI 线程** (Ctrl+C 按钮在 UI 线程上)
        if not self._excl_serial.acquire(timeout=timeout):
            return False
        try:
            self.write(b"\x03\x03")
            return True
        finally:
            self._excl_serial.release()

    def enter_raw(self, tries=5):
        """进 raw REPL。bPuppy 的 voice 模块 UART 回显会插进握手里, 需重试。
        The voice module's UART echo can corrupt the handshake — retry."""
        with self.exclusive():
            for _ in range(tries):
                self.write(b"\x03")        # 先打断, 确保不在跑程序
                time.sleep(0.15)
                self.write(b"\x01")        # Ctrl-A
                got = self.read_until(b"raw REPL", timeout=1.5)
                if b"raw REPL" in got:
                    self._mode = "raw"
                    time.sleep(0.1)
                    self.read_avail()
                    return True
        raise SerialError("cannot enter raw REPL / 进 raw REPL 失败 "
                          "(no response — check port/power)")

    def enter_repl(self, timeout=2.0):
        """回普通 REPL / back to normal REPL

        ★ 必须和其它串口操作互斥: 这是**改变板子模式**的动作。别人正跑 raw_exec
          时发这个 `\\x02`, 板子会中途退出 raw REPL 并打出**开机横幅**,
          那边收到的就不是 `OK...` 而是横幅 →
          `bad raw REPL response: b'\\r\\nMicroPython v3.0-...'`。
          (这个错真出现过: 连接线程探测完切回 REPL, 撞上刷新线程在列目录。)

        带超时是为了**不冻住 UI 线程** —— 这个函数会被 UI 线程调
        (run_bg 收尾的 `back_to_repl`)。拿不到锁就先不切, 稍后还有机会:
        真正在跑的那个操作收尾时也会 post 一次 `back_to_repl`。
        """
        if not self._excl_serial.acquire(timeout=timeout):
            return False                   # 别人在用串口, 让他的操作先跑完
        try:
            self.write(b"\x02")            # Ctrl-B
            time.sleep(0.15)
            self._mode = "repl"
            return True
        finally:
            self._excl_serial.release()

    def raw_exec(self, code, timeout=8.0):
        """在 raw REPL 里执行一段代码, 返回 (stdout, stderr)。

        raw REPL 的返回格式 / response format: OK<stdout>\\x04<stderr>\\x04>
        """
        with self.exclusive():             # 别让终端读线程抢走响应
            if self._mode != "raw":
                self.enter_raw()
            self.read_avail()              # 丢掉残留 / drop leftovers
            self.write(code.encode("utf-8") + b"\x04")

            buf = b""
            deadline = time.time() + timeout
            while time.time() < deadline:
                chunk = self.read_avail()
                if chunk:
                    buf += chunk
                    if buf.rstrip().endswith(b"\x04>"):
                        break
                else:
                    time.sleep(0.01)

        if not buf:
            raise SerialError("raw REPL timeout (%.0fs)" % timeout)
        # 去掉开头的回显/杂讯, 从第一个 OK 开始
        i = buf.find(b"OK")
        if i < 0:
            raise SerialError("bad raw REPL response: %r" % buf[:120])
        body = buf[i + 2:]
        end = body.rfind(b"\x04>")
        if end >= 0:
            body = body[:end]
        parts = body.split(b"\x04")
        out = parts[0] if len(parts) > 0 else b""
        err = parts[1] if len(parts) > 1 else b""
        return (out.decode("utf-8", "replace"),
                err.strip().decode("utf-8", "replace"))


# ======================================================================
# 设备文件操作 (基于 raw REPL) / device file ops over raw REPL
# ======================================================================

def dev_list(sm, path="/"):
    """列出设备目录 → [(名字, 大小或 None)]"""
    code = (
        "import os\n"
        "try:\n"
        "    ns = os.listdir(%r)\n"
        "except OSError as e:\n"
        "    print('ERR', e); ns = []\n"
        "for n in sorted(ns):\n"
        "    p = %r + ('/' if not %r.endswith('/') else '') + n\n"
        "    try:\n"
        "        st = os.stat(p)\n"
        "        print('%%s\\t%%d' %% (n, st[6]))\n"
        "    except OSError:\n"
        "        print('%%s\\t-1' %% n)\n"
    ) % (path, path, path)
    out, err = sm.raw_exec(code)
    if err and "ERR" in out:
        raise SerialError(out.strip())
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, size = line.rpartition("\t")
        if not name:
            continue
        try:
            sz = int(size)
        except ValueError:
            sz = -1
        items.append((name, sz if sz >= 0 else None))
    return items


def dev_stat(sm, path):
    out, err = sm.raw_exec(
        "import os\ntry:\n    st=os.stat(%r)\n    print(st[6])\n"
        "except OSError as e:\n    print(-1)\n" % path)
    try:
        return int(out.strip().splitlines()[-1])
    except Exception:
        return -1


def dev_read(sm, path, chunk=768, on_progress=None):
    """读设备文件 → bytes。分块 hex 传输, 避免超出 raw REPL 缓冲。"""
    size = dev_stat(sm, path)
    if size < 0:
        raise SerialError("cannot read %s (missing?)" % path)
    parts = []
    off = 0
    while off < size:
        n = min(chunk, size - off)
        code = ("import binascii\n"
                "f=open(%r,'rb'); f.seek(%d)\n"
                "print(binascii.hexlify(f.read(%d)).decode())\n"
                "f.close()\n") % (path, off, n)
        out, err = sm.raw_exec(code, timeout=6.0)
        hexs = "".join(out.split())
        if not hexs:
            raise SerialError("empty read at %s offset %d" % (path, off))
        parts.append(bytes.fromhex(hexs))
        off += n
        if on_progress:
            on_progress(off, size)
    return b"".join(parts)


def dev_write(sm, path, data, chunk=768, on_progress=None):
    """写设备文件。base64 分块, 每块长度是 4 的倍数才能独立解码。"""
    b64 = base64.b64encode(data).decode()
    b64_chunk = chunk * 4 // 3
    b64_chunk -= b64_chunk % 4              # 必须是 4 的倍数
    sm.raw_exec("f=open(%r,'wb')\n" % path)
    try:
        total = len(b64)
        for i in range(0, total, b64_chunk):
            piece = b64[i:i + b64_chunk]
            code = ("import binascii\n"
                    "f.write(binascii.a2b_base64(%r))\n") % piece
            out, err = sm.raw_exec(code, timeout=6.0)
            if err:
                raise SerialError("write failed: %s" % err.splitlines()[-1])
            if on_progress:
                on_progress(min(i + b64_chunk, total), total)
    finally:
        sm.raw_exec("f.close()")
    return len(data)


def dev_remove(sm, path):
    out, err = sm.raw_exec(
        "import os\nos.remove(%r)\nprint('OK')\n" % path)
    return "OK" in out


def dev_rename(sm, old, new):
    out, err = sm.raw_exec(
        "import os\nos.rename(%r, %r)\nprint('OK')\n" % (old, new))
    return "OK" in out


def dev_mkdir(sm, path):
    out, err = sm.raw_exec(
        "import os\ntry:\n    os.mkdir(%r)\n    print('OK')\n"
        "except OSError as e:\n    print('E', e)\n" % path)
    return "OK" in out


# ======================================================================
# 主界面 / main window
# ======================================================================

class CircleButton(tk.Label):
    """圆形工具按钮 —— 照 Arduino 工具栏规格 (正圆 + 图标)。

    为什么不用 ttk.Button:
      · ttk 按钮的形状由主题 element 决定, **做不出正圆**;
      · 圆角那套九宫格图对正圆也不适用 —— 圆没有可拉伸的中间段, 九宫格会拉变形。
    所以用 tk.Label 贴一张**画好的整圆图**, 自己绑点击与悬停。

    代价: 不进 ttk 的焦点环, Tab 键跳不到。可接受 —— 这两个动作在「工具」菜单里都有,
    且带快捷键 (Ctrl+K), 键盘用户走菜单即可。

    图标是**画进图里**的线描图形 (箭头插进竖条 = 连接 / 向上弹出 = 断开),
    不用字体字符: U+25B6 这类符号在部分字体下会渲染成彩色 emoji, 不可控。
    """

    def __init__(self, master, shape, bg, command, size=None):
        self._n = size or px(28)
        self._cmd = command
        self._enabled = True
        super().__init__(master, bd=0, highlightthickness=0, bg=bg)
        self._imgs = {}
        # 边距 0.20 —— 实拍 Arduino 圆里的图形约占直径的一半 (它圆 28px, 图形约 20px),
        # 早先用 0.32 缩得太小, 圆看着空; 后来又放到 0.14, 配粗笔画显得要撑破圆。
        _pad = int(self._n * 0.20)
        for key, fill, fg in (("",       C_TOOL_BG,    C_TOOL_FG),
                              ("hover",  C_TOOL_HOVER, C_TOOL_FG),
                              ("dim",    C_TOOL_DIM,   C_TOPBAR)):
            im = tk.PhotoImage(data=_rr_ppm(self._n, self._n, self._n / 2.0,
                                            _rgb(fill), _rgb(fill), _rgb(bg),
                                            mark=(shape, fg, _pad)))
            _ROUND_IMGS.append(im)          # ★ 必须留引用, 否则 GC 后是黑块
            self._imgs[key] = im
        self.configure(image=self._imgs[""], cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda _e: self._paint("hover"))
        self.bind("<Leave>", lambda _e: self._paint(""))

    def _paint(self, key):
        self.configure(image=self._imgs["dim" if not self._enabled else key])

    def set_enabled(self, on):
        self._enabled = bool(on)
        self.configure(cursor="hand2" if self._enabled else "")
        self._paint("")

    def _click(self, _e):
        if self._enabled:
            self._cmd()


class _Tip(object):
    """极简 tooltip —— 竖栏里只有图标, 不给文字就不知道哪个是哪个。

    自己写而不是找现成库: 单文件 + 免安装 exe 是 bTool 的硬性质, 引第三方
    控件库会把这条破掉。这里只要"悬停出一个小黄条", 二十行够了。
    """

    def __init__(self, widget, text):
        self.w = widget
        self.text = text
        self.win = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Button-1>", self._hide, add="+")

    def _show(self, _e=None):
        if self.win is not None or not self.text:
            return
        try:
            self.w.update_idletasks()
            x = self.w.winfo_rootx() + self.w.winfo_width() + px(PAD_XS)
            y = self.w.winfo_rooty() + px(PAD_XS)
        except Exception:
            return
        try:
            self.win = tk.Toplevel(self.w)
            self.win.wm_overrideredirect(True)
            self.win.wm_geometry("+%d+%d" % (x, y))
            tk.Label(self.win, text=self.text, bg="#FFFFE1", fg=C_TEXT_HI,
                     font=FONT_SMALL, bd=1, relief="solid",
                     padx=px(PAD_S), pady=px(PAD_XS)).pack()
        except Exception:
            self.win = None

    def _hide(self, _e=None):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None


class RailItem(tk.Frame):
    """左侧竖栏里的一项 —— 图标 + 选中时左沿 2px 竖条 (Arduino 的 activityBar)。

    为什么自绘 (tk.Frame + tk.Label) 而不是 ttk.Button:
      · activityBar 的选中标记是**贴着容器左沿的那条竖条**, 不是"整块变色" ——
        ttk 按钮的样式挂在主题 element 上, 做不出这种溢出到控件外的标记;
      · 图标是画好的位图 (_icon_ppm), 不用字体字符 —— U+25B6 这类符号在部分
        字体下会被渲染成彩色 emoji, 不可控。

    代价: 不进 ttk 的焦点环, Tab 键跳不到。可接受 —— 切页在「工具」菜单里没有
    对应项, 但竖栏常驻可见, 鼠标一步就到。
    """

    def __init__(self, master, icon, tip, command, n=None):
        tk.Frame.__init__(self, master, bg=C_RAIL_BG)
        self._cmd = command
        self._active = False
        self._n = n or px(28)
        # 三档各一张图: 未选中 / 悬停 / 选中。色差不大, 但正是这三档把
        # "哪个是当前页"和"鼠标停在哪个上"分清楚了。
        self._imgs = {}
        for key, fg in (("dim", C_RAIL_DIM), ("hover", C_RAIL_FG), ("on", C_RAIL_FG)):
            im = tk.PhotoImage(data=_icon_ppm(self._n, icon, fg, C_RAIL_BG))
            _ROUND_IMGS.append(im)          # ★ 必须留引用, 否则 GC 后是黑块
            self._imgs[key] = im
        # 左沿 2px 竖条: 平时与栏底同色 —— **纯占位**, 不然选中时图标会左右横跳
        self.marker = tk.Frame(self, width=px(2), bg=C_RAIL_BG)
        self.marker.pack(side="left", fill="y")
        self.lbl = tk.Label(self, bg=C_RAIL_BG, bd=0, highlightthickness=0,
                            image=self._imgs["dim"])
        # 竖向 padding 撑出 Arduino 那种"一项占一格"的疏朗间距
        self.lbl.pack(side="left", fill="both", expand=True,
                      pady=px(PAD_S) + px(PAD_XS))
        for w in (self, self.lbl):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda _e: self._cmd())
            w.bind("<Enter>", lambda _e: self._hover(True))
            w.bind("<Leave>", lambda _e: self._hover(False))
        # tooltip 只绑在图标标签上 —— 绑到整项的话, 鼠标从边框移进图标会先给
        # frame 发 <Leave>, 提示条会闪一下
        _Tip(self.lbl, tip)

    def _hover(self, on):
        if self._active:                 # 选中项不参与悬停 (它已经够醒目)
            return
        self.lbl.configure(image=self._imgs["hover" if on else "dim"])

    def set_active(self, on):
        """选中 = 左沿竖条点亮 + 图标转深色。背景**不变** (实拍如此)。"""
        self._active = bool(on)
        self.marker.configure(bg=C_RAIL_MARK if self._active else C_RAIL_BG)
        self.lbl.configure(image=self._imgs["on" if self._active else "dim"])


class WorkArea(tk.Frame):
    """工作区 = 顶部工具栏 + 左侧页面竖栏 + 内容区。

    照 Arduino IDE 2.x 的骨架 (它是 VS Code / Theia 那一套):

        ┌──────────────────────────────────────────────┐
        │ [●][●]   [串口 ▾]                       [✓]  │  顶部工具栏 (#006D70)
        ├────┬─────────────────────────────────────────┤
        │ ▍  │                                          │
        │ ▏  │              内容区                       │  左侧页面竖栏 (#ECF1F1)
        │ ▎  │                                          │
        └────┴─────────────────────────────────────────┘

    为什么把页面切换从顶部搬到左侧 (原先是通栏的横向页签):
      · 横向页签吃掉一整行**竖向**空间, 而竖向正是这个程序最缺的 —— 三个页面
        全是"上面一张表 + 下面一大片"的形状;
      · 竖栏只占一列, 而且切换目标**永远在同一位置**, 不用先在顶行找;
      · 这正是 Arduino 自己的做法 —— 实拍确认它的页面切换就在左侧竖栏。

    对外接口跟 ttk.Notebook 刻意做得接近, 少改调用方:
        add(frame, text, icon)   加一页 (icon = _ICONS 里的名字, 决定竖栏图标)
        select(frame)            切页 / 不传参则返回当前页
        select_index(i)          按序号切页
    """

    def __init__(self, master, on_change=None, **kw):
        tk.Frame.__init__(self, master, bg=C_FIELD, **kw)
        self._on_change = on_change
        self._pages = []            # [(frame, item)]
        self._current = None

        # ---- ① 顶部工具栏 ----
        # 工具区 (圆形 连接/断开 + 串口框) 由 App 往 self.tools 里填。
        # 照 Arduino: 工具按钮挂在工具栏条上, 与页面无关 —— 切页它不动。
        top = tk.Frame(self, bg=C_TOPBAR)
        top.pack(fill="x")
        self.tools = tk.Frame(top, bg=C_TOPBAR)
        self.tools.pack(side="left", padx=(px(PAD_M), px(PAD_M)), pady=px(PAD_XS) + 1)
        # 工具栏最右侧: 圆形状态徽标 (✓ 已连接 / ✕ 未连接)。
        # 只显示不可点 —— 连接/断开是左边那两个圆按钮的事, 这里只回答"现在是哪种状态"。
        self._badge_n = px(26)
        self._badge = {}
        # 两种状态共用同一个**浅色圆底** (#7FCBCD), 只换里面的图形:
        #   已连接 = 蓝对勾 ✓   未连接 = 蓝叉 ✕
        # ⚠ 图形色用 C_ACCENT (#008184) 而不是 C_TOOL_FG (#006D70) —— 用户要的是
        #   "蓝叉", #006D70 偏墨绿, #008184 才是 Arduino 调色板里最蓝的那个。
        _bpad = int(self._badge_n * 0.22)
        for key, shape in (("on", "check"), ("off", "cross")):
            im = tk.PhotoImage(data=_rr_ppm(self._badge_n, self._badge_n,
                                            self._badge_n / 2.0,
                                            _rgb(C_TOOL_BG), _rgb(C_TOOL_BG),
                                            _rgb(C_TOPBAR),
                                            mark=(shape, C_ACCENT, _bpad)))
            _ROUND_IMGS.append(im)
            self._badge[key] = im
        self.lbl_badge = tk.Label(top, bg=C_TOPBAR, bd=0, highlightthickness=0,
                                  image=self._badge["off"])
        self.lbl_badge.pack(side="right", padx=(0, px(PAD_M)))
        tk.Frame(self, height=1, bg=C_TOPBAR_LN).pack(fill="x")

        # ---- ② 主体: 左侧页面竖栏 + 内容区 ----
        mid = tk.Frame(self, bg=C_FIELD)
        mid.pack(fill="both", expand=True)
        self._rail = tk.Frame(mid, bg=C_RAIL_BG, width=px(C_RAIL_W))
        self._rail.pack(side="left", fill="y")
        # ★ 关掉几何传播 —— 否则竖栏会被里面那个 26px 的图标撑开/缩窄, 几个页面的
        #   图标宽度略有差异时, 竖栏宽度还会跟着跳。
        self._rail.pack_propagate(False)
        tk.Frame(mid, width=1, bg=C_BORDER).pack(side="left", fill="y")
        self.body = tk.Frame(mid, bg=C_FIELD)
        self.body.pack(side="left", fill="both", expand=True)

    def add(self, frame, text, icon=None):
        """加一页。frame 放进内容区, 同一时刻只有一页挂在上面。"""
        item = RailItem(self._rail, icon or "ic_chip", text,
                        lambda f=frame: self.select(f))
        item.pack(fill="x")
        self._pages.append((frame, item))
        if self._current is None:
            self.select(frame)
        return frame

    def select(self, frame=None):
        """切页。不传参 → 返回当前页 (跟 Notebook.select() 一个用法)。"""
        if frame is None:
            return self._current
        self._current = frame
        for f, item in self._pages:
            active = (f is frame)
            item.set_active(active)
            if active:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
        if self._on_change:
            self._on_change()

    def select_index(self, i):
        if 0 <= i < len(self._pages):
            self.select(self._pages[i][0])

    def set_online(self, on):
        """工具栏右侧那个圆徽标: ✓ 已连接 / ✕ 未连接"""
        try:
            self.lbl_badge.configure(image=self._badge["on" if on else "off"])
        except Exception:
            pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()

        # ---- 先载入上次的偏好 ----
        # ⚠ 必须在 _build_ui() 之前: 语言要先生效, 否则整个界面会用错语言建一遍
        self.cfg = cfg_load()
        if self.cfg.get("lang") in LANG:
            set_lang(self.cfg["lang"])
        self.fw_dir = self.cfg.get("fw_dir") or os.getcwd()
        self.local_dir = self.cfg.get("local_dir") or os.getcwd()
        self.dev_dir = "/"

        # ⚠ 根窗口的默认底色是系统灰 (#F0F0F0)。任何**没被控件盖住**的地方都会
        #   露出它 —— 以前顶部那块「连接」LabelFrame 正好挡着, 删掉之后就露出来了
        #   (表现为菜单栏下面凭空多出一条灰带)。统一成内容色, 一劳永逸。
        self.configure(bg=C_FIELD)
        self.title(tr("title"))
        set_window_icon(self)              # 标题栏 / 任务栏图标
        self._setup_geometry()

        self.sm = SerialManager()
        self.msgq = queue.Queue()          # 工作线程 → UI
        self.busy = False
        self._reader_on = False            # 终端读取线程开关
        self._drain_id = None              # UI 消息泵的定时器 id (关窗时要取消)
        self._port_timer = None            # 串口轮询的定时器 id

        self._build_ui()
        self._apply_cfg_to_widgets()       # 芯片/波特率填回控件
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._drain_id = self.after(60, self._drain)

        # esptool 日志钩子:
        # set_logger 会把单例的 __class__ 换掉, 所以先装好, 之后 esptool 所有
        # 输出都走 BridgeLogger。sink/progress 在烧写前动态注入即可。
        if EsptoolLogger:
            try:
                EsptoolLogger().set_logger(BridgeLogger())
            except Exception:
                pass

        self.refresh_ports()
        self._auto_ports()                 # 之后定时自己刷 (插拔板子能跟上)

        # ★ 本地文件列表开机就要填 —— 之前只在连接后/切目录时才刷,
        #   于是"打开程序 → 文件管理"看到的是空的, 得先点一下别处才出来。
        self.refresh_local()

    # ------------------------------------------------------------------
    # UI 构建 / build UI
    # ------------------------------------------------------------------
    def _setup_geometry(self):
        """按**工作区**定窗口大小与位置 (不写死)。

        改之前是 geometry("1140x840") + minsize(980,700): 那些数字是逻辑像素,
        在 125% 缩放的屏上变成实际 1445x1099, 而工作区只有 1020 高 —— 窗口底部
        (状态栏和「关于」)直接出屏。而窗口尺寸**不存偏好**(_save_state 里没有),
        所以每次启动都这样, 不是只有第一次。

        现在按工作区算, 并留 8% 余量; 大屏上不无限拉伸, 仍以 px(1140x840) 封顶。
        """
        wa = work_area()
        if wa:
            _, _, sw, sh = wa
        else:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(px(1140), int(sw * 0.92))
        h = min(px(840), int(sh * 0.92))
        x = max(0, (sw - w) // 2)
        y = max(0, int((sh - h) * 0.35))     # 略偏上, 比正中好看 (也更像原生的初始位)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.minsize(px(900), px(620))

    def _build_ui(self):
        st = ttk.Style()
        # ★ 用 clam 而不是 vista (实测 2026-09-29):
        #   vista 主题的页签/按钮是**原生元素画的**, 会**忽略 background/foreground** ——
        #   设了不生效但不报错。实测把选中页签设成"实色底+白字", 结果是**白字落在
        #   原生白底上** ⇒ 页签文字整个看不见。同理工具栏那个主按钮也上不了色。
        #   而"选中态一眼可辨"和"主操作按钮要醒目"恰恰是这次改造的两个核心诉求,
        #   所以改用 clam: 它全部由 Tk 自己画, 配色/内边距/字号都听配置的。
        for th in ("clam", "vista"):
            try:
                st.theme_use(th)
                break
            except Exception:
                continue
        self._style_setup(st)
        self._build_menu()

        # ---- 顶部工具栏 ----
        # 只留**设备相关**的三件: 连接 / 断开 / 串口选择 (照 Arduino —— 它的工具栏
        # 左边是"对当前项目做什么", 右边是"对着哪台设备做", 而 bTool 没有项目概念,
        # 所以只剩设备那一半)。芯片型号和连接状态在**状态栏**。
        self.detected_chip = ""            # 连上探测到的, 空 = 还没认出来
        self.repl_ok = False               # 连上的板子有 MicroPython 吗 (决定文件管理能不能用)

        # ---- 中部: 工作区 (顶部工具栏 + 左侧页面竖栏 + 内容) ----
        # 页面切换在**左侧竖栏**里, 照 Arduino (实拍确认它的页面切换也在左侧)。
        # 原先是一条通栏的横向页签, 吃掉一整行竖向空间 —— 而三个页面全是
        # "上面一张表 + 下面一大片"的形状, 竖向正是最缺的。
        wa = WorkArea(self, on_change=self._on_tab_changed)
        wa.pack(fill="both", expand=True)
        self.wa = wa

        # ---- 工具栏左侧: 圆形 连接/断开 + 串口选择框 ----
        # 照 Arduino: 工具按钮跟设备相关, 挂在工具栏条上, 与页面无关 (切页它不动)。
        self.btn_connect = CircleButton(wa.tools, "connect", C_TOPBAR,
                                        self.on_connect)
        self.btn_connect.pack(side="left")
        self.btn_disconnect = CircleButton(wa.tools, "disconnect", C_TOPBAR,
                                           self.on_disconnect)
        self.btn_disconnect.pack(side="left", padx=(px(PAD_XS), px(PAD_L)))
        # 串口选择框 (Arduino 的 toolbar.dropdown: 白底 + #DAE3E3 边框 + #4E5B61 字)
        self.cb_port = ttk.Combobox(wa.tools, width=20, state="readonly",
                                    style="Strip.TCombobox")
        self.cb_port.pack(side="left")
        self.cb_port.bind("<<ComboboxSelected>>", self._on_combo_port)

        self.tab_flash = ttk.Frame(wa.body)
        self.tab_repl = ttk.Frame(wa.body)
        self.tab_files = ttk.Frame(wa.body)
        wa.add(self.tab_flash, text=tr("tab_flash"), icon="ic_flash")
        wa.add(self.tab_repl, text=tr("tab_repl"), icon="ic_term")
        wa.add(self.tab_files, text=tr("tab_files"), icon="ic_file")

        self._build_flash_tab()
        self._build_repl_tab()
        self._build_files_tab()        # ---- 状态栏 / status bar ----
        # 「关于」放这儿: 连接栏那一排已经很挤了, 而关于是偶尔点一次的东西。
        # 先 pack 按钮再 pack 状态文字 —— 否则文字会把整行占满, 按钮被挤没。
        # ★ 状态栏用 Arduino 的 statusBar 配色: **深 teal 底 + 浅字** —— 这是它
        #   最好认的特征之一, 也把"窗口到此结束"这条界线画清楚了 (原来只有一条
        #   1px 灰线, 和内容区分不开)。
        self.var_status = tk.StringVar(value=tr("ready"))
        bar = ttk.Frame(self, style="Status.TFrame")
        bar.pack(fill="x", side="bottom")
        row = ttk.Frame(bar, style="Status.TFrame")
        row.pack(fill="x")
        ttk.Button(row, text=tr("about"), width=7, style="Status.TButton",
                   command=self.on_about).pack(side="right",
                                               padx=(px(PAD_XS), px(PAD_S)),
                                               pady=px(PAD_XS))
        # 串口选择挪进菜单之后, 必须有个地方**常驻**显示"现在对着哪个口" ——
        # 否则就得开菜单才知道, 那就退化成了"藏起来的设置"。
        # ★ 连接状态指示: 带颜色的圆点 + 粗体。
        #   原来它在顶部工具栏里; 那一栏删掉后搬到这里 —— 状态栏是常驻的,
        #   放这儿比放在会被删掉的工具栏上更合理。
        #   用 tk.Label 而不是 ttk.Label: 某些 ttk 主题会忽略 foreground。
        self.lbl_dot = tk.Label(row, bg=C_BAR, text="●", fg=C_DANGER,
                                font=FONT_UI_BOLD)
        self.lbl_dot.pack(side="left", padx=(px(PAD_M), px(PAD_XS)), pady=px(PAD_XS))
        self.var_devline = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.var_devline, style="Status.TLabel",
                  anchor="w").pack(side="left", pady=px(PAD_XS))
        ttk.Label(row, textvariable=self.var_status, style="Status.TLabel",
                  anchor="w").pack(side="left", fill="x", expand=True,
                                   padx=px(PAD_L), pady=px(PAD_XS))

    def _style_setup(self, st):
        """把主题默认外观改成这套界面的统一规范。

        ⚠ 必须在 theme_use() **之后**调 —— 换主题会把已设的样式清掉。
        改之前全文件只有 _build_ui 里那一句 `ttk.Style()`, 之后**一个 configure
        都没有** —— 所有控件都是主题默认样子: 列表行高偏挤、页签三个长得几乎一样
        (选中态只差一点背景明度)、按钮内边距为零。这就是"看着随意"的直接原因。
        """
        R4 = px(4)          # 统一圆角半径 (4 逻辑px, 与 Windows 11 系统控件同量级)
        # ---- 底色 ----
        # 内容区 = **纯白** (Arduino 的 editor.background)。窗口"框"的部分另给
        # Chrome.* 样式 (#ECF1F1) —— 这样页签的"激活项白色"才有东西可对比。
        st.configure(".", background=C_FIELD, foreground=C_TEXT, font=FONT_UI)
        st.configure("TFrame", background=C_FIELD)
        st.configure("TLabel", background=C_FIELD, foreground=C_TEXT, font=FONT_UI)
        st.configure("TLabelframe", background=C_FIELD, bordercolor=C_BORDER,
                     relief="solid", borderwidth=1)
        st.configure("TLabelframe.Label", background=C_FIELD, foreground=C_TEXT,
                     font=FONT_UI)
        st.configure("TCheckbutton", background=C_FIELD, foreground=C_TEXT, font=FONT_UI)
        st.map("TCheckbutton", background=[("active", C_FIELD)])
        st.configure("TSeparator", background=C_BORDER)

        # 窗口"框"那一层: 顶部连接栏 / 页签条 —— Arduino 的 #ECF1F1
        st.configure("Chrome.TFrame", background=C_CHROME)
        st.configure("Chrome.TLabel", background=C_CHROME, foreground=C_TEXT,
                     font=FONT_UI)
        st.configure("Chrome.TLabelframe", background=C_CHROME,
                     bordercolor=C_BORDER, relief="solid", borderwidth=1)
        st.configure("Chrome.TLabelframe.Label", background=C_CHROME,
                     foreground=C_TEXT, font=FONT_UI)
        st.configure("Chrome.TCheckbutton", background=C_CHROME, foreground=C_TEXT,
                     font=FONT_UI)
        st.map("Chrome.TCheckbutton", background=[("active", C_CHROME)])
        # 状态栏: Arduino 的 statusBar 是**深 teal + 浅字** —— 它最好认的特征之一
        st.configure("Status.TFrame", background=C_BAR)
        st.configure("Status.TLabel", background=C_BAR, foreground=C_FIELD,
                     font=FONT_UI)

        # ---- 输入类: 也做圆角 ----
        # 必须跟按钮**成套**: 一半圆角一半直角, 比全直角更乱。
        # 做法是把 field 元素换成九宫格图 (Entry.field / Combobox.field), 内部
        # 的 padding/textarea 结构照旧 —— 所以输入、选中、只读这些行为都不受影响。
        # ⚠ 下拉箭头**不能**画进九宫格图里 —— ttk 的 image element 对中间那格是
        #   **平铺(tile)** 而不是拉伸, 箭头会沿控件高度重复好几遍 (实测: 一个下拉框
        #   里叠了三个 ∨)。中间格只有是纯色时, tile 与 stretch 才看不出区别 ——
        #   这也是圆角按钮一直正常的原因。
        #   箭头仍交给 Combobox.downarrow 元素, 且必须排在有 -side 的位置上。
        for sty, text_el, tail in (("TEntry", "Entry.textarea", ()),
                                   ("TCombobox", "Combobox.textarea",
                                    (("Combobox.downarrow",
                                      {"side": "right", "sticky": "ns"}),))):
            install_round_field(st, sty, R4, C_FIELD, {
                "":         (C_FIELD,  C_BORDER),
                "focus":    (C_FIELD,  C_ACCENT_LT),
                "disabled": (C_CHROME, C_BORDER),
            }, text_el, tail)
            st.configure(sty, fieldbackground=C_FIELD, background=C_FIELD,
                         foreground=C_TEXT, bordercolor=C_BORDER,
                         lightcolor=C_BORDER, darkcolor=C_BORDER,
                         padding=(px(PAD_S), px(PAD_XS)))
        # ⚠ 别显式设 arrowsize —— clam 的 downarrow 元素被垂直拉伸时会**平铺**
        #   箭头图案, 显式给小尺寸会叠出好几个 ∨ (实测 3 个)。留空让它用元素
        #   自己的自然尺寸, 正好填满高度, 就只有一个。
        # 下拉箭头那块的底色调成和 field 一样 (白), 否则右边会挂个灰方块,
        # 把圆角右边缘咬掉一块
        st.configure("TCombobox", selectbackground=C_SEL)
        st.map("TCombobox",
               fieldbackground=[("readonly", C_FIELD), ("disabled", C_CHROME)],
               foreground=[("disabled", C_MUTED)],
               arrowcolor=[("active", C_ACCENT)])
        st.map("TEntry", foreground=[("disabled", C_MUTED)])

        # 页签条上的串口框 —— 同一套圆角图, 只是 **outside 换成条带色**,
        # 否则四角会露出白色方块 (outside 是烤进图里的, 容器色必须匹配)。
        install_round_field(st, "Strip.TCombobox", R4, C_TOPBAR, {
            "":         (C_FIELD,  C_BORDER),
            "focus":    (C_FIELD,  C_ACCENT_LT),
            "disabled": (C_WIDGET, C_BORDER),
        }, "Combobox.textarea",
            (("Combobox.downarrow", {"side": "right", "sticky": "ns"}),))
        st.configure("Strip.TCombobox", fieldbackground=C_FIELD,
                     background=C_FIELD, foreground=C_TEXT,
                     bordercolor=C_BORDER, lightcolor=C_BORDER,
                     darkcolor=C_BORDER, padding=(px(PAD_S), px(PAD_XS)))
        st.map("Strip.TCombobox",
               fieldbackground=[("readonly", C_FIELD), ("disabled", C_WIDGET)],
               foreground=[("disabled", C_MUTED)])

        # ---- 列表: 行高与字号 (默认 rowheight 配 9pt 中文偏挤) ----
        st.configure("Treeview", rowheight=px(24), font=FONT_UI,
                     background=C_FIELD, fieldbackground=C_FIELD,
                     foreground=C_TEXT, bordercolor=C_BORDER,
                     lightcolor=C_BORDER, darkcolor=C_BORDER)
        st.configure("Treeview.Heading", font=FONT_UI, background=C_CHROME,
                     foreground=C_TEXT, padding=(px(PAD_S), px(PAD_XS)),
                     relief="flat", bordercolor=C_BORDER)
        st.map("Treeview.Heading", background=[("active", C_HILITE)])
        # 选中行用**淡青底 + 深字** (Arduino 的 list.activeSelectionBackground
        # 是 #00818433, 20% 透明青; Tk 不支持透明度, 这里用等值的白底混色)
        st.map("Treeview", background=[("selected", C_SEL)],
               foreground=[("selected", C_TEXT_HI)])

        # ---- 按钮 ----
        # 全部走**圆角** (见文件上方 install_round_button 的说明)。这里只给颜色和
        # 状态, 形状由那套九宫格图负责。
        #
        # ⚠ 两条实测踩过的坑:
        #   ① `background` 等颜色**必须是 '#RRGGBB' 字符串**。传元组 (255,255,255)
        #      不会报错, 但按钮上的**文字会整个消失** —— 排查了半天。
        #   ② 图必须留引用 (见 _ROUND_IMGS), 否则 GC 后渲染成**纯黑块**。
        #
        # outside = 按钮所在**容器**的底色, 会被烤进四角。放错地方四角就会露出一块
        # 不对的色。所以页面上的按钮和框上的按钮是两套。

        # Arduino 的按钮分两级底 + 一级无底, 这里照搬:
        #   toolbar.button.background = #7FCBCD  → 工具栏/动作行 (常用)
        #   button.background         = #008184  → 主操作 (一屏一个)
        #   secondaryButton           = 无底 + 青字 → 低频/辅助
        #
        # ★ 所以 **TButton 默认就是"浅青实底"** (第二级) —— 一次到位, 不必逐个
        #   按钮去标 style。要降级成"无底青字"的低频按钮才显式写 Plain.TButton。
        #
        # ⚠ 两条实测踩过的坑:
        #   ① 颜色**必须是 '#RRGGBB' 字符串**。传元组 (255,255,255) 不报错, 但按钮上的
        #      **文字会整个消失** —— 排查了半天。
        #   ② 图必须留引用 (见 _ROUND_IMGS), 否则 GC 后渲染成**纯黑块**。
        # outside = 按钮所在**容器**的底色, 会被烤进四角。放错地方四角会露出一块
        # 不对的色。所以页面上的按钮和框上的按钮是两套。

        # ⚠ 描边色**等于**填充色 = 看不见边框 = 纯平色块 (Arduino 就是这么做的)。
        #   只在前两级这么干; 输入框和第三级才留一根 1px 发丝线。

        # 第二级 · 动作按钮 · 放在白页面/面板上
        # ⚠ 禁用态的填充别用"和容器同色" —— 那样整块消失只剩一圈 1px 描边,
        #   圆角处看着像缺口 (实测)。用 C_WIDGET 各差一档, 形状才看得出来。
        install_round_button(st, "TButton", R4, C_FIELD, {
            "":         (C_ACCENT_T2, C_ACCENT_T2),   # 淡青实底, 无描边
            "active":   (C_ACCENT_LT, C_ACCENT_LT),   # 悬停 → Arduino 原值 #7FCBCD
            "pressed":  (C_ACCENT,    C_ACCENT),
            "disabled": (C_WIDGET,    C_BORDER),
        }, (px(PAD_M), px(PAD_XS)), C_ACCENT_DK)

        # 第二级 · 落在 #ECF1F1 的框上 (顶部连接栏那排)
        install_round_button(st, "Chrome.TButton", R4, C_CHROME, {
            "":         (C_ACCENT_T2, C_ACCENT_T2),
            "active":   (C_ACCENT_LT, C_ACCENT_LT),
            "pressed":  (C_ACCENT,    C_ACCENT),
            "disabled": (C_WIDGET,    C_BORDER),
        }, (px(PAD_M), px(PAD_XS)), C_ACCENT_DK)

        # 第三级 · 低频/辅助按钮: **无实底**, 只有青字 (Arduino 的 secondaryButton)。
        #   用在「…」选目录、状态栏「关于」、对话框的确定这类地方 —— 它们不需要抢注意力。
        #   ⚠ 这级必须显式指定 style, 否则默认会拿到第二级的浅青实底。
        install_round_button(st, "Plain.TButton", R4, C_FIELD, {
            "":         (C_FIELD,    C_BORDER),
            "active":   (C_HILITE,   C_BORDER_2),
            "pressed":  (C_BORDER,   C_ACCENT),
            "disabled": (C_WIDGET,   C_BORDER),
        }, (px(PAD_M), px(PAD_XS)), C_ACCENT)

        # ★ 第一级 · 主操作 (连接 / 烧写): Arduino 的 button.background
        #   = #008184 实底 + 浅字。一屏一个, 视线自然落上去。
        install_round_button(st, "Accent.TButton", R4, C_CHROME, {
            "":         (C_ACCENT,    C_ACCENT),   # 描边=填充 ⇒ 无边框, 纯平
            "active":   (C_ACCENT_DK, C_ACCENT_DK),
            "pressed":  (C_ACCENT_DK, C_ACCENT_DK),
            "disabled": (C_WIDGET,   C_BORDER),
        }, (px(PAD_L), px(PAD_S)), C_WIDGET)

        # 同样第一级, 但落在**白页面**上的主按钮 (擦除并烧写)
        install_round_button(st, "AccentPage.TButton", R4, C_FIELD, {
            "":         (C_ACCENT,    C_ACCENT),
            "active":   (C_ACCENT_DK, C_ACCENT_DK),
            "pressed":  (C_ACCENT_DK, C_ACCENT_DK),
            "disabled": (C_WIDGET,   C_BORDER),
        }, (px(PAD_L), px(PAD_S)), C_WIDGET)

        # 状态栏上的按钮 (深 teal 底) —— 平时跟底色一样, 悬停才浮出来
        install_round_button(st, "Status.TButton", R4, C_BAR, {
            "":       (C_BAR,      C_BAR),
            "active": (C_ACCENT_DK, C_ACCENT_DK),
            "pressed":(C_ACCENT_DK, C_ACCENT_DK),
        }, (px(PAD_S), px(PAD_XS)), C_FIELD, FONT_UI)

        # 进度条: progressBar.background = #005C5F, 槽用淡青 #7FCBCD
        # 进度条: 填充 = progressBar.background (#005C5F 深青, 对比度拉满);
        #   槽 = #DAE3E3。
        #   ⚠ 槽**不能**用 #ECF1F1(C_CHROME) 或浅青 ——
        #     · 浅青跟第二级按钮同色, 铺满一整行会把薄荷绿搞得太泛滥;
        #     · #ECF1F1 在白页面上几乎等于背景, 0% 时整条看着像一根虚影 (用户实测反馈)。
        st.configure("TProgressbar", background=C_ACCENT_DK, troughcolor=C_BORDER,
                     bordercolor=C_BORDER_2, lightcolor=C_ACCENT_DK,
                     darkcolor=C_ACCENT_DK, thickness=px(16))
        st.configure("Vertical.TSeparator", background=C_BORDER)

    # ------------------------------------------------------------------
    # 菜单栏 / menu bar
    # ------------------------------------------------------------------
    GITHUB = "https://github.com/bobyuhit/bTool"

    def _build_menu(self):
        """顶部菜单栏 —— 比照 Arduino IDE, 但按 bTool 自己的功能裁剪过。

        三栏: 「工具」「设置」「帮助」。
        · 不要「文件」: bTool 没有"新建/打开/保存/另存"这类文档操作; 固件列表的增删
          本来就有一排按钮, 再在菜单里做一遍是重复入口。
        · 不要「编辑」: 没有代码可编辑 (Arduino 的编辑栏是给编辑器的)。
        · 「设置」对应 Arduino 的「首选项」, 装的是用户偏好 (现在只有语言;
          以后加界面比例/主题也放这)。
        · 设备类操作全在「工具」, 对应 Arduino 工具栏的 端口/串口监视器/获得开发板信息。
        """
        bar = tk.Menu(self)
        self.menubar = bar

        # ---- 工具 ----
        # 只分三块, 别再多插分隔线 —— 一块两三行还各插一条会碎得很难扫。
        t = tk.Menu(bar, tearoff=0)      # tearoff=0: 去掉顶上那条虚线"撕离"项
        # 「串口」放在工具菜单里 (Arduino 的「工具 → 端口」也是子菜单)。
        # 内容是**动态**的: 每次扫到端口就重建一次, 见 _apply_ports。
        # 当前选中的口用单选圆点标出 —— 不用开下拉也看得出连的是哪个口。
        self.var_port = tk.StringVar(value="")   # 当前选中的口 (整条显示串, 如 "COM5  (CH343)")
        self.var_ports = []                      # 扫到的口 (显示串) —— 两者都由 _apply_ports 维护
        self.menu_ports = tk.Menu(t, tearoff=0)
        t.add_cascade(label=tr("port_menu"), menu=self.menu_ports)
        t.add_separator()
        t.add_command(label=tr("connect"), accelerator="Ctrl+K",
                      command=self.on_connect)
        self.mi_conn = t.index("end")        # 记下索引: 连接/断开的可点状态要随
        t.add_command(label=tr("disconnect"), command=self.on_disconnect)
        self.mi_disc = t.index("end")        # 连接状态变 (按钮删了, 改由菜单项反映)
        t.add_separator()
        t.add_command(label=tr("read_chip"), accelerator="Ctrl+I",
                      command=self.on_read_chip)
        bar.add_cascade(label=tr("menu_tools"), menu=t)

        # ---- 设置 ----
        # 对应 Arduino 的「文件 → 首选项」。语言是一年改一次的设置, 放这里而不是
        # 占工具栏位置。以后加"界面比例""主题"同样归这一栏。
        s = tk.Menu(bar, tearoff=0)
        lm = tk.Menu(s, tearoff=0)
        self.var_lang_menu = tk.StringVar(value=_LANG_ID)
        for k in ("zh", "en"):
            lm.add_radiobutton(label=LANG[k]["lang_name"], value=k,
                               variable=self.var_lang_menu,
                               command=lambda kk=k: self._switch_lang_checked(kk))
        s.add_cascade(label=tr("language"), menu=lm)
        bar.add_cascade(label=tr("menu_settings"), menu=s)

        # ---- 帮助 ----
        h = tk.Menu(bar, tearoff=0)
        h.add_command(label=tr("user_manual"),
                      command=lambda: self._open_doc("用户手册.md"))
        h.add_command(label=tr("dev_notes"),
                      command=lambda: self._open_doc("开发说明.md"))
        h.add_separator()
        h.add_command(label=tr("about_app"), command=self.on_about)
        bar.add_cascade(label=tr("menu_help"), menu=h)

        self.config(menu=bar)
        self._bind_accels()

    def _open_doc(self, name):
        """打开在线文档。

        ⚠ 只能开**在线**的: 打包成单文件 exe 后, 仓库里的 .md 根本没跟着打进去,
          指本地路径在 exe 里必然是死链。
        """
        import webbrowser
        try:
            webbrowser.open("%s/blob/master/%s" % (self.GITHUB, name))
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def _bind_accels(self):
        """绑定菜单上标的快捷键。

        ★ 终端有焦点时**一律让路** —— Ctrl+C / Ctrl+D 是 MicroPython 的 readline
          在用的 (中断 / EOF), 被菜单抢走就没法打断板子上跑的程序了。菜单里标着的
          快捷键只是一份"提示", 真正的取舍在这里。
        """
        def bind(seq, fn):
            def handler(_e):
                try:
                    if self.focus_get() is self.txt_term:
                        return None            # 不拦, 原样发给板子
                except Exception:
                    pass
                fn()
                return "break"
            self.bind_all(seq, handler)

        bind("<Control-o>", self.on_add_fw)
        bind("<Control-k>", self.on_connect)
        bind("<Control-i>", self.on_read_chip)
        bind("<F5>", self.refresh_ports)

    def _switch_lang_checked(self, lang_id):
        """菜单里的语言单选 —— 跟旧的下拉一样走 _switch_lang, 但要防重复触发。"""
        if lang_id != _LANG_ID:
            self._switch_lang(lang_id)

    def _on_tab_changed(self, _evt=None):
        # 切到 REPL 页时把键盘焦点交给终端 —— 否则光标不闪、敲字没反应
        # (Text 控件只在有焦点时才显示插入光标)
        try:
            if self.wa.select() is self.tab_repl:
                self.txt_term.focus_set()
        except Exception:
            pass

    # ---- REPL 页 / repl tab ----
    def _build_repl_tab(self):
        p = self.tab_repl

        # ★ 终端区 —— **可直接在里面敲**, 没有单独的输入框。
        #   按键不本地插入, 而是原样发给板子; 屏幕上看到的字符是板子**回显**
        #   回来的 (MicroPython 的友好 REPL 会回显输入)。两处都插就会重影。
        self.txt_term = tk.Text(p, height=18, wrap="char",
                                bg=C_TERM_BG, fg=C_TERM_FG,
                                insertbackground=C_TERM_FG,
                                selectbackground=C_TERM_SEL,
                                font=FONT_MONO,
                                undo=False)
        self.txt_term.pack(fill="both", expand=True, padx=8)
        self.txt_term.bind("<Key>", self.on_term_key)
        self.txt_term.bind("<Return>", lambda e: self.term_send(b"\r"))
        self.txt_term.bind("<KP_Enter>", lambda e: self.term_send(b"\r"))
        self.txt_term.bind("<BackSpace>", lambda e: self.term_send(b"\x08"))
        self.txt_term.bind("<Tab>", lambda e: self.term_send(b"\t"))
        # ★ Ctrl+C **分流**: 有选中 → 只复制; 没选中 → 打断板子 (见 on_ctrl_c)。
        #   不加分流的话, 想复制一段输出就会顺手把板子上跑的程序打断。
        self.txt_term.bind("<Control-c>", self.on_ctrl_c)
        self.txt_term.bind("<Control-d>", lambda e: self.term_send(b"\x04"))
        self.txt_term.bind("<<Paste>>", self.on_term_paste)
        # 真终端里"复制/粘贴"另有专键, 这两个**不分流**, 永远就是复制/粘贴
        self.txt_term.bind("<Control-Shift-C>", lambda e: self.term_copy())
        self.txt_term.bind("<Control-Insert>", lambda e: self.term_copy())
        self.txt_term.bind("<Shift-Insert>", self.on_term_paste)
        # 右键菜单: 复制 / 粘贴 / 全选 / 清屏
        self.txt_term.bind("<Button-3>", self.on_term_menu)
        self._build_term_menu()

        # 半个转义序列的暂存区: 串口 read 会把 "\x1b[3~" 切在两块里,
        # 收不全就留到下一次 (见 term_write)。**不能初始化成局部变量** ——
        # 必须跨调用活下来。
        self._term_esc = ""

        # ---- 方向键 / Home / End / Delete ----
        # 转发 ANSI 转义序列, 由**板子**处理 —— MicroPython 的 readline 认这些:
        #   ESC[A/B 上/下 (翻历史), ESC[C/D 右/左, ESC[H/F Home/End, ESC[3~ Delete
        # 固件侧已确认支持: esp32 端口 ROM 级别 = EXTRA_FEATURES,
        # 而 MICROPY_REPL_EMACS_KEYS 要求 >= EXTRA_FEATURES (py/mpconfig.h:708)。
        # 历史 8 条 (MICROPY_READLINE_HISTORY_SIZE)。
        # ⚠ 别自己维护历史: 板子那边已经有, 本地再记一份会两边不一致。
        for seq, code in (
            ("<Up>",     b"\x1b[A"),
            ("<Down>",   b"\x1b[B"),
            ("<Right>",  b"\x1b[C"),
            ("<Left>",   b"\x1b[D"),
            ("<Home>",   b"\x1b[H"),
            ("<End>",    b"\x1b[F"),
            ("<Delete>", b"\x1b[3~"),
            ("<Prior>",  b"\x1b[5~"),     # PageUp
            ("<Next>",   b"\x1b[6~"),     # PageDown
        ):
            self.txt_term.bind(seq, lambda e, c=code: self.term_send(c))

        row = ttk.Frame(p)
        row.pack(fill="x", padx=px(8), pady=(px(6), px(8)))
        ttk.Label(row, text=tr("term_hint"), foreground=C_MUTED).pack(side="left")
        # ★ 只做"切回普通 REPL"这**一个方向** —— 没有"手动进 raw"。
        #   raw 是文件操作用的**程序化**模式: 工具自己切进去、做完自己切出来,
        #   人没有理由主动进去 (键盘输入在里面本来就无效)。
        #   所以这按钮是**兜底**: 万一哪里漏了没收回来, 用户有个手动出口。
        #   平时它是灰的, 只有真卡在 raw 时才可点 —— 见 _sync_mode_ui。
        self.btn_raw = ttk.Button(row, text=tr("to_repl"), width=11,
                                  command=self.on_recover_repl)
        self.btn_raw.pack(side="right", padx=4)
        ttk.Button(row, text=tr("clear"), width=7,
                   command=self.clear_repl).pack(side="right", padx=4)
        ttk.Button(row, text="Ctrl+C", width=8,
                   command=self.on_interrupt).pack(side="right", padx=4)

    # ---- 烧写页 / flash tab ----
    def _build_flash_tab(self):
        p = self.tab_flash

        bar = ttk.Frame(p)
        bar.pack(fill="x", padx=px(8), pady=8)
        ttk.Button(bar, text=tr("add_fw"),
                   command=self.on_add_fw).pack(side="left")
        ttk.Button(bar, text=tr("remove_sel"),
                   command=self.on_del_fw).pack(side="left", padx=6)
        ttk.Button(bar, text=tr("clear"),
                   command=self.on_clear_fw).pack(side="left")

        ttk.Label(bar, text=tr("flash_baud")).pack(side="left", padx=(px(20), px(2)))
        self.cb_fbaud = ttk.Combobox(
            bar, width=10, state="readonly",
            values=["115200", "230400", "460800", "921600"])
        self.cb_fbaud.set("921600")
        self.cb_fbaud.pack(side="left")

        # 读芯片信息 —— 放最右边, 和"管理固件列表"那几个按钮分开 (它是另一类操作)
        ttk.Button(bar, text=tr("read_chip"),
                   command=self.on_read_chip).pack(side="right")

        cols = ("addr", "file", "size")
        # 高度 3 行就够 (一般就是 bootloader + 分区表 + app 三个),
        # 给 8 行会白白吃掉一大块竖向空间, 把下面的 REPL 挤没
        self.tv_fw = ttk.Treeview(p, columns=cols, show="headings", height=3)
        self.tv_fw.heading("addr", text=tr("col_addr"))
        self.tv_fw.heading("file", text=tr("col_fw"))
        self.tv_fw.heading("size", text=tr("col_size"))
        self.tv_fw.column("addr", width=px(100), anchor="center")
        self.tv_fw.column("file", width=px(560))
        self.tv_fw.column("size", width=px(110), anchor="e")
        self.tv_fw.pack(fill="both", expand=True, padx=8)
        self.tv_fw.bind("<Double-1>", self.on_edit_addr)

        ttk.Label(p, foreground=C_WARN, justify="left",
                  text=tr("erase_warn")).pack(fill="x", padx=px(8), pady=(px(6), px(0)))

        run = ttk.Frame(p)
        run.pack(fill="x", padx=px(8), pady=8)
        self.var_erase = tk.BooleanVar(value=False)
        ttk.Checkbutton(run, text=tr("erase_first"),
                        variable=self.var_erase).pack(side="left")
        # ★ 烧写是本页的**主操作** -> 用实底主按钮, 跟一层次要按钮拉开
        self.btn_flash = ttk.Button(run, text=tr("erase_and_flash"),
                                    style="AccentPage.TButton",
                                    command=self.on_flash)
        self.btn_flash.pack(side="right")

        # 烧写进度 —— 左边标明这是什么, 中间进度条, 右边百分比。
        # (原来只有一条光秃秃的进度条 + 下面一行"等待操作", 没头没尾,
        #  用户根本看不出那是什么栏)
        pbrow = ttk.Frame(p)
        pbrow.pack(fill="x", padx=px(8), pady=(px(0), px(4)))
        ttk.Label(pbrow, text=tr("flash_progress")).pack(side="left")
        self.pb = ttk.Progressbar(pbrow, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True, padx=6)
        self.var_pb = tk.StringVar(value=tr("idle"))
        ttk.Label(pbrow, textvariable=self.var_pb, width=30,
                  anchor="w").pack(side="left")

        # 烧写日志 —— 必须带标签, 否则就是个没头没尾的空白框
        ttk.Label(p, text=tr("flash_log")).pack(anchor="w", padx=px(8), pady=(px(8), px(0)))
        # ★ 用 Arduino 的**输出面板**配色: 纯黑底 + 白字
        #   (arduino.output.background / .foreground 就是 #000000 / #ffffff)。
        #   顺带统一了: 原先"终端 Consolas 11 / 日志 Consolas 9"两个等宽框两个字号,
        #   现在都是 FONT_MONO。
        self.txt_flash = tk.Text(p, height=6, wrap="word",
                                 bg=C_OUT_BG, fg=C_OUT_FG,
                                 insertbackground=C_OUT_FG,
                                 selectbackground=C_ACCENT_LT,
                                 relief="flat", font=FONT_MONO)
        self.txt_flash.pack(fill="both", expand=True, padx=px(8), pady=(px(2), px(8)))

    # ---- 文件管理页 / files tab ----
    def _build_files_tab(self):
        p = self.tab_files

        # 顶部提示条: 板子上没有 MicroPython 时这里说明原因 (正常时是空的)。
        # 见 _sync_file_ui —— 文件管理靠 MicroPython 的 os API, 没有它就没得管。
        self.lbl_vfs_hint = ttk.Label(p, text="", foreground=C_DANGER)
        self.lbl_vfs_hint.pack(fill="x", padx=8)

        pan = ttk.PanedWindow(p, orient="horizontal")
        pan.pack(fill="both", expand=True, padx=px(8), pady=8)

        # 左: 本地 / left: local
        left = ttk.LabelFrame(pan, text=tr("local_files"), padding=6)
        pan.add(left, weight=1)

        lb = ttk.Frame(left)
        lb.pack(fill="x")
        self.var_local = tk.StringVar(value=self.local_dir)
        e_loc = ttk.Entry(lb, textvariable=self.var_local)
        e_loc.pack(side="left", fill="x", expand=True)
        e_loc.bind("<Return>", lambda _e: self.refresh_local())   # 手改路径后按回车生效
        ttk.Button(lb, text="…", width=3, style="Plain.TButton",
                   command=self.on_pick_dir).pack(side="left", padx=(px(4), px(0)))

        self.tv_local = ttk.Treeview(left, columns=("name", "size"),
                                     show="headings", selectmode="extended")
        self.tv_local.heading("name", text=tr("col_name"))
        self.tv_local.heading("size", text=tr("col_size"))
        self.tv_local.column("name", width=px(260))
        self.tv_local.column("size", width=px(90), anchor="e")
        self.tv_local.pack(fill="both", expand=True, pady=(px(6), px(0)))
        self.tv_local.bind("<Double-1>", self.on_local_open)

        # 中: 操作按钮 / middle: buttons
        mid = ttk.Frame(pan)
        pan.add(mid, weight=0)
        ttk.Label(mid, text="").pack(pady=30)
        # 这几个要留引用: 板子不是 MicroPython 时得把它们置灰 (见 _sync_file_ui)
        self.btn_up = ttk.Button(mid, text=tr("upload"), width=12,
                                 command=self.on_upload)
        self.btn_up.pack(pady=4)
        self.btn_down = ttk.Button(mid, text=tr("download"), width=12,
                                   command=self.on_download)
        self.btn_down.pack(pady=4)
        ttk.Separator(mid, orient="horizontal").pack(fill="x", pady=10)
        self.btn_rename = ttk.Button(mid, text=tr("rename"), width=12,
                                     command=self.on_rename)
        self.btn_rename.pack(pady=4)
        self.btn_del = ttk.Button(mid, text=tr("delete"), width=12,
                                  command=self.on_delete)
        self.btn_del.pack(pady=4)
        ttk.Separator(mid, orient="horizontal").pack(fill="x", pady=10)
        self.btn_devref = ttk.Button(mid, text=tr("refresh"), width=12,
                                     command=self.refresh_device)
        self.btn_devref.pack(pady=4)

        # 右: 设备 / right: device
        right = ttk.LabelFrame(pan, text=tr("device_vfs"), padding=6)
        pan.add(right, weight=1)

        rb = ttk.Frame(right)
        rb.pack(fill="x")
        self.var_dev = tk.StringVar(value="/")
        # 这里**没有**"转到"按钮: 设备的 VFS 基本就一个根目录, 没什么可"转到"的;
        # 而且中间那栏已经有个「刷新」干的是同一件事 (都调 refresh_device)。
        # 要换目录就直接改这个框, 按回车。
        e_dev = ttk.Entry(rb, textvariable=self.var_dev)
        e_dev.pack(side="left", fill="x", expand=True)
        e_dev.bind("<Return>", lambda _e: self.refresh_device())

        self.tv_dev = ttk.Treeview(right, columns=("name", "size"),
                                   show="headings", selectmode="extended")
        self.tv_dev.heading("name", text=tr("col_name"))
        self.tv_dev.heading("size", text=tr("col_size"))
        self.tv_dev.column("name", width=px(260))
        self.tv_dev.column("size", width=px(90), anchor="e")
        self.tv_dev.pack(fill="both", expand=True, pady=(px(6), px(0)))
        self.tv_dev.bind("<Double-1>", self.on_dev_open)

    # ------------------------------------------------------------------
    # 切换语言 / language switch
    # ------------------------------------------------------------------
    # Tkinter 改文本要逐个控件改; 控件太多, 直接重建 UI 更省事。
    # 重建前保存状态, 重建后恢复, 用户看不出差别 (REPL 内容也不会丢)。
    # Tkinter has no global re-translate; rebuilding the UI is simpler than
    # tracking every widget.  Save state before, restore after.

    def _switch_lang(self, lang_id):
        st = self._save_state()
        set_lang(lang_id)
        for w in self.winfo_children():
            w.destroy()
        self._build_ui()
        self._restore_state(st)
        self.title(tr("title"))
        self._save_cfg()

    def _save_state(self):
        return {
            "repl": self.txt_term.get("1.0", "end-1c"),
            "flash_log": self.txt_flash.get("1.0", "end-1c"),
            "port": self.var_port.get(),
            "fbaud": self.cb_fbaud.get(),
            "erase": self.var_erase.get(),
            "local_dir": self.var_local.get(),
            "dev_dir": self.var_dev.get(),
            "fw_rows": [self.tv_fw.item(i, "values")
                        for i in self.tv_fw.get_children()],
            "dev_rows": [self.tv_dev.item(i, "values")
                         for i in self.tv_dev.get_children()],
            "connected": str(self.menu_tools.entrycget(self.mi_disc, "state")) == "normal",
            "pb": self.pb["value"],
        }

    def _restore_state(self, st):
        if st["port"]:
            self.var_port.set(st["port"])
            self._rebuild_port_menu()
            self._render_dev()
        self.cb_fbaud.set(st["fbaud"])
        self.var_erase.set(st["erase"])
        self.var_local.set(st["local_dir"])
        self.var_dev.set(st["dev_dir"])

        # ★ 本地列表要重扫一遍。_save_state **存的是设备目录的 rows, 没存本地的**,
        #   而且重建界面后 tv_local 本来就是空的 —— 不补这一下, 切完语言
        #   文件管理页左边就空了。(直接读盘比存下来更好: 拿到的永远是最新的)
        self.refresh_local()

        for row in st["fw_rows"]:
            self.tv_fw.insert("", "end", values=row)
        for row in st["dev_rows"]:
            self.tv_dev.insert("", "end", values=row)

        if st["repl"]:
            self.txt_term.insert("end", st["repl"])
            # ★ 光标要跟到文末 —— 渲染是"在光标处画", 而往 end 插入
            #   **不会**搬动 insert mark (只有往光标处插才会)
            self.txt_term.mark_set("insert", "end-1c")
            self.txt_term.see("insert")
        if st["flash_log"]:
            self.txt_flash.insert("end", st["flash_log"])

        self.pb["value"] = st["pb"]

        # 状态栏**不**沿用旧文字 —— 否则切完语言还留着上一门语言的句子。
        # 按当前状态重新生成一条本地化的。
        if st["connected"]:
            self.menu_tools.entryconfigure(self.mi_conn, state="disabled")
            self.menu_tools.entryconfigure(self.mi_disc, state="normal")
            self.var_status.set(tr("connected_status", port=self.sm._port))
        else:
            self.var_status.set(tr("ready"))
        self._render_dev()

        self._sync_mode_ui()               # 「切回 REPL」按钮的可点状态

        if self.busy:
            self.set_busy(True)

    # ------------------------------------------------------------------
    # 线程 → UI 消息泵 / worker thread → UI pump
    # ------------------------------------------------------------------
    def post(self, kind, **kw):
        self.msgq.put((kind, kw))

    # 每轮消息泵最多处理这么多条, 积压时把剩下的留到下一轮。
    # ⚠ 不能写成 `while True: get_nowait()` 直到队列空: 板子持续刷屏时, 读线程
    #   灌消息的速度可以超过这里处理的速度 (原生 USB 的虚拟串口能跑到 Mbps 级,
    #   比 115200 快两个数量级), 队列越积越多、这一轮就越跑越久, 最后**永远出
    #   不来** —— 主线程卡死, 窗口一动不动, 只能强杀进程。加上限后最坏也只是
    #   "终端显示滞后", 不再有卡死这条路径。
    DRAIN_MAX = 200

    def _drain(self):
        try:
            for _ in range(self.DRAIN_MAX):
                try:
                    kind, kw = self.msgq.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(kind, kw)
                except Exception:
                    # 兜底本身也要护住: log_repl 自己也可能抛 (窗口已销毁等),
                    # 那会直接冲出下面的 finally。
                    try:
                        self.log_repl(tr("ui_error") + traceback.format_exc())
                    except Exception:
                        pass
        finally:
            # ★ 必须放 finally: 万一上面抛出, 放外面的话 after 就不会被安排,
            #   消息泵**永久停摆** —— 界面从此收不到后台的任何更新, 看着也像卡死。
            self._drain_id = self.after(60, self._drain)

    def on_about(self):
        """「关于」—— 功能、作者、几个必须知道的点。

        用 Toplevel + Text 而不是 messagebox.showinfo:
        ① 内容长, messagebox 会把窗口撑得很怪;
        ② Text 里的字**能选中复制** —— 别人问"你这个报错怎么来的", 直接复制走。
        """
        win = tk.Toplevel(self)
        win.title(tr("about"))
        win.transient(self)                     # 跟着主窗口最小化
        win.geometry("620x520")
        win.minsize(480, 360)

        head = ttk.Frame(win)
        head.pack(fill="x", padx=px(14), pady=(px(12), px(6)))
        ttk.Label(head, text="%s %s" % (APP_NAME, APP_VERSION),
                  font=FONT_H1).pack(anchor="w")
        ttk.Label(head, text=tr("about_build", stamp=build_stamp()),
                  foreground=C_MUTED).pack(anchor="w")
        ttk.Label(head, text=APP_AUTHOR,
                  foreground=C_MUTED).pack(anchor="w")

        txt = tk.Text(win, wrap="word", height=18, relief="flat",
                      bg=C_WIDGET, fg=C_TEXT, padx=px(12), pady=px(10), spacing1=1,
                      font=FONT_UI)
        txt.pack(fill="both", expand=True, padx=px(14), pady=(px(4), px(4)))
        # 小标题加粗: 文案里以 '#' 开头的行当标题 (两门语言共用这个约定)
        txt.tag_configure("h", font=FONT_UI_BOLD, spacing1=8, spacing3=2)
        for ln in tr("about_body"):
            if ln.startswith("#"):
                txt.insert("end", ln[1:] + "\n", "h")
            else:
                txt.insert("end", ln + "\n")
        txt.configure(state="disabled")         # 只读, 但**能选中复制**

        ttk.Button(win, text=tr("ok"), width=10, style="Plain.TButton",
                   command=win.destroy).pack(pady=(px(0), px(12)))
        win.bind("<Escape>", lambda _e: win.destroy())
        win.focus_set()

    def _sync_file_ui(self):
        """板子上没有 MicroPython 时, 把文件管理那几个按钮置灰。

        文件管理走的是 MicroPython 的 `os` API (listdir / open / rename / remove),
        **没有那个 REPL 就没有可浏览的文件系统** —— 这不是"没适配好", 是那头
        根本没有东西可管理: 裸机板上只有一份烧进 flash 的二进制。

        与其让用户点了弹一堆 raw REPL 超时, 不如一开始就说清楚。

        判据是**现成的**, 不用额外探测: 连接时有没有拿到 `>>>` 提示符。
        """
        ok = bool(self.repl_ok) and self.sm.is_open
        state = "normal" if ok else "disabled"
        for b in (self.btn_up, self.btn_down, self.btn_rename,
                  self.btn_del, self.btn_devref):
            try:
                b.configure(state=state)
            except Exception:
                pass
        try:
            if ok:
                self.lbl_vfs_hint.configure(text="")
            elif self.sm.is_open:
                self.lbl_vfs_hint.configure(text=tr("vfs_no_mpy"))
            else:
                self.lbl_vfs_hint.configure(text=tr("vfs_need_conn"))
        except Exception:
            pass

    def _render_chip(self):
        """芯片型号变了 -> 重画状态栏那一行。

        (原来它显示在顶部工具栏一个只读 Label 上; 那一栏已删, 现在并进状态栏,
         和串口/连接状态排在一起。名字保留 _render_chip, 调用点不用动。)
        """
        self._render_dev()

    def _handle(self, kind, kw):
        if kind == "term":
            self.term_write(kw["text"])          # 设备输出
        elif kind == "repl":
            self.log_repl(kw["text"])            # 应用消息
        elif kind == "repl_raw":
            self.log_repl(kw["text"], raw=True)
        elif kind == "repl_ok":
            self.repl_ok = bool(kw["ok"])
            self._sync_file_ui()
        elif kind == "chip_name":
            # ROM 认出来的芯片型号 → 填最上面那个只读显示
            self.detected_chip = kw["name"]
            self._render_chip()
        elif kind == "flash_log":
            self.txt_flash.insert("end", kw["text"] + "\n")
            self.txt_flash.see("end")
        elif kind == "progress":
            cur, total = kw["cur"], kw["total"]
            pct = 100.0 * cur / total if total else 0
            self.pb["value"] = pct
            self.var_pb.set(tr("progress_pct", pct=pct, cur=cur, total=total))
        elif kind == "status":
            self.var_status.set(kw["text"])
        elif kind == "pb_text":
            self.var_pb.set(kw["text"])
        elif kind == "busy":
            self.set_busy(kw["on"])
        elif kind == "connected":
            self._after_connect()
        elif kind == "disconnected":
            self._after_disconnect()
        elif kind == "files_dev":
            self._fill_dev(kw["items"])
        elif kind == "files_local":
            self._fill_local(kw["items"])
        elif kind == "refresh_local":
            self.refresh_local()
        elif kind == "back_to_repl":
            self._back_to_repl()
        elif kind == "msgbox":
            messagebox.showinfo(APP_NAME, kw["text"])
        elif kind == "msgbox_err":
            messagebox.showerror(APP_NAME, kw["text"])

    # ------------------------------------------------------------------
    # 小工具 / helpers
    # ------------------------------------------------------------------
    # ---- 终端 ----
    # 设计: 按键**不本地插入**, 一律发给板子, 屏幕上的字符是板子回显回来的。
    # MicroPython 的友好 REPL 会原样回显输入, 所以本地再插一次就会每个字符
    # 显示两遍。raw REPL 下没有回显, 但那时也不该用键盘输入 (那是程序化模式)。

    def term_send(self, data):
        """把一段字节发给板子 (终端按键的出口)"""
        try:
            if self.sm.is_open:
                self.sm.write(data)
        except Exception as e:
            self.term_write("\n[发送失败: %s]\n" % e)
        return "break"

    def on_term_key(self, ev):
        """普通字符键 → 转发给板子, 不让 Tk 插进 Text。"""
        if not self.sm.is_open:
            return "break"
        if self.sm.mode == "raw":
            # raw REPL 是程序化模式, 键盘输入没有意义
            return "break"
        if self.busy or self.sm.busy_io:
            # 正在跑文件操作/烧写 —— 这时敲键盘会把字符混进协议流,
            # 把 raw REPL 的 OK...\x04 应答搅坏
            return "break"
        if ev.char and ev.char.isprintable():
            return self.term_send(ev.char.encode("utf-8"))
        return "break"

    # ---- 终端的剪贴板 ----
    # ★ 为什么要给 Ctrl+C **分流**: 终端里 Ctrl+C 天生是"打断"(发 0x03), 不是复制。
    #   但用户的肌肉记忆是 Windows 的"Ctrl+C = 复制", 于是想复制一段输出时
    #   顺手就把板子上跑的程序打断了。
    #   Windows Terminal 的解法是按**有没有选中**分流:
    #       有选中 → 只复制 (不打断)
    #       没选中 → 打断   (没东西可复制, 这时 Ctrl+C 只能是打断)
    #   一个键同时满足两个需求, 而且**谁都不会误触发另一个**。
    #   想无条件复制就用 Ctrl+Shift+C / Ctrl+Insert (真终端的惯例键)。

    def _term_has_sel(self):
        try:
            return bool(self.txt_term.tag_ranges("sel"))
        except Exception:
            return False

    def term_copy(self):
        """把选中的文字放进系统剪贴板 —— **不往串口发任何字节**"""
        try:
            txt = self.txt_term.get("sel.first", "sel.last")
        except Exception:
            return "break"                     # 没选中, 无事可做
        try:
            self.clipboard_clear()
            self.clipboard_append(txt)
        except Exception:
            pass
        return "break"

    def term_select_all(self):
        self.txt_term.tag_add("sel", "1.0", "end-1c")
        return "break"

    def on_ctrl_c(self, _evt=None):
        """Ctrl+C: 有选中就只复制, 没选中才打断板子 (Tk 会传事件进来)"""
        if self._term_has_sel():
            return self.term_copy()
        self.on_interrupt()
        return "break"

    def _build_term_menu(self):
        """右键菜单。语言切换会重建界面, 所以旧的要先销毁, 免得越积越多。"""
        old = getattr(self, "menu_term", None)
        if old is not None:
            try:
                old.destroy()
            except Exception:
                pass
        m = tk.Menu(self, tearoff=0)
        m.add_command(label=tr("menu_copy"), command=self.term_copy)
        m.add_command(label=tr("menu_paste"), command=self.on_term_paste)
        m.add_separator()
        m.add_command(label=tr("menu_select_all"), command=self.term_select_all)
        m.add_command(label=tr("clear"), command=self.clear_repl)
        self.menu_term = m

    def on_term_menu(self, ev):
        # 没选中时把"复制"置灰 —— "现在能不能复制"一眼可见, 不用点了才知道
        self.menu_term.entryconfigure(
            0, state="normal" if self._term_has_sel() else "disabled")
        try:
            self.menu_term.tk_popup(ev.x_root, ev.y_root)
        finally:
            self.menu_term.grab_release()
        return "break"

    @staticmethod
    def _paste_by_lines_ok(txt):
        """这块多行代码能不能安全地**逐行发**?

        逐行发的好处: 板子按 `\\r` 逐行收, **每行都进 readline 历史**, ↑ 能翻出来。
        整段粘贴 (paste 模式) 则是绕开 readline 的, 不进历史。

        但逐行发有个**会改语义**的陷阱 —— 板子的自动缩进 (`readline_auto_indent`)
        会按上一行给新行补空格。看它的判据:

            int n = (j - i) / 4;                          // 上一行的前导空格数
            if (line->buf[line->len - 2] == ':') n += 1;  // 上一行以冒号结尾
            while (n-- > 0) { 补 4 个空格 }

        于是这种块逐行发会出事:

            if x:
                a = 1
            b = 2
            ^ 第 3 行的自动缩进沿用了上一行的 8 个空格 → `b = 2` 被**吸进 if 块**,
              只有 x 为真才跑 —— 悄悄改了程序行为, 比"翻不到历史"严重得多。

        所以**只有自动缩进保证一次都不插手**时才逐行发:
            没有行以空白开头   (否则 n>0)
            没有行以 ':' 结尾  (否则 n+=1)
        满足这两条时它恒加 0 个空格, 逐行发与整段粘贴**完全等价**。

        还差一条: **不能以复合语句关键字开头**。板子判断"输完没有"用的是
        `mp_repl_continue_with_input` (py/repl.c), 其中一句是

            if (starts_with_compound_keyword && i[-1] != '\\n') return true;
            //   if / while / for / try / with / def / class / async, 以及开头的 '@'

        所以 `if True: pass` 这种**单行**复合语句也会被判成"没输完" → 板子**卡在
        续行状态等空行**, 而我们是逐行发的、后面没有空行 → 用户就卡住了。

        另外空行一律退回 paste 模式 —— 空行在续行里会把块提前收掉, 不划算去推演。
        """
        KEYWORDS = ("if", "while", "for", "try", "with", "def", "class", "async")
        for ln in txt.split("\n"):
            if not ln.strip():
                return False                # 空行 / 纯空白行
            if ln[0] in " \t":
                return False                # 有缩进 → 自动缩进会插手
            if ln.rstrip().endswith(":"):
                return False                # 冒号结尾 → 下一行会被自动缩进
            if ln[0] == "@":
                return False                # 装饰器 → 板子等续行
            head = ln.split(None, 1)[0].rstrip("(")   # "if(a)" 也算
            if head in KEYWORDS:
                return False                # 复合语句 → 板子等空行才执行
        return True

    def on_term_paste(self, _evt=None):
        """把剪贴板内容发到板子 —— 单行直接发, **多行走 paste 模式**。

        ⚠ 两个坑, 都跟"换行"有关:

        1) **换行必须是 CR (`\\r`)**。板子的 readline 里只有 `c == '\\r'` 是回车执行
           (readline.c:195), **没有 `\\n` 分支** —— `\\n` 会掉到默认分支被当
           **普通字符**插进当前行 (`vstr_ins_char`)。剪贴板给的换行是 `\\n`,
           直接发过去多行代码会**并成一行** → `SyntaxError: invalid syntax`。

        2) **多行要走 paste 模式 (Ctrl-E … Ctrl-D)**。逐行发是行不通的:
           板子的自动缩进是开着的 (`MICROPY_REPL_AUTO_INDENT`, esp32 的 ROM 级别
           够), 带缩进的行会被**再加一层缩进**; 而且 `for`/`if`/`def` 这类复合语句
           还得再补一个空行才算输入完。paste 模式把整段**当文件**一次编译执行
           (pyexec.c:368), 两个问题都没有。
        """
        if not self.sm.is_open or self.sm.mode == "raw":
            return "break"                  # raw 是程序化模式, 别往里灌东西
        if self.busy or self.sm.busy_io:
            return "break"                  # 正在跑文件操作/烧写, 别搅协议流
        try:
            data = self.clipboard_get()
        except Exception:
            return "break"                  # 剪贴板里不是文本

        txt = data.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        if not txt:
            return "break"

        if "\n" not in txt:
            # ★ 单行: **只把字符打上去, 不补回车** —— 粘贴单行等于"帮你打字",
            #   执行与否由用户按 Enter 决定 (补了 \r 就成了"粘上就执行",
            #   用户来不及看一眼)。
            #   注意末尾绝不能有 \r, 板子只认 \r 是回车。
            return self.term_send(txt.rstrip("\r\n").encode("utf-8"))

        if self._paste_by_lines_ok(txt):
            # ---- 能逐行发就逐行发 ----
            # 这么做有两个好处:
            #   ① **每行都进板子的历史** —— `\r` 分支里就是 readline_push_history,
            #      所以 ↑ 能把这些行翻出来 (整段粘贴是绕开 readline 的, 翻不到)
            #   ② **最后一行不补回车** —— 它留在命令行上等用户按 Enter,
            #      和"粘单行"行为一致: 用户能先看一眼再决定跑不跑。
            #      前面几行必须补回车 —— 不补的话它们会跟最后一行黏成一行。
            lines = txt.split("\n")
            for ln in lines[:-1]:
                self.sm.write((ln + "\r").encode("utf-8"))
                time.sleep(0.02)
            self.sm.write(lines[-1].encode("utf-8"))     # ★ 末尾不加 \r, 等回车
            return "break"

        # ---- 多行: paste 模式 ----
        # paste 模式里 `\r` **不执行**, 只是"换行 + 报个 ==="; 真正执行的是 Ctrl-D。
        # 这里发 `\n` 而不是 `\r`: 收到的字节会**原样**进缓冲区当源码,
        # 而源码的行尾本就该是 `\n`。
        raw = txt.encode("utf-8")
        self.term_send(b"\x05")             # Ctrl-E 进 paste 模式
        time.sleep(0.15)                    # 等板子切过去
        try:
            # 分块 + 小睡: 板子的串口接收缓冲不大, 一口气灌进去会**丢字节** ——
            # 而丢字节是"悄悄改掉你几个字", 报个看不懂的错, 最难查
            for i in range(0, len(raw), 256):
                self.sm.write(raw[i:i + 256])
                time.sleep(0.02)
        except Exception:
            pass
        time.sleep(0.1)
        self.term_send(b"\x04")             # Ctrl-D 结束并执行 (放 try 外, 保证退出)
        return "break"

    # ---- 终端: 一个够用的小终端仿真器 ----
    # ★ 屏幕上的字符是板子**回显**回来的, 而板子的 readline 是**原地重画当前行**
    #   (靠控制码挪光标), 不是一行行往后追加。所以这里必须**解释**控制码:
    #
    #     按键        板子回什么                          含义
    #     ←           "\b"                                光标左移 (不删字符!)
    #     →           "\x1b[C"                            光标右移
    #     ↑ / ↓       "\b"×n + "\x1b[K" + 新行 + "\b"×m    擦掉本行重画
    #     Backspace   "\b" + "\x1b[K" + 剩余文本           删掉一个字符
    #
    #   两个坑:
    #     1) 裸 \b 是"移光标", **不是**"删字符"。当成删除 → 按 ← 会吞掉最后一个字。
    #     2) \x1b[.. 是控制码, 不能当文本插进去 → 否则按 → / ↑ 满屏乱码。
    #
    #   依据: shared/readline/readline.c 的 mp_hal_move_cursor_back() ——
    #   后退 <=4 步走 "\b" 快路径, 更多才发 "\x1b[<n>D"; 擦行统一发 "\x1b[K"。
    #   光标就是 Text 控件的 insert mark, 所以一切插入都往 "insert" 插。

    def _term_putc(self, c):
        """在光标处"打印"一个字符 —— **覆盖**, 不是插入。

        ★ 终端是块字符网格: 打印 = 光标处那个字符被替换掉, 然后光标右移一格。
          板子的 readline 完全建立在这条语义上, 两处都靠它:
            · **右箭头** (readline.c right_arrow_key): 它**不发 ESC[C**, 只是把
              光标下那个字符**重印一遍**来右移光标 ("draw over old chars to move
              cursor forwards")。按插入处理 → 字符被复制一份 (>>> abc → >>> abcc)。
            · **翻历史**: 只有新行**比旧行短**才擦行 (if line->len < last_line_len);
              新行更长时**不擦, 直接重印覆盖**。按插入处理 → 旧行残留跟新行拼在一起。
        """
        t = self.txt_term
        if c == "\n":
            # 换行: 光标先到行尾再落行 —— 真终端的 CR/LF 只移动光标, 不动已有文字。
            # (光标本来就在行尾时, 这两步等价于直接插一个换行)
            t.mark_set("insert", "insert lineend")
            t.insert("insert", "\n")
            return
        if t.compare("insert", "<", "insert lineend"):
            t.delete("insert", "insert + 1c")     # 覆盖掉原来的字符
        t.insert("insert", c)

    def _term_csi(self, params, final):
        """执行一条 CSI 序列 (ESC [ <参数> <终止符>)。
        只认板子真会发的那几条, **其余一律丢弃** —— 绝不能当文本插进去。"""
        t = self.txt_term
        num = int(params) if params.isdigit() else 0
        if final == "K":                       # 擦除: 光标 → 行尾
            t.delete("insert", "insert lineend")
        elif final == "D":                     # 光标左移 n
            for _ in range(max(1, num)):
                if t.compare("insert", "<=", "insert linestart"):
                    break
                t.mark_set("insert", "insert - 1c")
        elif final == "C":                     # 光标右移 n
            for _ in range(max(1, num)):
                if t.compare("insert", ">=", "insert lineend"):
                    break
                t.mark_set("insert", "insert + 1c")
        elif final == "H":                     # 行首
            t.mark_set("insert", "insert linestart")
        elif final == "F":                     # 行尾
            t.mark_set("insert", "insert lineend")
        # A/B (上下) 及其余: 板子不用, 忽略

    def term_write(self, text):
        """把设备返回的字节显示到终端 (解释控制码, 原地移动光标重画)。"""
        t = self.txt_term
        s = (self._term_esc + text).replace("\r\n", "\n").replace("\r", "\n")
        self._term_esc = ""
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == "\x1b":
                # 转义序列可能被 read 切在两块里, 收不全就留给下一次
                if i + 1 >= n:
                    self._term_esc = s[i:]
                    break
                if s[i + 1] == "[":
                    k = i + 2
                    while k < n and (s[k].isdigit() or s[k] in ";?"):
                        k += 1
                    if k >= n:
                        self._term_esc = s[i:]
                        break
                    self._term_csi(s[i + 2:k], s[k])
                    i = k + 1
                elif s[i + 1] == "O":
                    if i + 2 >= n:
                        self._term_esc = s[i:]
                        break
                    i += 3                     # ESC O <终止符>: 忽略
                else:
                    i += 2                     # 其它两字节序列: 忽略
                continue
            if c == "\b":
                # ★ 只管移光标, **不删字符** —— 要删的话板子会另发 \x1b[K
                if t.compare("insert", ">", "insert linestart"):
                    t.mark_set("insert", "insert - 1c")
            elif c == "\x07":
                pass                           # 响铃: 丢掉
            elif c == "\n" or c == "\t" or c >= " ":
                self._term_putc(c)
            # 其余控制字符: 丢弃
            i += 1
        # 行数上限, 免得长时间挂机把内存吃满
        # (insert mark 是 Tk 标记, 删头部若干行时会自动跟着上移, 不用重算)
        if int(t.index("insert").split(".")[0]) > 2000:
            t.delete("1.0", "500.0")
        t.see("insert")

    def log_repl(self, text, raw=False):
        """应用侧的消息 (状态/文件操作结果) —— 也写进终端, 换行结尾"""
        if raw:
            self.term_write(text)
        else:
            self.term_write(text + "\n")

    def clear_repl(self):
        self.txt_term.delete("1.0", "end")     # insert mark 会被 Tk 拉到 1.0
        self._term_esc = ""                    # 丢掉半截转义序列

    def set_busy(self, on):
        self.busy = on
        self.configure(cursor="watch" if on else "")
        self.btn_flash.configure(state="disabled" if on else "normal")
        self._render_dev()      # 忙的时候圆形按钮也要灰掉 (它自己看 self.busy)
        # ⚠ 连接项不能无脑恢复成 normal —— 忙完之后如果还连着, 它必须是灰的,
        #   否则会出现"已连接, 但『连接』又能点"的怪状态。
        if on:
            self.menu_tools.entryconfigure(self.mi_conn, state="disabled")
        else:
            self.menu_tools.entryconfigure(
                self.mi_conn,
                state="disabled" if self.sm.is_open else "normal")

    def need_conn(self):
        if not self.sm.is_open:
            messagebox.showwarning(APP_NAME, tr("please_connect"))
            return False
        return True

    # ------------------------------------------------------------------
    # REPL 模式回位
    # ------------------------------------------------------------------
    # ⚠ 文件操作 (列目录/上传/下载/删除/重命名) 都要进 raw REPL。做完**必须切回来**,
    #   否则板子停在 raw 模式 → 终端敲键盘被 on_term_key 丢弃 → "打字没反应/没回显"。
    #   而且这个内部切换用户从界面上看不出来 (按钮文字是点了才更新的)。
    def _back_to_repl(self):
        try:
            if self.sm.is_open and self.sm.mode == "raw":
                self.sm.enter_repl()
        except Exception:
            pass
        self._sync_mode_ui()

    def _sync_mode_ui(self):
        """界面跟着**实际**模式走 (内部切了也要反映出来)。

        「切回 REPL」按钮只在**真卡在 raw** 时才可点 —— 平时灰着, 不占注意力;
        一旦亮起来, 就是明确信号"终端现在收不到键盘输入, 点我"。
        """
        try:
            raw = self.sm.mode == "raw"
            self.btn_raw.configure(
                state="normal" if (raw and self.sm.is_open) else "disabled")
            if self.sm.is_open:
                s = tr("connected_status", port=self.sm._port)
                self.var_status.set(s + ("   [raw]" if raw else ""))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 偏好持久化 / settings
    # ------------------------------------------------------------------
    def _apply_cfg_to_widgets(self):
        """把存档里的波特率填回控件 (界面建好之后调)"""
        c = self.cfg
        if c.get("flash_baud"):
            self.cb_fbaud.set(c["flash_baud"])

    def _save_cfg(self):
        """把用户偏好写盘。存不成也不报错 —— 不该因为记偏好把程序搞崩。"""
        cfg_save({
            "lang": _LANG_ID,
            "fw_dir": self.fw_dir,
            "local_dir": self.local_dir,
            "port": self._port_name(),
            "flash_baud": self.cb_fbaud.get(),
        })

    def on_close(self):
        self._save_cfg()
        self._stop_reader()
        # ⚠ 两个定时器都要取消。**漏掉 _drain 的话**关窗后它还在队列里,
        #   一触发就打在被销毁的控件上 → Tcl 报 "invalid command name ..._drain"。
        #   打包成 --windowed 后没有控制台, 这个报错完全看不见, 所以只能靠这里堵。
        for tid in (self._port_timer, self._drain_id):
            try:
                if tid:
                    self.after_cancel(tid)
            except Exception:
                pass
        self.destroy()

    def run_bg(self, fn, *a, **kw):
        """把耗时操作丢到后台线程, 异常统一报给 UI"""
        def wrap():
            try:
                fn(*a, **kw)
            except Exception as e:
                self.post("msgbox_err", text="%s" % e)
                self.post("status", text=tr("failed", msg=e))
            finally:
                # ★ 所有后台操作结束**都必须**把终端收回普通 REPL。
                #   收在这里而不是各操作自己末尾, 是因为**出错路径也得走到**:
                #   文件操作失败时若停在 raw 模式, 键盘输入会被忽略 → 终端"哑"了,
                #   而那正是用户最需要敲字排查的时候。
                #   放这儿还有个好处: 新增操作不可能忘。
                #   _back_to_repl 对"本来就不在 raw"是空操作, 串口已关也安全。
                self.post("back_to_repl")
                self.post("busy", on=False)
        self.set_busy(True)
        threading.Thread(target=wrap, daemon=True).start()

    # ------------------------------------------------------------------
    # 连接 / connect
    # ------------------------------------------------------------------
    # ---- 串口列表 ----
    # 插拔板子时列表要自己跟上: 新插的冒出来, **拔掉的消失**。
    # 所以拆成两个入口 —— 手动按钮要报状态栏, 定时轮询**绝不能碰状态栏**
    # (否则每隔一会儿就把"已连接 COM14"顶成"找到 3 个端口")。

    PORT_POLL_MS = 1500

    def _scan_ports(self):
        """扫一遍系统串口, 返回下拉里显示用的字符串列表"""
        ports = []
        for p in serial.tools.list_ports.comports():
            desc = p.description or ""
            ports.append("%s  (%s)" % (p.device, desc) if desc else p.device)
        return ports

    def _apply_ports(self, ports):
        """把扫到的列表填进「工具 → 串口」子菜单, 并决定选中哪个。

        ★ 选中项优先级 (这套规则踩过坑, 别改): 当前选中的 > 存档里的 > 第一个。
          端口号换 USB 口/重启后会变, 所以一律按**端口名**匹配, 不按序号。
          当前选中的口**拔掉了就空着**, 不跳到别的口上去 —— 否则插回来时已经停在
          别的口上了, 用户还得手动找回来。
        """
        had = self._port_name()                    # 当前选中的口
        names = [v.split()[0] for v in ports]
        self.var_ports = list(ports)

        gone = bool(had) and had not in names
        pick = None
        for want in ("" if gone else had, self.cfg.get("port") or ""):
            if want and want in names:
                pick = want
                break
        if pick is None and not had and names:
            pick = names[0]                        # 本来就没选过 → 第一个
        self.var_port.set(next((v for v in ports if v.split()[0] == pick), "") if pick
                          else "")                 # 没得选 → 清空, 不留拔掉的口
        self._rebuild_port_menu()
        self._render_dev()

    def _rebuild_port_menu(self):
        """按 self.var_ports 重建「串口」子菜单 + 同步条带上那个框。

        Arduino 也是这两个入口并存 (工具栏下拉 + 工具→端口), 共用同一份选中值。
        """
        try:
            self.cb_port["values"] = self.var_ports
            self.cb_port.set(self.var_port.get())
        except Exception:
            pass
        m = self.menu_ports
        m.delete(0, "end")
        if not self.var_ports:
            m.add_command(label=tr("no_ports"), state="disabled")
            return
        for v in self.var_ports:
            m.add_radiobutton(label=v, value=v, variable=self.var_port,
                              command=self._on_pick_port)
        m.add_separator()
        m.add_command(label=tr("refresh_ports"), accelerator="F5",
                      command=self.refresh_ports)

    def _on_combo_port(self, _evt=None):
        """条带上的串口框换了选择"""
        val = self.cb_port.get()
        if val:
            self.var_port.set(val)
        self._on_pick_port()

    def _on_pick_port(self):
        """换串口 (菜单或条带上的框都走这里)"""
        self._render_dev()
        self._save_cfg()

    def _render_dev(self):
        """状态栏左侧常驻: 串口 · 波特率 · 连接状态"""
        port = self._port_name() or tr("no_ports")
        try:
            linked = self.sm.is_open
        except Exception:
            linked = False
        for w, fn in ((getattr(self, "lbl_dot", None), None),
                      (getattr(self, "btn_connect", None), "conn"),
                      (getattr(self, "btn_disconnect", None), "disc")):
            if w is None:
                continue
            try:
                if fn is None:
                    w.configure(fg=C_WIDGET if linked else C_DANGER)
                elif fn == "conn":
                    w.set_enabled(not linked and not self.busy)
                else:
                    w.set_enabled(linked and not self.busy)
                    self.wa.set_online(linked)
            except Exception:
                pass
        self.var_devline.set("%s %s  %d  %s   %s %s" % (
            tr("port_menu"), port, REPL_BAUD,
            tr("link_up") if linked else tr("not_connected"),
            tr("chip"), self.detected_chip or tr("chip_unknown")))

    def refresh_ports(self):
        """手动刷新 (按钮): 更新列表 + 报状态栏"""
        ports = self._scan_ports()
        self._apply_ports(ports)
        self.var_status.set(tr("ports_found", n=len(ports)))

    def _auto_ports(self):
        """定时轮询: 只更新列表和选中项, **不动状态栏**"""
        try:
            self._apply_ports(self._scan_ports())
        except Exception:
            pass                                   # 窗口正在销毁等, 忽略
        try:
            self._port_timer = self.after(self.PORT_POLL_MS, self._auto_ports)
        except Exception:
            pass

    def _port_name(self):
        raw = (self.var_port.get() or "").strip()
        return raw.split()[0] if raw else ""

    def _repl_baud(self):
        """REPL 连接的波特率 —— **固定 REPL_BAUD, 不给用户改**。

        为什么不做成可选: MicroPython 的 REPL 波特率不是用户程序决定的 (Arduino 的
        `Serial.begin(9600)` 才是那种), 它在**固件编译时**就定死了 (ESP32 端口的
        CONFIG_ESP_CONSOLE_UART_BAUDRATE, bPuppy 是 115200)。所以它跟 Arduino 的
        "上传速度"同类 (板子属性, 界面不给改), 而不是"串口监视器波特率"。
        原先是终端页顶部一个下拉, 已删 —— 改成设置项没意义, 一年也动不到一次。
        """
        return REPL_BAUD

    def on_connect(self):
        port = self._port_name()
        if not port:
            messagebox.showwarning(APP_NAME, tr("no_ports"))
            return

        repl_baud = self._repl_baud()

        def work():
            self.post("status", text=tr("connecting", port=port))
            sm = self.sm
            sm.open(port, repl_baud)
            sm.interrupt()                      # 打断, 拿干净提示符
            time.sleep(0.3)
            sm.read_avail()
            sm.write(b"\r")
            greet = sm.read_until(b">>>", timeout=2.0)
            self.post("repl", text=tr("connected", port=port, baud=repl_baud))
            if greet:
                self.post("repl_raw", text=greet.decode("utf-8", "replace"))
            else:
                self.post("repl", text=tr("no_prompt"))
            # 有没有 `>>>` = 板子上有没有 MicroPython。**文件管理靠它**:
            # 没有的话文件页那几个按钮得置灰 (见 _sync_file_ui)。
            # 必须**在 post("connected") 之前**发 —— 那个会触发 _after_connect,
            # 里面就要用这个值了。
            #
            # ⚠ 判据是 `>>> in greet`, **不是** `bool(greet)`:
            #   read_until 超时返回的是**读到的全部内容**, 不是"找到没找到"。
            #   板子还在开机时它返回的是开机日志 (非空) —— 用 bool() 判会得
            #   到"有 MicroPython", 于是文件按钮亮起、refresh_device 往下走、
            #   enter_raw 失败, 弹一句莫名其妙的 "cannot enter raw REPL"。
            #   (对照 enter_raw 里的正确用法: `if b"raw REPL" in got`)
            self.post("repl_ok", ok=(b">>>" in greet))
            self.post("connected")

        self.run_bg(work)

    def _after_connect(self):
        self.menu_tools.entryconfigure(self.mi_conn, state="disabled")
        self.menu_tools.entryconfigure(self.mi_disc, state="normal")
        self.var_status.set(tr("connected_status", port=self.sm._port))
        self._render_dev()                 # 状态圆点: 红 → 白
        self._start_reader()               # 终端实时回显
        self.txt_term.focus_set()
        self._sync_mode_ui()
        # 还不知道是什么芯片 → 提示一句去哪儿读。
        # 连接本身**读不到** (要读 ROM 就得复位板子), 所以这里只能指个路 ——
        # 不然用户只看到「未识别」, 不知道怎么把它变成有值。
        if not self.detected_chip:
            self.log_repl(tr("chip_hint"))
        self.refresh_device()
        self.refresh_local()

    def on_disconnect(self):
        try:
            if self.sm.is_open and self.sm.mode == "raw":
                self.sm.enter_repl()
        except Exception:
            pass
        self.sm.close()
        self._after_disconnect()

    def _after_disconnect(self):
        self._stop_reader()
        self.menu_tools.entryconfigure(self.mi_conn, state="normal")
        self.menu_tools.entryconfigure(self.mi_disc, state="disabled")
        self.var_status.set(tr("disconnected"))
        self._render_dev()
        self.tv_dev.delete(*self.tv_dev.get_children())
        self._sync_mode_ui()               # 没连接 → 「切回 REPL」置灰
        self._sync_file_ui()               # 没连接 → 文件管理置灰

    # ------------------------------------------------------------------
    # REPL
    # ------------------------------------------------------------------
    # 终端读取线程: 连接期间**持续**读串口, 把设备输出实时显示出来。
    # 走到 raw_exec / read_until 这类一问一答的操作时让路 (sm.busy_io),
    # 否则会把响应抢走导致那边超时。
    def _start_reader(self):
        if self._reader_on:
            return
        self._reader_on = True
        threading.Thread(target=self._reader_loop, daemon=True).start()

    def _stop_reader(self):
        self._reader_on = False

    # 终端回显的**最小间隔** (秒): 读到数据后至少歇这么久再读下一次。
    # ★ 没有它会出事 —— 原生 USB 的虚拟串口能跑到 Mbps 级 (比 115200 快两个数量
    #   级), 读线程会以串口速度往队列里灌消息, 而主线程每块都要逐字符解析再加
    #   一次 t.index()/t.see() (全是 Tcl 调用), 根本处理不完 → 队列越积越多 →
    #   _drain 卡死 → 窗口一动不动。节流后最坏是丢掉一部分显示, 不会卡界面。
    READ_MIN_INTERVAL = 0.03

    def _reader_loop(self):
        # ⚠ 循环条件**只能看 _reader_on**, 不能带 self.sm.is_open:
        #   烧写时会主动关掉串口 (让给 esptool), 那一刻 is_open 变 False,
        #   若把它写进 while 条件, 线程会直接退出 —— 烧完重开串口后
        #   终端就再也不刷新了。所以串口没开时只是跳过这一轮, 线程留着。
        while self._reader_on:
            if not self.sm.is_open or self.sm.busy_io:
                time.sleep(0.05)
                continue
            try:
                data = self.sm.read_avail()
            except Exception:
                time.sleep(0.05)
                continue
            if data:
                self.post("term", text=data.decode("utf-8", "replace"))
                time.sleep(self.READ_MIN_INTERVAL)      # 节流, 见上面的说明
            else:
                time.sleep(0.02)

    def on_interrupt(self):
        if not self.need_conn():
            return
        try:
            self.sm.interrupt()
            self.log_repl("\n" + tr("log_ctrl_c"))
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def on_recover_repl(self):
        """把终端从 raw REPL 收回普通 REPL (手动兜底出口)

        正常情况**不该用到** —— 文件操作收尾由 `run_bg` 的 finally 统一负责。
        用户需要点这个按钮, 就说明哪里漏了。
        """
        if not self.need_conn():
            return
        try:
            if self.sm.mode == "raw":
                self.sm.enter_repl()
                self.log_repl(tr("log_to_repl"))
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))
        self._sync_mode_ui()

    # ------------------------------------------------------------------
    # 文件: 本地 / local files
    # ------------------------------------------------------------------
    def refresh_local(self):
        d = self.var_local.get().strip() or os.getcwd()
        if not os.path.isdir(d):
            return
        self.local_dir = d
        items = []
        # 不在根目录时给一个"上级目录"入口 (双击 ../ 上去)
        up = os.path.dirname(os.path.abspath(d))
        if up and up != os.path.abspath(d):
            items.append(("../", None))
        try:
            for n in sorted(os.listdir(d), key=str.lower):
                full = os.path.join(d, n)
                if os.path.isdir(full):
                    items.append((n + "/", None))
                else:
                    try:
                        items.append((n, os.path.getsize(full)))
                    except OSError:
                        items.append((n, None))
        except OSError as e:
            self.log_repl(tr("local_dir_err", msg=e))
        self.post("files_local", items=items)

    def _fill_local(self, items):
        self.tv_local.delete(*self.tv_local.get_children())
        for name, size in items:
            self.tv_local.insert("", "end", values=(
                name, "" if size is None else self.fmt_size(size)))

    def on_pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.local_dir)
        if d:
            self.var_local.set(d)
            self.refresh_local()
            self._save_cfg()          # local_dir 被 refresh_local 更新了

    def on_local_open(self, _evt=None):
        sel = self.tv_local.selection()
        if not sel:
            return
        name = self.tv_local.item(sel[0], "values")[0]
        # 注意先判 "../": 它也以 "/" 结尾, 顺序反了会被当成要进去的目录
        if name == "../":
            self.var_local.set(os.path.dirname(os.path.abspath(self.local_dir)))
            self.refresh_local()
        elif name.endswith("/"):
            self.var_local.set(os.path.join(self.local_dir, name.rstrip("/")))
            self.refresh_local()

    # ------------------------------------------------------------------
    # 文件: 设备 / device files
    # ------------------------------------------------------------------
    def refresh_device(self):
        if not self.need_conn():
            return
        if not self.repl_ok:
            return                  # 不是 MicroPython, 没有文件系统可列
        path = self.var_dev.get().strip() or "/"

        def work():
            self.post("status", text=tr("reading_dir", path=path))
            items = dev_list(self.sm, path)
            self.post("files_dev", items=items)
            self.post("status", text=tr("dir_items", path=path, n=len(items)))

        self.run_bg(work)

    def _fill_dev(self, items):
        self.tv_dev.delete(*self.tv_dev.get_children())
        self.tv_dev.insert("", "end", values=("../", ""))
        for name, size in items:
            self.tv_dev.insert("", "end", values=(
                name, "" if size is None else self.fmt_size(size)))

    def on_dev_open(self, _evt=None):
        sel = self.tv_dev.selection()
        if not sel:
            return
        name = self.tv_dev.item(sel[0], "values")[0]
        if name == "../":
            cur = self.var_dev.get().strip().rstrip("/")
            self.var_dev.set(os.path.dirname(cur) or "/")
            self.refresh_device()

    def _dev_selected(self):
        sel = self.tv_dev.selection()
        return [self.tv_dev.item(s, "values")[0] for s in sel
                if self.tv_dev.item(s, "values")[0] != "../"]

    def _local_selected(self):
        sel = self.tv_local.selection()
        return [self.tv_local.item(s, "values")[0] for s in sel]

    # ------------------------------------------------------------------
    # 上传 / 下载 / 删除 / 重命名
    # ------------------------------------------------------------------
    def on_upload(self):
        if not self.need_conn():
            return
        names = self._local_selected()
        if not names:
            messagebox.showwarning(APP_NAME, tr("select_local_first"))
            return
        ddir = self.var_dev.get().strip().rstrip("/") or ""

        def work():
            for name in names:
                if name.endswith("/"):
                    continue
                src = os.path.join(self.local_dir, name)
                dst = ddir + "/" + name
                size = os.path.getsize(src)
                self.post("status", text=tr("uploading", name=name, size=size))
                data = open(src, "rb").read()
                dev_write(self.sm, dst, data,
                          on_progress=lambda c, t: self.post(
                              "pb_text",
                              text=tr("uploading_pct", name=name, pct=100.0 * c / t)))
                self.post("repl", text=tr("log_upload_ok", name=name, dst=dst))
            self.post("status", text=tr("upload_done"))
            self.post("files_dev", items=dev_list(self.sm, self.var_dev.get()))

        self.run_bg(work)

    def on_download(self):
        if not self.need_conn():
            return
        names = self._dev_selected()
        if not names:
            messagebox.showwarning(APP_NAME, tr("select_dev_first"))
            return
        ddir = self.var_dev.get().strip().rstrip("/") or ""

        def work():
            for name in names:
                src = ddir + "/" + name
                dst = os.path.join(self.local_dir, name)
                self.post("status", text=tr("downloading", name=name))
                data = dev_read(self.sm, src,
                                on_progress=lambda c, t: self.post(
                                    "pb_text",
                                    text=tr("downloading_pct", name=name,
                                            pct=100.0 * c / t)))
                with open(dst, "wb") as f:
                    f.write(data)
                self.post("repl", text=tr("log_download_ok", src=src, dst=dst,
                                          n=len(data)))
            self.post("status", text=tr("download_done"))
            self.post("refresh_local")          # 下载完刷新本地列表

        self.run_bg(work)

    def on_delete(self):
        if not self.need_conn():
            return
        names = self._dev_selected()
        if not names:
            messagebox.showwarning(APP_NAME, tr("select_dev_first"))
            return
        if not messagebox.askyesno(
                APP_NAME, tr("confirm_delete", n=len(names),
                             names="\n".join(names))):
            return
        ddir = self.var_dev.get().strip().rstrip("/") or ""

        def work():
            for name in names:
                if dev_remove(self.sm, ddir + "/" + name):
                    self.post("repl", text=tr("log_delete_ok", name=name))
                else:
                    self.post("repl", text=tr("log_delete_fail", name=name))
            self.post("status", text=tr("delete_done"))
            self.post("files_dev", items=dev_list(self.sm, self.var_dev.get()))

        self.run_bg(work)

    def on_rename(self):
        if not self.need_conn():
            return
        names = self._dev_selected()
        if len(names) != 1:
            messagebox.showwarning(APP_NAME, tr("select_one"))
            return
        old = names[0]
        dlg = tk.Toplevel(self)
        dlg.title(tr("rename_title"))
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg, text=tr("new_name")).pack(padx=px(12), pady=(px(12), px(4)))
        ent = ttk.Entry(dlg, width=36)
        ent.pack(padx=12)
        ent.insert(0, old)
        ent.focus_set()

        def ok():
            new = ent.get().strip()
            dlg.destroy()
            if not new or new == old:
                return
            ddir = self.var_dev.get().strip().rstrip("/") or ""

            def work():
                if dev_rename(self.sm, ddir + "/" + old, ddir + "/" + new):
                    self.post("repl", text=tr("log_rename_ok", old=old, new=new))
                else:
                    self.post("repl", text=tr("log_rename_fail"))
                self.post("files_dev", items=dev_list(self.sm, self.var_dev.get()))
            self.run_bg(work)

        ent.bind("<Return>", lambda e: ok())
        ttk.Button(dlg, text=tr("ok"), style="Plain.TButton", command=ok).pack(pady=10)

    # ------------------------------------------------------------------
    # 烧写 / flashing
    # ------------------------------------------------------------------
    def on_add_fw(self):
        start = self.fw_dir if os.path.isdir(self.fw_dir) else self.local_dir
        paths = filedialog.askopenfilenames(
            title=tr("pick_fw_title"),
            filetypes=[(tr("ft_firmware"), "*.bin"), (tr("ft_all"), "*.*")],
            initialdir=start)
        if paths:
            # 记住这次选的目录 —— 下次开对话框直接落到这里。
            # 取第一个文件的所在目录 (多选时通常都在同一个目录)。
            self.fw_dir = os.path.dirname(paths[0])
            self._save_cfg()
        have = {self.tv_fw.item(i, "values")[1]
                for i in self.tv_fw.get_children()}
        for p in paths:
            if p in have:
                continue
            addr = guess_addr(p)
            self.tv_fw.insert("", "end", values=(
                "0x%X" % addr, p, self.fmt_size(os.path.getsize(p))))

    def on_del_fw(self):
        for i in self.tv_fw.selection():
            self.tv_fw.delete(i)

    def on_clear_fw(self):
        self.tv_fw.delete(*self.tv_fw.get_children())

    def on_edit_addr(self, _evt=None):
        sel = self.tv_fw.selection()
        if not sel:
            return
        item = sel[0]
        cur = self.tv_fw.item(item, "values")[0]

        dlg = tk.Toplevel(self)
        dlg.title(tr("addr_title"))
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg, text=tr("addr_label")).pack(padx=px(12), pady=(px(12), px(4)))
        ent = ttk.Entry(dlg, width=24)
        ent.pack(padx=12)
        ent.insert(0, cur)
        ent.focus_set()

        def ok():
            txt = ent.get().strip()
            dlg.destroy()
            try:
                v = int(txt, 16)
                self.tv_fw.set(item, "addr", "0x%X" % v)
            except ValueError:
                messagebox.showerror(APP_NAME, tr("addr_bad", txt=txt))

        ent.bind("<Return>", lambda e: ok())
        ttk.Button(dlg, text=tr("ok"), style="Plain.TButton", command=ok).pack(pady=10)

    def on_read_chip(self):
        """走 ROM bootloader 读芯片信息。

        **任何 ESP 都能读** —— ROM 是出厂烧在硅片里的, 板子上跑 MicroPython、
        裸机、还是出厂空白, 都一样。这是"针对所有 ESP 芯片"的那条路。

        ⚠ 代价: 芯片要**复位进下载模式**再复位回来, 上面跑的程序会重启。
          所以必须用户主动点 —— 不能做成连接时自动做 (那等于每次连板子都重启它)。
        """
        if esptool is None:
            messagebox.showerror(APP_NAME, tr("no_esptool"))
            return
        port = self._port_name()
        if not port:
            messagebox.showwarning(APP_NAME, tr("no_ports"))
            return
        if not messagebox.askyesno(APP_NAME, tr("read_chip_ask")):
            return

        repl_baud = self._repl_baud()
        self.txt_flash.delete("1.0", "end")
        self.pb["value"] = 0

        def log(t):
            self.post("flash_log", text=t)

        def work():
            # ---- 和烧写一样: 先让出串口给 esptool ----
            was_connected = self.sm.is_open
            if was_connected:
                try:
                    if self.sm.mode == "raw":
                        self.sm.enter_repl()
                except Exception:
                    pass
                self.sm.close()
                log(tr("fl_release"))
                time.sleep(0.6)

            BridgeLogger.sink = log
            BridgeLogger.progress = None
            try:
                rom_baud = esptool.ESPLoader.ESP_ROM_BAUD
                with esptool.cmds.detect_chip(port, rom_baud,
                                              connect_mode="default-reset") as esp:
                    lines = chip_detail_lines(esp)
                    for _ln in lines:
                        log(_ln)
                    if not lines:
                        log(tr("fl_chip", name=esp.CHIP_NAME))
                    self.post("chip_name", name=esp.CHIP_NAME)
                    log("")
                    # 上 stub 才能快速读 flash (否则 read_flash_slow, 3KB 好几秒)
                    esp = esptool.cmds.run_stub(esp)
                    try:
                        parts = read_partitions_rom(esp)
                        if parts:
                            log(tr("part_info", text=" · ".join(
                                "%s %s" % (lb, self.fmt_size(sz))
                                for lb, sz in parts)))
                            log("")
                    except Exception:
                        pass
                    log(tr("fl_reset"))
                    esptool.cmds.reset_chip(esp, "hard-reset")
            except Exception as e:
                log(tr("fl_failed", msg=e))
            finally:
                BridgeLogger.sink = None
                # ---- 恢复 REPL 连接 (和烧写收尾一致) ----
                if was_connected:
                    try:
                        time.sleep(1.2)
                        self.sm.open(port, repl_baud)
                        self.sm.interrupt()
                        time.sleep(0.3)
                        self.sm.read_avail()
                        log(tr("fl_reopen"))
                    except Exception as e:
                        log(tr("fl_reopen_fail", msg=e))

        self.run_bg(work)

    def on_flash(self):
        if esptool is None:
            messagebox.showerror(APP_NAME, tr("no_esptool"))
            return
        rows = self.tv_fw.get_children()
        if not rows:
            messagebox.showwarning(APP_NAME, tr("no_fw"))
            return
        addr_data = []
        for i in rows:
            addr_s, path, _ = self.tv_fw.item(i, "values")
            if not os.path.isfile(path):
                messagebox.showerror(APP_NAME, tr("file_missing", path=path))
                return
            addr_data.append((int(addr_s, 16), path))

        port = self._port_name()
        if not port:
            messagebox.showwarning(APP_NAME, tr("no_ports"))
            return
        do_erase = self.var_erase.get()
        baud = int(self.cb_fbaud.get())
        repl_baud = self._repl_baud()      # 主线程先取好; worker 里不碰控件

        self.txt_flash.delete("1.0", "end")
        self.pb["value"] = 0

        def log(t):
            self.post("flash_log", text=t)

        def work():
            # ---- 串口互斥: esptool 要自己开串口, 先让出 ----
            was_connected = self.sm.is_open
            if was_connected:
                try:
                    if self.sm.mode == "raw":
                        self.sm.enter_repl()
                except Exception:
                    pass
                self.sm.close()
                log(tr("fl_release"))
                # 关掉自己的串口后等一下再让 esptool 打开: 关闭瞬间 DTR/RTS 会掉,
                # 复位/BOOT 电路的电容需要时间恢复, 紧接着重开可能让芯片停在半复位
                # 状态。命令行没这一步 (它自始至终没开过串口)。
                # ⚠ 这只是保险, **不是** "Serial data stream stopped" 的解药 ——
                #   那个的真因是提速时机, 见下面的 ①②③。
                time.sleep(0.6)

            BridgeLogger.sink = log
            BridgeLogger.progress = lambda c, t: self.post("progress", cur=c, total=t)

            try:
                # 日志里**不报芯片**了 —— 型号交给 esptool 自己认, 它紧接着就会
                # 打一行"芯片: ESP32-S3"(来自 esp.CHIP_NAME)。这里再报一个可能不
                # 准的, 反而是噪音。
                log(tr("fl_connect", baud=baud))
                # ★ 三段顺序一步都不能动, 错一步就是
                #   "Serial data stream stopped: Possible serial noise or corruption."
                #     ① 用 ROM 默认波特率 (115200) 连接
                #     ② 上 stub flasher 并让它跑起来
                #     ③ **最后**才提速
                #
                #   ① 依据 esptool/__init__.py:504 ——
                #        initial_baud = min(ESPLoader.ESP_ROM_BAUD, baud)
                #        # don't sync faster than the default baud rate
                #      直接 detect_chip(port, 921600) = 拿 921600 去同步握手。
                #
                #   ②③ 依据 esptool/__init__.py:592 / :600 的官方顺序 ——
                #        # 4) Upload the stub flasher
                #        esp = run_stub(esp, ...)
                #        # 5) Configure the baud rate
                #        esp.change_baud(baud)
                #      ROM bootloader 的 CHANGE_BAUDRATE **只改分频**, 不保证高速下
                #      还能稳定传数据: 命令本身能 ACK, 但紧接着传 stub 数据就断。
                #      stub 的实现才是为高速设计的 —— 所以提速必须等 stub 跑起来。
                rom_baud = esptool.ESPLoader.ESP_ROM_BAUD
                with esptool.cmds.detect_chip(port, rom_baud,
                                              connect_mode="default-reset") as esp:
                    # 芯片信息: **全打出来**, 和 esptool 命令行一个排版。
                    # 这些都是从 ROM 读的, 跟板子上跑什么固件无关 —— 裸机板一样有。
                    chip_lines = chip_detail_lines(esp)
                    if chip_lines:
                        for _ln in chip_lines:
                            log(_ln)
                        self.post("chip_name", name=esp.CHIP_NAME)
                    else:
                        log(tr("fl_chip", name=esp.CHIP_NAME))   # 兜底
                    log("")
                    # 分区表: stub 起来之后读才快 (没 stub 会走 read_flash_slow, 3KB 好几秒)
                    esp = esptool.cmds.run_stub(esp)    # ② 先上 stub
                    log(tr("fl_stub"))
                    try:
                        _parts = read_partitions_rom(esp)
                        if _parts:
                            log(tr("part_info", text=" · ".join(
                                "%s %s" % (lb, self.fmt_size(sz)) for lb, sz in _parts)))
                            log("")
                    except Exception:
                        pass
                    if baud > rom_baud:                 # ③ 再提速
                        try:
                            esp.change_baud(baud)
                            log(tr("fl_baud_up", baud=baud))
                        except esptool.NotImplementedInROMError:
                            log(tr("fl_baud_rom", baud=rom_baud))
                    if do_erase:
                        log(tr("fl_erasing"))
                        self.post("pb_text", text=tr("fl_erasing_pb"))
                        esptool.cmds.erase_flash(esp, force=True)
                        log(tr("fl_erase_done"))
                    log(tr("fl_writing", n=len(addr_data)))
                    esptool.cmds.write_flash(esp, addr_data, force=True)
                    log(tr("fl_verify"))
                    esptool.cmds.verify_flash(esp, addr_data)
                    log(tr("fl_reset"))
                    esptool.cmds.reset_chip(esp, "hard-reset")
                log(tr("fl_done"))
                self.post("pb_text", text=tr("fl_done_pb"))
                self.post("status", text=tr("fl_done_pb"))
            except Exception as e:
                # ★ 失败也必须写进日志框。只弹 messagebox 的话, 用户关掉弹窗后
                #   日志里只剩一句 "[串口已重新打开]", 看着像烧成功了。
                #   esptool 的异常信息 (如 Serial data stream stopped) 就是
                #   唯一能定位问题的线索, 不能丢。
                log(tr("fl_failed", msg=e))
                self.post("pb_text", text=tr("fl_failed", msg=e).split(":")[0])
                raise
            finally:
                BridgeLogger.sink = None
                BridgeLogger.progress = None
                # ---- 恢复 REPL 连接 ----
                if was_connected:
                    try:
                        time.sleep(1.2)
                        self.sm.open(port, repl_baud)   # 用 REPL 面板选的波特率
                        self.sm.interrupt()
                        time.sleep(0.3)
                        self.sm.read_avail()
                        log(tr("fl_reopen"))
                    except Exception as e:
                        log(tr("fl_reopen_fail", msg=e))

        self.run_bg(work)

    # ------------------------------------------------------------------
    @staticmethod
    def fmt_size(n):
        if n is None:
            return ""
        for u in ("B", "K", "M"):
            if n < 1024:
                return "%d %s" % (n, u) if u == "B" else "%.1f %s" % (n, u)
            n /= 1024.0
        return "%.1f G" % n


def main():
    if esptool is None:
        print("warning: esptool not installed, flashing disabled "
              "/ 警告: 没装 esptool, 烧写功能不可用")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
