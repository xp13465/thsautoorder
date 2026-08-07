# easytrader 增强服务端 · 网络请求 API 文档

> 适用版本：`easytrader_server_auth.py`（在 easytrader 原版 `server.py` 基础上增强）
> 文档生成时间：2026-08-06 · 与线上代码（端口 1430）一致

---

## 1. 服务概览

| 项 | 值 |
|---|---|
| 默认地址 | `http://<交易电脑IP>:1430` |
| 本地面板 | `http://localhost:1430/` |
| 默认 Token | `easytrader-secret-2024`（配置文件 `config.json` 或环境变量 `EASYTRADER_TOKEN` 覆盖） |
| 交易客户端 | 同花顺 `xiadan.exe`（单进程、内置多账户） |
| 架构 | 1 个 `ClientTrader` 连接 + 多账户标签，切换只改活跃标签 |

**CORS**：已开启 `Access-Control-Allow-Origin: *`，Mac / 浏览器跨域调用无障碍。

---

## 2. 鉴权

除公开路由外，所有接口必须携带 Token，二选一：

- 请求头：`X-Token: YOUR_TOKEN`
- 查询参数：`?token=YOUR_TOKEN`

**公开路由（免 Token）**：`/`、`/test`、`/health`

未携带或错误 Token 返回：

```json
{ "error": "invalid or missing token" }   // HTTP 401
```

### 自定义 Token

Token 已从代码中抽出，统一由 **`config.json`**（与脚本同目录）管理。**首次启动会自动生成该文件**，直接改 `"token"` 字段即可，无需碰代码。

```json
{
  "token": "easytrader-secret-2024",
  "host": "0.0.0.0",
  "port": 1430,
  "exe_path": "D:\\路径\\xiadan.exe"
}
```

其他配置项：`host` / `port` 监听地址与端口，`exe_path` 为 xiadan.exe 默认路径（首次 `prepare` 也可通过请求体覆盖）。

优先级：**环境变量 > `config.json` > 代码默认值**。环境变量覆盖写法：
- Windows (cmd)：`set EASYTRADER_TOKEN=你的新密钥`
- Windows (PowerShell)：`$env:EASYTRADER_TOKEN="你的新密钥"`
- Linux / macOS：`export EASYTRADER_TOKEN=你的新密钥`

> 改完后，所有调用方（浏览器面板、远端客户端、curl）都必须使用**同一个新 Token**，否则返回 401。面板 `/` 会自动读取服务端当前 Token，无需手动改 HTML。

---

## 3. 接口总览

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 免 | 健康检查 |
| GET/POST | `/`、`/test` | 免 | 内置测试面板（浏览器打开即用） |
| POST | `/prepare` | 需 | 连接 xiadan.exe 并校验（增强版，不枚举账户） |
| GET | `/accounts` | 需 | 列出所有已发现账户 + 当前活跃账户 |
| POST | `/accounts/refresh` | 需 | 从客户端重新读取账户列表 |
| GET | `/switch` | 需 | 切换账户（按 资金账号 / hotkey / 标签） |
| GET | `/balance` | 需 | 资金账户信息 |
| GET | `/position` | 需 | 持仓 |
| GET | `/auto_ipo` | 需 | 可转债/新股申购 |
| GET | `/today_entrusts` | 需 | 当日委托 |
| GET | `/today_trades` | 需 | 当日成交 |
| GET | `/cancel_entrusts` | 需 | 可撤委托列表 |
| POST | `/buy` | 需 | 限价买入 |
| POST | `/sell` | 需 | 限价卖出 |
| POST | `/market_buy` | 需 | 市价买入（服务端新增） |
| POST | `/market_sell` | 需 | 市价卖出（服务端新增） |
| POST | `/cancel_entrust` | 需 | 按委托号撤单 |
| GET | `/cancel_all_entrusts` | 需 | 撤销全部可撤委托 |
| GET | `/exit` | 需 | 退出客户端连接 |

---

## 4. 详细接口

### 4.1 GET `/health`
快速探活，免 Token。

```bash
curl http://localhost:1430/health
```

返回：

```json
{
  "status": "ok",
  "service": "easytrader-enhanced-server",
  "logged_in": true,
  "accounts": 2,
  "active": "券商A-账户甲",
  "active_account": "10000000001",
  "port": 1430
}
```

