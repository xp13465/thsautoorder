# M1 Mac 远端交易客户端
# 直接用 requests 调 Windows 交易电脑上的 easytrader server (Flask :1430)
# 无需在 Mac 上安装 easytrader (它依赖 Windows 专用库 pywinauto, Mac 装不了)
# 仅需: pip3 install requests
import requests


class MacTrader:
    def __init__(self, host, port=1430):
        self.base = f"http://{host}:{port}"
        self.s = requests.session()

    @staticmethod
    def _check(r):
        if r.status_code >= 300:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            raise Exception(f"HTTP {r.status_code}: {err}")
        return r.json()

    def prepare(self, broker, user=None, password=None, exe_path=None, comm_password=None):
        params = {"broker": broker}
        if user is not None:
            params["user"] = user
        if password is not None:
            params["password"] = password
        if exe_path is not None:
            params["exe_path"] = exe_path
        if comm_password is not None:
            params["comm_password"] = comm_password
        return self._check(self.s.post(self.base + "/prepare", json=params))

    @property
    def balance(self):
        return self._check(self.s.get(self.base + "/balance"))

    @property
    def position(self):
        return self._check(self.s.get(self.base + "/position"))

    def buy(self, security, price, amount):
        return self._check(self.s.post(
            self.base + "/buy",
            json={"security": security, "price": price, "amount": amount},
        ))

    def sell(self, security, price, amount):
        return self._check(self.s.post(
            self.base + "/sell",
            json={"security": security, "price": price, "amount": amount},
        ))

    def cancel_entrust(self, entrust_no):
        return self._check(self.s.post(
            self.base + "/cancel_entrust",
            json={"entrust_no": entrust_no},
        ))

    @property
    def today_entrusts(self):
        return self._check(self.s.get(self.base + "/today_entrusts"))

    @property
    def today_trades(self):
        return self._check(self.s.get(self.base + "/today_trades"))

    def auto_ipo(self):
        return self._check(self.s.get(self.base + "/auto_ipo"))

    def exit(self):
        return self._check(self.s.get(self.base + "/exit"))


if __name__ == "__main__":
    # === 配置区, 改成你的 ===
    WIN_HOST = "192.168.1.100"              # Windows 交易电脑 IP
    BROKER = "ths"                          # 同花顺专用客户端用 ths; 通用同花顺用 universal_client
    ACCOUNT = "资金账号"
    PASSWORD = "明文密码"
    EXE_PATH = r"D:\同花顺软件\同花顺\同花顺\同花顺\xiadan.exe"

    t = MacTrader(WIN_HOST)

    # 1. 登录 (同花顺需先在 Windows 上手动登录 xiadan.exe)
    print("登录:", t.prepare(BROKER, user=ACCOUNT, password=PASSWORD, exe_path=EXE_PATH))

    # 2. 查询资金与持仓
    print("资金:", t.balance)
    print("持仓:", t.position)

    # 3. 买入演示 (证券代码, 价格, 数量-股)
    #    r = t.buy("162411", price=0.55, amount=100)
    #    print("买入委托号:", r)

    # 4. 卖出演示
    #    r = t.sell("162411", price=0.60, amount=100)
    #    print("卖出委托号:", r)

    # 5. 撤单 (传 buy/sell 返回的 entrust_no)
    #    t.cancel_entrust("委托号")

    # 6. 查询当日委托与成交
    print("当日委托:", t.today_entrusts)
    print("当日成交:", t.today_trades)

    # 7. 退出
    print("退出:", t.exit())
