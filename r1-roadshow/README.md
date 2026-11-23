# 宇树 R1‑7A 路演运镜

从 ArmPi Ultra 移植 5 套人像的正式序列，另有 3 套文档预设、搜脸编排、3 套路演笔记里的运动和场地巡航。详细的逐套结论见 [运镜评估.md](运镜评估.md)。

## 离线规划和检查

2026-11-18 新增 [画面判断与运镜逻辑](director/README.md)：O6N 的 CIX NPU 检测人数、LQ50 上的 MiniCPM-V 判断场景，连续确认后选择大幅度低速镜头，当前部署输出为仿真提案。双人交流对应左到右 50° 扫拍，人像和巡航镜头按 2.2 倍增量规划。

在「此芯2.0」根目录运行。Python 需要 NumPy、SciPy 和 MuJoCo。默认读取相邻工程 `r1 7a机械臂/models/mjcf/right.xml`，可用 `--model` 指定同一右臂模型的其他路径。模型引用其原有 STL。规划器默认使用仿真参数。

```bash
python3 r1-roadshow/plan.py --list
python3 r1-roadshow/plan.py PortraitCloseFocusPreset \
  --out artifacts/PortraitCloseFocusPreset.r1traj \
  --report artifacts/PortraitCloseFocusPreset.json
python3 r1-roadshow/plan.py VenueTour --report artifacts/VenueTour.json
```

`--report` 包含每段到达的相机位置、姿态、实际移动与停留时间，另列回程长度。移植保留原序列和移动方向，五次缓动按现场关节速度和加速度要求自动延长。镜头运动在相机坐标下规划，沿位置和朝向连续求七轴解。归位优先走通过模型检查的直达路线，必要时沿已检查的拍摄路线返回。讲解版需用 `--narration-holds` 输入与六段对应的讲解秒数。

## 填写场地配置

以 [example.json](example.json) 为格式参考，另存现场配置，填入实测值和场地标记后才切到 `"mode": "field"`。样例是仿真用的合成机位与相机外参。

| 配置 | 输入 |
| --- | --- |
| `start_rad` | 相机已装好且机械臂有支撑的起点，按同一标定文件转换到模型 J1–J7 的弧度 |
| `joint_bounds_rad` | 现场已确认的七轴可用范围，依次为七对下限和上限，使用连续模型角度 |
| `motor_calibration` | 当前这台右臂的七行 `joint dir scale offset` 标定文件，绝对路径或相对现场配置 |
| `kp`、`kd`、`torque_limit_nm` | 按实机、支架和负载确认的逐轴控制参数 |
| `camera` | Link7 到镜头中心的平移和姿态、镜头及支架的包络半径、实际取流模式的水平与垂直视场 |
| `anchors.seated / standing` | 坐姿、站姿示教各自的正面中景 `q_rad`，填该机位的 `target_range_m` |
| `base_xyz_m` | 可选，模型基座在现场坐标的位置，坐标以米计，Z 向上 |
| `obstacles` | 将桌面、展台和固定物的实测包络填为 `center_m`、`size_m` 的轴对齐盒 |
| `venue`、`coverage_points_m` | 现场示教各分区机位和驻留时间，给每个必拍区域填入边缘及高度标记 |
| `limit_rad_s`、`limit_rad_s2` | 逐轴速度和加速度上限，适配器最高允许 0.25 rad/s、0.5 rad/s² |
| `travel_scale` | 可选，默认 1，允许大于 0 且不超过 4。大于 1 放大镜头位移与俯仰增量，仍须通过完整 IK、模型碰撞与速度检查，设置后按新画面验收 |

相机局部轴采用 **+X 向前、+Y 向左、+Z 向上**。姿态填写绕 X/Y/Z 的欧拉角，计算顺序是 `Rz · Ry · Rx`。同一 JSON 中的基座、场地、覆盖标记均用同一世界坐标。场地报告给出每个标记在哪些驻留位进入视野、哪些遗漏，以及哪些单个机位能同时看到所有标记。计算包含登记的遮挡盒和 5% 画幅边缘留量，最终用原始画面检查前景人物、相机裁切和灯光下的遮挡。

这台右臂曾出现 J4 等轴的编码器分支与 URDF 名义角度不同。现场标定必须把轴向、零位和分支映射到同一个连续模型坐标。用两处以上已知姿态复核并测镜头外参后再导出现场轨迹。模型按官方凸包检查自碰撞、地面、镜头包络和现场障碍盒。相邻连接和官方凸包重叠的 Link5 与 Link7 这一对在模型中排除，实物的腕部干涉和 USB 线束仍以现场检查为准。

## 今晚现场快测

O6N 已预置 ARM64 回放器。先按 [今晚实机.md](今晚实机.md) 检查设备、保存起点并排练 J1 扫镜。每次快照与扫镜自动保存运行目录，`pan-record` 把录像与关节日志放在一起。板上也有 `field/calibration.py`，可整理实机快照并按独立量角生成候选标定。

带电扫镜、抬镜和桌面取景使用同一份 `field/standby-reference.txt`。当前基准是现场确认的 21:06 展开位。`--reference` 传入的是收拢位检查记录。控制器先展开并连续核对七轴 5 秒，相机写入首帧后才开始镜头动作。

