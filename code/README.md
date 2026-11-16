# Ask-Advisor 代码导读

第一次读真实实验代码，可沿一批命令反馈追踪请求：3090 的 Terminus 2 适配器标记反馈，A40 的代理构造评分问题，System One 返回概率；满足条件后才进入 DSH 获取建议。双机启动顺序和验收方法见[实验交接指南](../docs/handoff.md)。

## 终端反馈、评分与响应

| 文件 | 职责 |
|---|---|
| [terminus_agent.py](ask_advisor_runner/terminus_agent.py) | Harbor 导入的自定义 Terminus 2 Agent；在调用模型之前接入命令反馈标记。 |
| [terminus_feedback.py](ask_advisor_runner/terminus_feedback.py) | 核对上一批 bash 命令的观察结果与待发送消息，给匹配的请求附上反馈摘要。 |
| [chat_interceptor.py](ask_advisor_runner/chat_interceptor.py) | 识别新反馈、去重、构造 System One 问题和 Advisor prompt，并把返回的建议加入后续模型输入。查看判断指令请找 _system_one_request。 |
| [proxy_http.py](ask_advisor_runner/proxy_http.py) | 校验聊天请求的路由和 JSON，调用拦截器后转发请求；定义普通响应和流式响应的表示。 |
| [proxy_runtime.py](ask_advisor_runner/proxy_runtime.py) | 实现带认证的 HTTP 服务和到 vLLM 的回环传输，转发聊天与评分请求，记录响应送达状态。 |
| [dsh_systemone_bridge.py](ask_advisor_runner/dsh_systemone_bridge.py) | 从原生评分结果读取 p_yes 并检查诊断和阈值，决定是否进入一次 DSH 咨询。 |
| [runtime_composition.py](ask_advisor_runner/runtime_composition.py) | 将评分、一次 Advisor 调用和审计组合起来，分别支持正式模式与仅评分模式。 |
| [runtime_entrypoint.py](ask_advisor_runner/runtime_entrypoint.py) | 正式代理启动入口。从机内环境读取配置，核验会话材料，选择适配器、模式并管理服务生命周期。 |
| [baseline_entrypoint.py](ask_advisor_runner/baseline_entrypoint.py) | 普通 Qwen 基线的认证聊天转发入口。 |
| [harbor_integration.py](ask_advisor_runner/harbor_integration.py) | 构造早期 Harbor 内置 qwen-coder 适配器使用的代理配置。Terminus 2 实验的适配器入口见本表第一项。 |

## DSH 会话、通信与 Advisor 建议

