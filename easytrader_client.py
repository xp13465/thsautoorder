# easytrader 远端客户端
# 运行在策略服务器上, 通过 HTTP 指挥本地交易电脑下单
# 依赖: pip install easytrader requests
#
# 前置条件:
#   1. 本地交易电脑已启动 easytrader_server.py (端口 1430 已开放)
#   2. 本地交易电脑已手动登录券商客户端 (同花顺 ths 必须先手动登录)
#
# broker 可选值:
#   ths              其他券商专用同花顺客户端 (需手动登录)
#   universal_client 通用同花顺客户端
#   yh_client        银行客户端
#   ht_client        华泰客户端 (需额外传 comm_password)
#   htzq_client      海通客户端
#   gj_client        国金客户端
#   xq               雪球组合

import easytrader.remoteclient as remoteclient

# 1. 连接本地交易服务, host 填交易电脑的 IP (内网或公网)
user = remoteclient.use(
    broker="ths",           # 改成你的券商类型
    host="192.168.1.100",   # 改成交易电脑 IP
    port=1430,
)

# 2. 登录客户端
#    同花顺 ths: 传 exe_path 让 server 端 connect 已登录的 xiadan.exe
#    其他客户端: 传 user/password 可自动登录
user.prepare(
    user="资金账号",
    password="明文密码",
    exe_path=r"C:\htzqzyb2\xiadan.exe",  # xiadan.exe 绝对路径, 改成你的
    # comm_password="通讯密码",          # 仅华泰 ht_client 需要
)

# 3. 查询
print("资金:", user.balance)
print("持仓:", user.position)

# 4. 买入: 证券代码, 价格, 数量(股)
result = user.buy("162411", price=0.55, amount=100)
print("买入委托号:", result)

# 5. 卖出
result = user.sell("162411", price=0.60, amount=100)
print("卖出委托号:", result)

# 6. 撤单, 传 buy/sell 返回的 entrust_no
# user.cancel_entrust("委托号")

# 7. 查询当日数据
print("当日委托:", user.today_entrusts)
print("当日成交:", user.today_trades)

# 8. 一键打新
# user.auto_ipo()

# 9. 退出
user.exit()
