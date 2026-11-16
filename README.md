# Ask-Advisor

本项目研究终端 Agent 何时应向外部 Advisor 求助，以及建议是否能改善任务完成情况。RTX 3090 运行 Harbor 和 Terminus 2，A40 提供执行模型、System One 评分和 Advisor 接入服务。

首次接手时，请先读[双机实验交接与操作顺序](docs/handoff.md)，再按下列入口查实现。

## 从哪里读代码

[代码目录逐文件说明](code/README.md)列出了运行入口、实验模块、联调脚本及每个测试文件的职责。

1. [Terminus 2 适配器](code/ask_advisor_runner/terminus_agent.py)调用[反馈标记逻辑](code/ask_advisor_runner/terminus_feedback.py)，标记一批终端命令产生的反馈。
2. [评分请求构造](code/ask_advisor_runner/chat_interceptor.py)中的 `_system_one_request` 包含完整英文判断指令、YES/NO 判据和输入字段。输入包括当前反馈步号，以及默认最多 24,000 字符的最近对话。
3. [System One 判断与 Advisor 调用](code/ask_advisor_runner/dsh_systemone_bridge.py)读取 YES/NO 概率及诊断。从第 3 个不同反馈起允许评分；当 p_yes ≥ 0.70 且诊断、去重和额度检查通过时，才允许请求建议，每任务最多 3 次。
4. [审计逻辑](code/ask_advisor_runner/runtime_composition.py)记录成功评分和桥接失败。[代理实现](code/ask_advisor_runner/proxy_runtime.py)记录聊天响应送达状态。

## 实验进展（2026-11-16）

make-doom-for-mips 的 30 分钟 trial 得分 0。Agent 在首次编译前超时；验收环境缺少 uvx，三项测试均未执行。4 次有效评分为 0.562177、0.437824、0.468791 和 0.294215，均低于门槛。另有 9 条桥接失败缺少失败阶段。普通 make 基线时限为 15 分钟，尚无法判断 Advisor 的效果。[完整实验结果与证据](docs/make-trial.md)


待验证的问题是评分上下文能否充分体现 Agent 遇到的困难，以及更换模型是否改变判断。正式审计保留了分数，未保存逐次请求正文。源码中的判断模板可查，历史输入重建需注明依据。

## 本地验证

从仓库根目录运行 Python 测试。涉及回环套接字的测试需要本机允许绑定临时端口。

```sh
python3 -m unittest discover -s code/tests
```

真实双机运行还依赖 Harbor 0.23、支持 System One 接口的 vLLM 和实验专用 DSH 服务。
