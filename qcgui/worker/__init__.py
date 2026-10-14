"""worker 子进程。

**这是整个项目里唯一 import 量化引擎的地方。**
界面进程与 JobManager 都不导入 ``qcgui.engines``，因此界面地址空间里
永远不存在 psi4 / ORCA 相关代码，引擎崩溃不会连带界面，取消就是杀进程。
"""
