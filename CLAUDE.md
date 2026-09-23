# CLAUDE.md — OctoPrint-CrealityCloud-With-Video

本文件是项目的架构说明文档,重点描述 **设备端与 CrealityCloud 之间的视频推流(直播 + 回放)逻辑**,帮助 Claude 会话和开发者快速理解代码。

## 项目概述

这是一个 OctoPrint 插件(包名 `octoprint_crealitycloud`,插件 ID `crealitycloud`,版本 1.1.6),把 Creality 打印机/树莓派盒子接入 CrealityCloud(创想云)。其中**视频推流**部分:设备端作为 **WebRTC 推流方(publisher)**,App 作为拉流方。整体分为三个平面:

- **控制面** — MQTT / ThingsBoard:设备激活、状态上报、推流开关
- **信令面** — WebSocket:SDP offer/answer、ICE 交换
- **媒体面** — WebRTC + 本地 RTSP:H.264 视频流(直播)或本地 mp4(回放)

## 总体架构图

```
┌────────────────────────────── 设备端 (OctoPrint 插件, Raspberry Pi) ──────────────────────────────┐
│                                                                                                     │
│  OctoPrint webcam 插件 (MJPEG)                                                                       │
│  http://127.0.0.1/webcam/?action=stream                                                             │
│        │                                                                                             │
│        │ ffmpeg (libx264, 常驻, 由 mediamtx runOnInit 拉起)                                          │
│        ▼                                                                                             │
│  rtsp-simple-server (mediamtx)  rtsp://127.0.0.1:8554/ch0_0                                         │
│        │                                                                                             │
│        │ aiortc MediaPlayer(format="rtsp")                                                          │
│        ▼                                                                                             │
│  RTCPeerConnection (H264+rtx, sendonly) ──WebRTC──► App                                             │
│                                                                                                     │
│  录制: MJPEG ──► ffmpeg 60s 分段 (按 printId) ──► concat output.mp4   (回放推流源)                   │
└──────────────┬──────────────────────────────────────────┬───────────────────────────────────────────┘
               │ 控制面                                    │ 信令面
        MQTT (ThingsBoard)                          WebSocket (wss)
        mqtt.crealitycloud.cn (region=0) /          api.crealitycloud.cn / .com
        mqtt.crealitycloud.com (海外)               /api/cxy/ws/webrtc/signal/push/{deviceName}
               │                                                │
        ┌──────▼────────────────────────────────────────────────▼──────┐
        │            CrealityCloud 云服务器 / App                        │
        └────────────────────────────────────────────────────────────────┘
```

## 一、控制面:MQTT / ThingsBoard(推流的"开关")

### 1. 设备激活(一次性)

1. 用户在 App 中扫码,前端 JS 把用户 JWT 提交给插件接口 `POST /get_token`(`__init__.py:102-122`)
2. `CrealityAPI.getconfig()` 用该 JWT 调 `POST /api/cxy/v2/device/user/importDevice`(带 MAC,`cxhttp.py:27-40`),先打 `.cn` 失败再打 `.com`
3. 返回 `deviceName`、`tbToken`、`iotType`、`regionId`(0=国内 cn,其它=海外 com),写入插件数据目录的 `config.json`(`config.py:42-46`)
4. 插件启动时 `CrealityCloud.__init__`(`crealitycloud.py:45`)读取配置并 `connect_thingsboard()`

### 2. 建立 IoT 连接(`crealitycloud.py:186-220`)

- 若 `iotType == 1`(阿里云凭据),先调 `exchangeTb`(`cxhttp.py:60-73`)换取 ThingsBoard token,并持久化 `iotType=2`
- `ThingsBoard`(`crealitytb.py:7`)按 region 选 host:`region != 0` → `mqtt.crealitycloud.com`,否则 `mqtt.crealitycloud.cn`(`crealitytb.py:27-29`),用 `tb_device_mqtt.TBDeviceMqttClient` 连接
- 连接后立即上报初始遥测/属性(`crealitytb.py:68-70`)
- 之后 3 秒一次的 `_iot_timer`(`crealitycloud.py:77,169-171`)调用 `sendAttributesAndTelemetry()`(`crealityprinter.py:287-293`),批量清空并上报 `_attributes_msg` / `_telemetry_msg`
- 云端下发的 RPC/属性经 `on_server_side_rpc_request` / `on_thing_prop_changed`(`crealitycloud.py:304,292`),用 `exec` 反射式地设置到 `CrealityPrinter` 的属性 setter 上

