# MiniMax H3 多卡采样、单 3080 Ti 解码：2026-10-03 实测与复现

> 公开复现记录。性能数值已按当日结果独立核对；统一 CLI 经本地模拟与行为测试，但打包期间没有启动 GPU 任务。历史路线实测不等于新入口已通过硬件端到端验收；高档输出的人工画质/音质验收仍待完成。

## 先看结果

本轮覆盖单任务冷启动与暖态采样、五秒低档六任务吞吐、高档 736×1280 五秒六任务矩阵、15 秒容量边界，以及 10GbE latent 交接。采样秒数、首个成片等待、六任务 makespan 分开列出；它们的起止定义不同，不能相加或互换。

### 单任务与容量数据

| 采样/解码路线 | 输入 | 帧数 | 采样阶段 | 解码尾段 | 状态与冷热 |
|---|---|---:|---:|---:|---|
| 4×T10 采样 → 3080 Ti | 416×736，5 秒 | 124 | 229.814 s | 12.181 s | 成功；冷模型请求 |
| 8×T10 采样 → 3080 Ti | 416×736，5 秒 | 124 | 110.395 s | 11.859 s | 成功；冷模型请求；相对 4 卡约 2.08×，采样时间少 52.0% |
| 8×T10 采样 → 3080 Ti | 736×1280，5 秒 | 124 | 167.286 s | 31.654 s | 成功；暖态采样 |
| 8×T10 采样 → 3080 Ti | 736×1280，10 秒 | 243 | 442.110 s | 59.319 s | 成功；暖态采样 |
| 8×T10 采样 → 3080 Ti | 544×960，15 秒 | 362 | 328.725 s | 50.405 s | 成功；暖态采样 |
| 8×T10 采样 → 3080 Ti | 640×1152，15 秒 | 未报告 | 约 204.389 s 后 OOM | — | 失败；未重试，也未继续测更高的 736×1280、15 秒 |
| 同机单 T10 解码既有 latent | 736×1280，5 秒 | 124 | 不适用 | 72.588 s | 成功；冷模型、无传输；FP16 视频 VAE，不与 3080 Ti 的 INT8 ConvRot 暖态尾段作硬件 A/B |

上表的“采样阶段”是提交到 ComfyUI history success；“解码尾段”是 watcher 从传输开始到 SaveVideo success。二者来自分段计时，不是保证同一时钟区间的端到端 wall time。

### 六任务吞吐矩阵

每组均为 6 个相同规格任务，服务启动时间不计入 makespan；低档组使用 416×736、5 秒，高档组使用 736×1280、5 秒。吞吐是 `6 × 3600 / makespan` 的批次折算值，不代表单片速度。

| 规格 | 拓扑 | 成功数 | 首片完成 | 六任务 makespan | 折算吞吐 |
|---|---|---:|---:|---:|---:|
| 416×736，5 秒 | 8×T10 + 3080 Ti | 6/6 | 133.314 s | 440.791 s | 49.00 片/小时 |
| 416×736，5 秒 | 4×T10 + 4×T10 + 3080 Ti | 6/6 | 146.396 s | 296.917 s | 72.75 片/小时 |
| 416×736，5 秒 | 4×T10 + 4×T10 + XTX + 3080 Ti | 6/6 | 147.707 s（真实首个 marker） | 266.324 s（协调器 monotonic） | 81.10 片/小时 |
| 416×736，5 秒 | 8×T10 + XTX + 3080 Ti | 6/6 | 136.910 s | 332.706 s | 64.92 片/小时 |
| 736×1280，5 秒 | 8×T10 + 3080 Ti | 6/6 | 238.642 s | 1024.217 s | 21.09 片/小时 |
| 736×1280，5 秒 | 8×T10 + XTX + 3080 Ti | 6/6 | 241.279 s | 945.328 s | 22.85 片/小时 |
| 736×1280，5 秒 | 4×T10 + 4×T10 + 3080 Ti | 6/6 | 346.771 s | 902.538 s | 23.93 片/小时 |
| 736×1280，5 秒 | 4×T10 + 4×T10 + XTX + 3080 Ti | 6/6 | 352.477 s | 677.538 s | 31.88 片/小时 |

