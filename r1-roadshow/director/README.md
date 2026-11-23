# 画面判断与运镜逻辑 · 2026-11-18

O6N 的 CIX NPU 运行 YOLOv8n 检测人数，后摩 LQ50 24GB 运行现有 MiniCPM-V 4.6 模型判断摄影场景。程序选择已检查的七轴仿真轨迹，保存判断回执、取流时间、输入图片哈希、轨迹哈希及幅度。当前输出是 **dry-run 镜头提案**，没有串口或电机调用。

## 已录入的逻辑

| 画面 | 镜头 |
| --- | --- |
| 两人面向彼此互动 | `ConversationSweep`：先到左位，左→右连续扫拍，回中；J1 总跨度 50° |
| 两人独立活动 | `GroupWide`：中心定场，左右覆盖，回中 |
| 多人讨论或独立活动 | `GroupWide`：中心定场，左右覆盖，回中 |
| 单人定点 | `PortraitStandingPreset`：中→近→中→远→中 |
| 单人向左／右走 | `FollowLeft/Right`：向对应方向转 25°，驻留后回中 |
| 坐到站 | `RiseFollow`：镜头中心抬高 10 cm，增加向上取景 10°，再回起点 |
| 无人 | 固定展开位待机。场地巡航预设可单独排练 |
| 遮挡、无法判断、接口异常或过期画面 | `Hold`：不产生新镜头提案 |

同一场景需要 **两组新画面连续确认**。镜头在其计划时长内不重复触发，触发之间至少间隔 12 秒。无人时等待下一次触发。画面采样间隔为 0.5 秒，积累三帧。每次模型调用结束后间隔 2 秒再判断。

静态场景使用最新原图缩至长边 640 像素直接推理，三格联系图只作日志预览。只有一个人的检测框确实横移或变高，才向模型提供连续原图，并开放对应的行走／起立选项。单张图不能触发动态场景。双人交流还要通过独立的看图是／否复核，同向背对相机行走不能直接当作交流。模型只能返回当前候选中的一个完整标签，不能自行填写关节角或执行命令。

这里的 `evidence` 是模型分类和 YOLO 人数的回执，不是口语内容识别或模型概率。数值自评置信度没有用于本次模型的判定；确认依据是检测人数、受约束的视觉分类、交流复核及时间上的连续确认。

## 幅度与速度

`config.json` 的 `motion.travel_scale=2.2` 用于原有人像、巡航的镜头位移与俯仰增量。`joint_span_deg=[50,30,30,45,30,20,40]` 是七轴逐轴幅度检查的**总跨度**，不是所有镜头的统一硬限位。完整镜头由 IK 决定各轴实际行程。

| 关节 | 逐轴检查总跨度 | 速度上限 |
| --- | ---: | ---: |
| J1 | 50° | 0.06 rad/s |
| J2 | 30° | 0.05 rad/s |
| J3 | 30° | 0.06 rad/s |
| J4 | 45° | 0.05 rad/s |
| J5 | 30° | 0.06 rad/s |
| J6 | 20° | 0.05 rad/s |
| J7 | 40° | 0.06 rad/s |

加速度上限逐轴为 0.10–0.12 rad/s²。导演轨迹采用七次缓动，每段起止的速度、加速度和加加速度均趋于零。100 Hz 预演实查的最大相邻帧关节变化约为 0.034°。双人扫拍约 77.42 秒，含起点稳镜、驻留和回位。

## 统一起点与带电待机

所有提案都绑定 **11 月 17 日 21:06 的固定展开位** [实机记录](../field/standby-home.json)。实机从收拢位准备时沿用 J4 先打开、J2 再抬起的顺序。到位后七轴稳住 5 秒再开始拍摄。每条预演首 5 秒保持同一个仿真起点，并精确回到同一组关节角。仿真角度沿用 `example.json`，实机轨迹仍需用现场关节标定和相机外参重建。走动和起立目前输出固定镜头预演。

