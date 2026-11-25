"""测试用的五功能模块（不参与产品运行）。

T1 WiFi 信道体检 / T3 BLE 广播信道风险评估 / T4 5.8G 频段守卫 / T5 可疑信号
抓拍存证 —— 这四项是前期**测试用途**的模块，产品只做信道检测（全频段滚动扫描 +
记忆热力），所以它们已从产品注册表里移出来。

为什么放在 ``_legacy/`` 而不是直接删：``core.registry.discover()`` 用
``pkgutil.iter_modules()`` 扫 ``modes/`` 顶层，**跳过下划线开头的名字** ——
放这里既不进产品注册表（上位机「单射频分时」的公平份额只剩 waterfall 一个
模块 = 100%），又保留全部代码与测试可跑。

跑法（与从前一致，只是路径多一层）：

    python -m modes._legacy.ble_risk --scenario ble_crowded
    python tests/test_ble_risk.py -v
"""
