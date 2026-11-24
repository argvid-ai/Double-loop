# Argvid 双loop主动智能家庭摄影机器人：R1-7A 视觉跟随、自动摄像与机械臂控制

## 快速体验家庭拍摄助理

[Argvid Family Agent](argvid-family-agent/README.md) 提供主对话、异步工作任务、端侧观察和剪辑复看的双服务演示。Linux 或 macOS 需安装 uv 与兼容的 Python 3.11+。从本仓根目录运行离线回归：

```sh
(cd argvid-family-agent && uv sync --locked && uv run --no-sync pytest -q tests)
```

首次启动请按[配置说明](argvid-family-agent/README.md)填写自己的模型地址、模型 ID 和密钥。此后在根目录运行 `sh start.sh` 启动，`sh start.sh status` 查看状态，`sh start.sh stop` 停止。默认使用模拟输入。机械臂部分保留原目录和独立设备依赖。


本项目使用 **Radxa Orion O6N（此芯平台）+ EMEET C60E 4K Light + 宇树 R1-7A 右臂**，实现人员检测、进出区域触发录像、人体和右手姿态跟随，以及按画面场景选择机械臂运镜。板端还提供独立的末端点动程序 `r1_jog`，可通过键盘或本地 API 调用。

本 README 整理了 2026-11-15 至 2026-11-22 的代码、交接记录及板端调用文档。**GitHub 发布检查日期：2026-11-23，北京时间。** 实测数据对应记录时的安装和标定，设备重新安装后需要重新核对，不能直接当作当前姿态的可用行程。

**仓库范围：** 此仓库收录本地可用源码、8 份仿真导演轨迹、历史交接文档和[验证摘要](validation/README.md)。板端独有的 `arm-control/`、完整 `r1-followcam/`、SDK、模型权重及现场标定未取得源码快照，相关功能只能依照文档与板端工程复现。原始录像、图片、运行日志、构建产物和大型参考包不进入 Git。`r1-director-ui/director_panel.py` 缺页面资源，故未纳入本次可运行代码清单。

## 1. 从哪里开始

