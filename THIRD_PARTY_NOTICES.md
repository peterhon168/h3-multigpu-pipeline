# 第三方来源与许可证

本仓库是运行封装与实测报告，不是 MiniMax H3、ComfyUI 或 Raylight 的官方发行。

| 文件/组件 | 来源 | 许可证与说明 |
|---|---|---|
| `h3-multigpu-pipeline/assets/comfyui_h3_multigpu_clip/` | [Eanya-Tonic/2080ti-minimax-h3](https://github.com/Eanya-Tonic/2080ti-minimax-h3)，`custom_nodes/comfyui_h3_multigpu_clip/` | 文件保留 Apache-2.0 SPDX；本包为本地 A6 使用版本，不宣称与上游最新版本逐字一致 |
| `assets/patches/raylight-sm75-ulysses.patch` | [komikndr/raylight](https://github.com/komikndr/raylight) | Apache-2.0；本地 SM75 FP16 通信适配 diff，基线 `63ae5b8` |
| `assets/patches/comfy-turing-lerp.patch` | [Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI) | GPL-3.0；本地 Turing lerp 适配 diff，基线 `bdcb886` |

Apache-2.0 和 GPL-3.0 的原文分别提供于 `licenses/`。第三方文件按各自许可证分发，原有权利归属不改变。Apache 来源的本地改动/版本冻结及 GPL 补丁均不代表上游官方支持。

其余新编写的协调脚本、latent handoff 节点、配置和文档随本次公开源代码发布；本次未额外授予一个统一的开源许可证。需要商业再分发或额外授权时，请联系仓库维护者，不要将第三方许可证自动套用到整个仓库。模型权重不随仓库分发，使用者须自行遵守相应模型条款。
