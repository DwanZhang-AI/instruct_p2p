from __future__ import annotations

import math
import random
import sys
from argparse import ArgumentParser

import einops
import k_diffusion as K
import numpy as np
import torch
import torch.nn as nn
from einops import rearrange
from omegaconf import OmegaConf
from PIL import Image, ImageOps
from torch import autocast

sys.path.append("./stable_diffusion")

from .stable_diffusion.ldm.util import instantiate_from_config


class CFGDenoiser(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.inner_model = model

    def forward(self, z, sigma, cond, uncond, text_cfg_scale, image_cfg_scale):
        cfg_z = einops.repeat(z, "1 ... -> n ...", n=3)
        cfg_sigma = einops.repeat(sigma, "1 ... -> n ...", n=3)
        cfg_cond = {
            "c_crossattn": [torch.cat([cond["c_crossattn"][0], uncond["c_crossattn"][0], uncond["c_crossattn"][0]])],
            "c_concat": [torch.cat([cond["c_concat"][0], cond["c_concat"][0], uncond["c_concat"][0]])],
        }
        out_cond, out_img_cond, out_uncond = self.inner_model(cfg_z, cfg_sigma, cond=cfg_cond).chunk(3)
        return out_uncond + text_cfg_scale * (out_cond - out_img_cond) + image_cfg_scale * (out_img_cond - out_uncond)


def load_model_from_config(config, ckpt, vae_ckpt=None, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    if vae_ckpt is not None:
        print(f"Loading VAE from {vae_ckpt}")
        vae_sd = torch.load(vae_ckpt, map_location="cpu")["state_dict"]
        sd = {
            k: vae_sd[k[len("first_stage_model.") :]] if k.startswith("first_stage_model.") else v
            for k, v in sd.items()
        }
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)
    return model

# def modify(model, input_img, edit_prompt, model_type):
#     parser = ArgumentParser()
#     parser.add_argument("--resolution", default=512, type=int)
#     parser.add_argument("--steps", default=20, type=int)
#     parser.add_argument("--config", default="/localdisk1/hanghua/regionllm/agent/instruct_p2p/configs/generate.yaml", type=str)
#     parser.add_argument("--ckpt", default="/localdisk1/hanghua/regionllm/agent/instruct_p2p/checkpoints/MagicBrush-epoch-52-step-4999.ckpt", type=str)
#     parser.add_argument("--vae-ckpt", default=None, type=str)
#     parser.add_argument("--cfg-text", default=7.5, type=float)
#     parser.add_argument("--cfg-image", default=1.5, type=float)
#     parser.add_argument("--seed", type=int)
#     args = parser.parse_args()

#     if model_type == 'magicbrush':
#         args.ckpt = "/localdisk1/hanghua/regionllm/agent/instruct_p2p/checkpoints/MagicBrush-epoch-52-step-4999.ckpt"
#     elif model_type == 'instructp2p':
#         args.ckpt = '/localdisk1/hanghua/regionllm/agent/instruct_p2p/checkpoints/instruct-pix2pix-00-22000.ckpt'

#     model.eval().cuda()
#     model_wrap = K.external.CompVisDenoiser(model)
#     model_wrap_cfg = CFGDenoiser(model_wrap)
#     null_token = model.get_learned_conditioning([""])

#     seed = random.randint(0, 100000) if args.seed is None else args.seed
#     input_image = input_img
#     width, height = input_image.size
#     factor = args.resolution / max(width, height)
#     factor = math.ceil(min(width, height) * factor / 64) * 64 / min(width, height)
#     width = int((width * factor) // 64) * 64
#     height = int((height * factor) // 64) * 64
#     input_image = ImageOps.fit(input_image, (width, height), method=Image.Resampling.LANCZOS)

#     if edit_prompt == "":
#         return input_image

#     with torch.no_grad(), autocast("cuda"), model.ema_scope():
#         cond = {}
#         cond["c_crossattn"] = [model.get_learned_conditioning([edit_prompt])]
#         input_image = 2 * torch.tensor(np.array(input_image)).float() / 255 - 1
#         input_image = rearrange(input_image, "h w c -> 1 c h w").to(model.device)
#         cond["c_concat"] = [model.encode_first_stage(input_image).mode()]

#         uncond = {}
#         uncond["c_crossattn"] = [null_token]
#         uncond["c_concat"] = [torch.zeros_like(cond["c_concat"][0])]

#         sigmas = model_wrap.get_sigmas(args.steps)

#         extra_args = {
#             "cond": cond,
#             "uncond": uncond,
#             "text_cfg_scale": args.cfg_text,
#             "image_cfg_scale": args.cfg_image,
#         }
#         torch.manual_seed(seed)
#         z = torch.randn_like(cond["c_concat"][0]) * sigmas[0]
#         z = K.sampling.sample_euler_ancestral(model_wrap_cfg, z, sigmas, extra_args=extra_args)
#         x = model.decode_first_stage(z)
#         x = torch.clamp((x + 1.0) / 2.0, min=0.0, max=1.0)
#         x = 255.0 * rearrange(x, "1 c h w -> h w c")
#         edited_image = Image.fromarray(x.type(torch.uint8).cpu().numpy())

#     return edited_image


def modify(model, input_img, edit_prompt, model_type,
           resolution=512, steps=20, cfg_text=7.5, cfg_image=1.5, seed=None):
    # Model checkpoint selection based on model_type
    if model_type == 'magicbrush':
        ckpt = "/localdisk1/hanghua/regionllm/agent/instruct_p2p/checkpoints/MagicBrush-epoch-52-step-4999.ckpt"
    elif model_type == 'instructp2p':
        ckpt = '/localdisk1/hanghua/regionllm/agent/instruct_p2p/checkpoints/instruct-pix2pix-00-22000.ckpt'

    model.eval().cuda()
    model_wrap = K.external.CompVisDenoiser(model)
    model_wrap_cfg = CFGDenoiser(model_wrap)
    null_token = model.get_learned_conditioning([""])

    seed = random.randint(0, 100000) if seed is None else seed
    width, height = input_img.size
    factor = resolution / max(width, height)
    factor = math.ceil(min(width, height) * factor / 64) * 64 / min(width, height)
    width, height = int((width * factor) // 64) * 64, int((height * factor) // 64) * 64
    input_img = ImageOps.fit(input_img, (width, height), method=Image.Resampling.LANCZOS)

    if edit_prompt == "":
        return input_img

    with torch.no_grad(), autocast("cuda"), model.ema_scope():
        cond = {
            "c_crossattn": [model.get_learned_conditioning([edit_prompt])]
        }
        input_img_tensor = 2 * torch.tensor(np.array(input_img)).float() / 255 - 1
        input_img_tensor = rearrange(input_img_tensor, "h w c -> 1 c h w").to(model.device)
        cond["c_concat"] = [model.encode_first_stage(input_img_tensor).mode()]

        uncond = {
            "c_crossattn": [null_token],
            "c_concat": [torch.zeros_like(cond["c_concat"][0])]
        }

        sigmas = model_wrap.get_sigmas(steps)

        extra_args = {
            "cond": cond,
            "uncond": uncond,
            "text_cfg_scale": cfg_text,
            "image_cfg_scale": cfg_image,
        }
        torch.manual_seed(seed)
        z = torch.randn_like(cond["c_concat"][0]) * sigmas[0]
        z = K.sampling.sample_euler_ancestral(model_wrap_cfg, z, sigmas, extra_args=extra_args)
        x = model.decode_first_stage(z)
        x = torch.clamp((x + 1.0) / 2.0, min=0.0, max=1.0)
        x = 255.0 * rearrange(x, "1 c h w -> h w c")
        edited_image = Image.fromarray(x.type(torch.uint8).cpu().numpy())

    return edited_image