低档三源组的 `147.707 s` 来自真实 pipeline marker；`266.324 s` 是协调器 monotonic makespan。原协调器 `observed_seconds` 的采样时刻受等待子进程影响，不作为逐片完成时间使用。高档三源组吞吐比高档 `4+4+3080 Ti` 提高 33.2%，性能候选通过；24 条高档输出的人工逐条画质/音质门仍待审。

## 逐轮变化与结论

### 1. 冻结 A6 基线：扩到八张 T10

416×736、5 秒的同口径冷模型请求从 4×T10 的 229.814 秒降至 8×T10 的 110.395 秒，约 2.08 倍；这说明八卡池适合压低单条冷启动采样时延。冷请求不等于后续暖态采样，也不能拿来代替批量吞吐。

8×T10 单条暖态还成功完成 736×1280、5 秒和 10 秒，以及 544×960、15 秒。640×1152、15 秒则在约 204.389 秒后发生真实任务 OOM：单 rank 还需 1.06 GiB、当时只剩 558.62 MiB，栈位于 MiniMax MLP/LoRA 路径。按预设边界没有同状态重试，也没有尝试 736×1280、15 秒。它是本轮长片容量的失败边界，不是普通告警。

### 2. 五秒低档：一池八卡与双四卡池

六个 seed 为 `987654321123–128`。8 卡单池 makespan 440.791 秒；两个互斥 4 卡池共用一条 3080 Ti 解码队列时为 296.917 秒，六片吞吐提高 48.5%。双池首片晚约 13.082 秒，但后续并行出片改善了批次总时间。这是五秒低档批量结论，不外推成 10 秒双池实测。

两池任务采样中，两条冷任务约 119.998/120.182 秒，暖任务约 76.238–77.131 秒；8 卡单池第一条采样为 107.594 秒，其余暖任务约 64.110–64.117 秒。解码队列会让成对 latent 等待，因此短完成间隔不等于稳定周期。

### 3. 五秒低档：增加第三个 XTX 采样源

两组 4×T10 加一张 RX 7900 XTX，共享单一 3080 Ti 队列，六任务成功。makespan 从 296.917 秒降至 266.324 秒，吞吐从 72.75 增至 81.10 片/小时，即 11.5%。这低于本项目新增复杂度至少换取 15% 吞吐的默认准入线，所以是可用候选，不取代五秒低档默认双四卡池。

逐片时刻按真实远端 marker 重建；第一片 147.707 秒，最后 marker 266.094 秒，协调器 monotonic makespan 266.324 秒。不要把 coordinator `observed_seconds` 重排后的记录当成真实完成序列。watcher 停止后用 `--include-existing` 接手已验收 latent 的恢复路径通过。

### 4. 五秒低档：八卡整体池再加 XTX

8×T10 单池与 XTX 各承担三条任务，makespan 332.706 秒、64.92 片/小时。相对纯 8 卡吞吐提高 32.5%，但比双四卡池低 10.8%，比三采样源低 20.0%。XTX 冷采样 197.882 秒，队列 `max_pending=0`，所以这一组主要受冷启动与任务分配影响，不是解码排队；不进入默认短批量调度。

### 5. 高档 736×1280、5 秒：四拓扑矩阵

四组每组 6/6 成功，均重新冷启动采样器与解码器。单条首片最快的是 8×T10 + 3080 Ti（238.642 秒）；六片总吞吐最高的是两个 4×T10 池加 XTX（677.538 秒，31.88 片/小时）。高档时三源的吞吐比双四卡池提高 33.2%，超过 15% 复杂度门槛，但只代表性能门。

该高档四卡 worker 有约 0.94 GiB 的 allocator OOM warning，随后 Raylight/aimdo 继续执行；相关拓扑均为 6/6 success，所有 latent 与媒体门通过。这是本次可恢复的分配告警，说明显存余量紧，应保留告警并持续监控。它不同于上文 8 卡 15 秒档导致任务终止的 fatal OOM。

高档三源组的 decoder 队列最高 pending 为 1；24 条 MP4 均通过结构与完整解码检查、逐帧非重复自动门，但人工完整观看和试听尚未完成。审核前不升级为高档生产默认。

## 固定配置、版本与补丁来源