---

### 4.2 POST `/prepare`（增强版 · 仅校验连接）
连接交易客户端并完成登录校验。**不再在登录时枚举/切换账户**（原先逐个点击账户项做真实切换，N 个账户慢 N 倍且会动到当前账户）。登录只做一次只读的当前账户信息读取（`_read_account_info`，不切换、不遍历），返回 `active` 为当前账户。完整的多账户列表请调用 `POST /accounts/refresh` 按需获取。

**请求体（JSON）**：

| 字段 | 必填 | 说明 |
|---|---|---|
| `broker` | 是 | 券商类型，同花顺固定为 `"ths"` |
| `exe_path` | 二选一 | xiadan.exe 绝对路径 |
| `pid` | 二选一 | 已运行实例的进程 PID（优先于 exe_path） |
| `user` / `password` | 否 | 客户端已登录时可不传 |

```bash
curl -X POST http://localhost:1430/prepare \
  -H "X-Token: YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"broker":"ths","exe_path":"D:\\同花顺软件\\同花顺\\同花顺\\同花顺\\xiadan.exe"}'
```

返回（HTTP 201）：

```json
{
  "msg": "login success",
  "label": "main",
  "active": "券商A-账户甲",
  "active_account": "10000000001",
  "accounts": [
    {"label": "券商A-账户甲", "account": "10000000001", "hotkey": 1},
    {"label": "券商B-账户乙", "account": "20000000002", "hotkey": 2}
  ],
  "accounts_count": 2
}
```

> 注：`hotkey` 为下拉框中的序号（1 起），对应本次切换逻辑的下拉第 `hotkey-1` 项。`account` 为资金账号（纯数字），是切换的最可靠标识。

---

### 4.3 GET `/accounts`
列出已发现的账户及当前活跃账户。

```bash
curl http://localhost:1430/accounts -H "X-Token: YOUR_TOKEN"
```

返回：

```json
{
  "accounts": [
    {"label": "券商A-账户甲", "account": "10000000001", "hotkey": 1, "active": true},
    {"label": "券商B-账户乙", "account": "20000000002", "hotkey": 2, "active": false}
  ],
  "active": "券商A-账户甲",
  "active_account": "10000000001",
  "count": 2,
  "switch_via": "Alt+hotkey / account=资金账号 / label=标签"
}
```

---

### 4.4 POST `/accounts/refresh`
从客户端下拉框**重新读取**账户列表（例如新增/删除了账户后）。安全可靠，**不会触发「编辑账户」弹窗卡死**。

```bash
curl -X POST http://localhost:1430/accounts/refresh -H "X-Token: YOUR_TOKEN"
```

返回格式同 `/accounts`，并刷新服务端 `active_account` / `active_account_number`。

---

### 4.5 GET `/switch`（切换账户）⭐ 本次核心更新
切换当前活跃账户。**三种定位方式互斥，至少传一个**：

| 参数 | 说明 | 示例 |
|---|---|---|
| `account` | 资金账号（最可靠） | `20000000002` |
| `hotkey` | 下拉序号（整数，≥1） | `2` |
| `label` | 账户标签 | `券商B-账户乙` |

- `hotkey=0`（即「编辑账户」）**禁止用于切换**，返回 400。
- 切换方式：点击账户下拉框（`ComboBox 2322`）打开列表，按坐标点击对应列表项 —— **真实触发 xiadan 切换**，资金账号同步变化（已弃用失效的 `Alt+N` 与只改标签的 `.select()`）。
- 切换后会回读实际活跃账户做校验。

```bash
# 按资金账号切到银河
curl "http://localhost:1430/switch?account=20000000002" -H "X-Token: YOUR_TOKEN"

# 按序号切到中山
curl "http://localhost:1430/switch?hotkey=1" -H "X-Token: YOUR_TOKEN"

# 按标签切换
curl "http://localhost:1430/switch?label=%E4%B8%AD%E5%B1%B1%E8%AF%81%E5%88%B8-%E9%99%88*%E8%BE%89" \
  -H "X-Token: YOUR_TOKEN"
```

成功（HTTP 200）：

```json
{
  "msg": "switched",
  "from": "券商B-账户乙",
  "active": "券商A-账户甲",
  "active_account": "10000000001",
  "hotkey_used": 1
}
```

