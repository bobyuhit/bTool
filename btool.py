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
        "language": "语言:",
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
        "not_connected": "● 未连接",
        "linked": "● 已连接 {port} @{baud}",
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
    },

    "en": {
        "lang_name": "English",
        "title": "bTool — ESP32 Tool",

        "connection": "Connection",
        "port": "Port:",
        "refresh": "Refresh",
        "chip": "Chip:",
        "language": "Language:",
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
        "not_connected": "● Not connected",
        "linked": "● Connected {port} @{baud}",
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

        self.title(tr("title"))
        set_window_icon(self)              # 标题栏 / 任务栏图标
        self.geometry("1140x840")
        self.minsize(980, 700)

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
    def _build_ui(self):
        st = ttk.Style()
        for th in ("vista", "clam"):
            try:
                st.theme_use(th)
                break
            except Exception:
                continue

        # ---- 顶部: 连接栏 / top bar ----
        top = ttk.LabelFrame(self, text=tr("connection"), padding=8)
        top.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(top, text=tr("port")).grid(row=0, column=0, sticky="w")
        self.cb_port = ttk.Combobox(top, width=22, state="readonly")
        self.cb_port.grid(row=0, column=1, padx=(4, 6))

        ttk.Button(top, text=tr("refresh"), width=7,
                   command=self.refresh_ports).grid(row=0, column=2)

        ttk.Label(top, text=tr("chip")).grid(row=0, column=3,
                                             padx=(12, 0), sticky="w")
        # ★ **只读显示, 不给手选** —— 芯片型号是连上后探测出来的, 手选没有意义:
        #   ① 这个值**不驱动任何行为**。烧写时 esptool 自己会认芯片
        #      (`esp.CHIP_NAME` → 日志那行"芯片: ESP32-S3"), 当年手选的值
        #      唯一去处就是日志里那句"连接芯片 (xxx)" —— 纯装饰, 而且**会说谎**。
        #   ② 手选本来是给"读不到芯片"当后备的, 但那种情况本来也不该烧写,
        #      留个能选的框只会让人以为自己选对了。
        self.detected_chip = ""            # 连上探测到的, 空 = 还没认出来
        self.repl_ok = False               # 连上的板子有 MicroPython 吗 (决定文件管理能不能用)
        self.lbl_chip = ttk.Label(top, width=11, foreground="#888")
        self.lbl_chip.grid(row=0, column=4, padx=(4, 6), sticky="w")
        self._render_chip()

        ttk.Label(top, text=tr("language")).grid(row=0, column=5,
                                                 padx=(8, 0), sticky="w")
        self.cb_lang = ttk.Combobox(
            top, width=8, state="readonly",
            values=[LANG[k]["lang_name"] for k in ("zh", "en")])
        self.cb_lang.set(LANG[_LANG_ID]["lang_name"])
        self.cb_lang.grid(row=0, column=6, padx=(4, 6))
        self.cb_lang.bind("<<ComboboxSelected>>", self.on_lang_change)

        self.btn_conn = ttk.Button(top, text=tr("connect"), width=10,
                                   command=self.on_connect)
        self.btn_conn.grid(row=0, column=7, padx=(12, 4))
        self.btn_disc = ttk.Button(top, text=tr("disconnect"), width=10,
                                   command=self.on_disconnect, state="disabled")
        self.btn_disc.grid(row=0, column=8)

        # ★ 连接状态指示灯 —— 光靠按钮灰显/状态栏小字太不显眼, 这里给一个
        #   带颜色的粗体指示。用 tk.Label 而不是 ttk.Label: 某些 ttk 主题
        #   会忽略 foreground。
        self.lbl_conn = tk.Label(top, text=tr("not_connected"),
                                 fg="#c00000", font=("", 10, "bold"))
        self.lbl_conn.grid(row=0, column=9, padx=(18, 0), sticky="w")

        # ---- 中部: 三个选项卡 —— 烧写 / REPL 终端 / 文件管理 ----
        # 三者互不干扰, 各自占满整个区域。原来是"烧写在上、REPL 常驻在下",
        # 挤在同一屏里 → 两个都变小, 而且 REPL 还要和烧写页抢竖向空间。
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(4, 4))
        self.nb = nb

        self.tab_flash = ttk.Frame(nb)
        self.tab_repl = ttk.Frame(nb)
        self.tab_files = ttk.Frame(nb)
        nb.add(self.tab_flash, text=tr("tab_flash"))
        nb.add(self.tab_repl, text=tr("tab_repl"))
        nb.add(self.tab_files, text=tr("tab_files"))

        self._build_flash_tab()
        self._build_repl_tab()
        self._build_files_tab()

        # 切到 REPL 页时把键盘焦点交给终端 —— 否则光标不闪、敲字没反应
        # (Text 控件只在有焦点时才显示插入光标)
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # ---- 状态栏 / status bar ----
        # 「关于」放这儿: 连接栏那一排已经很挤了, 而关于是偶尔点一次的东西。
        # 先 pack 按钮再 pack 状态文字 —— 否则文字会把整行占满, 按钮被挤没。
        self.var_status = tk.StringVar(value=tr("ready"))
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        ttk.Separator(bar, orient="horizontal").pack(fill="x")
        row = ttk.Frame(bar)
        row.pack(fill="x")
        ttk.Button(row, text=tr("about"), width=7,
                   command=self.on_about).pack(side="right", padx=(4, 8), pady=2)
        ttk.Label(row, textvariable=self.var_status,
                  anchor="w").pack(side="left", fill="x", expand=True,
                                   padx=8, pady=3)

    def _on_tab_changed(self, _evt=None):
        try:
            if self.nb.select() == str(self.tab_repl):
                self.txt_term.focus_set()
        except Exception:
            pass

    # ---- REPL 页 / repl tab ----
    def _build_repl_tab(self):
        p = self.tab_repl

        # REPL 自己的波特率 —— 跟"烧写波特率"是两回事:
        #   烧写波特率只在烧写时用 (esptool), REPL 波特率在连接设备时用。
        #   两者可以不同 (例: 烧写 921600 求快, REPL 用固件实际的 115200)。
        rb = ttk.Frame(p)
        rb.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(rb, text=tr("repl_baud")).pack(side="left")
        self.cb_replbaud = ttk.Combobox(
            rb, width=10, state="readonly",
            values=["115200", "230400", "460800", "921600"])
        self.cb_replbaud.set(str(REPL_BAUD))
        self.cb_replbaud.pack(side="left", padx=(4, 6))
        ttk.Label(rb, text=tr("repl_baud_hint"),
                  foreground="#666").pack(side="left")

        # ★ 终端区 —— **可直接在里面敲**, 没有单独的输入框。
        #   按键不本地插入, 而是原样发给板子; 屏幕上看到的字符是板子**回显**
        #   回来的 (MicroPython 的友好 REPL 会回显输入)。两处都插就会重影。
        self.txt_term = tk.Text(p, height=18, wrap="char",
                                bg="#1e1e1e", fg="#d4d4d4",
                                insertbackground="#d4d4d4",
                                selectbackground="#264f78",
                                font=("Consolas", 11),
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
        row.pack(fill="x", padx=8, pady=(6, 8))
        ttk.Label(row, text=tr("term_hint"), foreground="#666").pack(side="left")
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
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text=tr("add_fw"),
                   command=self.on_add_fw).pack(side="left")
        ttk.Button(bar, text=tr("remove_sel"),
                   command=self.on_del_fw).pack(side="left", padx=6)
        ttk.Button(bar, text=tr("clear"),
                   command=self.on_clear_fw).pack(side="left")

        ttk.Label(bar, text=tr("flash_baud")).pack(side="left", padx=(20, 2))
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
        self.tv_fw.column("addr", width=100, anchor="center")
        self.tv_fw.column("file", width=560)
        self.tv_fw.column("size", width=110, anchor="e")
        self.tv_fw.pack(fill="both", expand=True, padx=8)
        self.tv_fw.bind("<Double-1>", self.on_edit_addr)

        ttk.Label(p, foreground="#a33", justify="left",
                  text=tr("erase_warn")).pack(fill="x", padx=8, pady=(6, 0))

        run = ttk.Frame(p)
        run.pack(fill="x", padx=8, pady=8)
        self.var_erase = tk.BooleanVar(value=False)
        ttk.Checkbutton(run, text=tr("erase_first"),
                        variable=self.var_erase).pack(side="left")
        self.btn_flash = ttk.Button(run, text=tr("erase_and_flash"),
                                    command=self.on_flash)
        self.btn_flash.pack(side="right")

        # 烧写进度 —— 左边标明这是什么, 中间进度条, 右边百分比。
        # (原来只有一条光秃秃的进度条 + 下面一行"等待操作", 没头没尾,
        #  用户根本看不出那是什么栏)
        pbrow = ttk.Frame(p)
        pbrow.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(pbrow, text=tr("flash_progress")).pack(side="left")
        self.pb = ttk.Progressbar(pbrow, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True, padx=6)
        self.var_pb = tk.StringVar(value=tr("idle"))
        ttk.Label(pbrow, textvariable=self.var_pb, width=30,
                  anchor="w").pack(side="left")

        # 烧写日志 —— 必须带标签, 否则就是个没头没尾的空白框
        ttk.Label(p, text=tr("flash_log")).pack(anchor="w", padx=8, pady=(8, 0))
        self.txt_flash = tk.Text(p, height=6, wrap="word",
                                 bg="#f6f6f6", font=("Consolas", 9))
        self.txt_flash.pack(fill="both", expand=True, padx=8, pady=(2, 8))

    # ---- 文件管理页 / files tab ----
    def _build_files_tab(self):
        p = self.tab_files

        # 顶部提示条: 板子上没有 MicroPython 时这里说明原因 (正常时是空的)。
        # 见 _sync_file_ui —— 文件管理靠 MicroPython 的 os API, 没有它就没得管。
        self.lbl_vfs_hint = ttk.Label(p, text="", foreground="#c00000")
        self.lbl_vfs_hint.pack(fill="x", padx=8)

        pan = ttk.PanedWindow(p, orient="horizontal")
        pan.pack(fill="both", expand=True, padx=8, pady=8)

        # 左: 本地 / left: local
        left = ttk.LabelFrame(pan, text=tr("local_files"), padding=6)
        pan.add(left, weight=1)

        lb = ttk.Frame(left)
        lb.pack(fill="x")
        self.var_local = tk.StringVar(value=self.local_dir)
        e_loc = ttk.Entry(lb, textvariable=self.var_local)
        e_loc.pack(side="left", fill="x", expand=True)
        e_loc.bind("<Return>", lambda _e: self.refresh_local())   # 手改路径后按回车生效
        ttk.Button(lb, text="…", width=3,
                   command=self.on_pick_dir).pack(side="left", padx=(4, 0))

        self.tv_local = ttk.Treeview(left, columns=("name", "size"),
                                     show="headings", selectmode="extended")
        self.tv_local.heading("name", text=tr("col_name"))
        self.tv_local.heading("size", text=tr("col_size"))
        self.tv_local.column("name", width=260)
        self.tv_local.column("size", width=90, anchor="e")
        self.tv_local.pack(fill="both", expand=True, pady=(6, 0))
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
        self.tv_dev.column("name", width=260)
        self.tv_dev.column("size", width=90, anchor="e")
        self.tv_dev.pack(fill="both", expand=True, pady=(6, 0))
        self.tv_dev.bind("<Double-1>", self.on_dev_open)

    # ------------------------------------------------------------------
    # 切换语言 / language switch
    # ------------------------------------------------------------------
    # Tkinter 改文本要逐个控件改; 控件太多, 直接重建 UI 更省事。
    # 重建前保存状态, 重建后恢复, 用户看不出差别 (REPL 内容也不会丢)。
    # Tkinter has no global re-translate; rebuilding the UI is simpler than
    # tracking every widget.  Save state before, restore after.

    def on_lang_change(self, _evt=None):
        name = self.cb_lang.get()
        for k, v in LANG.items():
            if v["lang_name"] == name:
                if k == _LANG_ID:
                    return
                self._switch_lang(k)
                return

    def _switch_lang(self, lang_id):
        st = self._save_state()
        set_lang(lang_id)
        for w in self.winfo_children():
            w.destroy()
        self._build_ui()
        self._restore_state(st)
        self.title(tr("title"))
        self.cb_lang.set(LANG[lang_id]["lang_name"])
        self._save_cfg()

    def _save_state(self):
        return {
            "repl": self.txt_term.get("1.0", "end-1c"),
            "replbaud": self.cb_replbaud.get(),
            "flash_log": self.txt_flash.get("1.0", "end-1c"),
            "port": self.cb_port.get(),
            "ports": list(self.cb_port["values"]),
            "fbaud": self.cb_fbaud.get(),
            "erase": self.var_erase.get(),
            "local_dir": self.var_local.get(),
            "dev_dir": self.var_dev.get(),
            "fw_rows": [self.tv_fw.item(i, "values")
                        for i in self.tv_fw.get_children()],
            "dev_rows": [self.tv_dev.item(i, "values")
                         for i in self.tv_dev.get_children()],
            "connected": self.btn_disc["state"] == "normal",
            "pb": self.pb["value"],
        }

    def _restore_state(self, st):
        if st["ports"]:
            self.cb_port["values"] = st["ports"]
        if st["port"]:
            self.cb_port.set(st["port"])
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

        self.cb_replbaud.set(st["replbaud"])
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
            self.btn_conn.configure(state="disabled")
            self.btn_disc.configure(state="normal")
            self.var_status.set(tr("connected_status", port=self.sm._port))
            self.lbl_conn.configure(
                text=tr("linked", port=self.sm._port, baud=self.sm._baud),
                fg="#008000")
        else:
            self.var_status.set(tr("ready"))
            self.lbl_conn.configure(text=tr("not_connected"), fg="#c00000")

        self._sync_mode_ui()               # 「切回 REPL」按钮的可点状态

        if self.busy:
            self.set_busy(True)

    # ------------------------------------------------------------------
    # 线程 → UI 消息泵 / worker thread → UI pump
    # ------------------------------------------------------------------
    def post(self, kind, **kw):
        self.msgq.put((kind, kw))

    def _drain(self):
        try:
            while True:
                kind, kw = self.msgq.get_nowait()
                try:
                    self._handle(kind, kw)
                except Exception:
                    self.log_repl(tr("ui_error") + traceback.format_exc())
        except queue.Empty:
            pass
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
        head.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Label(head, text="%s %s" % (APP_NAME, APP_VERSION),
                  font=("", 14, "bold")).pack(anchor="w")
        ttk.Label(head, text=tr("about_build", stamp=build_stamp()),
                  foreground="#666").pack(anchor="w")
        ttk.Label(head, text=APP_AUTHOR,
                  foreground="#666").pack(anchor="w")

        txt = tk.Text(win, wrap="word", height=18, relief="flat",
                      bg="#f6f6f6", padx=12, pady=10, spacing1=1,
                      font=("", 10))
        txt.pack(fill="both", expand=True, padx=14, pady=(4, 4))
        # 小标题加粗: 文案里以 '#' 开头的行当标题 (两门语言共用这个约定)
        txt.tag_configure("h", font=("", 10, "bold"), spacing1=8, spacing3=2)
        for ln in tr("about_body"):
            if ln.startswith("#"):
                txt.insert("end", ln[1:] + "\n", "h")
            else:
                txt.insert("end", ln + "\n")
        txt.configure(state="disabled")         # 只读, 但**能选中复制**

        ttk.Button(win, text=tr("ok"), width=10,
                   command=win.destroy).pack(pady=(0, 12))
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
        """按 detect 到的值刷新那个只读显示 (语言切换后也要重画)"""
        try:
            self.lbl_chip.configure(text=self.detected_chip or tr("chip_unknown"))
        except Exception:
            pass

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
        # ⚠ 连接按钮不能无脑恢复成 normal —— 忙完之后如果还连着, 它必须是灰的,
        #   否则会出现"已连接, 但『连接』按钮又能点"的怪状态。
        if on:
            self.btn_conn.configure(state="disabled")
        else:
            self.btn_conn.configure(
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
        if c.get("repl_baud"):
            self.cb_replbaud.set(c["repl_baud"])

    def _save_cfg(self):
        """把用户偏好写盘。存不成也不报错 —— 不该因为记偏好把程序搞崩。"""
        cfg_save({
            "lang": _LANG_ID,
            "fw_dir": self.fw_dir,
            "local_dir": self.local_dir,
            "port": self._port_name(),
            "flash_baud": self.cb_fbaud.get(),
            "repl_baud": self.cb_replbaud.get(),
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
        """把扫到的列表填进下拉, 并决定选中哪个"""
        had = self._port_name()                    # 当前下拉里选中的
        names = [v.split()[0] for v in ports]
        self.cb_port["values"] = ports

        # 选中项优先级: 当前选中的 > 存档里的 > 第一个。
        # 端口号换 USB 口/重启后会变, 所以一律按**端口名**匹配, 不按序号。
        # ★ 当前选中的口**拔掉了就空着**, 不跳到别的口上去 —— 否则插回来时
        #   已经停在别的口上了, 用户还得手动找回来。
        gone = bool(had) and had not in names
        pick = None
        for want in ("" if gone else had, self.cfg.get("port") or ""):
            if want and want in names:
                pick = want
                break
        if pick is None and not had and names:
            pick = names[0]                        # 本来就没选过 → 第一个
        if pick:
            self.cb_port.current(names.index(pick))
        else:
            self.cb_port.set("")                   # 没得选 → 清空, 不留拔掉的口

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
        raw = self.cb_port.get().strip()
        return raw.split()[0] if raw else ""

    def _repl_baud(self):
        """REPL 面板上选的波特率; 取不到就回退默认。"""
        try:
            return int(self.cb_replbaud.get())
        except (ValueError, AttributeError):
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
            self.post("repl_ok", ok=bool(greet))
            self.post("connected")

        self.run_bg(work)

    def _after_connect(self):
        self.btn_conn.configure(state="disabled")
        self.btn_disc.configure(state="normal")
        self.var_status.set(tr("connected_status", port=self.sm._port))
        self.lbl_conn.configure(          # 指示灯: 红 → 绿
            text=tr("linked", port=self.sm._port, baud=self.sm._baud),
            fg="#008000")
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
        self.btn_conn.configure(state="normal")
        self.btn_disc.configure(state="disabled")
        self.var_status.set(tr("disconnected"))
        self.lbl_conn.configure(text=tr("not_connected"), fg="#c00000")
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
        ttk.Label(dlg, text=tr("new_name")).pack(padx=12, pady=(12, 4))
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
        ttk.Button(dlg, text=tr("ok"), command=ok).pack(pady=10)

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
        ttk.Label(dlg, text=tr("addr_label")).pack(padx=12, pady=(12, 4))
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
        ttk.Button(dlg, text=tr("ok"), command=ok).pack(pady=10)

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