| 你的目标 | 入口 | 运行位置 |
| --- | --- | --- |
| 从程序调用末端上下、左右、前后移动 | [本地 API](#6-末端点动与本地-api)、[完整调用文档](API调用文档.md) | O6N：`~/r1-roadshow/arm-control/` |
| 人进入画面后自动录像，不带机械臂 | [人员触发录像](#8-人员进出区域触发录像) | O6N：`~/vision-recorder/` |
| 机械臂跟随人员和右手动作 | [人体与姿态跟随](#9-人体跟随与右手姿态演示) | O6N：`~/r1-followcam/` |
| 看人数、场景与镜头选择结果 | [视觉导演](#10-视觉导演场景判断与镜头选择) | O6N：`~/r1-roadshow/director/` |
| 画面触发机械臂运镜并录像 | [实机联动](#11-场景判断联动机械臂与录像) | O6N：`~/r1-roadshow/` |
| 规划人像、巡航等轨迹 | [离线规划与回放](#12-离线轨迹规划与回放) | 工作站规划，O6N 回放 |
| 带电保持当前位置 | [原地保持](#7-带电保持当前位置与退出) | O6N：专用 `r1_play` 构建 |
| 查历史实测、录像与待办 | [实测与限制](#14-已验证的结果与尚未完成的工作) | 本地 `artifacts/`、O6N `captures/` |

## 2. 当前完成情况

这些功能使用不同的入口，尚未合并成一个统一启动器。

| 模块 | 已有能力 | 当前边界 |
| --- | --- | --- |
| `vision_recorder.py` | CIX NPU 检测；进区确认、预录、离区延时；数字裁切跟随；MP4 + JSON | 只处理视频，不打开机械臂串口；无音轨 |
| `r1-followcam` | C60E 4K 取流，YOLO 人体检测，J1 水平居中与跟随，同步录像 | 相机 AI 构图会干扰画面反馈；完整工程在 O6N |
| 姿态演示 | YOLOv8s-Pose，右手驱动 J2，J1 跟随走位，J4/J7 准备和取景补偿 | 11 月 16 日完成 60 秒无录像实测；J4 随 J2 动态联动仍待验收 |
| `director/run.py` | YOLO 人数 + MiniCPM-V 场景分类，连续确认、冷却、日志和镜头提案 | 此入口自身始终是 `dry-run`，不直接驱动电机 |
| `director/field_session.py` | 消费实时判断，触发受限动作、首帧确认、录像、带电回位 | 11 月 18 日已实拍双人交流及两人独立分段扫拍；部分构图与路线待实拍 |
| 规划器 / `r1_play` | 七轴路径、模型检查、格式校验、回放、固定展开位待机与慢收 | 仿真路线不能直接进入实机；现场需要标定与净空核验 |
| `arm-control/r1_jog` | 末端平移点动，本地 Unix Socket API，100 Hz 控制 | 独立板端工程；相机/工具偏置、末端旋转点动未实现；新安装的实机效果需单独验收 |
| `r1_play --hold-snapshot` | 停机快照复核后原地保持，带保护退出 | 曾进入保持后因 J2 保护退出；必须保留承托，不能视作机械抱闸 |
| `r1-director-ui/director_capture.py`、`keyframes.py` | 双人群组采集、4K 静帧与动作/笑容关键帧筛选代码 | 11 月 22 日归档会话证明素材产出；当前源码的全部实机动作尚未逐项验收 |

**API 已完成不代表视觉系统已经通过新 API 全部接通。** 已有视觉联动走 `field_session.py → r1_play`；独立 API 走 `r1_jog`。两条控制链需要选择其一，不能同时控制同一串口。

## 3. 系统结构与坐标约定

```text
C60E USB 相机
  ├─ vision_recorder.py
  │    └─ YOLOv8n / CIX NPU → 区域触发 → 数字运镜 → MP4 + JSON
  ├─ r1-followcam
  │    └─ 人体 / 姿态检测 → UDP → arm_server → R1-7A
  └─ director/run.py
       ├─ YOLOv8n / CIX NPU：人数、检测框
       ├─ 可选 YOLOv8s-Pose：上半身构图
       └─ MiniCPM-V / LQ50：场景分类
            └─ director/runs/latest.json
                 └─ field_session.py → r1_play → R1-7A
                      └─ 共用原始取流录像，拍完带电回位

键盘 / Python / C++ / Shell
  └─ 本地 Unix Socket → arm-control/r1_jog
       └─ 末端目标 → 阻尼逆解 → SDK → 同一台 R1-7A
```

- 机械臂为右臂，关节顺序为 **J1–J7**，串口通常为 `/dev/ttyUSB0`，波特率 **6,000,000 bps**。
- `r1_jog` 基座坐标：**+X 向前，+Y 向左，+Z 向上**；不能直接将基座左/右等同于图像左/右。
- `r1_jog` 末端点为 **Link7 法兰原点**，没有加镜头偏置。法兰移动 10 mm 不等于已测得镜头移动 10 mm。
- 摄影规划器使用镜头外参，相机局部轴同样约定为 +X 前、+Y 左、+Z 上，外参旋转按 `Rz · Ry · Rx` 计算。
- 配置中长度通常为 **m**，关节角为 **rad**；点动 API 位移参数为 **mm**；画面 ROI 为 **0–1 归一化坐标**。
- SDK 角、电机原始角与模型角必须区分。多圈编码器分支不能仅按旧日志加减 π 或 2π 后直接控制。

## 4. 目录与设备连接

### 4.1 工作站根目录

```text
此芯2.0/
├── README.md                         # 本总说明
├── API调用文档.md                    # 2026-11-21 从 O6N 同步的调用文档
├── vision_recorder.py                # 不带机械臂的人员触发录像
├── r1-director-ui/                   # 新采集入口、关键帧筛选与姿态辅助代码
├── r1-pose-preview/                  # 姿态预览、试验编排、控制器构建源码
├── r1-roadshow/
│   ├── README.md / 运镜评估.md / 今晚实机.md
│   ├── plan.py / presets.py / trajectory.py
│   ├── play.cpp / CMakeLists.txt      # C++ 实机控制与回放
│   ├── example.json                 # 仿真样例，不是现场配置
│   ├── field/                       # 快照、实拍、标定与参考位
│   └── director/                    # 判断、联动、录像、轨迹生成与测试
├── validation/                     # 可随 Git 提交的验证摘要
├── R1-7a 视觉跟随 2026-11-16 交接记录.md
├── R1-7A 视觉运镜 2026-11-18 交接记录.md
└── .gitignore                      # 排除原始素材、密钥配置和构建产物
```

### 4.2 板端目录

工作站按上面的仓库布局组织源码。板端的 SDK、模型、标定文件、输出目录和完整跟随工程由部署方配置。

### 4.3 SSH

在工作站的 SSH 配置中设置自己的别名、账号和主机。文档中的通用连接示例为 `ssh <board-alias>`。通过受控渠道获取部署参数。
### 4.4 稳定设备路径

部署时在板端列出当前稳定设备路径，并确认对应的物理设备：

```bash
ls -l /dev/serial/by-id/ /dev/v4l/by-id/
```

部署时选择当前物理设备对应的稳定标识。重新插拔 USB 后复核视频流和串口。

## 5. 环境与运行前检查

| 用途 | 依赖 |
| --- | --- |
| NPU 检测、相机识别 | `~/yolo_test/p1-yolo-test/.venv/bin/python`，OpenCV，CIX 库 `/usr/share/cix/lib`，`yolov8n.cix` |
| 姿态识别 | `yolov8s-pose.cix`，已见于 `~/r1-followcam/models/` 与 `~/r1-pose-preview/models/`，按实际文件指定 |
| 视觉大模型 | 后摩 LQ50 24GB、MiniCPM-V 4.6 服务；地址由 `R1_VLM_ENDPOINT` 提供 |
| 离线摄影规划 | Python，NumPy、SciPy、MuJoCo，右臂 MJCF 及引用 STL |
| `r1_play` 编译 | CMake、C++17、OpenSSL 开发库、对应架构 SDK 电机库 |
| `r1_jog` 编译 | CMake、C++17、SDK 与对应架构电机库；不依赖 ROS 或 MuJoCo |
| 现场原始录像 | FFmpeg，或用 `R1_FFMPEG` 指定已有可执行文件 |

### 私有连接配置

仓库中的 `director/config.json` 将模型服务地址留空。运行导演视觉程序前，在本机设置 `R1_VLM_ENDPOINT`；安装 systemd 服务时还需设置 `R1_VLM_START_SCRIPT`。例如：

```bash
export R1_VLM_ENDPOINT='http://<VLM_HOST>:<PORT>'
export R1_VLM_START_SCRIPT='/absolute/path/to/model/start_script.sh'
cd r1-roadshow
bash director/install.sh
```

安装器使用本机的用户服务环境文件。部署方应在设备侧配置并保护模型服务地址和认证信息，核对安装版本、相机、串口以及资源占用情况。

实机操作的共同约束：

- 同一时刻只让一个控制进程持有机械臂串口；`r1_jog`、`r1_play`、`arm_server` 不能同时运行控制。
- 避免重复占用相机。导演联动录像复用视觉流；单独运行录制原型、姿态预览或现场原始录像前，先结束其他取流。
- 机械臂固定牢靠，臂体有承托。正常退出或故障进入 `mode 0` 后会失力，带电保持也可能因保护退出。
- 新安装、支架改动或编码器分支改变后，重新核对方向、参考位、标定和线束空间。旧参考位不能直接套用到新起点。
- 停视觉服务不等于停机械臂。联动控制器、API 进程和视觉服务各有自己的生命周期。

## 6. 末端点动与本地 API

公开接口说明见 [API调用文档.md](API调用文档.md)。使用离线数据核对请求、响应和状态转换；控制器版本、运行参数、驱动使能及退出流程请使用对应设备的受控部署文档。

## 7. 保持与退出

现场调试时区分带电保持与失力。退出后依据设备反馈确认状态，再交还设备。
## 8. 人员进出区域触发录像

这是独立摄像原型，**不打开机械臂串口**，复用板上 YOLO 工程的 NPU 推理与后处理。先跑视频，再接相机。

### 8.1 已有视频

```bash
cd ~/vision-recorder
LD_LIBRARY_PATH=/usr/share/cix/lib \
  ~/yolo_test/p1-yolo-test/.venv/bin/python vision_recorder.py \
  --video ~/yolo_test/p1-yolo-test/test_videos/people-detection.mp4 \
  --output recordings
```

### 8.2 C60E 实时取流

```bash
cd ~/vision-recorder
export R1_CAMERA_DEVICE='/dev/v4l/by-id/<当前C60E的video-index0设备>'
LD_LIBRARY_PATH=/usr/share/cix/lib \
  ~/yolo_test/p1-yolo-test/.venv/bin/python vision_recorder.py \
  --camera "$R1_CAMERA_DEVICE" \
  --zone 0.1,0.1,0.9,0.95 --output recordings
```

此入口请求 MJPEG **1920×1080、30 fps**，实际格式与帧率以相机协商为准。

| 参数 | 默认值 | 含义与范围 |
| --- | --- | --- |
| `--video` / `--camera` | 必选一个 | 文件与实时相机互斥 |
| `--yolo-dir` | `~/yolo_test/p1-yolo-test` | 模型和推理脚本 |
| `--output` | `recordings` | 输出目录 |
| `--zone` | `0,0,1,1` | 左、上、右、下；归一化坐标 |
| `--enter` | 0.35 s | 连续有人后确认进入，范围 0–3 s |
| `--pre` | 2 s | 进入确认前额外缓存，范围 0–5 s |
| `--post` | 3 s | 连续无人后的结束延时，范围 0–30 s |
| `--crop` | 0.86 | 横、纵各保留原尺寸 86%，缩放回输出大小；范围 0.7–1 |
| `--confidence` | 0.4 | 人员检测阈值，范围 0–1 |

进区判断用**原图人体框底边中心**，与输出裁切分开。只要区域内还有人，片段继续；多人时裁切尽量维持同一主体。`--crop 1` 输出全画幅。

每段生成无音轨 `person_时间戳.mp4` 和同名 JSON，记录 `start_s`、`trigger_s`、`end_s`、`reason`、帧数和 fps。秒数是本次输入的相对时间，不是 Unix 时间戳。缓存包含进入确认窗口，实际起录可覆盖约 `pre + enter` 的历史，启动初期以已有帧为限。

结束原因包括 `person_left`、`source_ended`、`stopped`、`camera_read_failed`、`input_gap`、`error`。Ctrl+C 封装当前片段；写入用 `.part.mp4`，完成后改名。磁盘剩余不足 2 GiB 时停止录制。

## 9. 人体跟随与右手姿态演示

本仓提供人员录制原型和姿态辅助代码。完整跟随系统还依赖设备侧的控制器、相机和推理环境。

11 月 15 日记录的参数包括画面中心 ±3.5% 死区、启动点附近约 ±0.28 rad 的 J1 窗口，以及有限次数、带冷却的基准重设。不要取消重设次数限制，不稳定检测下曾出现持续旋转问题。

### 9.2 预览与姿态流程

工作站可通过自行配置的 SSH 隧道查看板端预览。主机、用户、服务端口与设备实例由部署方在本机配置。姿态归零、跟随和回位需要逐台验证。画面与关节日志应结合判读。

## 10. 视觉导演：场景判断与镜头选择

细节见 [director/README.md](r1-roadshow/director/README.md)。视觉入口只生成提案，实机需要下一节控制会话。

### 10.1 场景与路线

| 场景 | 提案 / 基线动作意图 |
| --- | --- |
| 双人面对面交流 | `ConversationSweep`：左侧定场 → 左向右扫拍 → 回位 |
| 两人独立活动 / 多人 | `GroupWide`：左右覆盖，中间停留 |
| 单人定点 | `PortraitStandingPreset`：定点人像；构图排练模式可为定镜 |
| 单人向左 / 右走 | `FollowLeft` / `FollowRight`：同侧转动、驻留、回位 |
| 坐到站 | `RiseFollow`：预设抬升和抬镜提案，完整实时跟随待验收 |
| 无人 | 固定展开位等待，不自动发起场地巡航 |
| 遮挡、不确定、接口失败、过期画面 | `Hold`，不发起新镜头 |

YOLO 人数约束候选标签，双人交流增加独立是/否复核；检测框跨帧变化支持时才开放行走、起立分类。模型不输出关节角，也不能下发任意程序命令。

默认 `director/config.json`：0.5 秒采样、积累 3 帧；模型调用完成后间隔 2 秒；连续 2 组新画面确认；触发间隔至少 12 秒；最大画面年龄 8 秒；请求超时 7 秒。默认请求 **1920×1080、15 fps**，与 4K 姿态演示不同。

`motion.travel_scale=2.2`、双人扫拍 J1 总跨度 50° 及逐轴幅度表是**仿真规划参数**。11 月 18 日实机基线约 13.2°，不可把 50° 直接套入当前安装。

### 10.2 服务与单次测试

```bash
# O6N：查询状态、模型健康与日志
systemctl --user status r1-director r1-director-vlm --no-pager
curl -fsS "${R1_VLM_ENDPOINT%/}/health"
journalctl --user -u r1-director -n 30 --no-pager
```

手动测试先结束现有视觉取流；不要在实机联动中途切换输入：

```bash
cd ~/r1-roadshow
systemctl --user stop r1-director
systemctl --user start r1-director-vlm
./director/run.sh --duration 60

# 或用视频测试分类；回放不能触发实机
./director/run.sh --video /实际视频路径.mp4 --max-decisions 6
```

恢复后台判断用 `systemctl --user start r1-director`。首次安装用 `bash director/install.sh`，会核对预生成轨迹与配置哈希，启用模型、视觉两个服务；缺模型或轨迹先补齐依赖。

模型地址由 `R1_VLM_ENDPOINT` 指定，调用 `/v1/chat/completions`，模型名 `minicpm-v4.6-lq50`。

每次在 `director/runs/` 建立 UTC 命名的会话，主要产物：

- `session.json`：启动信息和代码哈希。
- `decisions.jsonl`：画面时间、人数、回执、触发状态、图片和轨迹哈希。
- `observation-*.jpg`：检查联系图，单会话保留最近 120 张。
- `finished.json`：收尾标记。
- `runs/latest.json`：跨会话最新状态，结束后可能仅含结束信息。

JSONL 按 4 MiB 轮转并保留两份备份。不足 2 GiB 或画面超时会结束判断。日志 UTC、单调时钟和本文北京时间应区分。

## 11. 场景判断联动机械臂与录像

这个入口驱动机械臂，需要先核对当前安装的参考位与标定。**11 月 21 日已有不同的停机角和手动抬升记录；不能仅因保留旧参考文件，就直接运行旧展开路线。**

### 11.1 起点、触发与停止

历史统一起点为 **2026-11-17 21:06 固定展开位**，数据在 [standby-home.json](r1-roadshow/field/standby-home.json) 与 [standby-reference.txt](r1-roadshow/field/standby-reference.txt)。

1. 从已测收拢位准备，J4 先打开，J2 再抬升。
2. 七轴核对固定展开位并稳定 5 秒，带电待机。
3. 只接受待机确认后采集的实时画面；回放、旧帧、未配置路线不触发。
4. 连续确认场景，确认录像首帧后再开始镜头。
5. 拍完反馈确认回到展开位，保持通电，重新稳住 5 秒。
6. 同一镜头不一直重复；切换场景或连续无人后重新入画才允许下一次。
7. 基线排练 Ctrl+C 请求回展开位，再 J2 下放、J4/J7 收拢并确认 mode 0。通用现场轨迹会话的退出不同，应按日志提供承托。

### 11.2 基线预检与实机入口

在 O6N `~/r1-roadshow` 执行。先省略 `--port`，只检查参考和标定：

```bash
python3 director/field_session.py \
  --rehearsal-park captures/20261117-210554-park-reference/park-reference.txt \
  --calibration ~/r1-tracker/r1-7a_sdk/config/joint_calib_right.txt
```

`--rehearsal-park` 应是本次可用的收拢位，不能只照抄历史路径。安装与行程复核后，在同一命令添加 `--port 实际串口 --record-shots` 才进入实机。

上半身构图需在视觉会话添加 `--pose-model /实际路径/yolov8s-pose.cix`，控制会话添加已生成的 `--level-plan /实际路径/level-rehearsal.txt --record-shots --frame-people`。`--frame-people` 依赖 `--level-plan`，不是所有路线通用选项。

视觉进程 `--output` 与联动进程 `--vision` 应指向同一个输出根目录，例如 `director/runs/` 与其中的 `latest.json`。录像经 `record-request.json`、`record-status.json` 协调，存入本次视觉会话 `clips/`。当前写 **MJPEG AVI + JSON**，宽度最多 1280，与独立录制原型的 MP4 不同。

构图用头部、双肩和腰线判断；必要时 J7 每次抬约 1°，等新帧复查。构图不足继续等，拍摄中连续失去构图则结束并回位。11 月 18 日最后抬镜后头部仍贴上沿，未录像，属于拦截而非已完成上半身拍摄。

### 11.3 自定义现场轨迹

```bash
# 有规划依赖和模型的工作站，进入 r1-roadshow
python3 director/build_plans.py \
  --field-profile /实际现场配置.json \
  --out /实际现场轨迹目录 --preset ConversationSweep

# O6N：传入轨迹与标定后，先预检
python3 director/field_session.py /实际现场轨迹目录 \
  --calibration /实际标定.txt --runner /实际构建/r1_play
```

实机显式添加 `--port`，并确保臂已在目录绑定的展开位。通用目录方式不会从任意收拢位自动展开。

改配置后必须重建轨迹；系统核对配置、标定、路线、参考位哈希，不手改哈希跳过不匹配。实机联动若放入临时用户服务，采用 `KillMode=mixed` 和足够的慢收时间，如 `TimeoutStopSec=95s`；结束确认 `Parked and verified mode 0 on all joints`，避免同时强杀会话与控制器打断慢收。

## 12. 离线轨迹规划与回放

详见 [路演运镜说明](r1-roadshow/README.md) 与 [逐套运镜评估](r1-roadshow/运镜评估.md)。

### 12.1 工作站规划

在本地根目录：

```bash
python3 r1-roadshow/plan.py --list
python3 r1-roadshow/plan.py PortraitCloseFocusPreset \
  --out artifacts/PortraitCloseFocusPreset.r1traj \
  --report artifacts/PortraitCloseFocusPreset.json
python3 r1-roadshow/plan.py VenueTour --report artifacts/VenueTour.json
```

默认模型为相邻工程 `../r1 7a机械臂/models/mjcf/right.xml`，STL 也要保留；换位置用 `--model /完整路径/right.xml`。

| 类别 | 已移植名称 |
| --- | --- |
| 原源码 5 套人像 | `PortraitDistanceFollowPreset`、`PortraitBiasSwitchPreset`、`PortraitCloseFocusPreset`、`PortraitShowcasePreset`、`PortraitStandingPreset` |
| 文档扩展 3 套 | `PortraitBiasPulsePreset`、`PortraitExtendedShowcasePreset`、`PortraitNarrativeShowcasePreset` |
| 搜索与路演辅助 | `PortraitSearch`、`RoadshowIdleScan`、`RoadshowCaptureSweep`、`RoadshowCapturePush`、`VenueTour` |

讲解版可用 `--narration-holds` 提供六段时长。设计中的实时重居中、失脸恢复、动态留白和坐站切换并未因此全部实现，规划器会对这些模板报告视觉依赖。

### 12.2 现场配置与标定

`example.json` 是 `simulation` 样例。另存现场配置，实测后填七轴模型起点 `start_rad`、关节范围、标定、逐轴增益与力矩、镜头相对 Link7 外参、包络半径、实际视场、坐/站锚点、场地标记和障碍盒，再切换为 `field`。

模型检查自碰撞、地面、镜头包络与已登记障碍；排除相邻连接及官方凸包重叠的 Link5/Link7。不覆盖未登记障碍、线束与实物腕部干涉。

板端整理快照和独立量角，生成候选标定：

```bash
cd ~/r1-roadshow
python3 field/calibration.py inspect captures/实际快照目录/arm.log
python3 field/calibration.py template captures/实际快照目录/arm.log --out observations.json
# 填至少两个独立姿态，第三姿态用于复核
python3 field/calibration.py fit observations.json --out joint_calib_right_candidate.txt
```

保留原标定，候选独立命名。核对 `q_motor_rad → 标定换算 → 模型角 → 正运动学`，不能将 `q_sdk_rad` 不经确认直接放入模型配置。

### 12.3 编译与检查

本地源码树布局下：

```bash
cmake -S r1-roadshow -B artifacts/r1-roadshow-build \
  -DR1_SDK="$HOME/r1-tracker/r1-7a_sdk" -DCMAKE_BUILD_TYPE=Release
cmake --build artifacts/r1-roadshow-build -j2
artifacts/r1-roadshow-build/r1_play --check artifacts/PortraitCloseFocusPreset.r1traj
```

O6N 源码直接在 `~/r1-roadshow/` 时用 `cmake -S . -B build`，同样指定 SDK。先确认源码版本，避免覆盖需保留的旧构建。

`--check` 不打开串口。现场入口形式：

```text
r1_play --execute 现场.r1traj 串口 现场标定.txt field/standby-reference.txt
r1_play --check-standby 现场标定.txt field/standby-reference.txt 镜头一.r1traj 镜头二.r1traj
r1_play --standby-loop 串口 现场标定.txt field/standby-reference.txt 镜头一.r1traj 镜头二.r1traj
```

现场轨迹绑定标定和共同起点，启动后稳住 5 秒。`--standby-loop` 输出 `POWERED_STANDBY` 后向**该播放器进程**发 `SIGUSR1` 请求下一镜；`RETURNED_POWERED_STANDBY` 表示回位保持。此信号约定不可套用到 API 或其他控制模式。

## 13. 实机原子动作与录像资料

入口在 `r1-roadshow/field/session.py`，都依赖特定参考位与已测空间：

| 模式 | 用途 |
| --- | --- |
| `snapshot` | 停机姿态采样，保存关节反馈 |
| `pan-record` / `pan-wide-record` | J1 扫镜录像 |
| `pan-powered-record` | 展开位左右扫镜，结束带电保持 |
| `pan-tilt-powered-record` | 扫镜与抬镜 |
| `standby-rehearsal-record` | 展开、稳镜、触发、回位、慢收排练 |
| `level-retreat-record` | J4 后仰与 J7 补偿，依赖实测水平参考 |
| `wrist-tabletop-record` | 桌面下俯与多机位 |
| `lateral-probe-record` / `straight-lateral-record` | 局部横移探测与实测参数回放 |
| `amplitude-probe-record` | 已确认净空下逐轴端点实测 |

```bash
# O6N：~/r1-roadshow
python3 field/session.py --help
python3 field/session.py snapshot
# 现场核对后才运行带动作模式；reference 是收拢位
python3 field/session.py pan-powered-record --reference /实际收拢位.txt
```

`captures/` 每场通常保存 `session.json`、`arm.log`、`report.json`、原始视频（常见 `camera.mkv`）。有些模式拍完继续带电，需按其约定向会话发 `SIGUSR2` 慢收，不能看到视频保存就直接断电。

日志关键字：导演联动看 `DIRECTOR_STANDBY`、`DIRECTOR_ACCEPTED`、`LEVEL_DIRECTOR_PHASE`、`CAMERA_RECORDING_STARTED`、`CAMERA_RECORDING_RESULT`；回放看 `PLAYBACK_START`、`PLAYBACK_RETURN`。是否失力应看停机反馈，不能只看 Python 是否退出。

## 14. 已验证的结果与尚未完成的工作

### 14.1 11 月 16 日：右手与走位

60 秒无录像实测中，J2 从约 73.9° 抬到 59.2°，跨度 **14.7°**；垂手走位的 J1 跨度约 **22.1°**，结束正常回位。该安装中 J2 角度减小表示上抬，74° 是现场下放边界，不能自动适用于重新安装。

详见 [11 月 16 日交接](R1-7a%20视觉跟随%202026-11-16%20交接记录.md)。

### 14.2 11 月 17 日：原子摄像动作

展开位 J1 扫镜约 **13.22°**，J7 抬镜约 **3.86°**，桌面下俯约 **5.78°**。短程横移已录像并回位，画面估计最大偏线约为横向位移的 **16%**，无独立毫米尺度，尚不是精密直线移动验收。

资料见 [现场流程与原子动作](r1-roadshow/今晚实机.md)，证据在本地 `artifacts/r1-roadshow-validation/`。

### 14.3 11 月 18 日：场景联动与构图

接通 C60E → CIX NPU → LQ50 → 机械臂运镜 → 自动录像。双人交流扫拍约 **13.2°**，录像后带电回位；两人独立的一条多轴分段扫拍已实拍，J1 端点约 −11.51° 与 +1.71°。

调平后仰曾估计俯仰约 0.00°、横滚约 1.05°，属于该次录像推算。上半身最后又抬镜 2°，头顶仍贴上沿，未开录像；收尾按流程收拢并复核 mode 0。

详见 [11 月 18 日交接](R1-7A%20视觉运镜%202026-11-18%20交接记录.md)，证据在本地 `artifacts/r1-director-validation/`。子目录旧 README 的“仍未实拍”描述反映更早阶段，应结合交接记录理解。

### 14.4 11 月 21 日：新起点、保持与 API

已有停机快照、原地保持及 J4 净空回位试验。新记录的 J2 编码器分支与历史角度明显不同；局部回位出现过 J2 停滞，保持也出现过 J2 保护退出。手动抬升快照保存了 SDK/电机角，模型角仍未确认。

`arm-control` 与本地 API 已在板端存在。本 README 核对了调用文档、配置、帮助和关键源码。**软件自检、API 模拟、实机实测属于不同验收层级**，新 API 不能直接复用旧摄像路线的角度与回位假设。

证据在本地 `artifacts/r1-auto-20261121/`；`j2-return-diagnosis.json` 保存诊断，`raised-stopped-134046.json` 保存抬升后的停机姿态。

### 14.5 下一步验收

- 当前安装重新确认七轴角度分支、标定、方向及有承托的起点。
- API 做小步上下、左右、前后实测，核对实际位移、姿态漂移、停止与保护。
- 高个站位通过上半身检查并实际录像；双人交流、左右跟拍的多轴扩幅补实拍。
- 姿态跟随补做 J4 随 J2 联动构图验收。
- 镜头外参、横移毫米尺度、线束与障碍包络独立测量。
- 音频与合成、实时失脸恢复、动态留白、完整坐站跟随尚未统一完成。
- YOLO/场景分类改接新 API 时，需新增统一调度、图像到基座方向映射、目标仲裁、超时与失联保持，现有接口不会自动完成这些工作。

## 15. 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| 找不到 `cv2` / CIX 模块 | 用已有 YOLO 虚拟环境及 `/usr/share/cix/lib`；导演优先用 `director/run.sh` |
| `GLIBCXX_3.4.30` 缺失 | 点动用自带 `build.sh` / `run.sh` 清外部库路径 |
| 相机打不开、节点变了 | 列 `/dev/v4l/by-id/`，选 `video-index0` 并查占用 |
| 臂转了，画面变化很小 | 排查 C60E AI 构图，同时看关节反馈，不只提高增益 |
| 镜子里多出一个人 | 用真人锁定；检测框数量不一定等于真人数 |
| 有提案但臂不动 | `run.py` 无臂；查 `field_session.py`、现场路线、`--port` |
| `Hold` / 无新触发 | 查新帧、画面年龄、连续确认、冷却、镜头结束、同场景解锁 |
| 哈希不匹配 | 核对文件版本，按当前配置/标定重建，不手改哈希 |
| API socket 不存在 | 确认启用 `--api`；实机 Enter 后才监听，查启动错误 |
| API 返回 `paused` | 先 `resume` 再运动 |
| `ok` 但没有实际到达 | 查裁剪、目标取消、指令位移、实测跟踪误差与逆解日志 |
| `pending_mm` 为 0 但没到原目标 | 逆解可能取消目标；与先前保存的目标比较 |
| 客户端退出后仍运动 | 断开不撤销目标；显式 `pause`，承托后 `stop` |
| 启动角超出 URDF 名义范围 | 临时窗口不等于标定正确；先核对分支与标定 |
| J2 不动、颤动、回位停滞 | 查实测角、力矩、速度、J4 净空与干涉，不强推旧回位 |
| 停服务没有按序收拢 | 区分视觉和控制会话；检查 `KillMode=mixed`、慢收超时及 mode 0 |

## 16. 文档导航与维护

| 文档 | 内容 |
| --- | --- |
| [API调用文档.md](API调用文档.md) | 本地控制接口的职责、使用条件与离线验证范围 |
| [r1-roadshow/README.md](r1-roadshow/README.md) | 规划、现场配置、编译、待机、回放 |
| [director/README.md](r1-roadshow/director/README.md) | 场景逻辑、部署、构图与联动参数 |
| [今晚实机.md](r1-roadshow/今晚实机.md) | 历史起点、原子动作、录像与标定 |
| [运镜评估.md](r1-roadshow/运镜评估.md) | ArmPi 模板来源、移植范围和视觉依赖 |
| [11 月 16 日交接](R1-7a%20视觉跟随%202026-11-16%20交接记录.md) | 右手、走位、关节方向和日志 |
| [11 月 18 日交接](R1-7A%20视觉运镜%202026-11-18%20交接记录.md) | 实拍联动、构图、回位与收拢 |

现场构建、控制器手册和标定资料由设备维护方按版本提供。

历史版本的离线与实机观测见[验证摘要](validation/README.md)。各项结果只对应记录中的构建与环境。

维护时注明日期、安装状态、构建和证据，将代码已实现、模拟通过、实机实测分开记录。`artifacts/`、`recordings/`、`director/runs/` 已由 `.gitignore` 排除；仿真轨迹 `director/plans/` 随仓库提交，不能直接当作实机路线。原始录像、标定和日志需通过私有渠道备份。原始参考包是设计来源，不代表 ROS、语音、底盘或产品拍摄能力已全部移植。

## 软件许可与商业使用

本项目有权授权的原创软件采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。在许可规定的用途内，可以使用、修改和分发软件。商业产品集成、收费服务等超出许可范围的用途，请事先向相应权利人取得单独的书面商业授权。

第三方设备 SDK、模型权重、参考动作包、外部测试视频及其他素材，按相应权利人的条款使用或分发。 商标与标识的使用须遵循权利人要求。