### 3. 推流触发点(核心)

App 打开直播/回放时,云端经 MQTT 下发三个属性,对应 `crealityprinter.py` 的 setter:

| MQTT 属性 | setter | 行为 |
|---|---|---|
| `token`(用户 JWT) | `crealityprinter.py:766-790` | 刷新 WebSocket/Webrtc 的 token;**若 `_webrtc_thread` 不存在或已死,启动 `start_webrtc_service` 线程**(独立 asyncio 事件循环)。即推流服务按需启动,由 token 到达触发 |
| `pullclient` | `crealityprinter.py:796-802` | 观看端 peerId,记为属性,信令中作为对端标识 |
| `livestream` | `crealityprinter.py:808-816` | 置 0 时把 `pullclient` 放入 `close_queue` → WebRTC 线程移除该 peer,App 关直播即结束推流 |

## 二、信令面:WebSocket

`start_webrtc_service`(`crealityprinter.py:835-857`):

1. 按 region 连接 `wss://api.crealitycloud.cn|com/api/cxy/ws/webrtc/signal/push/{deviceName}`(`crealityprinter.py:838-841`)
2. `WebSocketClient`(`signaling_channel.py:11`,基于 `websocket_client`):
   - `on_open` 发送 `join`(clientCtx 伪装 raspberry 设备 + `jwtToken`,`signaling_channel.py:86-103`)
   - `on_message`:非 `join` 消息放入 `websocket_queue`(`signaling_channel.py:74-79`)
   - 断线自动重连,最多 100 次(`signaling_channel.py:64-72,105-124`)
3. 协程 `websocket_msg_run`(`crealityprinter.py:818-833`)每 0.1s 轮询两个队列:
   - `websocket_queue` → `WebrtcManager.signaling_message_handler`
   - `close_queue` → `remove_peer(peerId)`;收到 `"all"` 时退出循环、关闭 WS 并停止 30s 定时器

### 信令消息处理(`webrtc_manager.py:48-93`)

| action / type | 含义 | 设备端动作 |
|---|---|---|
| `push_online` | App 客户端上线 | 保存云端下发的 ICE/TURN 服务器(`webrtc_manager.py:88-93`) |
| `ice_msg`(type=offer) | App 发来 SDP offer | `add_peer`(polite=True,`webrtc_manager.py:101`);offer 中含 `media` 字段(回放,值形如 `rec-tick-<ts>.h264`)→ `recorder.find_video(media)` 解析本地 mp4(`webrtc_manager.py:68-75`);然后 `update_session_description` 回 answer + 逐行 candidate |
| `ice_msg`(type=candidate) | App 的 ICE candidate | `update_ice_candidate` 解析并 `addIceCandidate`(`webrtc_manager.py:383-443`) |

**answer 的构造**(`webrtc_manager.py:303-381`):`setRemoteDescription(offer)` → `createAnswer()` → `setLocalDescription()`,然后从本地 SDP 文本里逐行抠出 `candidate:` 行(`i[2:]`),以单独的 `ice_msg`(type=candidate)消息发回——不依赖 `icecandidate` 事件。

## 三、媒体面:直播推流

### 1. 本地 RTSP 常驻中转

- 插件检测到 `/dev/video0` 存在时(`device_start`,`crealitycloud.py:391-410`),`video_start()` 启动 `bin/rtsp_server.sh` → 运行 `rtsp-simple-server`(mediamtx,按平台选 `bin/Linux32_armv7l` 或 `bin/Linux64_aarch64` 二进制)
- 其配置 `rtsp-simple-server.yml:140-142` 对 `ch0_0` 路径:

```yaml
paths:
  ch0_0:
    runOnInit: /usr/bin/ffmpeg  -r 10 -i http://127.0.0.1/webcam/?action=stream \
      -loglevel quiet -tune zerolatency -vcodec libx264 -preset ultrafast \
      -f rtsp rtsp://127.0.0.1:8554/ch0_0
```

  即:拉 OctoPrint webcam 插件的 **MJPEG 流** → H.264 编码(最近一次提交把 `h264_omx` 硬编改为 `libx264` 软编,兼容更多板子)→ 发布到本地 RTSP 8554。
