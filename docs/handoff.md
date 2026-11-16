# 双机实验交接

本文用于接手已有 RTX 3090 和 A40 部署。接手人需从项目负责人取得两机登录权限、3090 上既有的受控 Docker 权限，以及 A40 实验专用 DSH 配置、固定会话 pin 和代理凭据的机内位置。模型权重、任务镜像和提供原生 System One 接口的 vLLM 安装在实验机上。新机器从零部署这些外部组件尚未独立验证。

## 先确认实验停点

make 已完成一次真实运行，证据和局限见 [make 复盘](make-trial.md)。下一项已准备的 DNA 对照使用同一 Terminus 2、同一模型和同一 Verifier。其普通基线为 2 步、1 次工具调用、Reward 0。实验组只完成配置预览。恢复时以机器实际状态为准，先在 A40 检查 vLLM 进程和接口。真实 DNA trial 需负责人单独批准。

RTX 3090 工作根为 `$HOME/ask-advisor-e2e-3090`。以下为已有部署中的相对路径。

| 要找什么 | 工作根下的位置 |
|---|---|
| 30 分钟 make 实验组 | `jobs-terminus2-advisor/make-terminus2-advisor-20261115-01/` |
| 15 分钟 make 普通基线 | `jobs/2026-11-15__13-59-56/` |
| 30 分钟 DNA 普通基线 | `dna-terminus2.eDbixk/jobs/2026-11-15__14-20-10/` |
| 预览通过的 DNA 实验组配置 | `dna-terminus2-advisor-20261115-01.yaml` |
| 为 DNA 预留的结果根 | `jobs-dna-terminus2-advisor/` |

## 3090 核对运行环境

在上述工作根下，用原 Harbor 0.23.0 环境预览配置：

```sh
cd "$HOME/ask-advisor-e2e-3090"
venv-harbor023/bin/harbor run --config dna-terminus2-advisor-20261115-01.yaml --print-config
```

确认模型为 `openai/qwen3.8-27b-4bit`，适配器为 `ask_advisor_runner.terminus_agent:AskAdvisorTerminus2`，任务为冻结的 dna-assembly，并发与任务数均为 1。原任务定义来自 Terminal-Bench 提交 `7131e4375048a0e408a8fb404b5f499d726b695b`，task.toml 将 Agent 与 Verifier 时限分别设为 1800 秒。可在 Harbor 的 Python 环境将本仓库 code 目录放入 PYTHONPATH 后导入该适配器，以核实实际执行环境看得到它。

沿用机上已授权的 Docker 配置检查镜像，不改变共享权限：

```sh
sudo env DOCKER_CONFIG="$HOME/ask-advisor-e2e-3090/docker-config" docker image inspect --format '{{.Id}}' alexgshaw/dna-assembly:20251031
```

应得到 `sha256:940e10327e89b8034aa335c48e8e643414809f5ee2ffd2d2854688b996c822ee`。若不匹配，先核实镜像来源。make 曾因验收环境下载失败、找不到 uvx 而没有运行 pytest；报告成绩时须同时核对 verifier 输出。

## A40 恢复与验证

1. 在 A40 确认当前服务进程和本次启动日志，核对 vLLM 的模型名、回环监听及就绪状态。运行包须支持 `/v1/chat/completions` 与 `/v1/systemone`。只读核对可用模型时向现用的回环 origin 请求 `/v1/models`。端口从本次机内服务配置读取。
2. 使用既有实验 DSH 配置，沿用机内同值 HOME 和 DSH_HOME、原 Node PATH、`--profile a40-web`、回环监听与 `--no-open`。DSH 的模型地址带 `/v1`。按部署包的 RPC 格式 POST 到 `/api/session/list`，先确认未认证返回 401，再用本次 bootstrap 验证唯一根会话与本地 pin 唯一匹配。早期探索脚本里的 web 子命令不适用于该部署。
3. 为本次 trial 分配新的代理进程、回环端口和空审计文件，避免继承前次去重集合及调用额度。以机内环境注入下表参数。在仓库 code 目录执行 `python3 -m ask_advisor_runner.runtime_entrypoint`，应出现 `ASK_ADVISOR_PROXY=RUNNING`。未携带凭据 POST 到新代理聊天接口应返回 401。

| 正式代理变量 | 填写方式 |
|---|---|
| ASK_ADVISOR_ENABLE_RUNTIME、ASK_ADVISOR_HARNESS、ASK_ADVISOR_RUNTIME_MODE | 分别为 1、terminus-2、advisor |
| ASK_ADVISOR_MODEL | vLLM 当前 served model 名；Harbor 的 openai/ 为客户端 provider 前缀 |
| ASK_ADVISOR_PROXY_BIND_HOST、ASK_ADVISOR_PROXY_BIND_PORT | 回环地址及为本次分配的端口 |
| ASK_ADVISOR_VLLM_BASE_URL | vLLM 回环 origin，无 /v1 路径 |
| ASK_ADVISOR_PROXY_API_KEY | 从机器内的既有私密存储注入 |
| ASK_ADVISOR_PIN_DIR、ASK_ADVISOR_DSH_BOOTSTRAP_LOG | 已核实的固定会话目录与本次启动日志 |
| ASK_ADVISOR_AUDIT_LOG | 本次独立审计文件 |

## 打通 3090 到 A40，再运行一次

从 3090 主动建立 SSH 本地转发。下列变量由两机操作者在本机填写；监听地址使用任务容器可访问的 Docker 网桥网关。不要绑定所有接口。

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L "${BRIDGE_GATEWAY}:${LOCAL_PORT}:127.0.0.1:${A40_PROXY_PORT}" "${A40_SSH_TARGET}"
```

核实实际转发目标与本次正式代理一致。分别从 3090 宿主机和同网桥的临时容器未认证 POST 到聊天接口，两端都应返回 401。正式与 score-only 代理都会返回 401，所以还须核对转发端口和进程。临时容器使用已有固定镜像和 `--rm --pull=never`。

由负责人批准单个真实 trial 后，在已验证的 3090 运行环境将 OPENAI_API_BASE 和 OPENAI_BASE_URL 均指向网桥端点的 /v1，OPENAI_API_KEY 由机内私密存储注入。使用原受控 sudo 环境保留上述变量、适配器 PYTHONPATH 和专用 DOCKER_CONFIG，再以同一 Harbor 可执行文件执行 `run --config dna-terminus2-advisor-20261115-01.yaml`。作业名已有结果时先核对运行状态，避免重复启动。

## 如何验收和排查

3090 在 trial 子目录查看 result.json（成绩、异常和耗时）、agent/trajectory.json（命令与反馈）及 verifier/test-stdout.txt（测试是否实际执行）。A40 在本次审计里分别统计 bridge_completed、bridge_failed 与 chat_delivery，并读取完整送达、triggered 和 advisor_called 字段。第三个不同反馈起才允许评分，门槛 0.70，每任务最多 3 次 Advisor 调用。若只有一批反馈，零次评分符合预期。

此前独立 score-only 联调中，第 3 个不同反馈第一次产生评分，重放相同反馈后评分总数仍为 1。正式 make 的四个有效分数及 9 次失败另见复盘。失败事件缺少阶段，正式审计也没有保存逐次 prompt 正文。若研究判断质量，可查 README 链接的评分模板；若离线重建历史输入，记录所用轨迹和重建方法。跨机定位超时需要对齐时区、区分请求接收与完成，并核对是否有共同请求标识。
