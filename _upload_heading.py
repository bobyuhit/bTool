# -*- coding: utf-8 -*-
"""临时: 把 heading.py 传到板子 (base64 over REPL)。用完即删。"""
import base64
import sys
import time

import serial

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM11"
SRC = r"D:\HiWonder\CODE\Dog\bPuppy\mpy_modules\heading.py"
DST = "/heading.py"

data = open(SRC, "rb").read()
b64 = base64.b64encode(data).decode("ascii")
print("源文件 %d 字节 -> base64 %d 字符" % (len(data), len(b64)))

s = serial.Serial(PORT, 115200, timeout=0.3)
time.sleep(0.3)
s.reset_input_buffer()
s.write(b"\x03")
time.sleep(0.3)
s.write(b"\r\n")
time.sleep(0.8)
s.reset_input_buffer()


def cmd(line, wait=0.4):
    s.write(line.encode() + b"\r\n")
    time.sleep(wait)
    buf = b""
    while True:
        c = s.read(512)
        if not c:
            break
        buf += c
    return buf.decode("utf-8", "replace")


cmd("import ubinascii", 0.5)
cmd("f = open('%s', 'wb')" % DST, 0.5)

CHUNK = 192
sent = 0
for i in range(0, len(b64), CHUNK):
    part = b64[i:i + CHUNK]
    out = cmd("n = f.write(ubinascii.a2b_base64('%s'))" % part, 0.35)
    if "Traceback" in out or "Error" in out:
        print("\n块 %d 出错:" % i)
        print(out)
        sys.exit(1)
    sent += len(part)
    sys.stdout.write("\r  已发 %d/%d" % (sent, len(b64)))
    sys.stdout.flush()
print()

print(cmd("f.close()", 0.8).strip()[-70:])
print(cmd("import os; print('SIZE', os.stat('%s')[6])" % DST, 0.8).strip()[-90:])
s.close()
