# easytrader 远端交易 · M1 Mac 操作演示文档

## 一、架构与角色定位

```
M1 Mac (你, 发指令)          Windows 交易电脑 (装 easytrader + 同花顺)
┌─────────────────┐         ┌──────────────────────────────┐
│  mac_trader_     │  HTTP   │  easytrader server (Flask)   │
│  client.py       │ ──────> │  :1430                       │
│  requests 库     │  :1430  │        │                     │
└─────────────────┘         │        v                     │
                            │  同花顺 xiadan.exe (需先登录) │
                            └──────────────────────────────┘
```

- **Mac 是指令端**。通过 HTTP 请求指挥 Windows 交易电脑下单。
- **Windows 是交易服务端**。运行 easytrader 的 Flask 服务，并操作同花顺客户端。

## 二、为什么 Mac 不装 easytrader

easytrader 的 `requirements.txt` 里有 `pywinauto==0.6.6`，这是 Windows 专用的 GUI 自动化库，Mac 上无法安装。所以 Mac 端 pip install easytrader 会失败。

解决方案：Mac 端只用 `requests` 直接调 Windows 上的 HTTP API。零依赖陷阱，跨平台无障碍。本仓库已提供 `mac_trader_client.py` 封装好全部接口。

> 来源：easytrader `requirements.txt` 与 `easytrader/server.py`、`easytrader/remoteclient.py` 源码（GitHub）

## 三、前置准备（Windows 端，一次性）

1. 在 Windows 上手动打开并登录同花顺下单程序 `xiadan.exe`（同花顺不支持自动登录）
2. 在 Windows 上运行 `easytrader_server.py`，启动 Flask 监听 `0.0.0.0:1430`
3. Windows 防火墙放行 1430 端口
4. 拿到 Windows 电脑的 IP（内网如 `192.168.x.x`，公网则用公网 IP）

## 四、Mac 端环境准备

```bash
# macOS 自带 python3, 或 brew install python
pip3 install requests
```

把 `mac_trader_client.py` 拷到 Mac 任意目录。

## 五、三步上手演示

### 第 1 步：改配置

打开 `mac_trader_client.py`，改 `__main__` 里的配置区：

```python
WIN_HOST = "192.168.1.100"            # Windows 交易电脑 IP
BROKER = "ths"                         # 同花顺专用客户端
ACCOUNT = "你的资金账号"
PASSWORD = "你的明文密码"
EXE_PATH = r"C:\htzqzyb2\xiadan.exe"   # Windows 上 xiadan.exe 路径
```

### 第 2 步：确认 Windows 端就绪

Windows 上同花顺已登录 + server 已启动。Mac 上先 ping 通：

```bash
ping 192.168.1.100
curl http://192.168.1.100:1430/balance
```

能返回 JSON（或登录提示）就说明通了。

### 第 3 步：运行演示

```bash
python3 mac_trader_client.py
```

会依次执行：登录 → 查资金/持仓 → 查当日委托/成交 → 退出。买入卖出默认注释掉，确认无误后取消注释即可实盘。

## 六、完整调用流程（演示脚本里的步骤）

| 步骤 | 方法 | HTTP | 说明 |
|---|---|---|---|
| 1 登录 | `t.prepare(...)` | POST /prepare | 传 broker 账号密码 exe_path |
| 2 查资金 | `t.balance` | GET /balance | 返回可用资金、总资产等 |
| 3 查持仓 | `t.position` | GET /position | 返回各股票持仓 |
| 4 买入 | `t.buy(code, price, amount)` | POST /buy | 返回 entrust_no 委托号 |
| 5 卖出 | `t.sell(code, price, amount)` | POST /sell | 返回 entrust_no |
| 6 撤单 | `t.cancel_entrust(no)` | POST /cancel_entrust | 传委托号 |
| 7 当日委托 | `t.today_entrusts` | GET /today_entrusts | 含已报/已成/已撤 |
| 8 当日成交 | `t.today_trades` | GET /today_trades | 仅成交记录 |
| 9 一键打新 | `t.auto_ipo()` | GET /auto_ipo | 自动申购今日新股 |
| 10 退出 | `t.exit()` | GET /exit | 关闭客户端 |

## 七、curl 命令速查（不改代码也能测）

把 `WIN_IP` 换成你的 Windows 交易电脑 IP。

