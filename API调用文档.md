# R1-7A 本地 API 调用文档

本文是 `r1_jog` 本地 API 的完整调用说明，面向需要从 Python、C++、Shell
等程序控制机械臂末端的使用者。

## 0. 快速验证

终端 A 启动无硬件模拟服务：

```bash
cd /home/radxa/r1-roadshow/arm-control
./run.sh --dry-run --api /tmp/r1_jog.sock
```

终端 B 调用：

```bash
python3 - <<'PY'
import json
import socket
import time

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/tmp/r1_jog.sock")
stream = sock.makefile("rwb")

def call(command):
    stream.write((command + "\n").encode())
    stream.flush()
    return json.loads(stream.readline())

print(call("status"))
print(call("jog up 2"))        # 目标 +10 mm
time.sleep(1.0)
print(call("status")["rel_mm"])
print(call("stop"))
PY
```

实机把启动命令换成 `./run.sh --api /tmp/r1_jog.sock`，仍需在终端按 Enter 使能。

## 1. 当前提供什么

`r1_jog` 默认只提供命令行。通过 `--api PATH`（或配置文件 `api_socket`）启用后，
会在本地 Unix Domain Socket 上提供一套**行命令 + JSON 响应**的接口，供 Python、
C++、Shell 等程序直接调用。

特点：

- 与控制循环同进程，命令在下一个 10 ms 周期执行，不额外引入线程；
- 与键盘共用同一套限位、限速、工作空间和故障停机逻辑；
- 控制对象仍是**末端位置**，底层是对关节下发位置 FOC；
- 默认不开启，不会自动暴露到网络；
- 最多 8 个客户端，所有客户端共享同一份状态。

## 2. 启动

```bash
# 实机：键盘确认 Enter、FOC 使能成功后才开始监听
./run.sh --api /tmp/r1_jog.sock

# 无硬件模拟：有 TTY 时可同时用键盘；无 TTY 时自动进入纯 API 模式
./run.sh --dry-run --api /tmp/r1_jog.sock
```

也可以写进配置：

```ini
api_socket=/tmp/r1_jog.sock
```

行为说明：

- 实机模式仍要求操作员按 Enter 使能，API 不能跳过这一步；
- 无 TTY 且启用了 API 的 dry-run 会一直运行，直到收到 `stop`、Ctrl+C 或 SIGTERM；
- socket 权限为 `0660`；
- 如果该路径已有服务在监听，启动会失败并提示 `API socket 已被其他进程占用`；
- 路径不存在或上次异常退出留下的旧文件会被自动清理。

## 3. 协议

- 客户端发送 UTF-8 文本，一行一条命令，以 `\n` 结束，单行最长 512 字节；
- 服务端对每条命令返回一行 JSON；
- 命令名不区分大小写，参数也做小写归一化；
- 同一连接内的命令按顺序执行；
- 响应延迟通常小于 10 ms，但**运动本身**是按 25 mm/s 限速执行的，需要轮询状态。

一次调用在字节层面就是：

```
发送: status\n
接收: {"ok":true,"api_version":1,...}\n
```

## 4. 命令

| 命令 | 示例 | 说明 |
| --- | --- | --- |
| `ping` | `ping` | 连通性检查 |
| `status` | `status` | 返回完整状态 |
| `help` | `help` | 返回命令列表 |
| `jog <轴> [次数]` | `jog up 5` | 每次 5 mm，次数 1..200；轴见下表 |
| `move_rel <dx> <dy> <dz>` | `move_rel 10 0 -5` | 相对**当前目标**增加位移，单位 mm，单轴 ±1000 |
| `pause` | `pause` | 取消未完成目标并保持当前位置 |
| `resume` | `resume` | 恢复接受点动命令 |
| `stop` | `stop` | 正常停机（mode 0）并退出程序 |

`jog` 的轴：

| 轴 | 方向 | 等价写法 |
| --- | --- | --- |
| `u` | +Z 上 | `up` |
| `d` | -Z 下 | `down` |
| `l` | +Y 左 | `left` |
| `r` | -Y 右 | `right` |
| `f` | +X 前 | `forward` |
| `b` | -X 后 | `back` |

`move_rel` 是叠加到当前目标点，而不是以当前位置为基准：连续两次
`move_rel 10 0 0` 等价于 `move_rel 20 0 0`。目标点超出工作空间时会被裁剪，
响应里的 `clamped` 为 `true`。

`jog`、`move_rel` 在暂停状态下返回错误 `paused`，需要先 `resume`。

## 5. 响应字段

成功响应的 `ok` 为 `true`，主要字段：

| 字段 | 单位 | 含义 |
| --- | --- | --- |
| `api_version` | - | 当前为 1，后续只做增量字段 |
| `state` | - | `running` 或 `paused` |
| `enabled` | - | 是否已进入实机 FOC；dry-run 为 `false` |
| `dry_run` | - | 是否无硬件模拟 |
| `pose_m` | m | 当前指令末端位置（基座系，由关节目标正解得到） |
| `rel_mm` | mm | 当前**指令**位置相对启动位置，不是实测位置 |
| `target_rel_mm` | mm | 目标位置相对启动位置 |
| `pending_mm` | mm | 目标与当前指令的剩余距离 |
| `step_mm` | mm | 配置的单步长度，默认 5 |
| `workspace_mm` | mm | 相对启动位置的工作空间半宽 |
| `q_rad` | rad | 7 个关节的目标角 |
| `q_meas_rad` | rad | 7 个关节的反馈角（dry-run 与目标相同） |
| `track_err_rad` | rad | 当前最大关节跟踪误差 |
| `clients` | - | 当前连接数 |
| `clamped` | - | 仅点动类响应有，目标是否被工作空间裁剪 |