- 因为 `runOnInit` 常驻,App 随时发起 offer 时 `rtsp://127.0.0.1:8554/ch0_0` 都有流可取,避免了每次推流重新起 ffmpeg 的延迟。

### 2. WebRTC 发送

`update_local_streams`(`webrtc_manager.py:199-240`):

- 非 Darwin/Windows 分支(Linux/树莓派):
  - `peer['media']` 为空(直播)→ `MediaPlayer('rtsp://127.0.0.1:8554/ch0_0', format="rtsp")`(`webrtc_manager.py:222`)
  - `peer['media']` 非空(回放)→ `MediaPlayer(self.filepath)`(本地 mp4,`webrtc_manager.py:224`)
- video track 挂到 transceiver(`sendonly`),**强制编解码偏好 H264 + rtx**(`webrtc_manager.py:235-240`)
- 自定义 `MediaPlayer`(`media_handlers.py:195`):用 PyAV `av.open` 解封装,`player_worker` 后台线程(`media_handlers.py:83-152`)逐帧 decode 后通过 `asyncio.run_coroutine_threadsafe` 塞进 track 队列;`rtsp` 在 `REAL_TIME_FORMATS`(`media_handlers.py:17-37`)中,不做节流

## 四、回放推流

1. **录制**(`recorder.py`):
   - `PRINT_STARTED` 事件 → `recorder.set_printid(printId)` + `recorder.run()`(`crealitycloud.py:526-529`)
   - `start_recorder`(`recorder.py:117-149`):ffmpeg 从 MJPEG 源(`recorder.py:30`)按 **60 秒分段**(`-segment_time 60`)写入 `creality_recorder/<日期>/<printId>/<HH-MM-SS>.mp4`,编码 `h264_omx`;1s watchdog(`top_of_hour_restart`,`recorder.py:160-171`)负责 ffmpeg 掉线重启
   - 打印结束/取消(DISCONNECTED / PRINT_CANCELLED / PRINT_DONE,`crealitycloud.py:459-562`)→ `recorder.stop()` → `concat_video`(`recorder.py:386-409`)用 concat `-c copy` 无损拼成 **`output.mp4`**,并删除分段文件;`vlist.json` 记录每个 printId 的 start/end 时间(`recorder.py:235-278`)
2. **回放请求映射**:`find_video`(`recorder.py:411-441`)把 `rec-tick-<ts>.h264` 的 ts(+28800 即东八区)换算成日期/时刻,在 `vlist.json` 中找到覆盖该时刻的 printId,返回 `creality_recorder/<日期>/<printId>/output.mp4`
3. **推流**:`update_local_streams` 检测到 media 非空时改用 `MediaPlayer(本地 mp4)`,走同一条 WebRTC 链路推给 App

## 五、状态上报与终止

- `track_states`(`webrtc_manager.py:493-511`):监听 `iceconnectionstatechange`,每次变化立即向 WS 发 `push_state`(iceState/clientId/connectedTime);状态 `failed` 时把该 peer 放入 `close_queue` 断开
- 30s 定时兜底:`_pc_update_timer` → `peerconnection_upadate`(`crealityprinter.py:850-851,859-861`)
- 空闲自退出:`push_state` 内部计数(`webrtc_manager.py:547-554`)**连续 10 次(约 5 分钟)无活跃 peer 就向 close_queue 放 `"all"`**,`websocket_msg_run` 收到后退出,整个信令/推流服务停止
- App 关闭直播页 → 云端下发 `livestream=0` → `close_queue` → `remove_peer`(停止 MediaPlayer、关闭 RTCPeerConnection,`webrtc_manager.py:445-477`)

## 关键文件对照