- T10 采样：主 H3 为 INT8 ConvRot hybrid；Qwen3-VL 32B 为 INT8 ConvRot；FL2V Turbo LoRA 为 BF16。虽然 LoRA 文件名含 `4step`，这些测试实际是六步 Euler/simple。
- 当日 workflow 中的模型文件名：`10Eros_Max_h3_TURBO-hybrid_beta4_int8_convrot.safetensors`、`qwen3vl_32b_minimax_h3_int8_convrot.safetensors`、`minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors`。T10 采样侧 VAE 为 `minimax_h3_video_vae_fp16.safetensors` 与 `minimax_h3_audio_vae_fp32.safetensors`；3080 Ti 解码侧视频 VAE 为 `minimax_h3_video_vae_int8_convrot.safetensors`，音频 VAE 为 `minimax_h3_audio_vae_fp32.safetensors`。
- 分布式配置：FSDP=true、CPU offload=false；4 卡 Ulysses=4/Ring=1，8 卡 Ulysses=8/Ring=1；T10 attention 为 `TORCH_EFFICIENT`。
- XTX 采样用 CK attention；视频/音频 VAE 分别按对应路线加载。T10 采样侧记录为 FP16 视频 VAE、FP32 音频 VAE；3080 Ti 解码侧为 INT8 ConvRot 视频 VAE、FP32 音频 VAE。
- 当日 T10 与 RTX 3080 Ti 日志均记录 PyTorch `2.13.0+cu130`；这里的 `cu130` 是 PyTorch 构建标签，不等同于 NVIDIA 驱动版本。XTX 日志记录 PyTorch `2.12.0+rocm7.14.0`。
- 主代理在收尾时只读记录的 T10 源码快照：ComfyUI HEAD `5fa5e05d9ecc54f0f412c28242b38506cf9a8ea4`；真实目录 `custom_nodes/raylight` HEAD `1a4ee4a608e67d3570ad75d1318a210466f1a76b`，Raylight 工作树干净。T10 同环境记录 PyTorch `2.13.0+cu130`（pip 显示 `2.13.0`）、ray `2.58.0`、xfuser `0.4.4`、safetensors `0.8.0`。该只读收尾快照标识源码工作树，不单独证明每个长驻进程已加载的模块字节完全相同。
- 3080 Ti 只读收尾快照记录 ComfyUI HEAD `e80c1570b6b44a2557d5d8e341e05782d18c9bbb`；解码日志报告 ComfyUI `0.34.0`、Python `3.12.3`、PyTorch `2.13.0+cu130`。XTX 环境记录 PyTorch `2.12.0+rocm7.14.0`、comfy-kitchen `0.2.32`、safetensors `0.8.0`；隔离 app 不含 `.git`，XTX 源码 commit 未锁定。没有提供完整 lockfile，因此这里只报告已有只读快照和日志中实际观察到的版本，不假设另有依赖锁文件。
- 两组复现补丁已放入下载包：`assets/patches/comfy-turing-lerp.patch` 对应 ComfyUI `bdcb886 → 5fa5e05`（`torch.lerp` dtype 兼容）；`assets/patches/raylight-sm75-ulysses.patch` 对应 Raylight `63ae5b8 → 1a4ee4a`（SM75 FP16 Ulysses 与 lerp 兼容）。两侧 base 已包含 AV 采样修复；历史出处见 `h3_handoff/sm75_ab/RESULTS_2026-09-22.md`。补丁文件 SHA-256 分别为 `72736bfc1eec4eb6b3c3e0fff8e504aa5bad2ecc055f6d0fd8a2eba19d457d48` 与 `affde1c6f03df0d7946aac92c706c777bb1c4371ae3f0c4cad3cecec4cd5aeee`。
- A6 多卡 CLIP loader 在包内 `assets/comfyui_h3_multigpu_clip/__init__.py`，安装到 ComfyUI 的 `custom_nodes/comfyui_h3_multigpu_clip/__init__.py`；SPDX Apache-2.0，上游来源 Eanya-Tonic；SHA-256 `b899f87628c562425f026be339461bee2859620e98add1d2efe43b9fcaec613c`。

## 网络、输出与质量门

10GbE 链路两端均观察到 10Gb/s 全双工，双向 ping 均低于 0.3 ms。13,129,844-byte latent 连续三次传输耗时约 0.981/0.585/0.567 秒且 SHA-256 一致；因小文件受 SSH 建连开销影响，且一端没有 `iperf3` 客户端，本轮没有链路极限带宽测试。真实 latent 路径也完成传输与 3080 Ti 解码，产物为 416×736、124 帧、24 fps、AAC 32 kHz 双声道。

