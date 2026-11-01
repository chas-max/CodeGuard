"""CodeGuard-Agent：基于 LangGraph 与沙箱自愈的企业级 Repo 自动化 Bug 修复与单测生成 Multi-Agent 系统。

四能力闭环：
1. 自动发现异常：machine_review 静态门控（AST 语法 + 高危调用 + 签名契约）+ 沙箱 F2P/P2P 测试失败捕获
2. 规划处理方案：director_plan（AST 调用图定位 Top-K 故障文件 + 符号契约冻结）+ director_resolve（异常统筹）
3. 执行处理：write_draft 四阶段补丁编写 + sandbox_execute 逐槽位 ReAct 自愈
4. 检测处理结果：F2P 复现测试 + P2P 回归测试 + 分支覆盖率三重确定性门控
"""

__version__ = "0.2.0"