| 文件 | 职责 |
|---|---|
| `octoprint_crealitycloud/__init__.py` | OctoPrint 插件入口;`/get_token` 激活、`/status`、回放文件的 HTTP Range 接口(`get_recorder_file`:184)、gcode 钩子(M220/SD 进度) |
| `octoprint_crealitycloud/crealitycloud.py` | 主控制器:MQTT 连接、事件驱动(打印开始→录制、断开→停止)、rtsp_server 启动、VOD(遗留) |
| `octoprint_crealitycloud/crealityprinter.py` | 设备状态模型 + **推流开关**(token/livestream/pullclient 三个 MQTT 属性的 setter)、WebRTC 服务线程 |
| `octoprint_crealitycloud/signaling_channel.py` | `websocket_client` 封装:join 注册、队列化收消息、自动重连 |
| `octoprint_crealitycloud/webrtc_manager.py` | aiortc 对端管理:offer/answer、ICE、媒体源选择、push_state 上报与空闲退出 |
| `octoprint_crealitycloud/media_handlers.py` | 自定义 MediaPlayer/Blackhole(PyAV 解码 + 后台线程帧队列) |
| `octoprint_crealitycloud/recorder.py` | 录像 60s 分段/拼接 output.mp4/回放文件定位(find_video) |
| `octoprint_crealitycloud/crealitytb.py` | ThingsBoard MQTT 封装(tb_device_mqtt),region 选 host |
| `octoprint_crealitycloud/cxhttp.py` | 创想云 REST:importDevice(激活)、exchangeTb(换 TB token) |
| `octoprint_crealitycloud/config.py` | 读写 `config.json`(deviceName/deviceSecret/iotType/region)与遗留 `p2pcfg.json` |
| `octoprint_crealitycloud/bin/` | mediamtx 双平台二进制 + `rtsp-simple-server.yml`(runOnInit 挂 ffmpeg) |
| `octoprint_crealitycloud/static/js/crealitycloudlive.js` | 前端 Tab:本地 Web 界面的录像回放列表/播放(与 App 推流无关) |

## 配置

- `config.json`(插件数据目录,由 `/get_token` 生成):`deviceName`、`deviceSecret`(TB token)、`iotType`、`region`(0=国内)
- 直播依赖:OctoPrint **Webcam 插件** 的 `http://127.0.0.1/webcam/?action=stream`(MJPEG)

## 遗留 / 未生效代码(修改时注意)

| 位置 | 说明 |
|---|---|
| `crealitycloud.py:642-674` `start_p2p_service` / `start_video_service` | 老 P2P 方案(靠云端下发 `APILicense`/`DIDString`/`InitString` 跑 `p2p_server.sh`),`video_start` 中相关逻辑已注释;`DIDString` setter 仍会 fire `CrealityCloud-Video` 事件(`crealityprinter.py:509`),但该事件现在只是重启 rtsp_server |
| `crealitycloud.py:680-705` FIFO `/tmp/rpfifo` + `read_pipe_play` | 监听 RTSP 日志的 `OPTIONS` 请求再调 `recorder.play()`,但 `recorder.play` 开头直接 `return`(`recorder.py:202-208`),整条链路已失效;`recorder.py:299-384` 的 `start_play`/`create_play_list` 同样是遗留 RTSP 推流 |
| `crealitycloud.py:63-65` + `timelapse_vod_upload:707-727` | 硬编码的阿里云 VOD STS **测试凭证**;`MOVIE_DONE` 里的上传调用已被注释(`crealitycloud.py:577-582`),时差片上传当前不工作 |
| `webrtc_manager.py:34-46,479-491` | 启动时主动 HTTP 拉 `iceServersJwt` 的逻辑被注释,ICE/TURN 服务器现在**完全依赖云端经 `push_online`/`offer` 下发** |

## 开发环境

- 目标平台:Raspberry Pi(armv7l / aarch64 双架构二进制见 `bin/`),Python 3.8+
- 依赖(`setup.py:36`):`OctoPrint>1.3.8`、`paho_mqtt==1.6.1`、`pyjwt==2.8.0`、`ffmpy==0.3.1`、`tb-mqtt-client==1.2`、`websocket_client`、`av==10.0.0`、`aiortc==1.5.0`(及遗留的 aliyun VOD SDK)
- 安装:`pip install -e .`(需在同一 Python 环境下装有 OctoPrint 的 `octoprint_setuptools`)
- 系统依赖:`ffmpeg`(recorder 与 mediamtx 的 runOnInit 都调用)、`/usr/bin/ffmpeg` 路径在 yml 中写死
- 注意:代码中 Windows/macOS 分支(如 `webrtc_manager.py:209-216`、`recorder.py:124-133`)仅为开发调试残留,生产路径是 Linux 分支

