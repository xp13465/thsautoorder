# easytrader 交易服务端 · 部署包

把本目录整体拷到装有同花顺 `xiadan.exe` 的 Windows 交易电脑上即可使用。

## 1. 环境要求
- Windows 10/11，并已安装、且**手动登录**过同花顺 `xiadan.exe`（同花顺必须手动登录一次，服务端无法代登）
- Python 3.10 ~ 3.14（实测 3.14 可跑，32 位客户端 + 64 位 python 的告警无害）
- 调用方（策略机/Mac）与服务端在同一局域网，或经 VPN/隧道可达

## 2. 安装依赖
```bat
pip install -r requirements.txt
```

## 3. 启动（推荐用管理器，带守护进程）
```bat
python easytrader_manager.py start      # 后台启动
python easytrader_manager.py daemon     # 守护进程（崩溃自动重启，推荐长期运行）
python easytrader_manager.py status     # 查看运行状态
python easytrader_manager.py stop       # 停止
python easytrader_manager.py logs       # 查看日志
```
也可直接运行核心服务（无守护）：
```bat
python easytrader_server_auth.py
```

## 4. 防火墙（允许局域网远端访问）
以**管理员**身份运行 `add_firewall_rule.bat`，放行 1430 端口入站。
或手动：
```bat
netsh advfirewall firewall add rule name="easytrader" dir=in action=allow protocol=TCP localport=1430
```

## 5. 访问
- 本地面板（浏览器）： http://localhost:1430/
- 健康检查（免 Token）： http://localhost:1430/health

## 6. 鉴权
- 默认 Token： `easytrader-secret-2024`
- **改 Token 首选改 `config.json`**（与脚本同目录，首次启动自动生成）：把 `"token"` 字段改成你自己的随机字符串。
  ```json
  { "token": "你的密钥", "host": "0.0.0.0", "port": 1430, "exe_path": "D:\\路径\\xiadan.exe" }
  ```
- 也可用环境变量覆盖（优先级高于 `config.json`）： `set EASYTRADER_TOKEN=你的密钥`
- 调用方式：请求头 `X-Token: <token>` 或查询参数 `?token=<token>`
- 免鉴权路由： `/`、`/test`、`/health`（面板与健康检查）

## 7. 远端调用（Mac / 策略服务器）
- 完整接口文档： `easytrader_api_doc.md`
- 客户端示例： `easytrader_client.py` / `mac_trader_client.py`
- 调用地址： `http://<交易电脑IP>:1430`

## 8. 文件说明
| 文件 | 用途 |
|---|---|
| `easytrader_server_auth.py` | 核心增强服务端（Token 鉴权 + 多账户切换 + 内置面板） |
| `easytrader_test.html` | 内置测试面板（`/` 路由读取） |
| `easytrader_manager.py` | 服务管理器（启停 / 守护进程） |
| `easytrader_api_doc.md` | 网络请求 API 文档 |
| `easytrader_client.py` / `mac_trader_client.py` | 远端 HTTP 客户端示例 |
| `easytrader_mac_demo.md` | Mac 端调用示例 |
| `benchmark_engines.py` | 离线对比 EasyOCR vs Tesseract 识别引擎（可选，需自备验证码样本） |
| `add_firewall_rule.bat` / `easytrader_start.bat` / `easytrader_stop.bat` | 运维批处理 |

## 9. 切换账户说明（重要）
服务端切换账户采用**点击账户下拉框列表项**的方式（非 Alt+N 快捷键），
会自动发现并排除末尾的「编辑账户」项，避免弹窗卡死。
切换接口支持三种定位：`?account=资金账号` / `?hotkey=N` / `?label=标签`。
详见 `easytrader_api_doc.md` 的 `/switch` 章节。

## 10. 注意事项 / 已知问题

### 同花顺「股票复制识别」验证码（默认自动解卡）
- **默认 `Copy` 策略**（`config.json` 的 `grid_strategy="copy"`）走 easytrader 原生 `Ctrl+C` 复制剪贴板读取，高频查询会让同花顺弹出「股票复制识别」验证码、卡死客户端。
- 本服务**内置 EasyOCR 自动识别并填入验证码**：检测弹窗 → 截图 → 局部裁剪数字条识别（~0.3s，100% 样本）→ 键盘填入并确认 → 验证关闭，全程对调用方透明。
- **务必安装 EasyOCR**：`pip install easyocr`（会一并拉取 CPU 版 torch，约数百 MB；首次运行联网下载 OCR 模型数十 MB）。必须装在**运行 `xiadan.exe` 的那台 Windows 交易机、且是启动本服务所用的同一个 python**（本机为 `pythoncore-3.14-64`）。装好后在 `/health` 可见 `ocr_ready=true`。
- **冷启动已规避**：服务启动时即预热 EasyOCR，首个验证码不会出现 ~5s 的推理尖刺。
- （可选）切到 `Xls` 策略降低验证码触发频率（仍走同一套自动解卡）：`config.json` 设 `"grid_strategy": "xls"`。它只是降频、不根除；默认 `Copy` 已被自动解卡完全覆盖，无需手动处理。
- 如需离线对比/压测识别引擎，跑同目录 `benchmark_engines.py`（Tesseract 对比可选：`pip install pytesseract` 并另行安装 Tesseract 二进制）。
- 详见 `easytrader_api_doc.md` 第 8.1 节。

### 临时文件自动清理
- `Xls` 策略每次查询把网格另存为临时 xls 到 `ths_grid_tmp/`（脚本同级目录）。原版只写不删，会堆积。
- 本服务已包装 `XlsAutoClean`：读取后即删（用完即清）+ 每次 `/prepare` 清理历史遗留；删除走 `ctypes DeleteFileW` 绕开系统对 `os.remove`/`del` 的安全拦截，清理可靠。正常情况下该目录应为空。