```bash
# 登录
curl -X POST http://WIN_IP:1430/prepare \
  -H "Content-Type: application/json" \
  -d '{"broker":"ths","user":"账号","password":"密码","exe_path":"C:\\htzqzyb2\\xiadan.exe"}'

# 查资金 / 持仓
curl http://WIN_IP:1430/balance
curl http://WIN_IP:1430/position

# 买入 / 卖出
curl -X POST http://WIN_IP:1430/buy \
  -H "Content-Type: application/json" \
  -d '{"security":"162411","price":0.55,"amount":100}'

curl -X POST http://WIN_IP:1430/sell \
  -H "Content-Type: application/json" \
  -d '{"security":"162411","price":0.60,"amount":100}'

# 撤单
curl -X POST http://WIN_IP:1430/cancel_entrust \
  -H "Content-Type: application/json" \
  -d '{"entrust_no":"委托号"}'

# 当日委托 / 成交 / 退出
curl http://WIN_IP:1430/today_entrusts
curl http://WIN_IP:1430/today_trades
curl http://WIN_IP:1430/exit
```

## 八、broker 类型对照表

按 Windows 上实际装的客户端传对应字符串（来源 `easytrader/api.py` 的 `use()` 函数）。

| broker 字符串 | 券商客户端 |
|---|---|
| `ths` / 同花顺客户端 | 其他券商专用同花顺客户端（需手动登录） |
| `universal_client` / 通用同花顺客户端 | 同花顺免费版通用客户端 |
| `ht_client` / 华泰客户端 | 华泰（需额外传 comm_password 通讯密码） |
| `htzq_client` / 海通证券客户端 | 海通 |
| `gj_client` / 国金客户端 | 国金 |
| `yh_client` / 银河客户端 | 银河 |
| `wk_client` / 五矿客户端 | 五矿 |
| `gf_client` / 广发客户端 | 广发 |
| `xq` / 雪球 | 雪球组合（非实盘） |
| `miniqmt` | miniqmt 官方量化接口 |

你这台 Windows 装的是同花顺客户端，演示里用 `ths`。如果用的是同花顺免费版通用客户端，改成 `universal_client`。

## 九、常见问题排查

- **Mac 连不上 1430**
  Windows 防火墙没放行，或 IP 不对，或不在同一网段。公网访问需路由器端口转发或用 SSH 隧道。

- **prepare 报 connect 失败**
  同花顺没在 Windows 上登录、`exe_path` 路径不对、`broker` 类型与实际客户端不匹配。同花顺 `ths` 必须先手动登录 xiadan.exe。

- **balance/position 返回空或报错**
  部分券商禁剪切板拷贝，导致 server 取不到持仓。需在 Windows server 端设 `user.grid_strategy = grid_strategies.Xls`（改 server 源码或 prepare 后回调里设）。

- **buy 返回了但没成交**
  正常。返回的是 `entrust_no` 委托号，表示委托已报。是否成交看 `today_trades`。

- **Mac 上 pip install easytrader 失败**
  预期行为。pywinauto 不支持 Mac。本方案本就不需要装 easytrader，只装 requests。

## 十、安全建议（重要）

`server.py` 源码里没有任何鉴权。1430 端口直接暴露公网等于把交易权限敞开给全网。三种加固方式任选：

1. **SSH 隧道（推荐，最省事）**
   Mac 上执行：
   ```bash
   ssh -L 1430:localhost:1430 你的用户名@Windows公网IP
   ```
   然后 `mac_trader_client.py` 里 `WIN_HOST` 改成 `127.0.0.1`，流量走加密隧道，不暴露端口。

2. **Nginx 反向代理 + Basic Auth**
   Windows 上用 Nginx 反代 1430，加 HTTP Basic 认证。

3. **限定内网 / VPN**
   只在内网或 VPN 环境访问，公网完全不开放 1430。

## 十一、来源

- easytrader 远端服务文档 https://easytrader.readthedocs.io/zh-cn/master/remote/
- easytrader 使用文档 https://easytrader.readthedocs.io/zh-cn/master/usage/
- 源码 https://github.com/shidenggui/easytrader
  - `easytrader/server.py`（Flask 路由定义）
  - `easytrader/remoteclient.py`（远端客户端实现）
  - `easytrader/api.py`（`use()` 函数 broker 映射）
  - `requirements.txt`（pywinauto 等 Windows 依赖）
