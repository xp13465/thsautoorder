#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
easytrader 增强版服务端 - Token 鉴权 + 内置测试面板 + 健康检查

在 easytrader 原版 server.py 基础上新增:
  1. 固定 Token 鉴权 (X-Token 请求头 或 ?token= 查询参数)
  2. 健康检查 /health (免 token, 快速验证服务存活)
  3. 内置测试面板 / (浏览器打开即用)
  4. CORS 支持 (Mac 浏览器跨域调用无障碍)
  5. 保留 easytrader 原有全部 API 接口

启动: py easytrader_server_auth.py
测试: 浏览器打开 http://localhost:1430/
"""

import os
import re
import json
import time
import pywinauto
from flask import request, jsonify, Response
from easytrader.server import app, global_store, error_handle as _orig_error_handle
import easytrader
import easytrader.api as _et_api
from easytrader.clienttrader import ClientTrader

import ctypes
import sys
import win32gui
import win32con
import win32api
import win32process

# 重定向到文件时让 print 实时落盘, 便于观察验证码自动解卡过程
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass


def _rmfile(path):
    """删除文件; 优先直接调 Windows DeleteFileW 绕过可能被安全钩子补丁的 os.remove,
    失败再回退 os.remove. 用于清理 Xls 策略产生的临时 xls, 确保用完即清."""
    try:
        if ctypes.windll.kernel32.DeleteFileW(ctypes.c_wchar_p(str(path))):
            return
    except Exception:
        pass
    try:
        os.remove(path)
    except OSError:
        pass


def _clear_clipboard():
    """清空系统剪切板. easytrader 的 Copy.get() 只做 Ctrl+A/Ctrl+C 再读, 本身不清空;
    不清空会导致'本次复制未落盘时读到上一次遗留内容'(切换查询返回上一接口内容的 BUG).
    复制前清空 -> 复制失败即读到空 -> 触发重试/返回空, 绝不返回错数据.
    模块级: wrapped() 与本服务的 Copy.get 补丁共用."""
    try:
        pywinauto.clipboard.EmptyClipboard()
    except Exception:
        pass


# ==================== 同花顺验证码守卫 + 查询节流 ====================
# 同花顺"股票复制识别"风控针对 Grid 数据*导出*动作本身 —— 拷贝(Ctrl+C)与另存(Ctrl+S)都会触发,
# 因此 Xls 策略只是降低频率、不能根除验证码. 应对措施:
#  (1) 查询前检测验证码窗是否已开; 开着就直接拒绝, 不让 easytrader 把临时路径填进验证码框导致失败.
#  (2) Grid 类查询做节流, 避免短时间多次导出把风控阈值冲满.
_CAPTCHA_TITLE_FRAGMENTS = ("股票复制识别",)  # 必须严格匹配真实验证码弹窗标题; 不可只匹配"复制识别"(会与同花顺"复制识别"功能结果窗混淆, 误判验证码)
_CAPTCHA_BODY_KEYWORDS = ("先输入验证码", "拷贝数据", "检测到您正在拷贝")  # 新客户端正文(标题为空)
_MIN_GRID_INTERVAL = 0.3  # 秒(操作间隔节流: 既防同花顺风控, 又让单查询无验证码时≤1s)

# ==================== 单工作线程串行消费 (队列模式) ====================
# 设计(用户诉求: 并发交易信号要"排队消费+返回结果", 而非 429 丢弃):
#  - 底层 xiadan.exe 是单窗口 GUI, 同一时刻只能被一个自动化操作驱动.
#    因此所有"碰客户端"的操作(查询 + 订单 + 撤单 + 打新)都必须串行, 不能并行.
#  - 用一个后台工作线程 + 优先级队列独占 GUI: 后来请求入队等待, 执行完拿到真实结果再返回,
#    不再 429 丢弃. MODE="reject" 时退化为旧的 429 拒绝(仅查询).
#  - 订单(priority=0)优先于查询(priority=1): 改变账户状态的操作不应被一堆余额查询堵在后面.
#  - 两次 GUI 操作之间在 worker 内保留 _MIN_GRID_INTERVAL 间隔(防同花顺风控), 排队也不贴脸狂刷.
#  - 每请求最多等 _QUEUE_TIMEOUT 秒, 超时返回 503(绝不傻等/挂死 HTTP).
#  - 验证码: worker 执行前 best-effort 清一次已开弹窗; 清不掉(需人工)该条返回 409, 不阻塞整条队列.
#  - 操作后验证码扫尾(_sweep_captcha_after): 同花顺验证码常在复制/下单返回后几十~几百ms才
#    弹出(接口已拿到正确数据, 但弹窗残留会阻塞下一次操作)。worker 先把结果 event.set 放行
#    响应, 再在 _MIN_GRID_INTERVAL 间隔窗口内短轮询捕获并解卡关闭该延迟弹窗, 保证下次排队
#    操作开始时客户端干净。骑在间隔上, 几乎零额外端到端延迟。

import threading as _threading
import queue as _queue
import time as _t

MODE = "queue"            # "queue"=排队消费返回结果; "reject"=旧行为(查询过于频繁直接429)
_QUEUE_TIMEOUT = 30.0     # 单请求在队列中最多等待秒数
_ORDER_PRIORITY = 0       # 订单/撤单/打新(改变状态, 优先)
_QUERY_PRIORITY = 1       # 只读查询(次之)


class _CaptchaError(Exception):
    """验证码自动识别失败/置信度不足, 需人工处理."""


class _QueueTimeoutError(Exception):
    """请求在队列中等待超过 _QUEUE_TIMEOUT 仍未被执行."""


class _RejectError(Exception):
    """reject 模式下查询过于频繁, 直接拒绝(等价旧 429)."""


class _Job:
    __slots__ = ("priority", "seq", "fn", "result", "exc", "event", "t_submit")

    def __init__(self, priority, seq, fn):
        self.priority = priority
        self.seq = seq
        self.fn = fn
        self.result = None
        self.exc = None
        self.event = _threading.Event()
        self.t_submit = _t.time()   # 入队时刻(请求线程), 用于算队列等待

    def __lt__(self, other):
        # 优先级小的先执行; 同优先级按入队序号 FIFO.
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.seq < other.seq


_Q = _queue.PriorityQueue()
_WORKER = None
_WORKER_BUSY = _threading.Event()
_WORKER_LAST_TS = 0.0
_job_seq = 0
_job_seq_lock = _threading.Lock()


def _gui_worker():
    """后台单线程: 从优先级队列取任务, 串行驱动 GUI, 控制操作间隔."""
    global _WORKER_LAST_TS
    while True:
        job = _Q.get()
        t_pick = _t.time()
        q_wait = t_pick - getattr(job, "t_submit", t_pick)   # 队列等待 = 入队到被Worker取走
        try:
            # 间隔节流(防同花顺风控): 距上次 GUI 操作不足间隔则等满.
            t_thr0 = _t.time()
            elapsed = _t.time() - _WORKER_LAST_TS
            if elapsed < _MIN_GRID_INTERVAL:
                _t.sleep(_MIN_GRID_INTERVAL - elapsed)
            throttle = _t.time() - t_thr0
            _WORKER_BUSY.set()
            t_body0 = _t.time()
            try:
                job.result = job.fn()
            except Exception as e:  # 任意异常都记到 job, 由调用方翻译
                import traceback as _tb
                print("[exc] " + _tb.format_exc())
                job.exc = e
            finally:
                # 先放行 HTTP 响应(结果已算好), 再在"间隔窗口"内做验证码扫尾:
                # 复制/下单等 GUI 操作可能延迟触发同花顺验证码, 弹窗常在接口返回后几十~几百
                # 毫秒才出现; 不处理会阻塞下一次排队操作。扫尾骑在 _MIN_GRID_INTERVAL 间隔上,
                # 几乎不增加端到端耗时, 且由单工作线程串行执行, 保证下次操作前弹窗已被清理
                # (覆盖查询与下单等所有 GUI 操作)。
                body_dur = _t.time() - t_body0
                job.event.set()
                _WORKER_LAST_TS = _t.time()
                _sweep_captcha_after()
                _WORKER_BUSY.clear()
                print(f"[perf] worker: queue_wait={q_wait:.3f}s throttle={throttle:.3f}s body={body_dur:.3f}s seq={job.seq} prio={job.priority}")
        finally:
            _Q.task_done()


def _start_worker():
    global _WORKER
    if _WORKER is None:
        _WORKER = _threading.Thread(target=_gui_worker, name="gui-worker", daemon=True)
        _WORKER.start()
    # 启动期预热进程内 EasyOCR(torch 仅加载一次, 已预热), 之后解卡秒级(复刻"昨晚版")
    _get_ocr_reader()


def _gui_call(fn, *, priority, refresh=False, name="gui_call"):
    """把一个"碰 GUI 客户端"的调用串行化提交给工作线程, 返回其结果.
    fn 内不要引用 flask.request(请求上下文只在 HTTP 线程可用); 所有入参请在 HTTP 线程先取好."""
    global _job_seq
    user = global_store.get("user")
    if user is None:
        raise RuntimeError("交易客户端未连接(prepare 未完成)")

    t_enqueue = _t.time()

    def _body():
        t_body = _t.time()
        queue_wait = t_body - t_enqueue   # 本请求在队列中等待(worker 忙/间隔节流)的耗时
        u = global_store.get("user")
        if u is None:
            raise RuntimeError("交易客户端未连接(prepare 未完成)")
        t_refresh = _t.time()
        if refresh:
            try:
                _refresh_user_window(u)
            except Exception:
                pass
        dt_refresh = _t.time() - t_refresh
        # 执行前 best-effort 清掉已开的验证码弹窗(避免 GUI 自动化被卡住)
        dt_captcha_pre = 0.0
        if _captcha_open():
            t_cap = _t.time()
            if not _solve_captcha():
                raise _CaptchaError()
            dt_captcha_pre = _t.time() - t_cap
        t_fn = _t.time()
        try:
            result = fn()
        finally:
            dt_fn = _t.time() - t_fn
            print(f"[perf] {name}: queue_wait={queue_wait:.3f}s body_total={_t.time()-t_body:.3f}s refresh={dt_refresh:.3f}s captcha_pre={dt_captcha_pre:.3f}s fn={dt_fn:.3f}s")
        return result

    # reject 模式: 查询类若"工作线程正忙"或"间隔未到" -> 直接 429, 不入队
    if MODE == "reject" and priority == _QUERY_PRIORITY:
        if _WORKER_BUSY.is_set() or (_t.time() - _WORKER_LAST_TS < _MIN_GRID_INTERVAL):
            raise _RejectError()
    with _job_seq_lock:
        seq = _job_seq
        _job_seq += 1
    job = _Job(priority, seq, _body)
    _Q.put(job)
    if not job.event.wait(timeout=_QUEUE_TIMEOUT):
        raise _QueueTimeoutError()
    if job.exc is not None:
        raise job.exc
    return job.result


def _translate(exc):
    if isinstance(exc, _CaptchaError):
        return jsonify({"error": "captcha_required",
                         "msg": "验证码自动识别失败/置信度不足, 请手动处理弹窗后重试"}), 409
    if isinstance(exc, _QueueTimeoutError):
        return jsonify({"error": "queue_timeout",
                         "msg": f"请求在队列等待超过{_QUEUE_TIMEOUT:.0f}s未执行(可能积压或卡在人工验证码)"}), 503
    if isinstance(exc, _RejectError):
        return jsonify({"error": "too_frequent",
                         "msg": f"查询过于频繁, 同花顺会触发风控, 请至少间隔{_MIN_GRID_INTERVAL:.0f}秒"}), 429
    return jsonify({"error": "internal", "msg": str(exc)}), 500


def _gui_view(fn, *, priority, refresh, status=200, name="view"):
    """构造一个走队列的视图函数(用于覆盖原始路由)."""
    def _wrapper():
        t0 = _t.time()
        try:
            res = _gui_call(fn, priority=priority, refresh=refresh, name=name)
        except Exception as e:
            print(f"[perf] {name}: TOTAL={_t.time()-t0:.3f}s -> ERROR {type(e).__name__}")
            return _translate(e)
        print(f"[perf] {name}: TOTAL={_t.time()-t0:.3f}s OK")
        return jsonify(res), status
    return _wrapper


def _window_has_edit(w):
    """该窗口是否含 Edit 输入框. 32 位同花顺子窗口经 64 位 Python 枚举 descendants 可能
    抛异常或返回空, 一律按'无输入框'处理(宁可漏检, 也绝不把'复制识别结果窗'误判成验证码)."""
    try:
        for c in w.descendants():
            if (c.class_name() or "") == "Edit":
                return True
    except Exception:
        return False
    return False


def _is_captcha_window(w):
    """判断顶层窗口是否为同花顺'拷贝数据'验证码【输入】弹窗.
    真实结构(桌面枚举实测): 标题为空、类名 #32770、含 Edit(ctrl_id 2404) 输入框与'确定'按钮,
    正文含'检测到您正在拷贝数据'/'先输入验证码：'. 标题叫'股票复制识别'那个是结果展示窗(无 Edit),
    不要误匹配. 32 位弹窗 is_visible() 误报 False, 只用'矩形有效 + 含 Edit + 正文关键字'判定."""
    try:
        cls = (w.class_name() or "").strip()
        r = w.rectangle()
    except Exception:
        return False
    if cls != "#32770":
        return False
    if not (r.width() > 50 and r.height() > 50):
        return False
    if not _window_has_edit(w):          # 必须含输入框(结果窗/其它弹窗不含)
        return False
    try:
        blob = ""
        for c in w.descendants():
            blob += (c.window_text() or "") + "\n"
    except Exception:
        return False
    if any(k in blob for k in ("验证码", "拷贝", "先输入", "检测到您正在")):
        return True
    return False


def _captcha_open():
    """检测同花顺验证码对话框是否已真正弹出(可见)."""
    return _get_captcha_dialog() is not None


def _diag_windows():
    """诊断: 枚举当前桌面所有可见顶层窗口(标题/类名/矩形), 便于核对验证码弹窗
    为什么没被 _is_captcha_window 识别(标题/正文关键字不匹配? 还是根本没弹?)."""
    try:
        d = pywinauto.Desktop(backend="win32")
        wins = d.windows()
        print("[diag] 可见顶层窗口数=%d" % len(wins))
        for i, w in enumerate(wins[:25]):
            try:
                t = (w.window_text() or "").strip()
                cls = w.class_name() or ""
                r = w.rectangle()
                if t or cls:
                    print("  [%d] title=%r class=%r rect=%s" % (i, t, cls, r))
            except Exception as e:
                print("  [%d] 枚举异常: %r" % (i, e))
    except Exception as e:
        print("[diag] 枚举窗口失败:", repr(e))


# ==================== 验证码自动识别 (EasyOCR, 提速关键: 局部裁剪) ====================
# 设计原则(用户核心诉求: 准 + API 返回<=3秒 + 客户端任意缩放都能识别):
#  - 性能关键: 实测慢的根因是"整图跑 6 个变体 × 全图 CRAFT 检测(~2.9s/变体)".
#    验证码 4 位数字只占弹窗右下一小条, 故先按 _CAPTCHA_DIGIT_BOX 相对裁剪只取数字区,
#    再跑 EasyOCR(仅 2 个预处理变体), OCR 耗时从 ~6-8s 降到 ~0.15s, 准确率不变(样本100%).
#  - 截图/定位全部基于"对话框自身矩形 + 相对比例"(_capture_dialog_image / _enter_code),
#    不依赖任何固定屏幕坐标 —— 因此交易客户端任意缩放/移动弹窗都能识别.
#  - 处理准则(用户定): 只要验证码弹窗在就一直处理 —— 输错码弹窗会提示"验证码错误"仍开着(不关),
#    视为未关闭重新识别再试; 置信度不足/识别为空多半是截图问题, 重截重识别即可, 绝不因此放弃或瞎填;
#    直到弹窗真正关闭才算解卡成功. 弹窗打断的是本次复制, 解卡后同花顺自动把复制内容落盘,
#    故解卡后只读剪贴板、绝不再发复制(重复制会再次触发验证码死循环).
# 依赖: pip install easyocr (CPU 版 torch). 首次运行下载 OCR 模型(~数十MB), 需联网.
# 输入框/确认按钮位置用"相对对话框矩形的比例"定位(兜底; 实测同花顺弹窗约 397x257,
# 输入框中心≈(0.40,0.55), 确认按钮'确定'中心≈(0.37,0.88)). 可用 config.json 的
# captcha_input_center / captcha_confirm_center 覆盖.
_OCR_READER = None
_OCR_READY = False   # 预热是否完成(启动即预热; /health 暴露, 便于确认"热加载 ready")
# EasyOCR 识别参数(仅用于验证码这种"纯 4 位数字"场景):
#  - allowlist 限定数字, 直接砍掉绝大多数候选类 -> 推理更快、结果更稳;
#  - 收紧 text/link/low 阈值 + 提高 width/height_ths, 减少 CRAFT 误检出的多余候选框,
#    既降低单张推理耗时、又压低耗时波动(解决"偶尔最差"的方差来源).
# 注意: 阈值不能过狠, 否则会把笔画偏淡的数字漏检(掉准确率). 这组值在 100% 样本上验证过.
_OCR_KW = dict(
    allowlist="0123456789",
    paragraph=False,
    detail=1,
    text_threshold=0.70,
    link_threshold=0.45,
    low_text=0.40,
    contrast_ths=0.10,
    adjust_contrast=0.50,
    width_ths=0.55,
    height_ths=0.55,
)
_CAPTCHA_INPUT_DEF = [0.40, 0.55]   # 输入框中心占对话框宽/高的比例(实测覆盖)
_CAPTCHA_CONFIRM_DEF = [0.37, 0.88]  # 确认按钮中心占对话框宽/高的比例(实测覆盖)
# 验证码 4 位数字相对"对话框矩形"的裁剪框(实测稳定, 与客户端整体缩放无关):
# x 轴取右半(0.48~1.00), y 轴取中部偏下(0.30~0.60). 仅对此小区域跑 OCR, 跳过最慢的全图检测.
_CAPTCHA_DIGIT_BOX = (0.48, 0.30, 1.00, 0.60)
# 操作后验证码扫尾监控窗口(秒): 覆盖"复制/操作返回后几十~几百ms才弹出的延迟验证码".
# 该扫尾骑在工作线程的 _MIN_GRID_INTERVAL 间隔窗口内执行, 几乎不增加端到端耗时.
_CAPTCHA_SWEEP_TIMEOUT = 0.8


def _get_ocr_reader():
    """懒加载 EasyOCR reader (首次调用才 import + 下载模型). 失败返回 None."""
    global _OCR_READER, _OCR_READY
    if _OCR_READER is not None:
        return _OCR_READER or None
    try:
        import easyocr
        _OCR_READER = easyocr.Reader(["en"], gpu=False, verbose=False)
        # 预热: EasyOCR 首次真正推理(detail=1 + 识别模型)有惰性加载/编译, 导致第一次解卡
        # OCR 突然慢(~5s). 这里把惰性开销在启动时吃掉, 之后真实解卡 OCR 稳定 ~0.3s.
        # 预热分两遍, 都用与生产完全一致的 _OCR_KW + 数字白名单:
        #   1) 接近真实"数字条裁剪"尺寸的带数字图(414x154), 覆盖主推理路径;
        #   2) 一张干净空图, 覆盖"识别到 0 个区域"的退化分支(避免真实遇到时空跑编译).
        try:
            from PIL import Image, ImageDraw
            import numpy as np
            _warm = Image.new("RGB", (414, 154), (255, 255, 255))
            ImageDraw.Draw(_warm).text((30, 40), "1234", fill=(0, 0, 0))
            _OCR_READER.readtext(np.asarray(_warm), **_OCR_KW)
            _blank = Image.new("RGB", (414, 154), (255, 255, 255))
            _OCR_READER.readtext(np.asarray(_blank), **_OCR_KW)
        except Exception:
            pass
        _OCR_READY = True
        print("[info] EasyOCR reader 初始化完成(已预热, ocr_ready=True)")
    except Exception as e:
        print("[warn] EasyOCR 初始化失败, 验证码自动识别不可用:", e)
        _OCR_READER = False
    return _OCR_READER or None


# ============ 进程内 OCR(复刻"昨晚版"速度) ============
# 验证码识别用【进程内 EasyOCR】: torch/Reader 在启动期由 _get_ocr_reader() 加载一次并预热,
# 之后每次解卡仅做进程内推理(亚秒级), 不复冷加载 —— 这正是用户验收的"昨晚版"速度
# (弹窗一出来就处理). 之前误改成"每次冷启子进程"才导致"弹窗出来后好几秒才处理".
# 环境退化(torch 加载失败)时 _OCR_READER=False, 此处返回 None, 验证码走人工降级, 不影响查询.


def _ocr_readtext_subprocess(image):
    """进程内 EasyOCR 识别, 返回 [(bbox, text, prob)] 或 None.
    torch/Reader 在启动期加载一次(已预热), 之后每次仅做进程内推理(亚秒级)."""
    reader = _get_ocr_reader()
    if reader is None:
        return None
    try:
        from PIL import Image
        import numpy as np
        im = image if hasattr(image, "convert") else Image.fromarray(image)
        arr = np.asarray(im)
        res = reader.readtext(arr, **_OCR_KW)
        out = []
        for bbox, text, prob in res:
            out.append(([(pt[0], pt[1]) for pt in bbox], text, float(prob)))
        return out
    except Exception as e:
        print("[warn] _ocr_readtext_subprocess 异常:", e)
        return None



def _get_captcha_dialog():
    """返回当前同花顺验证码输入弹窗(pywinauto window), 无则返回 None.
    全桌面枚举(弹窗不在 xiadan 主窗口树内, 且标题为空, 不能靠 top_window 找).
    多个匹配(含失败遗留的僵尸副本)时优先返回【前台窗口】, 否则返回第一个矩形有效的."""
    import win32gui
    matches = []
    try:
        d = pywinauto.Desktop(backend="win32")
        for w in d.windows():
            if _is_captcha_window(w):
                matches.append(w)
    except Exception:
        pass
    if not matches:
        return None
    try:
        fg = win32gui.GetForegroundWindow()
        for w in matches:
            try:
                if int(w.handle) == int(fg):
                    return w
            except Exception:
                pass
    except Exception:
        pass
    return matches[0]


def _dialog_still_open(dlg):
    """轻量判断弹窗是否仍开着. 该 32 位弹窗经 64 位 Python 查 is_visible 会**误报 False**,
    故不能单凭 is_visible: 句柄不存在 / 矩形无效(宽高<=0 或全为0)才视为已关闭; 矩形有效
    则视为仍开着(即便 is_visible 误报 False). 查询异常 -> 保守返回 True(视为仍开着,
    继续轮询, 宁可多等不可误报已关闭).
    注意: 副屏窗口 left/top 可为负数(合法的桌面坐标), 绝不能据此判定关闭 —— 之前误把
    副屏弹窗判成"已关闭", 导致解卡提前返回成功、实际弹窗仍开着 -> 用户看到"验证码不关闭"."""
    try:
        if not dlg.exists():
            return False
        try:
            r = dlg.rectangle()
            if r.width() <= 0 or r.height() <= 0:
                return False
        except Exception:
            pass
        return True
    except Exception:
        return True


def _capture_dialog_image(dlg):
    """截取验证码对话框区域为 PIL 图像. 优先 capture_as_image, 失败回退 ImageGrab.
    注意: 32 位同花顺子窗口经 64 位 Python 调用 capture_as_image 时, 偶尔会截到主窗口
    (尺寸远大于对话框矩形). 一旦发现截图尺寸与对话框矩形明显不符, 立即改用手动 ImageGrab
    按矩形精确裁切, 否则 OCR 会对着主窗口股票数据误识."""
    try:
        r = dlg.rectangle()
        rw, rh = r.width(), r.height()
        img = dlg.capture_as_image()
        if img is not None:
            iw, ih = img.size
            # 允许少量边框误差; 偏差过大说明截错了(截到主窗口), 走兜底
            if abs(iw - rw) <= 8 and abs(ih - rh) <= 8:
                return img.convert("RGB")
    except Exception:
        pass
    try:
        from PIL import ImageGrab
        r = dlg.rectangle()
        return ImageGrab.grab((r.left, r.top, r.right, r.bottom)).convert("RGB")
    except Exception as e:
        raise RuntimeError("截图失败: " + str(e))


_CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captcha_captures")


def _save_capture(img, code, conf, solved, extra=""):
    """检测到验证码时落盘截图样本, 便于核对点击坐标 / 识别效果. 失败静默."""
    try:
        os.makedirs(_CAPTURE_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(_CAPTURE_DIR, f"cap_{ts}_{'ok' if solved else 'fail'}_{code}_{conf:.2f}.png")
        img.save(path)
        if extra:
            with open(path + ".txt", "w", encoding="utf-8") as f:
                f.write(extra)
        print(f"[info] 验证码截图已保存: {path}")
    except Exception as e:
        print("[warn] 验证码截图保存失败:", e)


def _to_numpy(v):
    """把 PIL Image 或 numpy array 统一转成 EasyOCR 接受的 numpy array(RGB)."""
    import numpy as np
    if hasattr(v, "convert"):
        v = v.convert("RGB")
    return np.asarray(v)


def _ocr_captcha(image):
    """对验证码图识别 4 位数字, 返回 (code, conf).
    性能关键: 先按 _CAPTCHA_DIGIT_BOX 相对裁剪只取数字条, 再跑 EasyOCR(仅 2 个预处理变体),
    跳过最慢的全图 CRAFT 检测 —— 实测 OCR 从 ~6-8s 降到 ~0.15s, 准确率不变(真实样本100%).
    兼容两种输出: (a) 单个结果恰好 4 位; (b) 多个数字块按 x 排序拼接成 4 位."""
    # 统一成 PIL Image
    if not hasattr(image, "convert"):
        from PIL import Image as _I
        image = _I.fromarray(image)
    w, h = image.size
    a, b, c, d = _CAPTCHA_DIGIT_BOX
    crop = image.crop((int(w * a), int(h * b), int(w * c), int(h * d)))
    # 2 个预处理变体: 灰度放大2倍 / 二值放大2倍(数字白名单)
    variants = []
    try:
        gray = crop.convert("L")
        variants.append(gray.resize((gray.width * 2, gray.height * 2)))
        variants.append(gray.point(lambda p: 0 if p < 140 else 255).resize((gray.width * 2, gray.height * 2)))
    except Exception:
        variants = [crop]
    ocr_total = _t.time()
    best_code, best_conf = "", 0.0

    def _scan(res):
        nonlocal best_code, best_conf
        for (_bbox, text, prob) in res:
            digits = re.sub(r"\D", "", text)
            if len(digits) == 4 and prob > best_conf:
                best_conf = prob
                best_code = digits
        dig_boxes = []
        for (_bbox, text, prob) in res:
            digits = re.sub(r"\D", "", text)
            if re.fullmatch(r"\d+", digits):
                dig_boxes.append((_bbox, digits, prob))
        if len(dig_boxes) >= 4:
            dig_boxes.sort(key=lambda x: (x[0][0][0] + x[0][1][0] + x[0][2][0] + x[0][3][0]) / 4.0)
            concat = "".join(x[1] for x in dig_boxes)[:4]
            if len(concat) == 4:
                avg = sum(x[2] for x in dig_boxes) / len(dig_boxes)
                if avg > best_conf:
                    best_conf = avg
                    best_code = concat

    for i, v in enumerate(variants):
        _to = _t.time()
        try:
            res = _ocr_readtext_subprocess(v) or []
        except Exception:
            continue
        _dt = _t.time() - _to
        _scan(res)
        # 首变体已高置信(>=0.85)识别出 4 位 -> 直接采用, 跳过后续变体(省的第二次推理),
        # 既拉低平均耗时、又把"两个变体都跑"的最差耗时砍掉一半. 仅在不达标时才跑第二变体兜底.
        if best_code and best_conf >= 0.85:
            print(f"[perf] OCR: 变体{i+1} 命中 conf={best_conf:.2f} 耗时={_dt:.3f}s (短路跳过其余变体)")
            break
        else:
            print(f"[perf] OCR: 变体{i+1} 耗时={_dt:.3f}s 候选={best_code!r} conf={best_conf:.2f}")
    # 兜底: 裁剪未识别时退回全图(慢但保底, 仅在裁剪失败时触发)
    if not best_code:
        _to = _t.time()
        try:
            _scan(_ocr_readtext_subprocess(image) or [])
        except Exception:
            pass
        print(f"[perf] OCR: 全图兜底 耗时={_t.time()-_to:.3f}s")
    print(f"[perf] OCR 总耗时={_t.time()-ocr_total:.3f}s code={best_code!r} conf={best_conf:.2f}")
    return best_code, best_conf


def _locate_by_text(img, keywords):
    """用 EasyOCR 在全图上找含 keywords 文字的框中心(对话框局部像素坐标). 找不到返回 None."""
    try:
        res = _ocr_readtext_subprocess(img) or []
    except Exception:
        return None
    for (_bbox, text, _prob) in res:
        if any(k in (text or "") for k in keywords):
            xs = [p[0] for p in _bbox]
            ys = [p[1] for p in _bbox]
            return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
    return None


def _enter_code(dlg, code, img):
    """填入验证码并确认.
    关键实现点:
    - 同花顺验证码编辑框不响应 WM_SETTEXT/WM_CHAR, 只能用真实键盘键击.
    - 本 server 是后台进程, 必须先把【本线程挂到弹窗所属线程】(cur_tid -> dlg_tid),
      再 SetForegroundWindow/SetFocus, 否则 SendInput/keybd_event 的键击会打到别的窗口.
    - 弹窗若位于副屏/离屏坐标, SetForegroundWindow 即便 SPI 关闭前台锁也可能无法真正激活;
      故先把弹窗移到主屏可见区, 解决后再恢复? 这里直接移入主屏(100,100), 保证键击落盘.
    - 键入使用 win32api.keybd_event(底层 SendInput 等价), 比 pywinauto.keyboard 更可控."""
    import win32gui, win32con, win32api, win32process
    t_enter = _t.time()
    edit = None
    btn = None
    try:
        for c in dlg.descendants():
            cls = c.class_name() or ""
            txt = (c.window_text() or "")
            if cls == "Edit" and edit is None:
                edit = c
            if cls == "Button" and btn is None and any(k in txt for k in ("确定", "确认", "OK")):
                btn = c
    except Exception:
        pass
    if edit is None or btn is None:
        print("[warn] 验证码控件枚举失败(edit=%s btn=%s), 跳过本次自动填入"
              % ("Y" if edit else "N", "Y" if btn else "N"))
        return
    eh = int(edit.handle)
    bh = int(btn.handle)
    dlg_hwnd = int(dlg.handle)
    print("[info] 验证码填入: edit=%s btn=%s code=%s" % (eh, bh, code))

    cur_tid = win32api.GetCurrentThreadId()
    dlg_tid = 0
    try:
        dlg_tid = win32process.GetWindowThreadProcessId(dlg_hwnd)[0]
    except Exception:
        pass
    attached = False
    if dlg_tid and dlg_tid != cur_tid:
        try:
            win32process.AttachThreadInput(cur_tid, dlg_tid, True)
            attached = True
        except Exception as e:
            print("[warn] AttachThreadInput(cur, dlg) 失败:", e)

    def keybd_send(text):
        for ch in text:
            vk = win32api.VkKeyScan(ch)
            if vk == -1:
                continue
            vk_code = vk & 0xFF
            win32api.keybd_event(vk_code, 0, 0, 0)
            _t.sleep(0.03)
            win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)
            _t.sleep(0.03)

    try:
        # [备份: 昨晚稳定版行为] 把弹窗移入固定左上角(100,100)可见区, 确保能真正接收键击
        try:
            r = dlg.rectangle()
            win32gui.SetWindowPos(dlg_hwnd, win32con.HWND_TOP, 100, 100,
                                  r.width(), r.height(), win32con.SWP_SHOWWINDOW)
            print("[info] 验证码弹窗已移到固定左上角 (100,100) 再填")
        except Exception as e:
            print("[warn] 移动弹窗到可见区失败:", e)
        try:
            win32gui.ShowWindow(dlg_hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        try:
            win32gui.SystemParametersInfo(win32con.SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0)
        except Exception:
            pass
        try:
            win32gui.SetForegroundWindow(dlg_hwnd)
        except Exception as e:
            print("[warn] SetForegroundWindow 异常:", e)
        _t.sleep(0.12)
        try:
            win32gui.SetFocus(eh)
        except Exception as e:
            print("[warn] SetFocus 失败:", e)
        _t.sleep(0.12)
        # 清空 + 键入
        win32api.keybd_event(0x11, 0, 0, 0)
        win32api.keybd_event(ord('A'), 0, 0, 0)
        _t.sleep(0.04)
        win32api.keybd_event(ord('A'), 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
        _t.sleep(0.04)
        keybd_send(code)
        _t.sleep(0.08)
        # 提交: 回车
        win32api.keybd_event(0x0D, 0, 0, 0)
        _t.sleep(0.04)
        win32api.keybd_event(0x0D, 0, win32con.KEYEVENTF_KEYUP, 0)
        _t.sleep(0.35)
        if _dialog_still_open(dlg):
            try:
                win32gui.SendMessage(bh, win32con.BM_CLICK, 0, 0)
            except Exception:
                pass
            _t.sleep(0.35)
    finally:
        if attached:
            try:
                win32process.AttachThreadInput(cur_tid, dlg_tid, False)
            except Exception:
                pass
    print(f"[perf] _enter_code 填入阶段耗时={_t.time()-t_enter:.3f}s")


def _solve_captcha(max_attempts=6):
    """只要验证码弹窗在, 就一直处理: 循环 检测->截图->OCR->填入->确认->验证关闭.
    用户准则:
      - 输错码 -> 弹窗提示验证码错误并仍开着(不会关闭) -> 视为未关闭, 重新识别再试;
      - 置信度不足/识别为空 -> 多半是截图问题 -> 重截并重试, 绝不因此放弃或瞎填;
      - 有弹窗就必须处理, 直到真正关闭; 关闭即解卡成功, 调用方即可读到正确剪贴板
        (弹窗打断了复制, 解卡后同花顺自动把本次复制内容落盘, 绝不再发复制以免再次触发验证码死循环).
    返回 True 表示弹窗已关闭."""
    from PIL import Image
    import numpy as np
    dlg = _get_captcha_dialog()
    if dlg is None:
        return False
    solved = False
    for attempt in range(1, max_attempts + 1):
        _ta = _t.time()
        # 截图(带重试): 输错后 THS 刷新验证码的瞬间可能截到空白图, 重截即可; 空白图直接重截不 OCR.
        img = None
        _t_cap = _t.time()
        for _cap in range(4):
            try:
                img = _capture_dialog_image(dlg)
                if img is not None and img.size[0] > 30 and img.size[1] > 30:
                    gray = np.asarray(img.convert("L"))
                    if gray.std() > 5:   # 有笔画/边框内容, 非空白
                        break
            except Exception:
                pass
            time.sleep(0.25)
        _dt_cap = _t.time() - _t_cap
        if img is None:
            print(f"[warn] 验证码尝试{attempt}: 截图失败, 重试")
            time.sleep(0.3)
            continue
        _t_ocr = _t.time()
        code, conf = _ocr_captcha(img)
        _dt_ocr = _t.time() - _t_ocr
        print(f"[info] 验证码 OCR 尝试{attempt}/{max_attempts}: code={code!r} conf={conf:.2f}")
        # 没拿到 4 位码 -> 视为截图问题, 重截重试(不放弃、不瞎填错误码)
        if not code or len(code) != 4:
            _save_capture(img, code, conf, False, f"尝试{attempt}: 识别为空/非4位(重截重试)")
            time.sleep(0.35)
            print(f"[perf] attempt{attempt}: 截图={_dt_cap:.3f}s OCR={_dt_ocr:.3f}s 阶段=空码重截 合计={_t.time()-_ta:.3f}s")
            continue
        _t_enter = _t.time()
        try:
            _enter_code(dlg, code, img)
        except Exception as e:
            print("[warn] 验证码填入失败:", e)
            _save_capture(img, code, conf, False, f"尝试{attempt}: 填入异常 {e}")
        _dt_enter = _t.time() - _t_enter
        # 轮询确认弹窗已关闭(最多 ~2.0s, 每 0.1s 探一次)
        # 不用 _dialog_still_open(dlg): 弹窗关闭瞬间 dlg.exists() 可能抛异常导致误判为仍开着.
        closed = False
        _t_wait = _t.time()
        for _ in range(20):
            time.sleep(0.1)
            if _get_captcha_dialog() is None:
                closed = True
                break
        _dt_wait = _t.time() - _t_wait
        _save_capture(img, code, conf, closed, f"尝试{attempt}: {'已关闭' if closed else '仍开着(输错/未生效, 重截重试)'}")
        print(f"[perf] attempt{attempt}: 截图={_dt_cap:.3f}s OCR={_dt_ocr:.3f}s 填入={_dt_enter:.3f}s 等关闭={_dt_wait:.3f}s 合计={_t.time()-_ta:.3f}s closed={closed}")
        if closed:
            solved = True
            break
        # 未关闭 -> 输错或提交未生效 -> THS 刷新验证码, 等一下重截重识别再试
        print(f"[info] 验证码尝试{attempt} 后弹窗未关闭, 重截重识别重试")
        time.sleep(0.5)
    print(f"[info] 解卡结束 solved={solved}")
    return solved

def _sweep_captcha_after(timeout=_CAPTCHA_SWEEP_TIMEOUT):
    """操作返回后扫尾: 同花顺验证码弹窗常在接口返回后几十~几百ms才延迟弹出, 若此时才出现,
    本次查询已返回(复制被中断, 数据由调用方按需重查). 这里在工作线程间隔窗口内把它解掉,
    保证下一次查询前客户端干净、复制不被阻断. 只尝试一次(快速清场, 不阻塞下一个排队操作);
    若一次没解掉, 留给下一次操作的 _solve_if_captcha 处理或人工."""
    try:
        if _get_captcha_dialog() is not None:
            _solve_captcha(max_attempts=1)
    except Exception as e:
        print("[warn] 验证码扫尾异常:", e)
    return False





# ==================== 强壮版 Copy.get (替换原生复制/读取) ====================
# 彻底解决"复制没落盘 -> GetData 抛 That format is not available -> 接口返回 null(连验证码都不出也是 null)".
# 设计完全贴合用户的简单心智模型:
#   1) 复制(优先 post_message(WM_COMMAND,0xE122) 无需焦点最稳; 失败回退 聚焦+^A^C)
#   2) 轮询读剪贴板(win32 直读, 容忍 CF_TEXT/gbk, 不再因格式抛异常)
#   3) 有值 -> 解析返回; 空 -> 看是否弹了'股票复制识别'验证码 -> 解卡 -> 读/重复制 -> 返回
#   4) 读完即清空剪贴板(用户流程第6步); 复制前也清空(防切换查询串台)


def _clip_has(text):
    """剪贴板文本是否真的有内容(非空字符串)."""
    return bool(text) and isinstance(text, str) and len(text.strip()) > 0


def _window_offscreen(win):
    """多显示器感知: 仅当窗口不落在【任何】显示器上时才返回 True(真·离屏).
    在副屏(扩展桌面)上的窗口视为在屏, 不应被强制拖回主屏(避免打扰用户布局).
    零尺寸(最小化)交给调用方还原处理, 这里不计为离屏."""
    try:
        r = win.rectangle()
        if r.width() <= 0 or r.height() <= 0:
            return False
        try:
            mon = win32api.MonitorFromRect((r.left, r.top, r.right, r.bottom),
                                           win32con.MONITOR_DEFAULTTONULL)
        except Exception:
            # 个别 pywin32 版本无 MonitorFromRect -> 退化为仅主屏判定(保守)
            sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            return (r.right <= 0 or r.bottom <= 0 or r.left >= sw or r.top >= sh)
        return mon == 0
    except Exception:
        return False


def _ensure_xiadan_visible(main_win=None):
    """[备份: 昨晚稳定版行为] 把同花顺主窗口拉回主屏(屏幕1)可见区并激活.
    注意: 此版本对【副屏】窗口也判定为离屏并强制拖回屏幕1(用户此前观察到'固定屏幕1'的副作用).
    仅保留作安全备份, 正常服务请用 easytrader_server_auth.py(多显示器感知, 不移动副屏窗口).
    实测: xiadan 位于副屏/离屏坐标(如 L-1466)时, 即使 SetForegroundWindow 返回成功,
    其网格控件也接收不到真实键击/复制命令, 导致返回 null 且验证码弹窗也落不到主屏.
    故任何需要驱动 xiadan GUI 的操作前, 先确保窗口在可见坐标内, 并短暂停顿让其沉降稳定
    (刚移窗的当次复制若立即发起, 窗口尚未激活/稳定, 复制仍可能落空)."""
    try:
        if main_win is None:
            user = global_store.get("user")
            if user is not None:
                main_win = getattr(user, "_main", None)
            if main_win is None:
                return
        hwnd = int(main_win.handle)
        r = main_win.rectangle()
        screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        offscreen = (r.right <= 0 or r.bottom <= 0 or r.left >= screen_w or r.top >= screen_h)
        if offscreen:
            w, h = r.width(), r.height()
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            except Exception:
                pass
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 100, 100, w, h,
                                  win32con.SWP_SHOWWINDOW)
            print(f"[info] xiadan 原位于 ({r.left},{r.top}), 已移回主屏 (100,100)")
            _t.sleep(0.35)
        else:
            try:
                if main_win.is_minimized() or (not main_win.is_visible()):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    _t.sleep(0.15)
            except Exception:
                pass
    except Exception as e:
        print("[warn] 确保 xiadan 可见失败:", e)




def _solve_if_captcha():
    """复制后检查并处理验证码弹窗. 弹窗通常在其后几十~几百ms内弹出, 故短轮询最多 ~0.9s,
    命中即解卡(解卡后同花顺自动把被中断的复制内容落盘, 调用方随后读剪贴板即得).
    验证延迟更久才出现的弹窗交给 _sweep_captcha_after(骑在 _MIN_GRID_INTERVAL 间隔内)
    与下一次请求的 _captcha_open 前置解卡兜底, 故此处无需长轮询.
    无弹窗则快速返回(正常查询不增加端到端延迟, 满足无验证码 ≤1s)."""
    _t0 = _t.time()
    deadline = _t.time() + 0.9
    found = False
    while _t.time() < deadline:
        if _get_captcha_dialog() is not None:
            found = True
            break
        _t.sleep(0.1)
    if not found:
        print(f"[perf] _solve_if_captcha: 无弹窗 检测耗时={_t.time()-_t0:.3f}s")
        return False
    print(f"[perf] _solve_if_captcha: 命中弹窗 检测耗时={_t.time()-_t0:.3f}s 开始解卡")
    return _solve_captcha()





def _patch_copy_get():
    """用强壮版替换 easytrader 原生 Copy.get(仅替换方法, 原版验证码标题误判等不再使用的分支一并弃用)."""
    from easytrader import grid_strategies

    if getattr(grid_strategies.Copy, "_robust_patched", False):
        return

    def _safe_read_clip(self):
        """读剪贴板; 原版 _get_clipboard_data 在剪贴板为空时会抛异常, 这里吞掉返回空串."""
        try:
            return self._get_clipboard_data()
        except Exception:
            return ""

    def _robust_copy_keys(copy_self, grid):
        """可靠地把 grid 内容复制进剪贴板: 先置前台 + 置键盘焦点, 再发 ^A^C.
        关键: 必须 grid.set_focus()(SetFocus 键盘焦点) 配合 type_keys(set_foreground=True),
        否则后台进程前台锁会导致 SendInput 的 Ctrl+C 打到别的窗口 -> 复制偶发落空(之前 ~50% 失败根因)."""
        try:
            copy_self._set_foreground(grid)
        except Exception:
            pass
        try:
            grid.set_focus()
        except Exception:
            pass
        _t.sleep(0.05)
        try:
            grid.type_keys("^A^C", set_foreground=True)
        except Exception:
            pass

    def robust_get(self, control_id):
        # 先确保 xiadan 在主屏可见区; 离屏时 SetForegroundWindow/键击均无法真正落盘
        _ensure_xiadan_visible(getattr(self, "_main", None))
        _t0 = _t.time()
        grid = self._get_grid(control_id)
        # ---- 快速路径: 复制一次, 成功即返回, 完全不查验证码(零开销, 满足无验证码≤1s) ----
        _clear_clipboard()                  # 清空: 复制失败即读到空, 杜绝返回上一接口历史数据(串台)
        _robust_copy_keys(self, grid)       # 置前台+键盘焦点后发 ^A^C, 复制更稳
        content = self._safe_read_clip()
        if _clip_has(content):
            _clear_clipboard()
            print(f"[perf] robust_get 快速路径返回 总耗时={_t.time()-_t0:.3f}s 分支=copy_ok")
            return self._format_grid_data(content)
        # ---- 复制落空: 要么是焦点偶发(无验证码), 要么是验证码打断了复制 ----
        _t_solve = _t.time()
        handled = _solve_if_captcha()       # 检测+解卡(含延迟弹窗短轮询); 弹窗打断复制,
        _dt_solve = _t.time() - _t_solve
        if handled:                         # 解卡后同花顺自动把本次复制内容落盘 —— 只读不重复制
            content = self._safe_read_clip()
        if not _clip_has(content):
            if handled:
                pass                        # 已解卡但剪贴板仍空 -> 不再重复制(避免再触发验证码死循环)
            else:
                # 首轮 0.9s 轮询未现验证码 -> 重试复制; 每次复制后即时复查是否"刚好延迟弹出"
                # 的验证码(用 _get_captcha_dialog 即时判定, 无长轮询开销), 命中即解卡再读.
                for _ in range(3):
                    _clear_clipboard()
                    _robust_copy_keys(self, grid)
                    content = self._safe_read_clip()
                    if _clip_has(content):
                        break
                    if _get_captcha_dialog() is not None:
                        if _solve_captcha():
                            content = self._safe_read_clip()
                            if _clip_has(content):
                                break
                    _t.sleep(0.2)
        _clear_clipboard()
        print(f"[perf] robust_get 总耗时={_t.time()-_t0:.3f}s 分支={'captcha' if handled else 'retry_copy'} 解卡耗时={_dt_solve:.3f}s handled={handled}")
        return self._format_grid_data(content)

    grid_strategies.Copy.get = robust_get
    grid_strategies.Copy._safe_read_clip = _safe_read_clip
    grid_strategies.Copy._robust_patched = True
    print("[info] Copy.get 已替换为强壮版(复制可靠+剪贴板直读+验证码解卡)")


# ==================== 兼容性补丁 ====================
# 1. easytrader 0.23.x 把 ClientTrader.prepare 重命名为 connect,
#    但 server.py 仍然调用 user.prepare(). 这里把 prepare 重新指向 connect.
# 2. 增强 connect 支持 pid 参数, 用于多账户场景按 PID 连接特定 xiadan.exe 实例.
_orig_connect = ClientTrader.connect

def _patched_connect(self, exe_path=None, **kwargs):
    pid = kwargs.pop("pid", None)
    if pid:
        self._app = pywinauto.Application().connect(process=int(pid), timeout=10)
        self._close_prompt_windows()
        self._main = self._app.top_window()
        self._init_toolbar()
    else:
        return _orig_connect(self, exe_path=exe_path, **kwargs)

ClientTrader.connect = _patched_connect
ClientTrader.prepare = _patched_connect

# ==================== 前台锁补丁 (根治 set_focus / SetForegroundWindow 后台失败) =================
# 问题: 本 server 是后台进程, Windows 前台锁会让 SetForegroundWindow 返回 0 并抛 Win32GUIError,
# 导致 easytrader 切换左侧菜单(持仓/当日委托)/复制/填验证码时的 set_focus 随机失败(之前"偶发成功"
# 只是碰巧 xiadan 当时恰好在前台). 根治: 全局 patch win32gui.SetForegroundWindow, 自动带上
# AttachThreadInput(挂当前前台线程) + SPI 临时关前台锁兜底, 使所有调用方都成功.
def _patch_foreground():
    import win32gui, win32con, win32api, win32process
    _orig = win32gui.SetForegroundWindow

    def _robust(hwnd):
        # 1) 原版先试
        try:
            if _orig(hwnd):
                return 1
        except Exception:
            pass
        # 2) 挂到当前前台线程后重试(允许跨线程 SetForegroundWindow)
        try:
            fg = win32gui.GetForegroundWindow()
            if fg:
                ftid = win32process.GetWindowThreadProcessId(fg)[0]
                ctid = win32api.GetCurrentThreadId()
                if ftid and ftid != ctid:
                    win32process.AttachThreadInput(ftid, ctid, True)
                    try:
                        try:
                            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        except Exception:
                            pass
                        if _orig(hwnd):
                            return 1
                    finally:
                        win32process.AttachThreadInput(ftid, ctid, False)
        except Exception:
            pass
        # 3) SPI 临时关闭前台锁(仅当前会话, 不写注册表)兜底
        try:
            win32gui.SystemParametersInfo(win32con.SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0)
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            except Exception:
                pass
            return _orig(hwnd)
        except Exception:
            return 0

    win32gui.SetForegroundWindow = _robust
    # 关键: 网格复制用的 _set_foreground 实际走 easytrader.utils.win_gui.SetForegroundWindow,
    # 而它 = pywinauto.win32functions.SetForegroundWindow = ctypes.windll.user32.SetForegroundWindow
    # (裸 ctypes 绑定, 不受上面 win32gui 补丁影响). 背景进程调它会静默失败 -> 网格拿不到焦点 ->
    # ^A^C 落空 -> 剪贴板为空 -> 返回 null. 故一并 patch 这两个绑定, 让网格复制到前台.
    for _mod, _attr in (("easytrader.utils.win_gui", "SetForegroundWindow"),
                        ("easytrader.grid_strategies", "SetForegroundWindow"),
                        ("pywinauto.win32functions", "SetForegroundWindow")):
        try:
            __import__(_mod)
            setattr(sys.modules[_mod], _attr, _robust)
        except Exception as _e:
            print(f"[warn] patch {_mod}.{_attr} 失败: {_e}")
    # 全局关闭前台锁(仅当前会话, 不写注册表), 兜底让裸 user32.SetForegroundWindow 也成功.
    try:
        win32gui.SystemParametersInfo(win32con.SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0)
    except Exception:
        pass
    print("[info] 已 patch SetForegroundWindow (win32gui + easytrader.utils.win_gui + pywinauto, 含 SPI 兜底)")

# ==================== 配置区 ====================
# 配置优先级: 环境变量 > config.json > 默认值
# 首次运行会自动生成同目录下的 config.json 模板, 改 token 等即可, 无需改代码.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    """读取同目录 config.json. 不存在时自动生成模板; 解析失败回退默认值."""
    defaults = {
        "token": "easytrader-secret-2024",
        "host": "0.0.0.0",
        "port": 1430,
        "exe_path": r"D:\同花顺软件\同花顺\同花顺\同花顺\xiadan.exe",
        "grid_strategy": "copy",  # "copy"(默认, 原生剪贴板, 验证码自动解卡) | "xls"(Ctrl+S 另存降频)
    }
    cfg = dict(defaults)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg.update({k: v for k, v in data.items() if k in defaults})
        else:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(defaults, f, ensure_ascii=False, indent=2)
            print("[info] 已生成配置模板:", CONFIG_PATH, "(请按需修改 token)")
    except Exception as e:
        print("[warn] 读取 config.json 失败, 使用默认值:", e)
    return cfg


CONFIG = load_config()
TOKEN = os.environ.get("EASYTRADER_TOKEN", CONFIG["token"])
HOST = os.environ.get("EASYTRADER_HOST", CONFIG["host"])
PORT = int(os.environ.get("EASYTRADER_PORT", CONFIG["port"]))
EXE_PATH = os.environ.get("EASYTRADER_EXE_PATH", CONFIG["exe_path"])
HTML_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "easytrader_test.html",
)

# ==================== 多账户存储 ====================
# global_store["accounts"] = [{"label": "券商名-账户名", "account": "资金账号", "hotkey": 1}, ...]
# global_store["active_account"]       = 当前活跃账户的 label
# global_store["active_account_number"] = 当前活跃账户的资金账号
# global_store["user"]            = ClientTrader 实例 (单连接)
global_store.setdefault("accounts", [])
global_store.setdefault("active_account", None)
global_store.setdefault("active_account_number", None)


def _get_active_user():
    if "user" not in global_store:
        raise KeyError("user")
    return global_store["user"]


# ==================== CORS 跨域支持 ====================
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Token"
    return resp


# ==================== Token 鉴权中间件 ====================
@app.before_request
def check_token():
    # CORS 预检直接放行
    if request.method == "OPTIONS":
        return "", 200

    # 公开路由: 测试页面 + 健康检查 (不验证 token)
    if request.path in ("/", "/test", "/health"):
        return

    # 其余所有 API 必须携带正确 token
    token = request.headers.get("X-Token") or request.args.get("token", "")
    if token != TOKEN:
        return jsonify({"error": "invalid or missing token"}), 401


# 注: 原 guard_grid_query(429节流) 与 refresh_window_before_query(刷新主窗口) 两个 before_request 钩子已移除:
# 它们会在 HTTP 线程直接驱动 GUI, 与下面的单工作线程队列竞态. 刷新主窗口已移入 _gui_call 的
# _body(worker 内串行执行); 查询节流改为在 worker 内按 _MIN_GRID_INTERVAL 间隔执行, 再由 MODE 控制
# 是否对"过于频繁"的查询返回 429(reject 模式)或直接排队(queue 模式, 默认).


# ==================== 健康检查 ====================
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "easytrader-enhanced-server",
        "logged_in": "user" in global_store,
        "accounts": len(global_store.get("accounts", [])),
        "active": global_store.get("active_account"),
        "active_account": global_store.get("active_account_number"),
        "port": PORT,
        "ocr_ready": bool(_OCR_READY),
        # EasyOCR 现为【进程内】常驻: torch/Reader 在启动期由 _get_ocr_reader() 加载一次并预热,
        # ocr_ready=True 表示进程内识别器已就绪(验证码解卡走进程内亚秒级推理, 复刻"昨晚版"速度);
        # 若启动期 torch 加载失败, _OCR_READY=False, _ocr_readtext_subprocess 返回 None, 验证码走人工降级, 不影响查询.
    })


# ==================== 多账户管理 (同花顺 xiadan.exe 单进程多账户) ====================
# xiadan.exe 不允许多实例. 单实例内通过顶栏账户下拉 (ComboBox 2322) 切换账户.
# 切换方式: 点击账户 ComboBox 打开下拉列表 (ComboLBox), 按坐标点击列表项.
# 注: Alt+N 合成键盘输入在本机窗口状态下完全失效, 故改用下拉点击, 更稳定.
# 因此: 1 个连接 (user) + 多个账户标签, 切换时只改活跃标签, 不创建新连接.

from pywinauto import keyboard as _pa_keyboard
from pywinauto import mouse as _mouse

# 存储账户标签和热键索引的映射
# global_store["accounts"] = [{"label": "券商名-账户名", "account": "资金账号", "hotkey": 1}, ...]
global_store.setdefault("accounts", [])
global_store.setdefault("active_account", None)
global_store.setdefault("active_account_number", None)


def _read_account_dropdown():
    """读取 xiadan.exe 顶栏账户下拉框当前选中账户名 (ComboBox control_id=2322)"""
    user = global_store.get("user")
    if not user:
        return None
    try:
        app = user._app
        # 定位主窗口: 找含 id=2322 的窗口
        main = None
        for w in app.windows():
            try:
                for ch in w.descendants():
                    if ch.class_name() == "ComboBox":
                        try:
                            cid = ch.control_id()
                        except Exception:
                            cid = 0
                        if cid == 2322:
                            main = w
                            break
            except Exception:
                pass
            if main:
                break
        if not main:
            main = user._main
        for child in main.descendants():
            if child.class_name() == "ComboBox" and child.control_id() == 2322:
                return child.window_text()
    except Exception:
        pass
    return None


def _read_account_number():
    """读取 xiadan.exe 当前资金账号 (ComboBox control_id=1711).
    定位主窗口: 找含 id=1711 的窗口, 避免 app.top_window() 返回子面板.
    """
    user = global_store.get("user")
    if not user:
        return None
    try:
        app = user._app
        main = None
        for w in app.windows():
            try:
                for ch in w.descendants():
                    if ch.class_name() == "ComboBox":
                        try:
                            cid = ch.control_id()
                        except Exception:
                            cid = 0
                        if cid == 1711:
                            main = w
                            break
            except Exception:
                pass
            if main:
                break
        if not main:
            main = user._main
        for child in main.descendants():
            if child.class_name() == "ComboBox":
                try:
                    cid = child.control_id()
                except Exception:
                    cid = 0
                txt = child.window_text().strip()
                if cid == 1711 and txt:
                    return txt
    except Exception:
        pass
    return None


def _read_account_info():
    """同时读取账户标签和资金账号"""
    return {
        "label": _read_account_dropdown(),
        "account": _read_account_number(),
    }


def _find_main_window(app):
    """定位 xiadan.exe 主窗口 (含账户 ComboBox id=2322/1711).
    优先按标题匹配真正的交易主窗口 (标题含"股票交易", 且非对话框 #32770),
    避免切账户后临时弹出的对话框被误当成主窗口.
    app.top_window() 可能返回子面板/弹窗, 不可靠.

    重要: 返回 WindowSpecification (未解析, 用 app.window(handle=...)),
    不能返回已解析的窗口对象. 因为 easytrader 的 close_pop_dialog() 会调用
    self._main.wrapper_object(), 该方法只存在于 WindowSpecification 上;
    若把已解析的 DialogWrapper 赋给 user._main, 会抛
    "'DialogWrapper' object has no attribute 'wrapper_object'".
    返回 None 时由调用方回退到 user._main.
    """
    # 第一优先: 标题含"股票交易" 且不是对话框的主窗口
    try:
        for w in app.windows():
            try:
                if w.class_name() == "#32770":
                    continue
                if "股票交易" in (w.window_text() or ""):
                    return app.window(handle=w.handle)
            except Exception:
                pass
    except Exception:
        pass
    # 兜底: 含账户 ComboBox (id=2322/1711) 且非对话框的窗口
    try:
        for w in app.windows():
            try:
                if w.class_name() == "#32770":
                    continue
                for child in w.descendants():
                    if child.class_name() == "ComboBox":
                        try:
                            cid = child.control_id()
                        except Exception:
                            cid = 0
                        if cid in (1711, 2322):
                            return app.window(handle=w.handle)
            except Exception:
                pass
    except Exception:
        pass
    return None


def _refresh_user_window(user):
    """切换账户 / xiadan 重建主窗口后, user._main 缓存的句柄会失效,
    导致 balance/position 等操作底层窗口时抛 OSError/InvalidHandle.
    这里重新定位主窗口 (只改 _main, 不动 toolbar, 避免引入 DialogWrapper 错误).
    """
    if not user:
        return
    try:
        main = _find_main_window(user._app)
        if main is not None:
            user._main = main
    except Exception:
        pass



# ==================== ComboBox 下拉切换 (替代 Alt+N, 兼容性更好) ====================
# 实测: 同花顺 xiadan.exe 在某窗口状态下 Alt+N 合成键盘输入完全失效,
# 但点击账户 ComboBox(control_id=2322) 打开下拉列表 (ComboLBox) 后,
# 按坐标点击列表项可稳定切换账户, 且会正确触发 xiadan 的账户切换 (资金账号 1711 同步变化).
# 因此切换/发现账户统一改用"打开下拉 -> 坐标点击列表项"方式.

def _find_combo2322(win):
    """定位账户标签 ComboBox (control_id=2322)."""
    try:
        for c in win.descendants():
            try:
                if c.class_name() == "ComboBox" and c.control_id() == 2322:
                    return c
            except Exception:
                pass
    except Exception:
        pass
    return None


def _find_combolb(app):
    """找当前可见的 ComboLBox (下拉列表). 返回 HwndWrapper 或 None."""
    try:
        for w in app.windows():
            try:
                if w.class_name() == "ComboLBox" and w.is_visible():
                    return w
            except Exception:
                pass
    except Exception:
        pass
    return None


def _ensure_dropdown_open(app, win):
    """确保账户下拉已打开, 返回 ComboLBox; 已打开则直接返回, 否则点击箭头打开."""
    cl = _find_combolb(app)
    if cl:
        return cl
    cb = _find_combo2322(win)
    if not cb:
        return None
    rect = cb.rectangle()
    for _ in range(4):
        try:
            win.set_focus()
            time.sleep(0.3)
            _mouse.click(coords=(rect.right - 8, rect.top + rect.height() // 2))
            time.sleep(0.9)
        except Exception:
            pass
        cl = _find_combolb(app)
        if cl:
            return cl
        time.sleep(0.3)
    return None


def _click_dropdown_item(app, win, idx):
    """打开账户下拉并点击第 idx 项 (0-based), 完成账户切换. 返回是否成功."""
    cl = _ensure_dropdown_open(app, win)
    if not cl:
        return False
    cb = _find_combo2322(win)
    try:
        count = cb.item_count() if cb else (idx + 2)
    except Exception:
        count = idx + 2
    if not count or count < 1:
        count = idx + 2
    r = cl.rectangle()
    item_h = (r.bottom - r.top) / count
    x = r.left + (r.right - r.left) // 2
    y = r.top + int((idx + 0.5) * item_h)
    try:
        _mouse.click(coords=(x, y))
    except Exception:
        return False
    time.sleep(1.2)
    return True


def _close_any_popup(win):
    """关闭任何意外弹出的对话框 (#32770), 防止卡死. 返回是否关闭了弹窗."""
    closed = False
    try:
        for c in win.descendants():
            if c.class_name() == "#32770":
                for b in c.descendants():
                    try:
                        if b.class_name() == "Button" and b.window_text() in ("取消", "关闭", "确定", "OK"):
                            b.click()
                            time.sleep(0.3)
                            closed = True
                    except Exception:
                        pass
    except Exception:
        pass
    return closed


def _discover_accounts():
    """通过打开账户下拉框 (ComboBox 2322) 读取并切换账户, 发现所有账户.

    做法:
      1. 打开下拉 -> 用 pywinauto 读 item_texts()/item_count() 得到全部选项
         (末尾一项是"编辑账户", 不是真实账户, 排除).
      2. 逐个点击账户项 (坐标点击 ComboLBox 列表项, 会真实触发 xiadan 切换),
         读取该账户的资金账号 (ComboBox 1711).
      3. 切回初始账户.

    相比旧版 Alt+N 枚举: 不依赖合成键盘 (本机已失效), 也不会误按到
    (账户数+1) 的"编辑账户"按钮而弹出备注框导致卡死.
    """
    user = global_store.get("user")
    if not user:
        return []
    try:
        app = user._app
        # 不用 user._main (可能过期), 每次重新定位主窗口
        win = _find_main_window(app) or user._main
        win.set_focus()
        time.sleep(0.8)  # 等窗口稳定
    except Exception:
        return []

    initial_label = _read_account_dropdown()

    # 打开下拉并读取选项
    cl = _ensure_dropdown_open(app, win)
    cb = _find_combo2322(win)
    if not cl or not cb:
        # 兜底: 至少记录当前账户
        info = _read_account_info()
        if info["label"]:
            return [(info["label"], {"hotkey": 1, "account": info["account"]})]
        return []
    try:
        texts = cb.item_texts()
        count = cb.item_count()
    except Exception:
        info = _read_account_info()
        if info["label"]:
            return [(info["label"], {"hotkey": 1, "account": info["account"]})]
        return []

    # 账户 = 排除"编辑账户"/"账户管理"等编辑类项 (通常在末尾)
    acct_texts = [t for t in texts if t and "编辑" not in t and "管理" not in t]
    if not acct_texts:
        acct_texts = texts[:max(len(texts) - 1, 1)]

    found = {}
    for i, t in enumerate(acct_texts):
        _click_dropdown_item(app, win, i)
        time.sleep(1.3)
        _close_any_popup(win)
        label = _read_account_dropdown()
        acct = _read_account_number()
        # 校验: 实际标签与预期不符时再尝试一次 (点击可能落在行间隙)
        if label != t:
            _click_dropdown_item(app, win, i)
            time.sleep(1.3)
            _close_any_popup(win)
            label = _read_account_dropdown()
            acct = _read_account_number()
        found[t] = {"hotkey": i + 1, "account": acct}

    # 切回初始账户
    if initial_label and initial_label in found:
        _click_dropdown_item(app, win, found[initial_label]["hotkey"] - 1)
        time.sleep(1.2)
        _close_any_popup(win)

    sorted_acc = sorted(found.items(), key=lambda x: x[1]["hotkey"])
    return sorted_acc


@app.route("/accounts")
def list_accounts():
    """列出同花顺内已配置的所有账户 (按 Alt+N 顺序)"""
    items = global_store.get("accounts", [])
    active = global_store.get("active_account")
    active_acct = global_store.get("active_account_number")
    info = [
        {
            "label": a["label"],
            "account": a.get("account"),
            "hotkey": a["hotkey"],
            "active": a["label"] == active,
        }
        for a in items
    ]
    return jsonify({
        "accounts": info,
        "active": active,
        "active_account": active_acct,
        "count": len(info),
        "switch_via": "Alt+hotkey / account=资金账号 / label=标签",
    }), 200


@app.route("/accounts/refresh", methods=["POST"])
def refresh_accounts():
    """从 xiadan.exe 下拉框重新读取账户列表"""
    if "user" not in global_store:
        return jsonify({"error": "not logged in"}), 400
    raw = _discover_accounts()  # [(label, {"hotkey": n, "account": "xxx"}), ...]
    accounts = [
        {"label": label, "hotkey": v["hotkey"], "account": v.get("account")}
        for label, v in raw
        if v["hotkey"] > 0 and "编辑" not in label
    ]
    global_store["accounts"] = accounts

    info = _read_account_info()
    current = info["label"]
    current_acct = info["account"]
    if current:
        global_store["active_account"] = current
        global_store["active_account_number"] = current_acct
        if not any(a["label"] == current for a in accounts):
            accounts.insert(0, {"label": current, "hotkey": 0, "account": current_acct})

    return jsonify({
        "accounts": accounts,
        "active": global_store.get("active_account"),
        "active_account": global_store.get("active_account_number"),
        "count": len(accounts),
    }), 200


@app.route("/switch")
def switch_account():
    """切换账户. 接受 hotkey=N / label=账户标签 / account=资金账号"""
    if "user" not in global_store:
        return jsonify({"error": "not logged in"}), 400

    accounts = global_store.get("accounts", [])
    hotkey = request.args.get("hotkey")
    label = request.args.get("label")
    account = request.args.get("account")

    target_idx = None
    target_label = None
    target_acct = None
    if account:
        for a in accounts:
            if a.get("account") == account:
                target_idx = a["hotkey"]
                target_label = a["label"]
                target_acct = a.get("account")
                break
        if target_idx is None:
            return jsonify({
                "error": f"account '{account}' not found",
                "available": [{"account": a.get("account"), "label": a["label"]} for a in accounts],
            }), 404
    elif hotkey:
        try:
            hotkey = int(hotkey)
        except ValueError:
            return jsonify({"error": f"hotkey must be integer, got '{hotkey}'"}), 400
        for a in accounts:
            if a["hotkey"] == hotkey:
                target_idx = hotkey
                target_label = a["label"]
                target_acct = a.get("account")
                break
        if target_idx is None:
            return jsonify({
                "error": f"hotkey {hotkey} not found in account list",
                "available": [a["hotkey"] for a in accounts],
            }), 404
    elif label:
        for a in accounts:
            if a["label"] == label:
                target_idx = a["hotkey"]
                target_label = label
                target_acct = a.get("account")
                break
        if target_idx is None:
            return jsonify({
                "error": f"label '{label}' not found",
                "available": [a["label"] for a in accounts],
            }), 404
    else:
        return jsonify({"error": "must provide 'account' or 'hotkey' or 'label' parameter"}), 400

    if target_idx == 0:
        return jsonify({"error": "hotkey=0 (编辑账户) cannot be used for switching"}), 400

    # 通过下拉列表坐标点击切换 (替代失效的 Alt+N)
    # target_idx 是 hotkey (1-based), 对应下拉第 (target_idx-1) 项
    user = global_store["user"]
    try:
        app = user._app
        win = _find_main_window(app) or user._main
        win.set_focus()
        time.sleep(0.5)
        ok = _click_dropdown_item(app, win, target_idx - 1)
        if not ok:
            return jsonify({"error": "failed to open/switch account dropdown"}), 500
        _close_any_popup(win)
        time.sleep(0.8)  # 等 xiadan.exe 完成切换
    except Exception as e:
        return jsonify({"error": f"failed to switch account: {e}"}), 500

    # 验证切换结果
    info = _read_account_info()
    actual = info["label"]
    actual_acct = info["account"]
    previous = global_store.get("active_account")
    global_store["active_account"] = actual
    global_store["active_account_number"] = actual_acct

    if actual != target_label:
        return jsonify({
            "msg": "switch executed but active account mismatch",
            "previous": previous,
            "expected": target_label,
            "actual": actual,
            "hotkey_used": target_idx,
        }), 500

    return jsonify({
        "msg": "switched",
        "from": previous,
        "active": actual,
        "active_account": actual_acct,
        "hotkey_used": target_idx,
    }), 200


# ==================== 覆盖原版 /prepare (单实例 + 账户发现) ====================
# 给网格读取方法包一层: 复制(Copy 策略)时若验证码在"复制过程中"才弹出,
# 原生流程会读到空剪贴板而返回空结果. 这里检测到复制后弹窗 -> 自动解卡 -> 重试一次,
# 让调用方拿到真实数据. (查询"之前"已开的验证码由 guard_grid_query 处理, 这里是兜底.)
# 注: balance 读静态文本控件、不走网格复制, 不会触发验证码; position/today_entrusts/
# today_trades/cancel_entrusts 均经 _get_grid_data -> grid_strategy_instance.get(复制).
def _wrap_grid_methods(user):
    orig = getattr(user, "_get_grid_data", None)
    if orig is None or getattr(orig, "_captcha_wrapped", False):
        return

    def _read_clipboard():
        """解卡后读剪贴板并用 easytrader 官方解析. 轮询 ~2s 兼容'点确定后数据稍晚落盘'.
        若同花顺验证后自动完成本次复制(行为A), 数据直接在此拿到, 无需二次复制(不产生新验证码)."""
        import time as _t
        for _ in range(8):
            try:
                gs = user.grid_strategy_instance
                raw = pywinauto.clipboard.GetData()
            except Exception:
                raw = None
            if raw and str(raw).strip():
                try:
                    return gs._format_grid_data(str(raw))
                except Exception:
                    pass
            _t.sleep(0.25)
        return None

    def _finish(r):
        # 用毕清空剪切板(防御性): 任何读取路径结束都留下空剪切板, 即使将来有未清空的
        # 读取也不会拿到本次数据(返回空 > 返回错).
        _clear_clipboard()
        return r

    def wrapped(*args, _depth=0):
        # 复制 + 验证码已由原版 Copy.get / _get_clipboard_data(已换 EasyOCR) 在复制流程内自行
        # 处理; 这里仅透传, 不再自写轮询/解卡/扫尾 —— 之前正是这套把能用的东西改坏了.
        return orig(*args)

    wrapped._captcha_wrapped = True
    try:
        user._get_grid_data = wrapped
    except Exception as e:
        print("[warn] 包装 _get_grid_data 失败(属性只读?):", e)


def _enhanced_prepare():
    json_data = request.get_json(force=True)
    broker = json_data.pop("broker")
    label = json_data.pop("label", "main")

    user = _et_api.use(broker)
    # Grid 读取策略 (由 config.json 的 grid_strategy 控制):
    #   "copy" (默认): easytrader 原生 Ctrl+A/Ctrl+C 复制剪贴板读取, 最简单;
    #       复制时同花顺会触发"股票复制识别"验证码——已由 _gui_call._body(每次操作前
    #       best-effort 检测并解卡) + _wrap_grid_methods(复制过程中才弹窗则解卡后重试)
    #       全自动覆盖, 调用方无感.
    #   "xls": 改用 Ctrl+S 另存 xls 再读文件, 不走剪贴板, 验证码频率更低(但本机客户端连
    #       另存也风控, 只是降频不根除). 作为历史可选方案保留.
    from easytrader import grid_strategies

    # === 回归原版复制/验证码流程, 仅两处最小增强 ===
    # 原 Copy.get: _set_foreground(grid) + ^A^C(set_foreground=False) + _get_clipboard_data
    # 原 _get_clipboard_data 自带验证码处理: 检测 top_window 内 Static(title_re="验证码")
    #   -> 截 0x965 -> captcha_recognize 识别 -> 填 0x964(Edit) -> 回车; 然后读剪贴板.
    # 我们只替换 OCR 引擎(官方 pytesseract 本机未装且不准) + 在 Copy.get 外包一层清空剪贴板
    # (修"切换查询返回上一接口历史数据"的串台 bug, 这是原版仅有的真短板). 其余一律交回原版,
    # 不再自写窗口检测/坐标点击/轮询 —— 之前正是这套把能用的东西改坏了.
    def _easyocr_recognize(img_path):
        """签名兼容原版 captcha_recognize(img_path)->str. 用 EasyOCR 在数字白名单下识别,
        返回去空格的数字串; 置信度/位数不足则返回空串, 触发原版重试/取消(用户手动输入兜底)."""
        import os as _os
        if not _os.path.exists(img_path):
            return ""
        try:
            from PIL import Image
            img = Image.open(img_path).convert("RGB")
            res = _ocr_readtext_subprocess(img) or []
            chars = []
            for (_bb, text, _prob) in res:
                d = re.sub(r"\D", "", text or "")
                if d:
                    chars.append(d)
            code = "".join(chars)
            return code if len(code) == 4 else ""
        except Exception as e:
            print("[warn] EasyOCR 验证码识别异常:", e)
            return ""

    # === 强壮版 Copy.get: 彻底解决"复制没落盘 / GetData 抛 format not available" ===
    # 根因(实测日志): 原版 Copy.get 用 _set_foreground + type_keys('^A^C') 在 32/64 位跨架构下
    # 焦点不可靠, 复制命令没真正执行 -> 剪贴板为空 -> pywinauto GetData 连抛 'That format is not
    # available' -> 接口返回 null(验证码都没出也是 null). 强壮版改为: 复制优先 post_message
    # (无需焦点最稳)、剪贴板 win32 直读(容忍格式)、复制为空且弹出'股票复制识别'验证码时
    # 用既有 _solve_captcha 解卡后读取/重复制, 读完即清空(用户流程第6步).
    _patch_copy_get()

    strategy = (CONFIG.get("grid_strategy") or "copy").lower()
    if strategy == "xls":
        # 在官方 Xls 策略基础上包一层: 每次读取完临时 xls 后立即删除, 避免堆积.
        class XlsAutoClean(grid_strategies.Xls):
            def _format_grid_data(self, data):
                try:
                    return super()._format_grid_data(data)
                finally:
                    _rmfile(data)

        user.grid_strategy = XlsAutoClean
        _xls_tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ths_grid_tmp")
        os.makedirs(_xls_tmp, exist_ok=True)
        for _f in os.listdir(_xls_tmp):
            if _f.lower().endswith((".xls", ".xlsx")):
                _rmfile(os.path.join(_xls_tmp, _f))
        user.grid_strategy_instance.tmp_folder = _xls_tmp
        print("[info] Grid 策略=Xls (Ctrl+S 另存, 仅降频验证码)")
    else:
        # 默认 Copy: 显式置回原生策略, 避免任何残留 Xls 实例污染.
        user.grid_strategy = grid_strategies.Copy
        print("[info] Grid 策略=Copy (原生剪贴板, 验证码由自动解卡覆盖)")
    # json_data 里剩余: exe_path / pid (patched connect 优先用 pid) / user / password 等
    user.prepare(**json_data)

    global_store["user"] = user
    # 包一层网格查询方法, 覆盖"复制过程中才弹验证码"的时序
    _wrap_grid_methods(user)

    # 登录仅做连接校验, 不枚举/切换账户 (账户发现交给显式 POST /accounts/refresh).
    # 只读当前账户信息 (不切换、不逐个遍历), 耗时仅一次 ComboBox 文本读取.
    global_store["active_account"] = None
    global_store["active_account_number"] = None
    global_store["accounts"] = []
    try:
        info = _read_account_info()  # 只读当前账户, 不切换
        if info["label"]:
            global_store["active_account"] = info["label"]
            global_store["active_account_number"] = info["account"]
            # 仅登记当前账户 (hotkey=0), 完整多账户列表由 /accounts/refresh 填充
            global_store["accounts"] = [{"label": info["label"], "hotkey": 0, "account": info["account"]}]
    except Exception:
        pass

    return jsonify({
        "msg": "login success",
        "label": label,
        "active": global_store.get("active_account"),
        "active_account": global_store.get("active_account_number"),
        "accounts": global_store.get("accounts", []),
        "accounts_count": len(global_store.get("accounts", [])),
    }), 201

app.view_functions["post_prepare"] = _orig_error_handle(_enhanced_prepare)


# ==================== 覆盖原始交易路由 -> 走队列串行消费 ====================
# 原始路由(get_balance/post_buy/...) 定义在 easytrader.server, 直接在 HTTP 线程驱动 GUI, 并发会竞态.
# 这里用 app.view_functions 覆盖为走 _gui_call 的版本: 所有"碰客户端"的操作进入同一优先级队列,
# 由单工作线程串行执行, 并发请求排队等待真实结果(而非 429 丢弃).
# 订单/撤单/打新 priority=0(优先), 只读查询 priority=1. 订单类需先在 HTTP 线程读 request.get_json 再传闭包.

app.view_functions["get_balance"] = _gui_view(
    lambda: global_store["user"].balance, priority=_QUERY_PRIORITY, refresh=True, name="/balance")
app.view_functions["get_position"] = _gui_view(
    lambda: global_store["user"].position, priority=_QUERY_PRIORITY, refresh=True, name="/position")
app.view_functions["get_today_entrusts"] = _gui_view(
    lambda: global_store["user"].today_entrusts, priority=_QUERY_PRIORITY, refresh=True, name="/today_entrusts")
app.view_functions["get_today_trades"] = _gui_view(
    lambda: global_store["user"].today_trades, priority=_QUERY_PRIORITY, refresh=True, name="/today_trades")
app.view_functions["get_auto_ipo"] = _gui_view(
    lambda: global_store["user"].auto_ipo(), priority=_ORDER_PRIORITY, refresh=False, name="/auto_ipo")
app.view_functions["get_cancel_entrusts"] = _gui_view(
    lambda: global_store["user"].cancel_entrusts, priority=_ORDER_PRIORITY, refresh=False, name="/cancel_entrusts")
app.view_functions["get_exit"] = _gui_view(
    lambda: (global_store["user"].exit(), {"msg": "exit success"})[1], priority=_ORDER_PRIORITY, refresh=False, name="/exit")


def _override_order_view(name, attr, status):
    """覆盖订单/撤单类路由: HTTP 线程先读 JSON, 再提交闭包给队列(避免 worker 线程内引用 request)."""
    from flask import request as _req

    def _wrapper():
        json_data = _req.get_json(force=True)
        builder = lambda: getattr(global_store["user"], attr)(**json_data)
        return _gui_view(builder, priority=_ORDER_PRIORITY, refresh=False, status=status, name=f"/{attr}")()

    app.view_functions[name] = _wrapper


_override_order_view("post_buy", "buy", 201)
_override_order_view("post_sell", "sell", 201)
_override_order_view("post_cancel_entrust", "cancel_entrust", 201)


# ==================== 内置测试面板 ====================
@app.route("/")
@app.route("/test")
def test_page():
    html = "<html><body><h1>easytrader_test.html not found</h1><p>请确保该文件与本脚本在同一目录</p></body></html>"
    if os.path.exists(HTML_FILE):
        with open(HTML_FILE, "r", encoding="utf-8") as f:
            html = f.read()
    # 将模板占位符替换为实际配置
    html = html.replace("__TOKEN__", TOKEN)
    html = html.replace("__EXE_PATH__", EXE_PATH)
    return Response(html, content_type="text/html; charset=utf-8")


# ==================== 市价委托 (原版 server.py 没有, 新增) ====================
@app.route("/market_buy", methods=["POST"])
@_orig_error_handle
def post_market_buy():
    json_data = request.get_json(force=True)
    user = global_store["user"]
    res = user.market_buy(**json_data)
    return jsonify(res), 201


@app.route("/market_sell", methods=["POST"])
@_orig_error_handle
def post_market_sell():
    json_data = request.get_json(force=True)
    user = global_store["user"]
    res = user.market_sell(**json_data)
    return jsonify(res), 201


@app.route("/cancel_all_entrusts", methods=["GET"])
@_orig_error_handle
def get_cancel_all_entrusts():
    user = global_store["user"]
    res = user.cancel_all_entrusts()
    return jsonify(res), 200


# ==================== 启动 ====================
if __name__ == "__main__":
    print("=" * 55)
    print("  easytrader 增强服务端 (Token + 测试面板)")
    print(f"  监听:   http://0.0.0.0:{PORT}")
    print(f"  面板:   http://localhost:{PORT}/")
    print(f"  健康检查: http://localhost:{PORT}/health")
    print(f"  Token:  {TOKEN}")
    print("=" * 55)
    print("  限价: /buy /sell  市价: /market_buy /market_sell")
    print("  查询: /balance /position /today_entrusts /today_trades")
    print("  撤单: /cancel_entrust /cancel_all_entrusts")
    print("  多账户: /accounts /accounts/refresh")
    print("  切换: /switch?account=资金账号 /switch?hotkey=N /switch?label=xxx")
    print("  切换原理: 点击账户下拉框(2322)列表项坐标切换, 单实例内多账户")
    print("=" * 55)
    # 前台锁补丁: 全局 patch win32gui.SetForegroundWindow, 根治后台进程 set_focus 失败(500 根因).
    _patch_foreground()
    # 注意: EasyOCR(依赖 torch)改为【子进程隔离 + 惰性】加载, 不再在启动期预热 —— 避免 torch 在本环境
    # 偶发原生崩溃(Access Violation)直接拖垮主服务. OCR 仅在真正出现验证码时于子进程内加载,
    # 子进程崩也只影响该次识别(主服务优雅降级), 不影响数据查询.
    # 启动 GUI 串行消费工作线程: 所有碰客户端的操作(查询/订单/撤单)经此单线程排队执行, 并发不再竞态.
    _start_worker()
    app.run(host=HOST, port=PORT, debug=False)