成功的批量 latent 为有限 FP32 video/audio 张量。低档 MP4 为 H.264 416×736、124 帧、AAC 32 kHz 双声道、5.167 秒；高档 24 条为 H.264 736×1280、124 帧，均完成完整 FFmpeg 解码。自动结构/数值通过不代表主观画质或音质通过。

早期 `ModelSamplingAV` 破音修复在历史验收中通过：修复前后原始 FLAC 的 `Abs peak count` 约从 208 降至 1，并经完整试听确认破音问题已修复。当天首轮 4 卡/8 卡 A/B 人工审阅仍评价当前主观音质一般、只是可用；“破音已修复”不等于“音质已达到高质量”。画面整体可接受，偶发凭空出现衣服属于提示遵循问题。4 卡与 8 卡同 seed 成片并非逐像素等价，视频 SSIM All 为 0.827008。上述人工结论不扩展到后续三源、高档 24 条输出。

为避免公开私人内容，本文不发布原参考图、参考图 SHA-256、原 prompt、原始日志或媒体。复现时请使用有公开/复现授权的素材，并在同一组 A/B 中固定 prompt、reference、seed 和工作流；若更换输入，吞吐数字仍可参考，画面/声音与逐像素结果不再是本组的复现。

## 复现流程与 CLI 示例

以下命令使用下载后目录名 `h3-multigpu-pipeline`。在发布文章配套的运行目录执行；模型与 prompt/reference 由复现者准备，不放入公开包。

### 资产与环境

准备上表所列模型文件、已获授权且固定的 reference 和 prompt。T10/CUDA、3080 Ti/CUDA、XTX/ROCm 分别使用各自 ComfyUI 环境，不共用 Python 环境，也不要盲目升级已有依赖。已观察到的 torch、Ray、xFuser、comfy-kitchen、safetensors 版本列在上文；没有完整 lockfile，不假定额外依赖锁文件已交付。

包内 `config/public-defaults.json` 是 `config-adapt` 的默认配置源；`config/host-adapter.example.json` 只给本机 host map 结构。复制一份到本地私有目录，填入自己的 SSH alias、ComfyUI 根目录、Python、输入目录、运行目录和 API host；不要把填写后的本地 host map 或生成配置发布。

```bash
PKG="$(cd ./h3-multigpu-pipeline && pwd)"
LOCAL_DIR=./h3-local-private
mkdir -p "$LOCAL_DIR"
cp "$PKG/config/host-adapter.example.json" "$LOCAL_DIR/host-map.json"
# 只在本机编辑 host-map.json 中的 alias 和路径。
python3 "$PKG/scripts/pipeline.py" config-adapt \
  --host-map "$LOCAL_DIR/host-map.json" \
  --out "$LOCAL_DIR/local.json"
```

`config-adapt` 会从包内 `config/public-defaults.json` 读取基础路由，再合并本机 host map；若目标配置文件已存在，它会拒绝覆盖。T10、3080 Ti、XTX 环境按各自已安装的 ComfyUI venv 分开核对：

```bash
COMFYUI_ROOT=/your/local/ComfyUI
COMFYUI_VENV=/your/local/ComfyUI/venv
RAYLIGHT_ROOT="$COMFYUI_ROOT/custom_nodes/raylight"
source "$COMFYUI_VENV/bin/activate"
python -c 'import torch; print(torch.__version__)'
python -m pip show ray xfuser safetensors
git -C "$COMFYUI_ROOT" rev-parse HEAD
git -C "$COMFYUI_ROOT/custom_nodes/raylight" rev-parse HEAD
git -C "$COMFYUI_ROOT/custom_nodes/raylight" status --short
```

在 XTX 环境中激活它自己的 ROCm venv，并核对 `python -m pip show comfy-kitchen safetensors`；XTX 隔离 app 不含 `.git`，不应填造其源码 commit。上面的命令只盘点已有环境，不安装或升级依赖。

在新的隔离源码 checkout 上，分别确认 ComfyUI base 为 `bdcb886`、Raylight base 为 `63ae5b8` 后，先检查补丁再应用；不要对正在运行的生产 checkout 应用：

