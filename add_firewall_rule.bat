@echo off
:: 以管理员身份运行此文件, 添加 1430 端口入站防火墙规则
:: 右键 -> 以管理员身份运行

netsh advfirewall firewall add rule name="easytrader-server-1430" dir=in action=allow protocol=TCP localport=1430

if %errorlevel% equ 0 (
    echo.
    echo [+] 防火墙规则已添加, 端口 1430 已放行
    echo [+] Mac 现在可以通过局域网 IP 访问了
) else (
    echo.
    echo [!] 添加失败, 请右键此文件选择"以管理员身份运行"
)

pause
