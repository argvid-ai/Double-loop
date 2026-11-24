# Argvid Family Agent

Argvid 是为家庭设计的拍摄与剪辑助理。家人从主对话交代一件事；助理把需要等待的拍摄、选片和剪辑放到工作对话，结果再回到主对话。Muse 是语音唤醒词，不是产品名。

## 先选要复现的部分

| 入口 | 可以检查什么 | 需要什么 |
| --- | --- | --- |
| 本地回归 | 检查两端如何注册工具，以及唤醒词匹配后是否保留原问题 | Linux 或 macOS、Python 3.11+、uv；不用模型密钥或摄像头 |
| 双服务体验 | 在浏览器打开主对话和工作台；向自己的模型服务发请求 | 上述环境、支持工具调用的 OpenAI 兼容模型服务及密钥 |
| O6N 实机 | 用板端实际画面做判断和剪辑 | 自备 O6N、摄像头、相应设备驱动、模型权重和推理运行环境；不随源码分发 |

### 1. 无密钥验证

在仓库根目录运行（开发环境使用 Python 3.13）：

```sh
uv sync --locked
uv run --no-sync pytest -q tests
```

这些用例检验代码行为，不启动录音、录像或调用云端模型。唤醒用例使用已转写文字，不是声学识别测试。

### 2. 一条命令启动双服务

从本目录复制示例配置，生成的本机文件已被 Git 忽略。

```sh
cp profiles/harness-functional.json profiles/harness.local.json
```

在本机配置中填写自己的 `model_base_url`，并把 `cloud.model`、`edge.model` 改为服务支持且能调用工具的模型 ID。使用 HTTPS 地址；本机模型可使用回环 HTTP。把 `CLOUD_MODEL_API_KEY` 注入当前终端或交给密钥管理器。配置文件和命令行参数均无需写入密钥。示例使用模拟输入。

```sh
sh start.sh
sh start.sh status
sh start.sh stop
```

完成初次配置后，也可在 Double-loop 根目录执行上述三个命令。脚本按 `uv.lock` 安装依赖。默认工作台在 <http://127.0.0.1:8898>，端侧服务在本机 8899 端口。两个健康检查均应返回 200；模型连通性需通过自己的服务验证。端口占用时先查看状态。本地任务记录位于 `.state/`。

### 3. 对照实机 Demo 阅读源码

| 要看什么 | 入口 |
| --- | --- |
| 主对话、工作任务、时钟与事件唤醒 | `src/cloud_loop/harness_host.py`、`harness_scheduler.py`（同目录） |
| 实机主助理接入云端模型 | `src/cloud_loop/harness_pi_runtime.py`、`src/cloud_loop/pi/` |
| 捕获画面、端侧分阶段判断 | `src/cloud_loop/continuous_capture.py`、`src/cloud_loop/observation_staged.py` |
| 选片、渲染、复看与交付 | `src/cloud_loop/harness_media_project.py`、`harness_remotion.py`、`harness_media_review.py` |
| 共享相机输入 | `integrations/r1/shared_capture.py` |

实机上主助理服务和观察服务可以同驻 O6N；“云端”指按需调用的远程模型，不是把整路录像持续上传。本地体验不复现板端推理时延，也不会驱动机械臂。设备绑定、模型访问凭据、家庭录像和历史对话不在仓库中。

第三方来源与许可见 `THIRD_PARTY_NOTICES.md`，其中的许可仅适用于所标明的第三方内容。

## 白皮书与源码的对应关系

谢湘晋整理的《Argvid Family Agent 技术白皮书》第 5–8 页描述主对话、工作任务、端侧观察和执行回执；第 9–12 页说明选片与复看；第 16 页给出本地回归与双服务复现入口。公开模块基于参赛源码版本 `5ee8450`。本地模拟可验证编排与工具契约；板端时延、真实家庭素材和机械臂动作需要独立的设备与授权数据验证。

macOS 与 Linux 的基础双服务只需 Python、uv 和使用者自己的模型服务。Pi 桥接和 Remotion 渲染还需各自的 Node.js 运行环境；O6N 推理另需厂商驱动及合法取得的权重。基础依赖及可选语音、消息通道依赖见 [pyproject.toml](pyproject.toml)，确切版本与包哈希见 [uv.lock](uv.lock)。第三方适配声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，项目原创可授权代码的条款见 [根目录 LICENSE](../LICENSE)。板端实验、现场连接资料和可选第三方素材须由部署方自行准备。