```bash
git -C "$COMFYUI_ROOT" apply --check "$PKG/assets/patches/comfy-turing-lerp.patch"
git -C "$COMFYUI_ROOT" apply "$PKG/assets/patches/comfy-turing-lerp.patch"
git -C "$RAYLIGHT_ROOT" apply --check "$PKG/assets/patches/raylight-sm75-ulysses.patch"
git -C "$RAYLIGHT_ROOT" apply "$PKG/assets/patches/raylight-sm75-ulysses.patch"
```

A6 loader 可从包内 `assets/comfyui_h3_multigpu_clip/__init__.py` 安装到隔离 ComfyUI 的 `custom_nodes/comfyui_h3_multigpu_clip/__init__.py`；SPDX Apache-2.0，安装前可用 `shasum -a 256` 对照上文 SHA-256。

T10 还需将 `assets/h3_latent_handoff/` 安装到 `custom_nodes/h3_latent_handoff/`；XTX 和 decoder 只需该 handoff 节点目录。它提供 `SaveH3Latent` / `LoadH3Latent`，保留 FP32 video/audio latent 并校验有限值。请在停机的隔离环境安装，已有不同版本时先核对，勿覆盖运行中的节点。权重不包含在脚本包中。

### 先做离线计划，再启动服务

`plan` 为本地离线规划，不访问 host/API。例如五秒低档双四卡六任务与高档三源六任务：

```bash
python3 "$PKG/scripts/pipeline.py" plan \
  --mode four_four_plus_one --width 416 --height 736 --seconds 5 \
  --count 6 --seed 987654321123

python3 "$PKG/scripts/pipeline.py" plan \
  --mode four_four_xtx_plus_one --width 736 --height 1280 --seconds 5 \
  --count 6 --seed 987654321123
```

支持的 route 名为 `eight_plus_one`、`eight_xtx_plus_one`、`four_four_plus_one`、`four_four_xtx_plus_one`。本轮只对 416×736/5 秒和 736×1280/5 秒四条 route 全部有实测；736×1280/10 秒、544×960/15 秒只测 `eight_plus_one`。640×1152/15 秒为已知 fatal OOM，CLI plan 会标为 `known_oom` 并拒绝提交。计划结果不构成执行或媒体验收。

配置好本地 host map 后，可先作配置/服务预检，再管理对应模式：

```bash
python3 "$PKG/scripts/pipeline.py" preflight --config "$LOCAL_DIR/local.json" --mode four_four_plus_one
python3 "$PKG/scripts/pipeline.py" start --config "$LOCAL_DIR/local.json" --mode four_four_plus_one
python3 "$PKG/scripts/pipeline.py" status --config "$LOCAL_DIR/local.json" --mode four_four_plus_one
```

启动/状态/停止管理的是配置对应的服务。解码进程应只暴露目标 RTX 3080 Ti；本组现场是 `CUDA_VISIBLE_DEVICES=0`。首次实际执行前，在 API 上核对 GPU 可见性、工作流节点、VAE 和模型加载。

`preflight` 仅检查本地配置与模板，不联网；`start` 才会检查远端服务所有权、启动后 API、必需节点与模型选项。版本快照仍需自行核对。XTX 与 decoder 同机时将私有 host-map 的 `hosts.xtx.same_host_as_decoder` 设为 `true`，避免数据绕经协调电脑；10GbE/LAN 目标和源端 SSH 密钥路径也在该私有映射配置。

### 提交、等待、停止

`run` 和 `submit` 都要求显式提供 prompt/reference 文件；以下示例使用本机私有文件，内容不会随文章发布。后台提交后从 JSON 输出取 `run_id`，再用 `wait` 等待状态：

```bash
PROMPT_FILE=./private/prompt.txt
REFERENCE_FILE=./private/reference.png

python3 "$PKG/scripts/pipeline.py" run \
  --config "$LOCAL_DIR/local.json" --mode four_four_plus_one \
  --prompt-file "$PROMPT_FILE" --reference-file "$REFERENCE_FILE" \
  --width 416 --height 736 --seconds 5 --count 6 --seed 987654321123 \
  --background

RUN_ID=replace-with-run-id-from-output
python3 "$PKG/scripts/pipeline.py" wait "$RUN_ID" --timeout 7200
python3 "$PKG/scripts/pipeline.py" status --config "$LOCAL_DIR/local.json" --mode four_four_plus_one
```