| 文件 | 职责 |
|---|---|
| [dsh_managed_process.py](ask_advisor_runner/dsh_managed_process.py) | 管理由程序启动的 DSH 子进程，读取启动信息并在退出或启动失败时清理进程。 |
| [dsh_browser_session.py](ask_advisor_runner/dsh_browser_session.py) | 用启动 URL 建立带 Cookie 的认证会话，为 HTTP 和 WebSocket 提供访问。 |
| [dsh_runtime_transport.py](ask_advisor_runner/dsh_runtime_transport.py) | 一次性读取启动 URL，将底层 WebSocket 帧封装成 JSON 消息。 |
| [dsh_stdlib_websocket.py](ask_advisor_runner/dsh_stdlib_websocket.py) | 使用 Python 标准库连接回环 WebSocket，处理握手、文本帧和 ping。 |
| [dsh_transport.py](ask_advisor_runner/dsh_transport.py) | 封装只读 RPC，执行会话列表请求或读取一次 follow 快照后关闭流。 |
| [dsh_secure_transport.py](ask_advisor_runner/dsh_secure_transport.py) | 管理认证会话的打开和关闭，将只读操作限定在经过验证的会话范围内。 |
| [dsh_readonly.py](ask_advisor_runner/dsh_readonly.py) | 将会话列表与单次快照整理成根会话候选及结构摘要。 |
| [dsh_candidate_confirmation.py](ask_advisor_runner/dsh_candidate_confirmation.py) | 为候选根会话生成匿名摘要，确认选择后创建身份固定记录（pin）。 |
| [dsh_session_identity.py](ask_advisor_runner/dsh_session_identity.py) | 通过加盐指纹核验固定会话身份，生成一次性验证引用。 |
| [dsh_session_pin_store.py](ask_advisor_runner/dsh_session_pin_store.py) | 保存和加载 pin，核验文件权限、完整性与路径。 |
| [dsh_prompt_transport.py](ask_advisor_runner/dsh_prompt_transport.py) | 向已确认的会话发送一次授权 prompt，校验请求格式。 |
| [dsh_one_shot_bridge.py](ask_advisor_runner/dsh_one_shot_bridge.py) | 将授权绑定到指定 prompt 的哈希，并限制对应审批只能使用一次。 |
| [dsh_one_shot_flow.py](ask_advisor_runner/dsh_one_shot_flow.py) | 串联发送 prompt、处理审批和读取结果，将合格的建议交还调用方。 |
| [dsh_approval_transport.py](ask_advisor_runner/dsh_approval_transport.py) | 从事件流接收审批请求，对匹配的请求发送一次答复。 |
| [dsh_tool_call_resolver.py](ask_advisor_runner/dsh_tool_call_resolver.py) | 从会话快照中定位本次 Advisor 工具调用并核对其参数摘要。 |
| [dsh_advisor_result_reader.py](ask_advisor_runner/dsh_advisor_result_reader.py) | 从事件或快照读取与该调用匹配的结果，校验后提取建议。 |

## 离线实验与共用组件

| 文件 | 职责 |
|---|---|
| [__init__.py](ask_advisor_runner/__init__.py) | 包的公共导入入口，汇总供脚本和测试使用的类与函数。 |
| [config.py](ask_advisor_runner/config.py) | 定义实验配置及取值约束。 |
| [state.py](ask_advisor_runner/state.py) | 保存一次实验运行的状态。 |
| [runner.py](ask_advisor_runner/runner.py) | 离线运行器；处理评分、实验分组、调用额度和状态记录。 |
| [audit.py](ask_advisor_runner/audit.py) | 以追加方式写 JSONL 审计记录。 |
| [bridge.py](ask_advisor_runner/bridge.py) | 对审批参数计算规范化哈希，检查指定工具、会话和一次性审批条件。 |
| [dsh_events.py](ask_advisor_runner/dsh_events.py) | 用模拟事件核对从请求到结果的审计顺序和关联关系。 |
| [identifiers.py](ask_advisor_runner/identifiers.py) | 生成运行及运行范围内的标识符。 |
| [redaction.py](ask_advisor_runner/redaction.py) | 检测敏感文本，供调用方在记录或发送前检查。 |

## 独立脚本

| 文件 | 职责 |
|---|---|
| [check_proxy_deployment.py](check_proxy_deployment.py) | 检查部署环境变量是否齐备，只打印状态摘要，不启动服务。 |
| [inspect_dsh_e2e_structure.py](inspect_dsh_e2e_structure.py) | 早期 DSH 联调的结构检查脚本，会启动子进程；使用历史 web 子命令。 |
| [run_minimal_dsh_e2e.py](run_minimal_dsh_e2e.py) | 早期真实 DSH 单次咨询脚本，包含 prompt、审批与结果处理。当前部署的启动方式见交接指南。 |
| [tools/offline_demo.py](tools/offline_demo.py) | 用离线示例演示运行器并生成实验产物。 |

## 测试文件

