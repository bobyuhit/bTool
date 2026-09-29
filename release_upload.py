# -*- coding: utf-8 -*-
"""把 dist/bTool.exe 传成 GitHub Release 附件。

为什么需要你自己跑一下: 上传 Release 要 GitHub API token, 而 Claude 那边
**不允许把机器上存的凭据读出来** (那条拦截是对的 —— 凭据一旦进对话记录就泄了)。
所以这个脚本留给你自己执行: 它用 git 自己的凭据机制取 token, 全程只在你本机
内存里, 不落盘、不打印。

用法 (在这个目录下):
    python release_upload.py            # 发布 v1.1.0
    python release_upload.py --dry-run  # 只看会做什么, 不真传

可以反复跑 —— 已经存在的 Release 会**补齐标题/说明**, 附件大小一样就跳过上传。

前提: git push 能用 (能 push 就说明凭据是好的)。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "bobyuhit/bTool"
TAG = "v1.1.0"
NAME = "bTool v1.1.0"
ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "bTool.exe")
BASE = "https://api.github.com/repos/" + REPO
NL = chr(10)

NOTES = """## 界面

- **整体结构照 Arduino IDE 重排**: 顶部菜单栏 + 一条工具栏(连接/断开/串口) + **左侧图标竖栏**切页
- 配色、圆角、DPI 缩放全部对齐 Arduino IDE 2.x (色值是从实拍截图逐像素采的)
- 三个文件列表加了隔行底色, 表头立起来了 —— 原来一片白看不出行

## 新增「机器狗设置」页

- **几何标定**: 腿长 / 髋距 / 关节限位 / 重心偏移, 每栏各自一组「读取 / 写入」
- **三个校准弹框**: 舵机关节(8 路 x 0/90/180 三点) / 加速度计与陀螺仪 / 地磁

## REPL

- 新增 **自动滚屏** 和 **自动重连** 开关

## 修掉的真问题

- **连接卡死**: `self.menu_tools` 漏赋值, 让**每一个后台操作**都在 `set_busy()` 当场崩
  —— 而 pythonw 没有 stderr, 异常无声无息, 看着就是"转圈卡死"
- **连上了却说没跑 MicroPython**: `repl_ok` 判一次就不更新; 现在读线程看到 `>>>` 会自愈
- **圆角控件"漏光"**: 圆角渲染器把"内缩"写成了平移采样窗口, 边缘亮度溢出
- **状态栏被覆盖**: 每个操作的结果消息都会被下一轮轮询覆盖掉
- 关窗时显式释放串口 (原来靠操作系统在进程退出时收)
"""


def token_from_git():
    """问 git 要 github.com 的凭据 —— 和 git push 用的是同一份。

    只在本进程内存里流转, 不打印、不写盘。
    """
    p = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https" + NL + "host=github.com" + NL + NL,
        capture_output=True, text=True, cwd=os.path.dirname(ASSET))
    if p.returncode != 0:
        return None
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return None


def api(url, tok, data=None, method=None, raw=None, ctype="application/json"):
    hdr = {
        "Authorization": "Bearer " + tok,
        "Accept": "application/vnd.github+json",
        "User-Agent": "bTool-release",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = None
    if raw is not None:
        body = raw
        hdr["Content-Type"] = ctype
    elif data is not None:
        body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=hdr,
                                 method=method or ("POST" if body else "GET"))
    with urllib.request.urlopen(req, timeout=600) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt.strip() else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看会做什么")
    ap.add_argument("--tag", default=TAG)
    ap.add_argument("--notes", action="store_true", help="只打印发布说明")
    a = ap.parse_args()

    if a.notes:
        print(NOTES)
        return

    if not os.path.isfile(ASSET):
        sys.exit("找不到 %s —— 先打包:  python -m PyInstaller bTool.spec --noconfirm" % ASSET)
    size = os.path.getsize(ASSET)
    print("仓库 : %s" % REPO)
    print("版本 : %s  (%s)" % (a.tag, NAME))
    print("附件 : %s  (%.2f MB)" % (ASSET, size / 1048576))
    print("说明 : %d 字" % len(NOTES))

    if a.dry_run:
        print(NL + "--dry-run: 到此为止, 没联网。")
        return

    tok = token_from_git()
    if not tok:
        sys.exit(NL + "拿不到 GitHub 凭据 —— 先确认 git push 是能用的。")
    print("凭据 : 已从 git 取到 (不显示)")

    # ---- 建 / 复用 Release ----
    # 复用时要**补齐** —— 网页上手建的 release 常常只有 tag, 标题和说明都是空的;
    # 更要命的是 **/releases/latest 不一定认它** (实测: v1.1.0 明明比 v1.0.0 新,
    # latest 却还指着 v1.0.0)。而 README 的下载链接用的就是 latest ——
    # 不显式指定的话, 别人点"下载"拿到的还是旧包。make_latest 就是干这个的。
    try:
        rel = api("%s/releases/tags/%s" % (BASE, a.tag), tok)
        print("已存在 %s 的 Release (#%d), 补齐标题/说明并指定 latest" % (a.tag, rel["id"]))
        rel = api("%s/releases/%d" % (BASE, rel["id"]), tok, method="PATCH", data={
            "name": NAME, "body": NOTES, "make_latest": "true",
        })
    except urllib.error.HTTPError as e:
        if e.code != 404:
            sys.exit("查 Release 失败: %s %s" % (e.code, e.read()[:200]))
        rel = api(BASE + "/releases", tok, {
            "tag_name": a.tag, "name": NAME, "body": NOTES,
            "draft": False, "make_latest": "true",
        })
        print("已创建 Release %s (#%d)" % (a.tag, rel["id"]))

    # ---- 附件: 大小一样就跳过, 重跑一次不该再传 32MB ----
    fname = os.path.basename(ASSET)
    have = [x for x in rel.get("assets", []) if x["name"] == fname]
    if have and have[0]["size"] == size:
        print("附件已是这一份 (%.2f MB), 跳过上传" % (size / 1048576))
    else:
        for aset in have:                      # GitHub 不允许同名覆盖, 会 422
            api("%s/releases/assets/%d" % (BASE, aset["id"]), tok, method="DELETE")
            print("删掉旧附件 %s (%.2f MB)" % (aset["name"], aset["size"] / 1048576))
        # ⚠ 上传附件走的是 **uploads.github.com**, 不是 api.github.com ——
        #   用错域名会直接 404, 而且报错就一句 "Not Found", 指不到域名上。
        up = ("https://uploads.github.com/repos/%s/releases/%d/assets?name=%s"
              % (REPO, rel["id"], fname))
        with open(ASSET, "rb") as f:
            big = f.read()
        print("上传中 … (%.2f MB, 可能要一会儿)" % (len(big) / 1048576))
        out = api(up, tok, raw=big, ctype="application/octet-stream")
        print("完成: %s  (%.2f MB)" % (out.get("browser_download_url"),
                                      out.get("size", 0) / 1048576))

    # ---- 复查 latest 到底指到哪了 (README 的下载链接认的就是它) ----
    try:
        l = api(BASE + "/releases/latest", tok)
        good = l["tag_name"] == a.tag
        print("latest 现在指向: %s  %s" % (l["tag_name"], "OK" if good else "!! 还是旧的"))
        if not good:
            print("   (若仍是旧的, 去网页上 Edit release -> 勾 Set as the latest release)")
    except Exception as e:
        print("查 latest 失败: %s" % e)

    print(NL + "下载链接 (README 里那个, 永远指向最新):")
    print("  https://github.com/%s/releases/latest/download/bTool.exe" % REPO)


if __name__ == "__main__":
    main()