需停止时使用 `python3 "$PKG/scripts/pipeline.py" stop --config "$LOCAL_DIR/local.json" --mode four_four_plus_one`；也可增加 `--run-id "$RUN_ID"` 发送该 run 的停止请求。先检查状态并记录 manifest，不要将“已停止”当成已成功验收。run manifest 会保存本地 prompt/reference 路径，勿把本机状态目录作为公开附件。

新入口已通过离线队列 selftest 与八项主代理独立测试，包括模拟三源六任务完整协调、已提交解码的 journal 恢复、真实历史 MP4 的 CPU 全量解码、原子交接以及服务所有权。没有启动/停止远端服务或执行新的 GPU E2E，故这些命令是操作示例，不是本轮硬件执行记录。历史 makespan 以原协调器成片完成门计时；新入口另加完整 CPU 解码验收，不能把两者计时直接当同口径 A/B。

### 重启恢复与排查

- 发生错误时先停止新任务提交，等待或记录已提交任务，再检查 history、latent、checksum、pipeline marker 与 decoder 队列，不要把重启当作已完成证明。
- 短路径 Ray socket：首个八卡试启动因 Unix socket 完整路径超过 107 字节，在初始化阶段失败且未生成 latent/MP4；将临时 Ray 路径缩短后重跑冷态正式测试成功。
- 双池数据库：曾出现第二池共享默认 ComfyUI 数据库锁定提示，但该轮六任务成功；启动脚本随后改成每池独立数据库 URL，修改后未因非致命提示重跑六片 A/B。
- 恢复：旧 watcher 已验证 `--include-existing` 接管 latent。新入口用 `python3 "$PKG/scripts/pipeline.py" resume "$RUN_ID"`：先确保旧协调器已退出；成功项跳过，已提交解码只查原 journal，保存 latent 可继续解码，状态不明不重新采样。该行为已模拟验证，真实 GPU 断线恢复仍需补验收。
- 容量：高档 4 卡 allocator warning 曾恢复并完成全部任务；8 卡 640×1152、15 秒发生 fatal OOM。遇到 fatal OOM 应保留失败结果并停在已测边界，不将它和可恢复 warning 合并统计，也不假定更高档可用。
- 3080 Ti 是单解码队列。高档实测最多 pending=1；同时送入多个 latent 时分别统计等待和尾段，不能把 decoder tail 当 sampler 计算时间。

## 证据对照与未证实项

| 证据编号 | 原始材料 | 在本文支持的结论 |
|---|---|---|
| E1 | 当日总报告《MiniMax H3：8×Tesla T10、RX 7900 XTX 与 RTX 3080 Ti 多轮实测》 | 单项规格、冷/暖时延、10GbE、质量门、低档吞吐基线与轮次结论 |
| E2 | 三采样源报告《H3 三采样源 + 单 3080 Ti 解码实测》及其运行 manifest | 三源低档 6/6、marker 完成时刻、monotonic makespan、吞吐、watcher 恢复 |
| E3 | 双源报告《H3：8×T10 + RX 7900 XTX 双采样源、单 3080 Ti 解码实测》及其运行 manifest | 8 卡池加 XTX 的低档任务分配、完成时刻和队列状态 |
| E4 | 高档矩阵报告《MiniMax H3 736×1280 / 5 秒四拓扑吞吐实测》 | 四路 makespan、首片、吞吐、24 条媒体门及 4 卡 allocator warning |
| E5 | 工作流、旧 runner 与下载包 `scripts/pipeline.py`/`config/` | seed/profile 配置、分阶段计时定义、CLI 参数和配置适配流程；新统一 CLI 未做 E2E |
| E6 | `h3_handoff/sm75_ab/RESULTS_2026-09-22.md` 与主代理只读收尾现场版本记录 | 破音修复、lerp/Ulysses 兼容补丁历史来源；T10/3080 Ti 源码 HEAD、XTX commit 证据边界与 A6 loader SHA |

未锁定或未测试：XTX 隔离 app 的源码 commit；所有端点的完整 lockfile 与每个进程实际加载源码的字节级绑定；新统一 CLI 的真实硬件 E2E 和断线恢复验收；10GbE 链路极限吞吐；10 秒双四卡批量；高档 24 条人工观看/试听；三源低档相对默认路线的 15% 准入线；任何未列规格与未列拓扑。上述能力不得标成已验证。