| 文件 | 职责 |
|---|---|
| [test_chat_interceptor.py](tests/test_chat_interceptor.py) | 检查起评步数、去重、建议注入和每任务调用上限。 |
| [test_dsh_advisor_result_reader.py](tests/test_dsh_advisor_result_reader.py) | 检查结果关联、快照回退和无效建议处理。 |
| [test_dsh_approval_transport.py](tests/test_dsh_approval_transport.py) | 检查审批事件匹配、一次性答复和连接关闭。 |
| [test_dsh_browser_session.py](tests/test_dsh_browser_session.py) | 检查回环地址约束、Cookie 传递、凭据清理和认证失败。 |
| [test_dsh_candidate_confirmation.py](tests/test_dsh_candidate_confirmation.py) | 检查候选推荐、人工选择和重复会话处理。 |
| [test_dsh_managed_process.py](tests/test_dsh_managed_process.py) | 检查子进程正常退出、启动超时和提前终止时的清理。 |
| [test_dsh_one_shot_bridge.py](tests/test_dsh_one_shot_bridge.py) | 检查 prompt 哈希、单次授权以及重放处理。 |
| [test_dsh_one_shot_flow.py](tests/test_dsh_one_shot_flow.py) | 检查咨询步骤顺序，以及审批拒绝或结果不匹配时的处理。 |
| [test_dsh_readonly.py](tests/test_dsh_readonly.py) | 检查根会话筛选和快照字段，验证只读限制。 |
| [test_dsh_real_component_rehearsal.py](tests/test_dsh_real_component_rehearsal.py) | 使用脚本化传输串联实际组件，演练一次合成咨询流程。 |
| [test_dsh_runtime_transport.py](tests/test_dsh_runtime_transport.py) | 检查启动凭据的一次性读取与 JSON 帧编解码。 |
| [test_dsh_secure_transport.py](tests/test_dsh_secure_transport.py) | 检查认证会话生命周期和一次性身份引用。 |
| [test_dsh_session_identity.py](tests/test_dsh_session_identity.py) | 检查指纹匹配、篡改检测及审计字段。 |
| [test_dsh_session_pin_store.py](tests/test_dsh_session_pin_store.py) | 检查 pin 文件权限、篡改、符号链接和重复写入。 |
| [test_dsh_stdlib_websocket.py](tests/test_dsh_stdlib_websocket.py) | 检查回环握手、帧掩码、Cookie 和 ping。 |
| [test_dsh_systemone_bridge.py](tests/test_dsh_systemone_bridge.py) | 检查概率字段、阈值边界和无效评分处理。 |
| [test_dsh_tool_call_resolver.py](tests/test_dsh_tool_call_resolver.py) | 检查调用定位，覆盖参数不匹配和重复调用。 |
| [test_dsh_transport.py](tests/test_dsh_transport.py) | 检查只读 RPC 封装，以及读取快照后的取消与关闭。 |
| [test_offline_bridge.py](tests/test_offline_bridge.py) | 检查审批的工具名、会话、哈希和重试限制。 |
| [test_offline_dsh_events.py](tests/test_offline_dsh_events.py) | 检查事件顺序、重放、审批关联和结果链。 |
| [test_offline_runner.py](tests/test_offline_runner.py) | 检查实验分组、评分解析、去重、额度及状态管理。 |
| [test_proxy_http.py](tests/test_proxy_http.py) | 检查路由、异常请求、建议注入与流式响应转发。 |
| [test_proxy_runtime.py](tests/test_proxy_runtime.py) | 检查回环服务的认证、上游路由、流式送达和客户端断开。 |
| [test_runtime_composition.py](tests/test_runtime_composition.py) | 检查正式与仅评分模式的组合、审计和部署检查。 |
| [test_runtime_entrypoint.py](tests/test_runtime_entrypoint.py) | 检查启动日志选择、文件权限和审批策略。 |
| [test_terminus_feedback.py](tests/test_terminus_feedback.py) | 检查 Terminus 2 命令反馈匹配及请求标记。 |

在仓库根目录执行 `python3 -m unittest discover -s code/tests` 可运行测试。