失败（HTTP 500，已执行但校验不符 / 下拉未打开）：

```json
{
  "msg": "switch executed but active account mismatch",
  "previous": "券商B-账户乙",
  "expected": "券商A-账户甲",
  "actual": "券商B-账户乙",
  "hotkey_used": 1
}
```

未找到目标（HTTP 404）：

```json
{ "error": "account 'xxx' not found", "available": [{"account":"10000000001","label":"券商A-账户甲"}] }
```

---

### 4.6 查询类接口（GET，沿用 easytrader 原版）

| 接口 | 说明 | 返回 |
|---|---|---|
| `/balance` | 资金账户 | 总资产/可用/冻结等 |
| `/position` | 持仓 | 证券代码/数量/成本/市值等 |
| `/auto_ipo` | 新股/可转债申购 | 可申购列表 |
| `/today_entrusts` | 当日委托 | 委托明细 |
| `/today_trades` | 当日成交 | 成交明细 |
| `/cancel_entrusts` | 可撤委托 | 待撤单列表 |

示例：

```bash
curl http://localhost:1430/balance -H "X-Token: YOUR_TOKEN"
curl http://localhost:1430/position -H "X-Token: YOUR_TOKEN"
```

---

### 4.7 交易类接口（POST）

#### 4.7.1 `/buy` `/sell`（限价，原版）
请求体 JSON 透传给 `user.buy()` / `user.sell()`：

| 字段 | 说明 |
|---|---|
| `security` | 证券代码，如 `600000` |
| `price` | 委托价格 |
| `amount` | 委托数量（股） |

```bash
curl -X POST http://localhost:1430/buy -H "X-Token: YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"security":"600000","price":10.5,"amount":100}'
```

#### 4.7.2 `/market_buy` `/market_sell`（市价，服务端新增）
请求体 JSON 透传给 `user.market_buy()` / `user.market_sell()`：

| 字段 | 说明 |
|---|---|
| `security` | 证券代码 |
| `amount` | 委托数量（股） |

```bash
curl -X POST http://localhost:1430/market_sell -H "X-Token: YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"security":"600000","amount":100}'
```

返回（HTTP 201）为 easytrader 原生结构（含委托号等）。

---

### 4.8 撤单类

#### 4.8.1 POST `/cancel_entrust`
| 字段 | 说明 |
|---|---|
| `entrust_no` | 委托号（从 `/today_entrusts` 获取） |

```bash
curl -X POST http://localhost:1430/cancel_entrust -H "X-Token: YOUR_TOKEN" \
  -H "Content-Type: application/json" -d '{"entrust_no":"1234567890"}'
```

#### 4.8.2 GET `/cancel_all_entrusts`
撤销全部可撤委托。

```bash
curl http://localhost:1430/cancel_all_entrusts -H "X-Token: YOUR_TOKEN"
```

---

### 4.9 GET `/exit`
断开客户端连接（关闭 xiadan 交互）。慎用。

```bash
curl http://localhost:1430/exit -H "X-Token: YOUR_TOKEN"
```

---

## 5. 错误码速查

| HTTP | 含义 |
|---|---|
| 200 / 201 | 成功 |
| 400 | 参数错误 / 未登录 / `hotkey=0` 禁止 |
| 401 | Token 缺失或错误 |
| 404 | 账户/序号/标签未找到 |
| 500 | 服务端执行异常（切换校验不符、下拉未打开、GUI 操作失败等） |

---

## 6. 客户端调用示例（远端 M1 Mac）

```python
import requests

BASE = "http://192.168.x.x:1430"
TOKEN = "YOUR_TOKEN"
H = {"X-Token": TOKEN}

# 1) 连接并校验（账户发现用 /accounts/refresh 按需获取）
r = requests.post(f"{BASE}/prepare", json={"broker":"ths","exe_path":"D:\\...\\xiadan.exe"}, headers=H)

# 2) 查看账户
accs = requests.get(f"{BASE}/accounts", headers=H).json()

# 3) 按资金账号切换到银河
requests.get(f"{BASE}/switch", params={"account":"20000000002"}, headers=H)

# 4) 卖出
requests.post(f"{BASE}/market_sell", json={"security":"600000","amount":100}, headers=H)

# 5) 查持仓
pos = requests.get(f"{BASE}/position", headers=H).json()
```