## 编译与回放

在 O6N 上使用其现有宇树 SDK。C++ 回放器依赖 CMake、C++17、OpenSSL 开发库和对应架构的 `libmotor_controller`。生成轨迹可在工作站完成，把已检查的现场轨迹和报告交给板端。

```bash
cmake -S r1-roadshow -B artifacts/r1-roadshow-build \
  -DR1_SDK="$HOME/r1-tracker/r1-7a_sdk" -DCMAKE_BUILD_TYPE=Release
cmake --build artifacts/r1-roadshow-build -j2
artifacts/r1-roadshow-build/r1_play --check artifacts/PortraitCloseFocusPreset.r1traj
```

`--check` 离线复核七轴角度、速度、加速度及回位，仿真轨迹可查。现场轨迹入口为 `r1_play --execute 现场.r1traj 实际串口 标定文件 field/standby-reference.txt`。轨迹首帧须匹配同一份起点，实测七轴在该处稳定 5 秒后开始回放。执行器核对标定文件哈希和当前位置，用现有独占锁，收到每轴新的 CRC 合格反馈才接受该轮反馈，按 100 Hz 回放。`PLAYBACK_START` 和 `PLAYBACK_RETURN` 是现场录制或切换画面的提示点。正常结束回到有支撑的起点再确认 mode 0。故障处理发送停机帧，台架布置需考虑失力后臂体的承托。

当前版本由操作员点名播放，人像按现场示教的固定构图排练。原系统的弱跟随、持续人脸跟踪、语音触发及自动录像仍需要 O6N 视觉和录制流程联调。连续全场录像可先用固定全景机位或独立的全景相机，再把腕部画面作为运镜画面切入。

## 带电待机与连续播放

先通过已验证的展开流程将机械臂置于 11 月 17 日 21:06 固定展开位。使用同一个现场标定、同一组起点角度和同一套保持参数生成各条现场轨迹。连续播放入口会在打开串口前检查上述条件。

```bash
build/r1_play --standby-loop 实际串口 现场标定.txt field/standby-reference.txt 镜头一.r1traj 镜头二.r1traj
```

确认七轴稳住 5 秒后，播放器输出 `POWERED_STANDBY`，并在展开位带电等待。向该进程发送 `SIGUSR1` 会触发下一条预载镜头。每段平滑回到同一展开位，输出 `RETURNED_POWERED_STANDBY`，继续带电等候。列表播完后从首条重新排队。待机时可用 `Ctrl+C` 结束会话，程序随后发送停机帧并校验反馈。当前相机判断服务处于无臂预演，现场动作需使用已标定的轨迹。

## 水平后仰试拍

`r1_play --level-retreat 串口 标定.txt 收拢位.txt field/standby-reference.txt 水平参考J7弧度` 从同一个 21:06 展开位开始。水平参考需要用画面实测，腕部须留出 0.51° 补偿行程和 0.25° 余量。入口最多允许 J7 从基准抬镜 6°，参考位过渡的速度上限为 0.025 rad/s。此入口已通过虚拟串口测试，现场仍须核验镜头光轴。

操作员在 `LEVEL_RETREAT_STANDBY` 后发 `SIGUSR1`，腕部缓慢移到参考位。到 `LEVEL_RETREAT_VERIFY_REFERENCE` 后检查画面，确认水平再发 `SIGUSR1`，两轴用同一条曲线在 4 秒内移至后仰位。`LEVEL_RETREAT_HOLDING` 后发 `SIGUSR1`，动作按原路径回到 21:06 展开位。`LEVEL_RETREAT_RETURNED_POWERED` 表示已核对七轴回位并继续带电。结合既有流程，`SIGUSR2` 是单独的慢速收拢请求。

用 `python3 field/session.py level-retreat-record --reference 收拢位.txt --level-wrist-rad 水平参考J7弧度` 可以同步保存原始视频、各阶段角度和时间。录像确认首帧后会自动开始抬镜，接下来的画面确认和回位信号发给录像会话进程。校平阶段发 `SIGHUP` 可取消后仰并回到带电待机。

回位后看到 `LEVEL_RETREAT_READY_FOR_TAKE`，可继续向同一个录像会话发送 `SIGUSR1` 请求下一镜。每镜重新确认录像首帧和七轴在固定起点连续稳定 5 秒。后续录像按镜次分别存放，`session.json` 记录每镜的录像路径和实测角度。

## 逐轴扩幅测试

`python3 field/session.py amplitude-probe-record --reference 收拢位.txt --amplitude-plan 探测.txt` 从 21:06 展开位开始录制。探测文件首行为 `R1AMPLITUDE1` 加现场标定的 SHA-256，后续每行填写关节号和相对固定起点的带符号角度。使用现场已确认的活动空间选取端点。

每次只有一个轴改变角度。控制器记录设定及实到值，缓动回位，复核七轴连续在起点范围内 5 秒后执行下一步。某个端点未达标时记录偏差，仍须确认回位才能测下一轴。`AXIS_PROBE_COMPLETE` 表示所有轴到达且回位通过。该程序在回位后继续通电保持，慢收仍由 `SIGUSR2` 请求。
