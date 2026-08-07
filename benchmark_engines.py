# -*- coding: utf-8 -*-
"""
离线对比 EasyOCR(生产管线) vs Tesseract 在真实"股票复制识别"验证码样本上的:
  成功率(4位全对) / 稳定性(耗时波动) / 平均&最差耗时.
样本来自 captcha_captures/ 下 cap_*_ok_<CODE>_<conf>.png, 真值在文件名.
注意: 纯离线, 不碰线上客户端, 不触发风控.
"""
import os, re, sys, time, glob
from PIL import Image
import numpy as np

WS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WS)

import easytrader_server_auth as S   # 复用生产 OCR 管线 (_ocr_captcha / _CAPTCHA_DIGIT_BOX)

import pytesseract


def _find_tesseract():
    """按优先级探测 tesseract.exe; 找不到返回 None(交由 pytesseract 走 PATH 默认查找)."""
    cands = []
    env = os.environ.get("TESSERACT_CMD")
    if env:
        cands.append(env)
    cands.append(os.path.join(WS, "tesseract", "tesseract.exe"))          # 工作区子目录
    cands.append(r"C:\Program Files\Tesseract-OCR\tesseract.exe")          # 默认安装(需管理员)
    cands.append(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.append(os.path.join(local, "Tesseract-OCR", "tesseract.exe"))
    cands.append(os.path.join(os.path.expanduser("~"), "Tesseract-OCR", "tesseract.exe"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


TESS_CMD = _find_tesseract()
HAVE_TESS = TESS_CMD is not None
if HAVE_TESS:
    pytesseract.pytesseract.tesseract_cmd = TESS_CMD

CAP_BOX = S._CAPTCHA_DIGIT_BOX


def crop_digits(img):
    w, h = img.size
    a, b, c, d = CAP_BOX
    return img.crop((int(w * a), int(h * b), int(w * c), int(h * d)))


def easyocr_read(img):
    t0 = time.perf_counter()
    code, conf = S._ocr_captcha(img)        # 内部已按 CAP_BOX 裁剪 + 2 变体
    dt = time.perf_counter() - t0
    return code, conf, dt


def tess_read(img):
    crop = crop_digits(img)
    gray = crop.convert("L")
    variants = [
        gray,
        gray.resize((gray.width * 2, gray.height * 2)),
        gray.point(lambda p: 0 if p < 140 else 255),
        gray.point(lambda p: 0 if p < 140 else 255).resize((gray.width * 3, gray.height * 3)),
    ]
    best = ""
    t0 = time.perf_counter()
    for v in variants:
        for psm in (7, 8):
            cfg = f"--psm {psm} --oem 1 -c tessedit_char_whitelist=0123456789"
            try:
                txt = pytesseract.image_to_string(v, config=cfg)
            except Exception:
                continue
            dig = re.sub(r"\D", "", txt)
            if len(dig) == 4:
                best = dig
                break
        if best:
            break
    dt = time.perf_counter() - t0
    return best, dt


def gt_from_name(fn):
    m = re.search(r"_ok_(\d{4})_", fn)
    return m.group(1) if m else None


def main():
    files = sorted(glob.glob(os.path.join(WS, "captcha_captures", "cap_*_ok_*.png")))
    print(f"Tesseract available: {HAVE_TESS}  ({TESS_CMD if HAVE_TESS else 'not found'})")
    print(f"Samples (ok): {len(files)}\n")

    # 先显式预热(把模型加载 + 首次推理惰性开销在计时前吃掉):
    # 若不预热, 第一个样本的 _ocr_captcha 会把这个 ~5s 冷启动算进 OCR 耗时, 表现为"最差 4.6s".
    # 生产服务端已在启动时 _get_ocr_reader() 预热, 故稳态最坏耗时远小于冷启动; 这里预热是为了
    # 让对比反映"真实运行态"而非"首跑惩罚". 预热后第一个样本的时间也应纳入稳态统计.
    print("[warmup] 加载并预热 EasyOCR ...")
    S._get_ocr_reader()
    try:
        from PIL import Image, ImageDraw
        _d = Image.new("RGB", (414, 154), (255, 255, 255))
        ImageDraw.Draw(_d).text((30, 40), "1234", fill=(0, 0, 0))
        S._ocr_captcha(_d)   # 丢一次推理, 确保所有惰性编译路径都已跑过
    except Exception:
        pass
    print("[warmup] 完成\n")
    print(f"{'file':42} {'GT':5} {'EZ_code':9} {'EZ_cf':7} {'EZ_ms':7} {'T_code':9} {'T_ms':7} {'EZ':3} {'T':3}")
    ez_ok = tz_ok = 0
    ez_times, tz_times = [], []
    for f in files:
        gt = gt_from_name(os.path.basename(f))
        img = Image.open(f).convert("RGB")
        ec, ecf, et = easyocr_read(img)
        if HAVE_TESS:
            tc, tt = tess_read(img)
        else:
            tc, tt = "", 0.0
        ez_hit = (ec == gt)
        tz_hit = (HAVE_TESS and tc == gt)
        if ez_hit:
            ez_ok += 1
        if tz_hit:
            tz_ok += 1
        ez_times.append(et * 1000)
        if HAVE_TESS:
            tz_times.append(tt * 1000)
        print(f"{os.path.basename(f):42} {str(gt):5} {ec:9} {ecf:7.2f} {et*1000:7.1f} {tc:9} {tt*1000:7.1f} "
              f"{'OK' if ez_hit else 'X':3} {('-' if not HAVE_TESS else ('OK' if tz_hit else 'X')):3}")

    n = len(files)
    print("\n================ 汇总 ================")
    print(f"样本数: {n}")
    print(f"EasyOCR : 成功率 {ez_ok}/{n} = {ez_ok/n*100:5.1f}%  | 平均 {np.mean(ez_times):6.1f}ms  最差 {np.max(ez_times):6.1f}ms  最优 {np.min(ez_times):6.1f}ms")
    if HAVE_TESS:
        print(f"Tesseract: 成功率 {tz_ok}/{n} = {tz_ok/n*100:5.1f}%  | 平均 {np.mean(tz_times):6.1f}ms  最差 {np.max(tz_times):6.1f}ms  最优 {np.min(tz_times):6.1f}ms")
        faster = "Tesseract" if np.mean(tz_times) < np.mean(ez_times) else "EasyOCR"
        print(f"更快者(平均): {faster}")
    else:
        print("Tesseract: 未安装, 跳过 (安装后重跑本脚本即可对比)")
    print("\n注: 以上耗时已排除冷启动(脚本开头已预热). 此前看到的 ~4.6s 最差即首跑模型加载惩罚,")
    print("    生产服务端在启动时即预热, 不会出现该尖刺; 本脚本预热后第一个样本也已计入稳态统计.")


if __name__ == "__main__":
    main()