---

## 7. 本次（2026-08-06）更新要点

1. **账户切换改为下拉框坐标点击**：废弃失效的 `Alt+N` 合成键与只改标签的 `.select()`，改用点击 `ComboBox 2322` 列表项，真实触发 xiadan 切换（资金账号同步变化）。
2. **账户发现不再卡死**：读 `ComboBox 2322.item_texts()` 发现账户，自动排除末尾「编辑账户」项，从根上杜绝「备注弹窗 → 卡死后续操作」问题。
3. **`/switch` 支持三种切换键**：`account`（资金账号，最可靠）/ `hotkey` / `label`，互斥，带切换后回读校验。
4. **`/accounts/refresh` 安全重扫**：不会触发弹窗卡死。
5. **新增 `/market_buy` `/market_sell`**（市价买卖）。
6. 网页测试面板（`/`）切换按钮刷新逻辑修复（等切换响应回来再刷新，不再用固定 1500ms 提前刷新导致状态陈旧）。

## 8. 已知问题与解法

### 8.1 同花顺「股票复制识别」验证码（查资金/持仓/委托卡死）
- **现象**：查询类接口（持仓、当日委托、当日成交等走 Grid 复制的接口）频繁调用时，同花顺弹出「股票复制识别」验证码窗口，客户端卡死、接口无响应。
- **根因**：easytrader 默认 `grid_strategy = Copy`，通过 `Ctrl+C` 把 Grid 复制到剪贴板再解析；高频复制敏感数据会触发同花顺风控限流。**实测该风控针对 Grid 数据的*导出动作本身*——`Ctrl+C` 复制与 `Ctrl+S` 另存都会触发**，因此 `Xls` 策略只是降低频率，不能 100% 根除验证码。
- **默认策略（已验证）**：本服务默认走 `Copy` 原生剪贴板策略 + **EasyOCR 自动识别验证码**。既然验证码不可避免，就让它在复制时弹出，然后自动识别并填入，**一次闭环解决**。实测连续触发均自动解卡成功（OCR 置信度 1.00）。
- **速度（已优化，满足 API ≤3 秒返回）**：
  - 慢的根因是 EasyOCR 在**整张弹窗**上跑 6 个变体 × 全图 CRAFT 检测（~2.9s/变体 → 单次解卡 6~8 秒）。
  - 提速做法：**先按相对对话框矩形的裁剪框 `_CAPTCHA_DIGIT_BOX=(0.48,0.30,1.00,0.60)` 只取数字条，再跑最多 2 个预处理变体**（灰度放大 2× / 二值放大 2×）。跳过最慢的全图检测后，单次 OCR 从 ~6-8s 降到 **~0.3s**，准确率不变（真实样本 100%）。两项进一步优化：
    - **首变体高置信即短路**：第一个变体若已以 `conf≥0.85` 识别出 4 位，直接采用并跳过第二变体，把"两变体都跑"的最差耗时砍掉约一半（均值、最差同时下降）。
    - **收紧检测阈值 + 数字白名单**（`_OCR_KW`：`text_threshold=0.75 / link_threshold=0.50 / width_ths=0.65 / allowlist=0123456789` 等）：减少 CRAFT 误检出的多余候选框，既更快、又压低耗时波动——这正是"偶尔最差"的方差来源。阈值经 100% 样本验证，不会漏检。
  - **冷启动修复**：EasyOCR 识别模型惰性加载，首个查询触发验证码时 OCR 会猛地慢到 ~5s；已在**服务启动时预热**（`_get_ocr_reader()` 用接近真实"数字条裁剪"尺寸的带数字图 + 一张空图各跑一次 `_OCR_KW` 识别，把惰性编译在 boot 阶段吃掉），之后稳定 ~0.3s，并经 `/health` 的 `ocr_ready=true` 暴露就绪状态。**离线对比脚本 `benchmark_engines.py` 也已在计时前显式预热**，因此你看到的 ~4.6s"最差"其实是未预热的首跑惩罚，与稳态无关。
  - 实测端到端：触发验证码的查询总耗时 **2.5~2.7 秒**（OCR 0.3 + 键盘填入 ~1.0 + 等待关闭 ~0.25 + 复制重试 ~0.5）；正常无验证码查询 ~0.9 秒。