实机播放器在固定展开位带电等候。手动排练可用 `--standby-loop` 按顺序触发。相机联动使用下文的 `field_session.py`，按有效画面选择预载的现场轨迹。每段拍完后由七轴反馈确认已回到展开位，再接收新的画面触发。两种入口均已用虚拟串口检查。 [手动排练入口](../README.md#带电待机与连续播放)

### 用昨晚实测范围检查整段流程

首次接臂从已测收拢位做联动复测。控制器按实读逐轴补齐可恢复的收拢位偏差，保留原标定和展开顺序。七轴在 21:06 展开位稳住 5 秒后开始接收新场景。基线只在已实拍的 J1 扫拍区间内运行，目标总跨度约 13.5°。

| 场景 | 基线动作 |
| --- | --- |
| 双人交流 | 左侧定场 → 连续扫向右侧 → 回起点 |
| 两人独立活动或多人 | 左侧 → 回中定场 → 右侧 → 回起点 |
| 单人持续向左或向右走 | 向同侧慢转 → 稳镜 → 回起点 |

回位后持续带电并重新确认七轴稳住 5 秒。确认切换场景，或确认无人后重新入画，可以触发下一段。`Ctrl+C` 请求先回到展开位，再按原路线收拢停机。

为场景联动会话加上 `--record-shots` 后，同一路摄像头取流会在每次触发前先确认录像首帧。七轴回到带电待机位后保存视频。录像保存在视觉会话的 `clips/` 目录，场景联动日志的 `CAMERA_RECORDING_RESULT` 包含保存位置和帧数。视觉服务和场景联动会话需使用同一个 `runs/` 目录。

使用 `--rehearsal-park` 和 `--record-shots` 时，可加 `--amplitude-plan 探测.txt` 在场景等待前做逐轴扩幅测试。测试共用视觉取流并独立保存一段录像。测试结束且确认回到起点后，同一个控制进程继续接收场景触发。

从逐轴录像恢复各关节对镜头俯仰和横滚的影响后，可以用 `build_level_rehearsal.py --optics 实拍端点.json --geometry 局部几何估计.json --out 预演目录` 生成四条多轴预演。先从统一展开位平滑调平，再用 J4 后仰，并用 J3 和 J7 补偿。双人交流保持左到右贯穿，群景增加中心停留，单侧跟拍只转向自己的方向。每条都检查 100 Hz 帧间连续性和模型碰撞，并回到同一组起始角度。

现场排练使用同目录的 `level-rehearsal.txt`，向 `field_session.py` 添加 `--level-plan 预演目录/level-rehearsal.txt --rehearsal-park 收拢位.txt --record-shots`。控制器核对标定、共同起点和单轴实测窗口，按实时反馈生成缓动。关节不到位时结束该段运镜并尝试带电回位。日志中的 `LEVEL_DIRECTOR_PHASE` 记录各阶段的关节反馈，`CAMERA_RECORDING_STARTED` 包含用于和录像对齐的首帧时间。可用 `measure_level.py` 从录像核验俯仰和横滚，竖直参考取自已录制 J1 扫拍的旋转轴。

用 `systemd-run --user` 启动实机联动时，设置 `--property=KillMode=mixed --property=TimeoutStopSec=95s`，让会话主进程先收到停止请求并完成受控收拢。切换前在日志确认 `Parked and verified mode 0 on all joints`。

视觉会话添加 `--pose-model ~/r1-pose-preview/models/yolov8s-pose.cix` 后，使用板上的 YOLOv8s-Pose 逐人检查头部、双肩和腰线。现场会话在 `--level-plan` 上添加 `--frame-people` 后，机械臂先从固定展开位进入拍摄姿态。头部靠近上沿时，每次将 J7 向上调整约 1°，并等待新画面。暂时确认不了所有人的上半身就继续等待构图；开拍后如果连续失去构图，结束该镜头并回到带电待机位。单人在画面中站定时可触发定镜，双人交流仍可触发左到右扫拍。

在固定带电待机位遇到一次小幅 J3 速度突跳时，会话丢弃已排队的拍摄请求，保持原指令并检查七轴反馈。确认恢复到固定位置并稳住五秒后，才用新的会话序号等待新画面。超速持续、超出允许位置或扭矩异常时仍触发原有保护。

```bash
python3 director/field_session.py \
  --rehearsal-park captures/20261117-210554-park-reference/park-reference.txt \
  --calibration ~/r1-tracker/r1-7a_sdk/config/joint_calib_right.txt
```

不带串口参数只检查参考文件和标定。在现场，确认停机姿态和已测收拢位一致后，增加 `--port 实际串口`。双人交流 50° 方案及其余扩展镜头使用下文的现场标定轨迹。

### 相机触发实机的准备和预检

填写实测的关节标定、镜头外参、各轴在现场可用的范围和保持控制参数。`start_rad` 要用同一份现场标定，把 21:06 展开位的七轴原始电机角转换到模型坐标。每类场景可逐项验证。省略 `--preset` 会尝试生成所有自动触发镜头。 [现场配置说明](../README.md#填写场地配置)

```bash
python3 director/build_plans.py --field-profile 现场配置.json --out 现场运镜 --preset ConversationSweep
python3 director/field_session.py 现场运镜 --calibration 现场标定.txt
```

以上须在 `r1-roadshow` 目录运行。路径生成后可将轨迹目录和现场标定一并传至 O6N。省略串口时，原生播放器仅校验轨迹、共同起点和标定。实际运行时，把机械臂置于已验证并有支持的展开位，并给同一命令指定 `--port 实际串口`。播放器在起点稳住 5 秒后等待相机。只有在待机确认之后采集的连续现场画面才会触发动作。镜头完成后同一场景维持原位，直到确认切换到另一个镜头或连续确认无人。录像回放、过期画面及未配置的镜头会保留当前待机状态。结束拍摄会话时应先给臂体提供支撑，随后 `Ctrl+C` 请求当前镜头回位后停止。

## 运行与部署

工作站重新生成轨迹，使用工程现有的 NumPy、SciPy、MuJoCo 环境：

```bash
python3 r1-roadshow/director/build_plans.py
python3 -m unittest discover -s r1-roadshow/director/tests -v
```

修改 `config.json` 后必须重建轨迹；运行器会核对配置和所有轨迹的 SHA-256，拒绝不匹配的配置或轨迹。每份轨迹还必须通过板端 `build/r1_play --check`。

O6N 安装位置为 `~/r1-roadshow/director/`。在该板现有模型和 YOLO 环境中安装用户服务：

```bash
cd ~/r1-roadshow
export R1_VLM_ENDPOINT='http://<VLM_HOST>:<PORT>'
export R1_VLM_START_SCRIPT='/absolute/path/to/model/start_script.sh'
bash director/install.sh
systemctl --user status r1-director r1-director-vlm
journalctl --user -u r1-director -n 20 --no-pager
cat director/runs/latest.json
```

安装器把服务地址和启动脚本写入本机 `~/.config/r1-director.env`（权限 `600`），启用两个用户服务。部署时优先使用本机环回地址；不要提交此环境文件。停止相机判断使用 `systemctl --user stop r1-director`，重新启动使用 `systemctl --user start r1-director`。停止后做单次测试：

```bash
./director/run.sh --duration 60
./director/run.sh --video /实际视频路径.mp4 --max-decisions 6
```

每次启动在 `director/runs/` 新建 UTC 命名的会话目录，`session.json` 保存代码哈希，`decisions.jsonl` 保存判断，`finished.json` 标记结束。单会话保留最近 120 张联系图，JSONL 按 4 MiB 轮转并保留两份备份；旧日志里的联系图可能已清理。磁盘可用空间不足 2 GiB 或相机画面超时会结束判断。原始测试证据另存于工作站 `artifacts/r1-director-validation/`。

## 本次核查范围

历史记录中 15 条逻辑与视觉适配测试通过，10 份轨迹通过模型逐帧碰撞、速度、加速度、回位检查和板端 C++ 格式复核。2026-11-23 的仓库检查为 22 项单元测试和 8 份随仓库提交的仿真轨迹哈希校验。两张双人交流照片均选中左到右扫拍；同向背对相机的两人被交流复核改判为双人独立；单人照片选中人像，低信息画面保持。

双人照片编码成静态回放后，完整流程验证了首次确认、第二次触发、随后忙碌抑制。它验证的是软件链路，不是现场对谈视频准确率。自然人员进出视频还用于检查无人、人员出现、动态分支和回放结束。当前相机朝向桌面和线缆，现场对谈、真实坐到站以及接臂后的构图效果尚未实拍验收。

照片测试来源：[Kindel Media 双人讨论](https://www.pexels.com/photo/2-persons-discussing-at-the-table-7688116/)、[cottonbro 双人交流](https://www.pexels.com/photo/man-and-woman-sitting-at-table-having-a-conversation-6321929/)、[Ketut Subiyanto 含背景第三人的场景](https://www.pexels.com/photo/man-and-woman-talking-5055239/)。其他输入来自板上已有 COCO 测试图、人员进出视频。
