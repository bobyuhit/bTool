"""生成 bTool 的图标 —— 纯 Pillow 画出来的, 不需要美术素材。

跑一次会产出两样东西:
    bTool.ico           给 PyInstaller 打包用: --icon=bTool.ico
    _icon_preview.png   各尺寸的预览图 (含放大版), 用来确认小尺寸下糊不糊

还会在结尾打印一段 base64 —— 那是**窗口图标**用的, 要贴回 btool.py 的
ICON_PNG_B64。为什么要贴: 见 README 的「应用图标」一节 —— exe 里的图标资源
Tk 读不到, 窗口图标得在运行时自己设一次, 而内嵌 base64 才能让 btool.py
保持"自包含的单文件"。

    pip install pillow
    python make_icon.py
"""

import io
import base64
import textwrap

from PIL import Image, ImageDraw

# ---- 配色 ----
TOP = (45, 62, 90)          # 背板渐变: 上
BOT = (12, 18, 30)          # 背板渐变: 下
FG = (226, 232, 240)        # 芯片轮廓 / 引脚
ACC = (74, 222, 128)        # 中心点: 终端绿, 小尺寸下的识别色

# ICO 里塞的尺寸。Windows 会按场景自己挑 —— 任务栏用小图、预览用大图,
# 所以别只给一个尺寸, 否则小尺寸会被硬缩, 糊。
SIZES = (16, 24, 32, 48, 64, 128, 256)


def make(size):
    """画一枚 size x size 的图标: 深色圆角底 + 芯片轮廓 + 引脚 + 中心绿点。"""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # 竖向渐变底 (逐行画线, 比逐像素快得多)
    grad = Image.new("RGB", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / (s - 1) if s > 1 else 0
        gd.line([(0, y), (s, y)],
                fill=tuple(round(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3)))
    # 圆角遮罩: 圆角半径按比例走, 小尺寸也得有圆角, 否则像方块
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1],
                                           radius=max(2, round(s * 0.23)), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)

    # ---- 芯片 ----
    # 小尺寸要"放大主体、减少细节", 否则引脚糊成一坨看不清
    small = s <= 20
    b0, b1 = (s * 0.24, s * 0.76) if small else (s * 0.29, s * 0.71)
    d.rounded_rectangle([b0, b0, b1, b1], radius=max(1, round(s * 0.08)),
                        outline=FG, width=max(1, round(s * 0.06)))

    n = 2 if small else 3                     # 每边几根引脚
    plen = s * (0.10 if small else 0.115)     # 引脚伸出长度
    pw = max(1, round(s * 0.055))             # 引脚粗细
    for p in ([0.5] if n == 2 else [0.37, 0.5, 0.63]):
        c = s * p
        d.line([(b0 - plen, c), (b0, c)], fill=FG, width=pw)      # 左
        d.line([(b1, c), (b1 + plen, c)], fill=FG, width=pw)      # 右
        d.line([(c, b0 - plen), (c, b0)], fill=FG, width=pw)      # 上
        d.line([(c, b1), (c, b1 + plen)], fill=FG, width=pw)      # 下

    # 中心绿点: 小尺寸放大一点, 保证 16px 也有个识别色
    k = 0.10 if small else 0.06
    d.rounded_rectangle([s * (0.5 - k), s * (0.5 - k), s * (0.5 + k), s * (0.5 + k)],
                        radius=1, fill=ACC)
    return img


def main():
    imgs = {s: make(s) for s in SIZES}

    # 1) ICO —— 多尺寸打包, 让 Windows 自己挑
    imgs[256].save("bTool.ico", format="ICO", sizes=[(s, s) for s in SIZES])
    print("bTool.ico 已生成")

    # 2) 预览图 —— 原尺寸一行 + 小尺寸放大, 用来肉眼确认糊没糊
    pad = 18
    w = sum(SIZES) + pad * (len(SIZES) + 1)
    sheet = Image.new("RGB", (w, 256 + 2 * pad + 24 * 6 + 2 * pad), (245, 245, 245))
    x = pad
    for s in SIZES:
        sheet.paste(imgs[s], (x, pad + (256 - s) // 2), imgs[s])
        x += s + pad
    x = pad
    for s in (16, 24, 32):                    # 这三个是最容易糊的
        up = imgs[s].resize((s * 6, s * 6), Image.NEAREST)
        sheet.paste(up, (x, 256 + 2 * pad), up)
        x += s * 6 + pad
    sheet.save("_icon_preview.png")
    print("_icon_preview.png 已生成 —— 打开看一眼, 重点确认 16/24/32")

    # 3) 窗口图标用的 base64 —— 贴回 btool.py
    buf = io.BytesIO()
    make(64).save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    print("\n把下面这段贴回 btool.py 的 ICON_PNG_B64 = ( ... ):\n")
    print("\n".join('    "%s"' % line for line in textwrap.wrap(b64, 76)))


if __name__ == "__main__":
    main()