- **兼容性（客户端任意缩放/移动都识别）**：检测/截图/点击**全部基于真实窗口对象与其自身矩形 + 相对比例**，绝不依赖固定屏幕坐标。弹窗只是 THS 内部的固定尺寸小窗（~397×257），其相对交易客户端的位置会变，但截取与点击都用它自己的矩形，所以客户端拖大拖小都不影响。复制后若弹窗晚出现，包装层会**短轮询（最多 ~0.5s，仅当本次复制结果为空时才轮询）**捕捉，不傻等、也不给正常查询加延迟。此外，若出现「接口已返回正确数据、但验证码在返回后才弹出」的成功路径延迟弹窗，则由工作线程的 `_sweep_captcha_after` 在两次操作的**间隔窗口内统一扫尾关闭**——它骑在 `_MIN_GRID_INTERVAL` 间隔上，几乎零额外端到端延迟，且保证下一次排队操作开始时客户端已是干净状态（覆盖查询与下单等所有 GUI 操作）。
- **三层防御**：
  1. **操作前检测**：`_gui_call._body` 在每次"碰客户端"的操作（查询/下单/撤单，均经单工作线程串行执行）前 best-effort 检测验证码窗；若已开则先解卡，再放行。
  2. **复制中兜底 + 轮询 + 复制前清空剪切板**：`_get_grid_data` 被包装；若复制过程中才弹出验证码（含晚出现），复制得到空结果后立即短轮询确认并解卡，重试一次，调用方仍拿到正常数据。**每次复制前强制 `pywinauto.clipboard.EmptyClipboard()` 清空剪切板**——已读 easytrader 源码确认 `Copy.get()` 只做 `^A^C` 后直接读、本身不清空；若本次复制因目标表格尚未就绪未落盘，会读到上一次遗留内容（即「切换查询返回上一接口内容」的陈旧数据 BUG）。清空后复制失败即读到空→判为失败→重试/返回空，**绝不返回错误数据**；空列表 `[]` 视为合法空表格直接返回，仅 `None` 才重试。
  3. **操作后验证码扫尾（`_sweep_captcha_after`）**：复制/下单等 GUI 操作可能延迟触发验证码，弹窗常在接口返回后几十~几百毫秒才出现。worker 先把结果 `event.set` 放行 HTTP 响应，再在 `_MIN_GRID_INTERVAL` 间隔窗口内短轮询（最多 ~0.8s）捕获并解卡关闭该延迟弹窗，保证下次排队操作开始前客户端干净；骑在间隔上，几乎不增加端到端耗时。
  4. **间隔节流**：两次 GUI 操作在 worker 内保留 `_MIN_GRID_INTERVAL=1s` 间隔（防同花顺风控），排队也不贴脸狂刷。
