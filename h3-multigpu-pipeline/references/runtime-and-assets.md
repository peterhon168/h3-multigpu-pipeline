# 运行时、模型与资产约束

## 采样基线

- 保持六步 Euler/simple、T10 `TORCH_EFFICIENT` attention、FSDP 开启、CPU offload 关闭；4 卡池 Ulysses=4/Ring=1，8 卡池 Ulysses=8/Ring=1。
- 保持已修复的音视频 AV 路径、A1 FP16 通信路径和 A6 loader/clip 行为。除另行设计的单变量验证外，不替换它们，也不混入新 attention、Ring 或 CPU-offload 参数。
- 两组 4×T10 必须是互斥池，使用独立通信端口和服务状态；单 3080 Ti 解码器共享队列并按配置执行背压。

## 三类角色的模型目录映射

按各自 workflow template 保留角色目录映射；不要把 T10、XTX 和 decoder 的相对模型路径互相套用。

| 角色 | 主模型 | Qwen / CLIP | LoRA | 视频 VAE / 音频 VAE |
| --- | --- | --- | --- | --- |
| T10 sampler | `H3/10Eros_Max_h3_TURBO-hybrid_beta4_int8_convrot.safetensors` | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | `minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors` | `h3/minimax_h3_video_vae_fp16.safetensors` / `h3/minimax_h3_audio_vae_fp32.safetensors` |
| XTX sampler | `H3-AB/10Eros_Max_h3_TURBO-hybrid_beta4_int8_convrot.safetensors` | `MiniMax-H3/qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | `H3-AB/minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors` | `MiniMax-H3/minimax_h3_video_vae_fp16.safetensors` / `MiniMax-H3/minimax_h3_audio_vae_fp32.safetensors` |
| Decoder | — | — | — | 公共默认 `H3/minimax_h3_video_vae_int8_convrot.safetensors` / `H3/minimax_h3_audio_vae_fp32.safetensors` |

decoder 表示公开配置默认值，不代表某一台机器必须使用 `H3/` 子目录；私有 host-adapter/config 可按目标 ComfyUI 实际目录映射，例如 `MiniMax-H3/`。T10 sampler 本次模板使用 FP16 video VAE 与 FP32 audio VAE；decoder 配置的 INT8 video VAE 是另一角色的文件，不要混淆。先在本地 ComfyUI 模型目录确认文件存在，再由启动后的 API preflight 检查节点/依赖；Skill 不携带模型权重。

## CUDA 与 ROCm 分界

| 角色 | 运行栈 | 约束 |
| --- | --- | --- |
| T10 sampler pools | CUDA | 使用 8 卡单池或互斥的 4+4 双池；维持 `TORCH_EFFICIENT` 与已测 FSDP 设置 |
| RX 7900 XTX sampler | ROCm | 单卡隔离；沿用已测 ROCm 环境、pinned memory、CK attention 与设备可见性配置。本次测试观测到 NUMA node 1；按实际机器拓扑选择 NUMA 绑定，不把 node 1 当作通用默认值 |
| RTX 3080 Ti decoder | CUDA | 单解码队列；VAE 文件按上表配置 |

XTX 与 decoder 在同一主机时用本地交接/copy，不绕经 Mac。292↔241 的 direct 10GbE 与 LAN 都由本机 host adapter 决定；只有配置选定的路径通过连通、latent 完整性、decoder API 与真实解码验收后，才能称为该路径已验证。Mac 中转不属于 direct-10GbE 基线。

公开默认配置只放占位 SSH alias 和通用路径。host-adapter、适配后的配置及本地 run manifest 可能记录实际 IP 和目录；不得发布包含这些信息的本地日志、配置或 manifest。不要读取或回显私钥内容。

## 运行时快照与补丁

以下为包内 `config/public-defaults.json` 声明的依赖快照，不是实时主机探测结果。主机变更后单独核对源码与环境；`preflight` 仅检查本地配置，`start` 自动检查 API 和模板必需节点。

| 角色 | 版本/commit 快照 |
| --- | --- |
| T10 ComfyUI | `5fa5e05d9ecc54f0f412c28242b38506cf9a8ea4` |
| T10 Raylight | `1a4ee4a608e67d3570ad75d1318a210466f1a76b` |
| T10 Torch / Ray / xFuser / safetensors | `2.13.0+cu130` / `2.58.0` / `0.4.4` / `0.8.0` |
| Python interpreter version | 快照未登记；以启动后 preflight/依赖快照为准 |
| Decoder ComfyUI | `e80c1570b6b44a2557d5d8e341e05782d18c9bbb` |
| XTX ComfyUI | 快照未登记 commit |
| XTX Torch / comfy-kitchen | `2.12.0+rocm7.14.0` / `0.2.32` |

`assets/patches/comfy-turing-lerp.patch` 以 ComfyUI `bdcb886` 为基线；`assets/patches/raylight-sm75-ulysses.patch` 以 Raylight `63ae5b8` 为基线。先确认本机目标 checkout 的 HEAD 与对应基线完全一致，再运行 `git apply --check` 审核；本 Skill 不自动应用 patch，不安装系统依赖，也不改动现有生产 checkout。

`assets/comfyui_h3_multigpu_clip/` 是 Apache-2.0 A6 loader 资产；`assets/h3_latent_handoff/` 包含 `SaveH3Latent` / `LoadH3Latent`。两者均随包提供。将需要的节点目录安装到隔离 ComfyUI 的 `custom_nodes/`；已有不同版本时先核对，不覆盖运行中的节点。T10 需要两者，XTX 与 decoder 需要 handoff 节点。`start` 会检查所选模板的节点可用性。
