import hashlib
import json
import os

import torch
from safetensors.torch import load_file, save_file

import comfy.nested_tensor
import folder_paths


EXTENSION = ".h3latent"


def _validate(video, audio):
    if video.ndim != 5 or video.shape[1] != 24:
        raise ValueError(f"Invalid H3 video latent shape: {tuple(video.shape)}")
    if audio.ndim != 4 or audio.shape[1:3] != (32, 2):
        raise ValueError(f"Invalid H3 audio latent shape: {tuple(audio.shape)}")
    if not torch.isfinite(video).all() or not torch.isfinite(audio).all():
        raise ValueError("H3 latent contains NaN or Inf")


class SaveH3Latent:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "samples": ("LATENT",),
                "filename_prefix": ("STRING", {"default": "h3_handoff/H3"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "model/latent/minimax"

    def save(self, samples, filename_prefix="h3_handoff/H3", prompt=None, extra_pnginfo=None):
        nested = samples["samples"]
        if not getattr(nested, "is_nested", False) or len(nested.tensors) != 2:
            raise ValueError("Save H3 Latent expects a MiniMax H3 video+audio NestedTensor")

        video, audio = (tensor.detach().contiguous().cpu() for tensor in nested.tensors)
        _validate(video, audio)
        folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, self.output_dir
        )
        final_name = f"{filename}_{counter:05}_{EXTENSION}"
        final_path = os.path.join(folder, final_name)
        part_path = final_path + ".part"
        metadata = {
            "format": "minimax-h3-av-latent-v1",
            "prompt": json.dumps(prompt) if prompt is not None else "",
            "extra_pnginfo": json.dumps(extra_pnginfo) if extra_pnginfo is not None else "",
        }
        save_file({
            "video": video,
            "audio": audio,
            "format_version": torch.tensor([1], dtype=torch.int32),
        }, part_path, metadata=metadata)
        os.replace(part_path, final_path)
        digest = hashlib.sha256()
        with open(final_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        with open(final_path + ".sha256", "w", encoding="ascii") as stream:
            stream.write(f"{digest.hexdigest()}  {final_name}\n")
        return {
            "ui": {"latents": [{"filename": final_name, "subfolder": subfolder, "type": "output"}]},
            "result": (samples,),
        }


class LoadH3Latent:
    @classmethod
    def INPUT_TYPES(cls):
        root = folder_paths.get_input_directory()
        files = [name for name in folder_paths.recursive_search(root)[0] if name.endswith(EXTENSION)]
        return {"required": {"latent": (sorted(files),)}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "load"
    CATEGORY = "model/latent/minimax"

    def load(self, latent):
        path = folder_paths.get_annotated_filepath(latent)
        tensors = load_file(path, device="cpu")
        if "video" not in tensors or "audio" not in tensors:
            raise ValueError("H3 handoff file must contain video and audio tensors")
        video, audio = tensors["video"], tensors["audio"]
        _validate(video, audio)
        nested = comfy.nested_tensor.NestedTensor((video, audio))
        return ({"samples": nested},)

    @classmethod
    def IS_CHANGED(cls, latent):
        digest = hashlib.sha256()
        with open(folder_paths.get_annotated_filepath(latent), "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


NODE_CLASS_MAPPINGS = {"SaveH3Latent": SaveH3Latent, "LoadH3Latent": LoadH3Latent}
NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveH3Latent": "Save MiniMax H3 AV Latent",
    "LoadH3Latent": "Load MiniMax H3 AV Latent",
}