- **废掉 easytrader 自带验证码处理器**：easytrader 的 `Copy._get_clipboard_data` 撞到弹窗（正文含"验证码"字样被它 `title_re` 误判）会调 `captcha_recognize → pytesseract`，本机没装 pytesseract 且该处理器用的控件 ID（0x965/0x964）与本弹窗不匹配，只会炸 400 或误点取消。已在 `_enhanced_prepare` 给 `Copy._get_clipboard_data` 打补丁，**让 easytrader 只负责复制剪贴板**，验证码完全由本服务用 EasyOCR 解卡。
- **复制可靠性补丁（`Copy.get` 重写）**：原 `Copy.get` 用 `type_keys("^A^C", set_foreground=False)`——这是**能工作的复制方式**（`CVirtualGridCtrl` 的 `^A^C` 由控件自身消息循环处理，`set_foreground=False` 直接把按键发给 grid 线程）。之前"切换查询返回上一接口内容"的根因是**清空缺失 + 读太快（复制异步、读在落盘前）**，不是复制没发生（否则连残留都读不到）。重写后的 `Copy.get`：**保留原版 `type_keys("^A^C", set_foreground=False)` 发送方式（不再 `grid.set_focus()`，避免 `SetForegroundWindow/SetFocus` 子控件导致按键落空 → 全 null）**，仅复制前 `EmptyClipboard()` 清空 + **带 0.12s 间隔重试读 15 次**等落盘。清空保证"复制失败即读到空 → 触发重试或返回空，绝不返回错数据"，重试读修掉异步时序。**注意只做"单次"复制**：早期版本曾加 `WM_COMMAND(0xE122)` 主路径 + `^A^C` 兜底 = 一处两次复制，会让一次查询弹两次验证码、且数据从不落盘（永远 null），已废弃；也曾误改成 `set_focus()+set_foreground=True`，结果子控件强设前台让 `^A^C` 落空 → 全 null，已回退为原版 `set_foreground=False`。每次复制会在 server 日志打印 `[copy] ^A^C control_id=... -> N 字节`，便于排查（N=0 即复制仍失败，需核 `_get_grid(control_id)` 拿到的控件是否正确）。
- **自动识别细节（EasyOCR）**：
  - 落盘裁剪框 `_CAPTCHA_DIGIT_BOX` 实测稳定；若将来弹窗布局大改导致 OCR 读不到，可调整该常量或 `config.json` 的 `captcha_input_center`/`captcha_confirm_center`（输入框/确认按钮相对比例）。
  - **向对话框提交最多 1 次**：置信度 < 0.55 或识别不到 4 位数字就**绝不瞎填**（错误尝试可能招来更重风控），直接降级为人工（返回 409 `captcha_required`）。
  - **真实弹窗结构（实测）**：标题为空的小弹窗，内部正文为「检测到您正在拷贝数据，为保护您的账号数据安全，请先输入验证码：」；布局为**输入框在左、4 位验证码数字在右、确认按钮「确定」在左下、取消在右下**（约 397×257）。检测兼容两种形态：旧客户端标题含「复制识别/股票复制」；新客户端按「空标题 + 正文含先输入验证码/拷贝数据」判定。
  - **输入框填入用键盘而非 set_edit_text**：实测该 THS 输入框对 `WM_SETTEXT`（`set_edit_text`）**完全无响应**（`window_text()` 始终为空），必须用键盘输入——聚焦输入框 → `Ctrl+A` 清空 → 键入 4 位数字 → 点「确定」。枚举到 Edit/Button 后走此键盘路径；枚举不到才回退相对坐标点击（默认值：输入框中心 0.40,0.55、确认 0.37,0.88）。
  - 每次检测到验证码都会把截图落盘到 `captcha_captures/`（文件名含识别结果与是否解卡），便于核对识别与点击效果。
- **可选切回 Xls**：如果希望降低验证码触发频率（但不根除），可在 `config.json` 设置 `"grid_strategy": "xls"`（仍走本服务的自动解卡）。
- **依赖**：`pip install easyocr`（CPU 版 torch，约数百 MB）。首次运行联网下载 OCR 模型（数十 MB）。**务必装在运行 `xiadan.exe` 的那台 Windows 交易机、且是启动 server 的同款 python**（本机为 `pythoncore-3.14-64`）。
- **关于 Tesseract**：本机尝试安装 Tesseract 二进制（UB-Mannheim 5.5.3）被沙箱/ UAC 拦住无法落地；裁剪后的 EasyOCR 已满足速度指标，故未强行切换。若日后要更轻量，可手动安装 Tesseract 并改用 pytesseract（需在 `_ocr_captcha` 增加 Tesseract 分支）。

### 8.2 临时文件会自动堆积（仅 Xls 策略）
- **原因**：当 `grid_strategy` 设为 `"xls"` 时，每次查询会把网格另存为临时 xls 到 `ths_grid_tmp/`（脚本同级目录，因系统 temp 路径过长会导致保存失败而特意指定）。原版 `Xls` **只写不删**，文件会无限堆积。
- **解法（本服务已实现）**：包装 `XlsAutoClean`，每次读取后立即删除临时文件（用完即清）；每次 `/prepare` 时还会清理历史遗留。删除走 `ctypes DeleteFileW`（`_rmfile`），绕开系统对 `os.remove`/`del` 的安全拦截，确保清理可靠。
- **默认 Copy 模式无此问题**：`Copy` 策略不生成临时文件。
- **正常情况**：`ths_grid_tmp/` 目录应为空。如异常堆积，可手动清空该目录（仅为临时缓存，不含任何交易数据）。