示例：

```json
{"ok":true,"api_version":1,"state":"running","enabled":true,"dry_run":false,
 "pose_m":{"x":0.162076,"y":0.033576,"z":0.307136},
 "rel_mm":{"x":0.000,"y":0.000,"z":25.000},
 "target_rel_mm":{"x":0.000,"y":0.000,"z":25.000},
 "pending_mm":0.000,"step_mm":5.000,
 "workspace_mm":{"x":80.000,"y":100.000,"z":80.000},
 "q_rad":[0.0347,0.3614,0.7466,0.9099,0.0572,-0.0426,0.0376],
 "q_meas_rad":[0.0347,0.3614,0.7466,0.9099,0.0572,-0.0426,0.0376],
 "track_err_rad":0.0000,"clients":1}
```

错误响应：

```json
{"ok":false,"error":"paused"}
```

常见错误：

| error | 原因 |
| --- | --- |
| `paused` | 暂停中收到点动命令 |
| `unknown command` | 命令名不支持 |
| `unknown axis` | `jog` 的轴不合法 |
| `count must be 1..200` | 次数越界 |
| `each axis must be within +/-1000 mm` | 单轴位移过大 |
| `usage: ...` | 参数缺失 |
| `line too long` / `input too large` | 单行或缓冲区超限 |
| `too many clients` | 连接数超过 8 |

## 6. Python 客户端

```python
import json
import socket
import time


class R1Arm:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.file = self.sock.makefile("rwb")

    def call(self, command):
        self.file.write((command + "\n").encode())
        self.file.flush()
        return json.loads(self.file.readline())

    def wait_settled(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.call("status")
            if status["pending_mm"] < 0.05:
                return status
            time.sleep(0.05)
        raise TimeoutError("motion did not settle")

    def close(self):
        self.file.close()
        self.sock.close()


arm = R1Arm("/tmp/r1_jog.sock")
print(arm.call("status"))

arm.call("jog up 5")          # 目标 +25 mm
print(arm.wait_settled()["rel_mm"])
arm.call("move_rel 0 -10 0")  # 目标再向右 10 mm
print(arm.wait_settled()["rel_mm"])

arm.call("pause")
print(arm.call("status")["state"])
arm.call("resume")

arm.call("stop")
arm.close()
```

## 7. C++ 客户端要点

```cpp
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
sockaddr_un addr{};
addr.sun_family = AF_UNIX;
std::strncpy(addr.sun_path, "/tmp/r1_jog.sock", sizeof(addr.sun_path) - 1);
::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));

const std::string request = "jog up 5\n";
::write(fd, request.data(), request.size());

char buffer[4096];
const ssize_t n = ::read(fd, buffer, sizeof(buffer) - 1);
// n > 0 时解析一行 JSON
```

## 8. 安全与限制

- API 不绕过任何安全限制：25 mm/s 笛卡尔限速、0.25 rad/s 关节限速、
  工作空间盒、关节限位、跟踪误差与关节故障停机全部生效。
- 键盘点动有 20 mm 目标超前限制；API 没有这个限制，允许一次下发较大目标，
  但运动速度仍受 25 mm/s 约束。
- `stop` 会让机械臂失力下坠，调用前必须确保臂体有承托。
- 客户端断开连接**不会**停止正在执行的目标；需要停止请显式发送 `pause` 或 `stop`。
- 同一台机械臂同一时间只能有一个控制进程，不要同时运行 `r1_jog` 与 `r1_play`。
- 这是本地接口，不要用 TCP 直接暴露到网络；远程访问请走 SSH 隧道。
- 多客户端同时下发命令会相互覆盖目标，生产环境建议只保留一个控制客户端。
- API 没有额外的身份认证，依赖 socket 文件权限（`0660`）和操作系统用户隔离。

## 9. 时序

```
T+0 ms     客户端发送 "jog up 5"
T+≤10 ms   控制循环读取命令，目标 +25 mm，立即返回响应
T+1 s      按 25 mm/s 完成 25 mm 位移
```

因此客户端应按 `pending_mm` 轮询，不要假设命令返回时运动已经完成。

## 10. 逆解与可达性

API 命令处理只修改笛卡尔目标，不在响应前做逆解。逆解由控制循环在下一个
10 ms 周期内完成，键盘和 API 走同一条路径：

```
jog / move_rel -> 修改 p_target_ -> 立即返回 ok
                        ↓ 下一周期
                servoStep() -> solveIk() -> q_cmd -> 关节 FOC
```

因此：

- `ok:true` 只表示命令已被接受，不表示目标一定可达；
- 目标超出工作空间时会在命令处理阶段被裁剪，响应里的 `clamped` 为 `true`；
- 目标在工作空间内但逆解不可达（接近奇异、关节限位）时，控制循环会取消目标、
  保持当前位置并打印 `目标不可达，保持当前位置`，`pending_mm` 不会归零；
- 客户端应给每次运动设置超时，超时后发送 `pause` 并按失败处理；
- 当前版本**没有**在响应前做同步 IK 预检查，这是为了不阻塞 100 Hz 控制循环。

如果需要“命令返回时就判定不可达”，可以在命令处理里加一次 IK 预检查，
但会重复控制循环的计算，当前未启用。
