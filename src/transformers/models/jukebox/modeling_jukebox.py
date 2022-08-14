# coding=utf-8
# Copyright 2022 The OpenAI Team Authors and HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PyTorch Jukebox model."""

import math
import os
import time

import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
from packaging import version
from torch import nn


if version.parse(torch.__version__) >= version.parse("1.6"):
    is_amp_available = True
    # from torch.cuda.amp import autocast
else:
    is_amp_available = False

import gc
import sys

from tqdm import tqdm

from ...activations import ACT2FN
from ...modeling_utils import PreTrainedModel
from ...utils import add_start_docstrings, logging
from .configuration_jukebox import JukeboxConfig
from .tokenization_jukebox import get_relevant_lyric_tokens


logger = logging.get_logger(__name__)

_CHECKPOINT_FOR_DOC = "ArthurZ/jukebox-dummy"
_CONFIG_FOR_DOC = "JukeboxConfig"
_TOKENIZER_FOR_DOC = "JukeboxTokenizer"

JUKEBOX_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "openai/jukebox-dummy",
    "openai/jukebox-1b-lyrics",
    "openai/jukebox-5b-lyrics",
    # See all Jukebox models at https://huggingface.co/models?filter=jukebox
]


def empty_cache():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def get_range(list):
    return tqdm(
        list,
        leave=True,
        file=sys.stdout,
        bar_format="{n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]",
    )


####################################################################
# Attention and scalable transformer
# Import FusedLayerNorm if we have apex, otherwise use regular LayerNorm
try:
    from apex.normalization import FusedLayerNorm

    print("Using apex FusedLayerNorm")
except ImportError:
    from torch.nn import LayerNorm as FusedLayerNorm


class JukeboxConv1D(nn.Module):
    def __init__(self, n_in, n_out, zero_out=False):
        super(JukeboxConv1D, self).__init__()
        self.n_in = n_in
        self.n_out = n_out
        if zero_out:
            w = torch.zeros(n_in, n_out)
        else:
            w = torch.empty(n_in, n_out)

        b = torch.zeros(n_out)
        self.weight = nn.Parameter(w)
        self.bias = nn.Parameter(b)

    def forward(self, hidden_states):
        size_out = (*hidden_states.size()[:-1], self.n_out)
        hidden_states = torch.addmm(
            self.bias.type_as(hidden_states),
            hidden_states.view(-1, hidden_states.size(-1)),
            self.weight.type_as(hidden_states),
        )  # If hidden_states if float then float else half
        hidden_states = hidden_states.view(*size_out)
        return hidden_states


class ResConv1DBlock(nn.Module):
    def __init__(self, n_in, n_state, dilation=1, zero_out=False, res_scale=1.0):
        super().__init__()
        padding = dilation
        self.relu = nn.ReLU()
        self.conv1d_1 = nn.Conv1d(n_in, n_state, 3, 1, padding, dilation)
        self.conv1d_2 = nn.Conv1d(n_state, n_in, 1, 1, 0)
        self.res_scale = res_scale

    def forward(self, hidden_states):
        residuals = hidden_states
        hidden_states = self.relu(hidden_states)
        hidden_states = self.conv1d_1(hidden_states)
        hidden_states = self.relu(hidden_states)
        hidden_states = self.conv1d_2(hidden_states)
        return residuals + self.res_scale * hidden_states


class Resnet1D(nn.Module):
    def __init__(
        self,
        n_in,
        n_depth,
        m_conv=1.0,
        dilation_growth_rate=1,
        dilation_cycle=None,
        zero_out=False,
        res_scale=False,
        reverse_dilation=False,
        checkpoint_res=False,
    ):
        super().__init__()

        def _get_depth(depth):
            if dilation_cycle is None:
                return depth
            else:
                return depth % dilation_cycle

        blocks = []
        for depth in range(n_depth):
            blocks.append(
                ResConv1DBlock(
                    n_in,
                    int(m_conv * n_in),
                    dilation=dilation_growth_rate ** _get_depth(depth),
                    zero_out=zero_out,
                    res_scale=1.0 if not res_scale else 1.0 / math.sqrt(n_depth),
                )
            )

        self.checkpoint_res = checkpoint_res
        if reverse_dilation:
            blocks = blocks[::-1]
        self.resnet_block = nn.ModuleList(blocks)

    def forward(self, hidden_states):
        for block in self.resnet_block:
            hidden_states = block(hidden_states)
        return hidden_states


class EncoderConvBlock(nn.Module):
    def __init__(
        self,
        input_emb_width,
        output_emb_width,
        down_t,
        stride_t,
        width,
        depth,
        m_conv,
        dilation_growth_rate=1,
        dilation_cycle=None,
        zero_out=False,
        res_scale=False,
    ):
        super().__init__()
        blocks = []
        filter_t, pad_t = stride_t * 2, stride_t // 2
        if down_t > 0:
            for i in range(down_t):
                blocks.append(nn.Conv1d(input_emb_width if i == 0 else width, width, filter_t, stride_t, pad_t))
                blocks.append(
                    Resnet1D(width, depth, m_conv, dilation_growth_rate, dilation_cycle, zero_out, res_scale)
                )
        self.proj_out = nn.Conv1d(width, output_emb_width, 3, 1, 1)
        self.downsample_block = nn.ModuleList(blocks)

    def forward(self, hidden_states):
        for block in self.downsample_block:
            hidden_states = block(hidden_states)
        hidden_states = self.proj_out(hidden_states)
        return hidden_states


class DecoderConvBock(nn.Module):
    def __init__(
        self,
        input_emb_width,
        output_emb_width,
        down_t,
        stride_t,
        width,
        depth,
        m_conv,
        dilation_growth_rate=1,
        dilation_cycle=None,
        zero_out=False,
        res_scale=False,
        reverse_decoder_dilation=False,
        checkpoint_res=False,
    ):
        super().__init__()
        blocks = []
        if down_t > 0:
            filter_t, pad_t = stride_t * 2, stride_t // 2
            self.proj_in = nn.Conv1d(output_emb_width, width, 3, 1, 1)
            for i in range(down_t):
                blocks.append(
                    Resnet1D(
                        width,
                        depth,
                        m_conv,
                        dilation_growth_rate,
                        dilation_cycle,
                        zero_out=zero_out,
                        res_scale=res_scale,
                        reverse_dilation=reverse_decoder_dilation,
                        checkpoint_res=checkpoint_res,
                    )
                )
                blocks.append(
                    nn.ConvTranspose1d(
                        width, input_emb_width if i == (down_t - 1) else width, filter_t, stride_t, pad_t
                    )
                )

        self.upsample_block = nn.ModuleList(blocks)

    def forward(self, hidden_states):
        hidden_states = self.proj_in(hidden_states)
        for block in self.upsample_block:
            hidden_states = block(hidden_states)
        return hidden_states


class Encoder(nn.Module):
    def __init__(self, input_emb_width, output_emb_width, levels, downs_t, strides_t, **block_kwargs):
        super().__init__()
        self.input_emb_width = input_emb_width
        self.output_emb_width = output_emb_width
        self.levels = levels
        self.downs_t = downs_t
        self.strides_t = strides_t

        block_kwargs_copy = dict(**block_kwargs)
        if "reverse_decoder_dilation" in block_kwargs_copy:
            del block_kwargs_copy["reverse_decoder_dilation"]

        def level_block(level, down_t, stride_t):
            return EncoderConvBlock(
                input_emb_width if level == 0 else output_emb_width,
                output_emb_width,
                down_t,
                stride_t,
                **block_kwargs_copy,
            )

        self.level_blocks = nn.ModuleList()

        iterator = zip(list(range(self.levels)), downs_t, strides_t)
        for level, down_t, stride_t in iterator:
            self.level_blocks.append(level_block(level, down_t, stride_t))

    def forward(self, hidden_states):
        all_hidden_states = []

        # 64, 32, ...
        for level in range(self.levels):
            level_block = self.level_blocks[level]
            hidden_states = level_block(hidden_states)
            all_hidden_states.append(hidden_states)

        return all_hidden_states


class Decoder(nn.Module):
    def __init__(self, input_emb_width, output_emb_width, levels, downs_t, strides_t, **block_kwargs):
        super().__init__()
        self.input_emb_width = input_emb_width
        self.output_emb_width = output_emb_width
        self.levels = levels
        self.downs_t = downs_t
        self.strides_t = strides_t

        def level_block(level, down_t, stride_t):
            return DecoderConvBock(output_emb_width, output_emb_width, down_t, stride_t, **block_kwargs)

        self.level_blocks = nn.ModuleList()
        iterator = zip(list(range(self.levels)), downs_t, strides_t)
        for level, down_t, stride_t in iterator:
            self.level_blocks.append(level_block(level, down_t, stride_t))

        self.out = nn.Conv1d(output_emb_width, input_emb_width, 3, 1, 1)

    def forward(self, hidden_states, all_levels=True):
        hidden_state = hidden_states[-1]

        # 32, 64 ...
        for level in reversed(range(self.levels)):
            level_block = self.level_blocks[level]
            hidden_state = level_block(hidden_state)

            if level != 0 and all_levels:
                hidden_state = hidden_state + hidden_states[level - 1]

        hidden_state = self.out(hidden_state)
        return hidden_state


def dont_update(params):
    for param in params:
        param.requires_grad = False


def update(params):
    for param in params:
        param.requires_grad = True


def calculate_strides(strides, downs):
    return [stride**down for stride, down in zip(strides, downs)]


class JukeboxBottleneckBlock(nn.Module):
    def __init__(self, codebook_dim, codebook_width, mu):
        super().__init__()
        self.codebook_dim = codebook_dim
        self.codebook_width = codebook_width
        self.mu = mu
        self.reset_codebook()
        self.threshold = 1.0

    def reset_codebook(self):
        self.init = False
        self.codebook_sum = None
        self.codebook_elem = None
        self.register_buffer("codebook", torch.zeros(self.codebook_dim, self.codebook_width))

    def _tile(self, hidden_states):
        dim, embed_width = hidden_states.shape
        if dim < self.codebook_dim:
            n_repeats = (self.codebook_dim + dim - 1) // dim
            std = 0.01 / np.sqrt(embed_width)
            hidden_states = hidden_states.repeat(n_repeats, 1)
            hidden_states = hidden_states + torch.randn_like(hidden_states) * std
        return hidden_states

    def init_codebook(self, hidden_states):
        codebook_dim = self.codebook_dim  # mu,
        self.init = True
        # init k_w using random vectors from hidden_states codebook_w (index w?)
        codes = self._tile(hidden_states)
        self.codebook = codes[torch.randperm(codes.shape[0])][:codebook_dim]
        self.codebook_sum = self.codebook
        self.codebook_elem = torch.ones(codebook_dim, device=self.codebook.device)

    def update_codebook(self, hidden_states, latent_states):
        mu, codebook_width, codebook_dim = self.mu, self.codebook_width, self.codebook_dim
        with torch.no_grad():
            # Calculate new centres
            latent_states_onehot = torch.zeros(
                codebook_dim, hidden_states.shape[0], device=hidden_states.device
            )  # codebook_dim, N * L
            latent_states_onehot.scatter_(0, latent_states.view(1, hidden_states.shape[0]), 1)

            _codebook_sum = torch.matmul(latent_states_onehot, hidden_states)  # codebook_dim, w
            _codebook_elem = latent_states_onehot.sum(dim=-1)  # codebook_dim
            codes = self._tile(hidden_states)
            _random_codebook = codes[torch.randperm(codes.shape[0])][:codebook_dim]

            # Update centres
            old_codebook = self.codebook
            self.codebook_sum = mu * self.codebook_sum + (1.0 - mu) * _codebook_sum  # w, codebook_dim
            self.codebook_elem = mu * self.codebook_elem + (1.0 - mu) * _codebook_elem  # codebook_dim
            usage = (self.codebook_elem.view(codebook_dim, 1) >= self.threshold).float()
            self.codebook = (
                usage
                * (self.codebook_sum.view(codebook_dim, codebook_width) / self.codebook_elem.view(codebook_dim, 1))
                + (1 - usage) * _random_codebook
            )
            _codebook_prob = _codebook_elem / torch.sum(
                _codebook_elem
            )  # latent_states_onehot.mean(dim=-1)  # prob of each bin
            entropy = -torch.sum(_codebook_prob * torch.log(_codebook_prob + 1e-8))  # entropy ie how diverse
            used_curr = (_codebook_elem >= self.threshold).sum()
            usage = torch.sum(usage)
            dk = torch.norm(self.codebook - old_codebook) / np.sqrt(np.prod(old_codebook.shape))
        return dict(entropy=entropy, used_curr=used_curr, usage=usage, dk=dk)

    def preprocess(self, hidden_states):
        # NCT -> NTC -> [NT, C]
        hidden_states = hidden_states.permute(0, 2, 1).contiguous()
        hidden_states = hidden_states.view(-1, hidden_states.shape[-1])  # x_en = (N *L, w), k_j = (w, codebook_dim)

        if hidden_states.shape[-1] == self.codebook_width:
            prenorm = torch.norm(hidden_states - torch.mean(hidden_states)) / np.sqrt(np.prod(hidden_states.shape))
        elif hidden_states.shape[-1] == 2 * self.codebook_width:
            x1, x2 = hidden_states[..., : self.codebook_width], hidden_states[..., self.codebook_width :]
            prenorm = (torch.norm(x1 - torch.mean(x1)) / np.sqrt(np.prod(x1.shape))) + (
                torch.norm(x2 - torch.mean(x2)) / np.sqrt(np.prod(x2.shape))
            )

            # Normalise
            hidden_states = x1 + x2

        return hidden_states, prenorm

    def postprocess(self, latent_states, dequantised_states, x_shape):
        # [NT, C] -> NTC -> NCT
        N, T = x_shape
        dequantised_states = dequantised_states.view(N, T, -1).permute(0, 2, 1).contiguous()
        latent_states = latent_states.view(N, T)
        return latent_states, dequantised_states

    def quantise(self, latent_states):
        # Calculate latent code latent_states
        codebook_weights = self.codebook.t()
        distance = (
            torch.sum(latent_states**2, dim=-1, keepdim=True)
            - 2 * torch.matmul(latent_states, codebook_weights)
            + torch.sum(codebook_weights**2, dim=0, keepdim=True)
        )  # (N *L, b)
        min_distance, music_tokens = torch.min(distance, dim=-1)
        fit = torch.mean(min_distance)
        return music_tokens, fit

    def dequantise(self, music_tokens):
        dequantised_states = F.embedding(music_tokens, self.codebook)
        return dequantised_states

    def encode(self, latent_states):
        samples, _, seq_len = latent_states.shape

        # Preprocess.
        latent_states, _ = self.preprocess(latent_states)

        # Quantise
        music_tokens, _ = self.quantise(latent_states)

        # Postprocess.
        music_tokens = music_tokens.view(samples, seq_len)
        return music_tokens

    def decode(self, music_tokens):
        samples, seq_len = music_tokens.shape

        # Dequantise
        dequantised_states = self.dequantise(music_tokens)

        # Postprocess
        dequantised_states = (
            dequantised_states.view(samples, seq_len, self.codebook_width).permute(0, 2, 1).contiguous()
        )
        return dequantised_states

    def forward(self, hidden_states, update_codebook=True):
        samples, _, seq_len = hidden_states.shape

        # Preprocess
        hidden_states, prenorm = self.preprocess(hidden_states)

        # Init codebook if not inited
        if update_codebook and not self.init:
            self.init_codebook(hidden_states)

        # Quantise and dequantise through bottleneck
        music_tokens, fit = self.quantise(hidden_states)
        dequantised_states = self.dequantise(music_tokens)

        # Update embeddings
        if update_codebook:
            update_metrics = self.update_codebook(hidden_states, music_tokens)
        else:
            update_metrics = {}

        # Loss
        commit_loss = torch.norm(dequantised_states.detach() - hidden_states) ** 2 / np.prod(hidden_states.shape)

        # Passthrough
        dequantised_states = hidden_states + (dequantised_states - hidden_states).detach()

        # Postprocess
        music_tokens, dequantised_states = self.postprocess(music_tokens, dequantised_states, (samples, seq_len))
        return music_tokens, dequantised_states, commit_loss, dict(fit=fit, pn=prenorm, **update_metrics)


class JukeboxBottleneck(nn.Module):
    def __init__(self, codebook_dim, codebook_width, mu, levels):
        super().__init__()
        self.levels = levels
        self.level_blocks = nn.ModuleList()
        for level in range(self.levels):
            self.level_blocks.append(JukeboxBottleneckBlock(codebook_dim, codebook_width, mu))

    def encode(self, raw_audio):
        music_tokens = [
            level_block.encode(hidden_states) for (level_block, hidden_states) in zip(self.level_blocks, raw_audio)
        ]
        return music_tokens

    def decode(self, music_tokens, start_level=0, end_level=None):
        if end_level is None:
            end_level = self.levels
        quantised_audio = [
            level_block.decode(z) for (level_block, z) in zip(self.level_blocks[start_level:end_level], music_tokens)
        ]
        return quantised_audio

    def forward(self, input_audio):
        music_tokens, quantised_states, commit_losses, metrics = [], [], [], []
        for level in range(self.levels):
            level_block = self.level_blocks[-level - 1]
            hidden_states = input_audio[level]
            sampled_tokens, quantised_state, commit_loss, metric = level_block(
                hidden_states, update_codebook=self.training
            )
            music_tokens.append(sampled_tokens)
            if not self.training:
                # Be extra paranoid and make sure the encoder weights can't
                # change from straight-through estimator
                quantised_state = quantised_state.detach()
            quantised_states.append(quantised_state)
            commit_losses.append(commit_loss)
            if self.training:
                metrics.append(metric)
        return music_tokens, quantised_states, commit_losses, metrics


class JukeboxVQVAE(PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        if not config.sample_length:
            downsamples = calculate_strides(config.vqvae_strides_t, config.vqvae_downs_t)
            top_raw_to_tokens = np.prod(downsamples)
            config.sample_length = (
                (config.sample_length_in_seconds * config.sampling_rate // top_raw_to_tokens) * top_raw_to_tokens
            ).astype(int)

        input_shape = (config.sample_length, 1)
        block_kwargs = dict(
            width=config.vqvae_conv_block_width,
            depth=config.vqvae_conv_block_depth,
            m_conv=config.vqvae_m_conv,
            dilation_growth_rate=config.vqvae_dilation_growth_rate,
            dilation_cycle=config.vqvae_dilation_cycle,
            reverse_decoder_dilation=config.vqvae_reverse_decoder_dilation,
        )

        multipliers = config.vqvae_multipliers
        codebook_width = config.vqvae_emmbedding_width
        self.width = config.vqvae_width
        self.depth = config.vqvae_depth

        self.downs_t = downs_t = config.vqvae_downs_t
        self.strides_t = strides_t = config.vqvae_strides_t
        self.codebook_dim = codebook_dim = config.vqvae_codebook_dimension
        self.commit = config.vqvae_commit

        self.sample_length = input_shape[0]
        x_shape, x_channels = input_shape[:-1], input_shape[-1]
        self.x_shape = x_shape

        self.downsamples = calculate_strides(strides_t, downs_t)
        self.hop_lengths = np.cumprod(self.downsamples)
        self.levels = levels = config.vqvae_levels
        self.music_tokens_shapes = [(int(x_shape[0] // self.hop_lengths[-level - 1]),) for level in range(levels)]

        if multipliers is None:
            self.multipliers = [1] * levels
        else:
            self.multipliers = multipliers

        def _block_kwargs(level):
            this_block_kwargs = dict(block_kwargs)
            this_block_kwargs["width"] *= self.multipliers[level]
            this_block_kwargs["depth"] *= self.multipliers[level]
            return this_block_kwargs

        def encoder(level):
            return Encoder(
                x_channels,
                codebook_width,
                level + 1,
                downs_t[: level + 1],
                strides_t[: level + 1],
                **_block_kwargs(level),
            )

        def decoder(level):
            return Decoder(
                x_channels,
                codebook_width,
                level + 1,
                downs_t[: level + 1],
                strides_t[: level + 1],
                **_block_kwargs(level),
            )

        self.encoders = nn.ModuleList()
        self.decoders = nn.ModuleList()
        for level in range(levels):
            self.encoders.append(encoder(level))
            self.decoders.append(decoder(level))

        self.bottleneck = JukeboxBottleneck(codebook_dim, codebook_width, config.vqvae_lmu, levels)

    def preprocess(self, raw_audio):
        # x: NTC [-1,1] -> NCT [-1,1]
        raw_audio = raw_audio.permute(0, 2, 1).float()
        return raw_audio

    def postprocess(self, dequantised_states):
        # x: NTC [-1,1] <- NCT [-1,1]
        dequantised_states = dequantised_states.permute(0, 2, 1)
        return dequantised_states

    def _decode(self, music_tokens, start_level=0, end_level=None):
        # Decode
        if end_level is None:
            end_level = self.levels
        latent_states = self.bottleneck.decode(music_tokens, start_level=start_level, end_level=end_level)
        # Use only lowest level
        decoder, dequantised_state = self.decoders[start_level], latent_states[0:1]
        dequantised_state = decoder(dequantised_state, all_levels=False)
        dequantised_state = self.postprocess(dequantised_state)
        return dequantised_state

    def decode(self, music_tokens, start_level=0, end_level=None, bs_chunks=1):
        token_chunks = [torch.chunk(token, bs_chunks, dim=0) for token in music_tokens]
        dequantised_states = []
        for i in range(bs_chunks):
            music_tokens_i = [chunks[i] for chunks in token_chunks]
            dequantised_state = self._decode(music_tokens_i, start_level=start_level, end_level=end_level)
            dequantised_states.append(dequantised_state)
        return torch.cat(dequantised_states, dim=0)

    def _encode(self, raw_audio, start_level=0, end_level=None):
        # Encode
        if end_level is None:
            end_level = self.levels
        input_audio = self.preprocess(raw_audio)
        latent_states = []
        for level in range(self.levels):
            encoder = self.encoders[level]
            latent_state = encoder(input_audio)
            latent_states.append(latent_state[-1])
        music_tokens = self.bottleneck.encode(latent_states)
        return music_tokens[start_level:end_level]

    def encode(self, input_audio, start_level=0, end_level=None, bs_chunks=1):
        audio_chunks = torch.chunk(input_audio, bs_chunks, dim=0)
        music_tokens_list = []
        for chunk_i in audio_chunks:
            music_tokens_i = self._encode(chunk_i, start_level=start_level, end_level=end_level)
            music_tokens_list.append(music_tokens_i)
        music_tokens = [torch.cat(music_tokens_level, dim=0) for music_tokens_level in zip(*music_tokens_list)]
        return music_tokens

    def sample(self, n_samples):
        music_tokens = [
            torch.randint(0, self.codebook_dim, size=(n_samples, *music_tokens_shape))
            for music_tokens_shape in self.music_tokens_shapes
        ]
        return self.decode(music_tokens)

    def forward(self, raw_audio):
        # Encode/Decode
        input_audio = self.preprocess(raw_audio)
        latent_states = []
        for level in range(self.levels):
            encoder = self.encoders[level]
            latent_state = encoder(input_audio)
            latent_states.append(latent_state[-1])

        _, music_tokens, commit_losses, _ = self.bottleneck(latent_states)
        dequantised_states = []
        for level in range(self.levels):
            decoder = self.decoders[level]
            dequantised_state = decoder(music_tokens[level : level + 1], all_levels=False)
            dequantised_state.append(dequantised_state)

        for level in reversed(range(self.levels)):
            dequantised_state = self.postprocess(dequantised_states[level])

        commit_loss = sum(commit_losses)
        loss = self.commit * commit_loss

        return dequantised_state, loss


# Jukebox autoregressive model and its building blocks


class JukeboxMLP(nn.Module):
    def __init__(self, width, n_state, resid_dropout=0.0, afn="gelu", zero_out=False, init_scale=1.0):
        # a single channel is always used in original code
        super().__init__()
        self.c_fc = JukeboxConv1D(width, n_state)
        self.c_proj = JukeboxConv1D(n_state, width, zero_out)
        self.act = ACT2FN[afn]
        self.dropout = nn.Dropout(resid_dropout) if resid_dropout > 0.0 else lambda x: x

    def forward(self, hidden_states):
        hidden_states = self.c_fc(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.c_proj(hidden_states)
        hidden_states = self.dropout(hidden_states)
        return hidden_states


class JukeboxLayerNorm(FusedLayerNorm):
    def __init__(self, normalized_shape, eps=1e-5, elementwise_affine=True):
        super().__init__(normalized_shape, eps=eps, elementwise_affine=elementwise_affine)
        self.width = np.prod(normalized_shape)
        self.max_numel = 65535 * self.width

    def forward(self, input):
        if input.numel() > self.max_numel:
            return F.layer_norm(input.float(), self.normalized_shape, self.weight, self.bias, self.eps).type_as(input)
        else:
            return super(JukeboxLayerNorm, self).forward(input.float()).type_as(input)


def repeat(hidden_states, n_repeat, dim):
    if dim == -1:
        dim = len(hidden_states.shape) - 1
    return (
        hidden_states.view(
            int(np.prod(hidden_states.shape[: dim + 1])), 1, int(np.prod(hidden_states.shape[dim + 1 :]))
        )
        .repeat(1, n_repeat, 1)
        .view(*hidden_states.shape[:dim], n_repeat * hidden_states.shape[dim], *hidden_states.shape[dim + 1 :])
    )


def get_mask(mask, query_length, key_value_length, blocks, spread, device, sample, sample_t):
    # returns a mask of shape 1 x 1 x query_length x key_value_length or None if masking is not needed.
    if mask is None or query_length == 1:
        return None
    offset = sample_t - query_length if sample else max(key_value_length - query_length, 0)
    if mask == "autoregressive":
        # Masked dense
        mask = torch.ones(query_length, key_value_length, device=device).tril(offset)
    elif mask == "summary":
        # Masked summary
        mask = (
            torch.nn.functional.pad(
                torch.ones(query_length, query_length, device=device)
                .tril()
                .view(query_length, blocks, query_length // blocks)[:, :-1, -key_value_length // blocks :],
                (0, 0, 1, 0),
                value=1,
            )
            .contiguous()
            .view(query_length, key_value_length)
        )
    elif mask == "prime":
        mask = torch.ones(query_length, key_value_length, device=device).tril(offset)
    return mask.view(1, 1, query_length, key_value_length)


class JukeboxAttention(nn.Module):
    # previously FactoredAttention
    def __init__(
        self,
        width,
        n_ctx,
        n_state,
        num_heads,
        attn_dropout=0.0,
        resid_dropout=0.0,
        scale=True,
        mask=False,
        zero_out=False,
        init_scale=1.0,
        checkpoint_attn=0,
        attn_func=0,
        blocks=None,
        spread=None,
        encoder_dims=None,
        prime_len=None,
    ):
        super().__init__()
        self.width = width  # should have a better name
        self.n_ctx = n_ctx  # NOTE: n_ctx could be different within operations. This is complete n_ctx
        self.n_state = n_state
        self.num_heads = num_heads
        self.scale = scale
        self.mask = mask
        if attn_func == 6:
            self.c_attn = JukeboxConv1D(width, n_state)
            self.c_enc_kv = JukeboxConv1D(width, n_state * 2)
        else:
            self.c_attn = JukeboxConv1D(width, n_state * 3)
        self.c_proj = JukeboxConv1D(n_state, width, zero_out)
        self.attn_dropout = nn.Dropout(attn_dropout) if attn_dropout > 0.0 else lambda x: x
        self.resid_dropout = nn.Dropout(resid_dropout) if resid_dropout > 0.0 else lambda x: x

        # Sequence of length seq_len is factored as [blocks, seq_len // blocks]
        self.attn_func = attn_func
        self.qkv, self.attn, self.attn_mask = {
            0: (self.factored_qkv, self.dense_attn, "autoregressive"),  # Attend to all positions
            1: (self.factored_qkv, self.block_attn, "autoregressive"),  # Attend to your block
            2: (self.factored_qkv, self.transpose_block_attn, "autoregressive"),  # Attend to transpose block
            3: (self.factored_qkv, self.prev_block_attn, None),  # Attend to previous block
            4: (self.factored_qkv, self.summary_attn, "summary"),  # Attend to last position of each block
            5: (self.factored_qkv, self.summary_spread_attn, "summary"),
            6: (self.decode_qkv, self.decode_attn, None),
            7: (self.prime_qkv, self.prime_attn, "prime"),
        }[
            attn_func
        ]  # Attend to last key position of each block

        self.blocks = blocks
        self.spread = spread
        if blocks is not None:
            self.block_ctx = n_ctx // blocks
        self.checkpoint_attn = checkpoint_attn  # 0: None, 1: Attn after heads split, 2: Attn

        self.sample_t = 0
        self.cache = {}
        self.encoder_dims = encoder_dims
        self.prime_len = prime_len
        self.record_attn = False
        self.w = None

    def _attn(self, query_states, key_states, value_states, sample):
        scale = 1.0 / math.sqrt(math.sqrt(self.n_state // self.num_heads))
        if self.training:
            attention_weight = torch.matmul(query_states * scale, key_states * scale)
        else:
            attention_weight = torch.matmul(query_states, key_states)
            attention_weight.mul_(scale * scale)
        attn_weight_type = attention_weight.dtype
        attention_weight = attention_weight.float()
        if self.mask:
            # Generate appropriate mask to mask out all positions before current
            # Might take up lot of memory for dense, so can cache it
            mask = get_mask(
                self.attn_mask,
                query_states.size(-2),
                key_states.size(-1),
                self.blocks,
                self.spread,
                attention_weight.device,
                sample,
                self.sample_t,
            )
            if mask is not None:
                attention_weight = attention_weight * mask + -1e9 * (1 - mask)
        attention_prob = F.softmax(attention_weight, dim=-1).type(attn_weight_type)
        if self.record_attn:
            self.attention_prob = attention_prob
            if self.attn_func == 7:
                # only keep music queries and lyrics keys/values
                self.attention_prob = self.attention_prob[:, :, self.prime_len :, : self.prime_len]
        attention_prob = self.attn_dropout(attention_prob)
        context_states = torch.matmul(attention_prob, value_states)
        return context_states

    def merge_heads(self, hidden_states):
        hidden_states = hidden_states.permute(0, 2, 1, 3).contiguous()
        new_hidden_states_shape = (*hidden_states.size()[:-2], hidden_states.size(-2) * hidden_states.size(-1))
        return hidden_states.view(*new_hidden_states_shape)  # in Tensorflow implem: fct merge_states

    def split_heads(self, hidden_states, k=False):
        new_hidden_states_shape = (
            *hidden_states.size()[:-1],
            self.num_heads,
            hidden_states.size(-1) // self.num_heads,
        )
        hidden_states = hidden_states.view(*new_hidden_states_shape)  # in Tensorflow implem: fct split_states
        if k:
            return hidden_states.permute(0, 2, 3, 1)
        else:
            return hidden_states.permute(0, 2, 1, 3)

    def dense_attn(self, query, key, value, sample):
        query = self.split_heads(query)
        key = self.split_heads(key, k=True)
        value = self.split_heads(value)
        context_states = self._attn(query, key, value, sample)
        context_states = self.merge_heads(context_states)
        return context_states

    def block_attn(self, query, key, value, sample):
        _, block_ctx = (
            self.blocks,
            self.block_ctx,
        )  # block_ctx is seq_len // blocks for complete seq_len ie seq_len = n_ctx. Sampling has less l
        batch_size, seq_len, embed_dim = value.shape  # For sample, q_l = 1, k_l = v_l = sample_t
        if sample:
            assert seq_len == self._suff_cache_len(), f"{seq_len} != {self._suff_cache_len()}"
            return self.dense_attn(query, key, value, sample).view(batch_size, 1, embed_dim)
        else:
            query_length = query.shape[1]
            query = query.view(batch_size * query_length // block_ctx, block_ctx, embed_dim)
            if query_length < seq_len:
                seq_len = query_length
                key = key[:, -seq_len:].contiguous()
                value = value[:, -seq_len:].contiguous()
            key = key.view(batch_size * seq_len // block_ctx, block_ctx, embed_dim)
            value = value.view(batch_size * seq_len // block_ctx, block_ctx, embed_dim)
            return self.dense_attn(query, key, value, sample).view(batch_size, seq_len, embed_dim)

    def transpose_block_attn(self, query, key, value, sample):
        _, block_ctx = (
            self.blocks,
            self.block_ctx,
        )  # block_ctx is seq_len // blocks for complete seq_len ie seq_len = n_ctx. Sampling has less l
        batch_size, seq_len, embed_dim = value.shape  # For sample, q_l = 1, k_l = v_l = sample_t
        if sample:
            block_l = (seq_len - 1) % block_ctx
            key = key[:, block_l::block_ctx, :]
            value = value[:, block_l::block_ctx, :]
            return self.dense_attn(query, key, value, sample).view(batch_size, 1, embed_dim)
        else:
            query_length = query.shape[1]
            query = (
                query.view(batch_size, query_length // block_ctx, block_ctx, embed_dim)
                .transpose(1, 2)
                .contiguous()
                .view(batch_size * block_ctx, query_length // block_ctx, embed_dim)
            )
            key = (
                key.view(batch_size, seq_len // block_ctx, block_ctx, embed_dim)
                .transpose(1, 2)
                .contiguous()
                .view(batch_size * block_ctx, seq_len // block_ctx, embed_dim)
            )
            value = (
                value.view(batch_size, seq_len // block_ctx, block_ctx, embed_dim)
                .transpose(1, 2)
                .contiguous()
                .view(batch_size * block_ctx, seq_len // block_ctx, embed_dim)
            )
            return (
                self.dense_attn(query, key, value, sample)
                .view(batch_size, block_ctx, query_length // block_ctx, embed_dim)
                .transpose(1, 2)
                .contiguous()
                .view(batch_size, query_length, embed_dim)
            )

    def prev_block_attn(self, query, key, value, sample):
        _, block_ctx = (
            self.blocks,
            self.block_ctx,
        )  # block_ctx is seq_len // blocks for complete seq_len ie seq_len = n_ctx. Sampling has less l
        batch_size, seq_len, embed_dim = value.shape  # For sample, q_l = 1, k_l = v_l = sample_t
        if sample:
            assert seq_len == self._suff_cache_len(), f"{seq_len} != {self._suff_cache_len()}"
            block = (seq_len - 1) // block_ctx
            prev_l = (block - 1) * block_ctx
            if block > 0:
                assert prev_l == 0
                key = key[:, prev_l : prev_l + block_ctx, :]
                value = value[:, prev_l : prev_l + block_ctx, :]
            else:
                key = torch.zeros(batch_size, block_ctx, embed_dim, device=query.device, dtype=query.dtype)
                value = torch.zeros(batch_size, block_ctx, embed_dim, device=query.device, dtype=query.dtype)
            return self.dense_attn(query, key, value, sample).view(batch_size, 1, embed_dim)
        else:
            query_length = query.shape[1]
            query = query.view(batch_size * query_length // block_ctx, block_ctx, embed_dim)
            key = torch.nn.functional.pad(
                key.view(batch_size, seq_len // block_ctx, block_ctx, embed_dim)[:, :-1, :, :], (0, 0, 0, 0, 1, 0)
            ).view(batch_size * seq_len // block_ctx, block_ctx, embed_dim)
            value = torch.nn.functional.pad(
                value.view(batch_size, seq_len // block_ctx, block_ctx, embed_dim)[:, :-1, :, :], (0, 0, 0, 0, 1, 0)
            ).view(batch_size * seq_len // block_ctx, block_ctx, embed_dim)
            if query_length < seq_len:
                qb = query_length // block_ctx
                kb = seq_len // block_ctx
                seq_len = query_length
                key = (
                    key.view(batch_size, kb, block_ctx, embed_dim)[:, -qb:]
                    .contiguous()
                    .view(batch_size * qb, block_ctx, embed_dim)
                )
                value = (
                    value.view(batch_size, kb, block_ctx, embed_dim)[:, -qb:]
                    .contiguous()
                    .view(batch_size * qb, block_ctx, embed_dim)
                )
            return self.dense_attn(query, key, value, sample).view(batch_size, seq_len, embed_dim)

    def summary_attn(self, query, key, value, sample):
        blocks, block_ctx = (
            self.blocks,
            self.block_ctx,
        )  # block_ctx is seq_len // blocks for complete seq_len ie seq_len = n_ctx. Sampling has less l
        batch_size, seq_len, embed_dim = value.shape  # For sample, q_l = 1, k_l = v_l = sample_t
        if sample:
            key = torch.nn.functional.pad(key[:, block_ctx - 1 : blocks * block_ctx - 1 : block_ctx, :], (0, 0, 1, 0))
            value = torch.nn.functional.pad(
                value[:, block_ctx - 1 : blocks * block_ctx - 1 : block_ctx, :], (0, 0, 1, 0)
            )
            return self.dense_attn(query, key, value, sample).view(batch_size, 1, embed_dim)
        else:
            key = torch.nn.functional.pad(
                key.view(batch_size, blocks, seq_len // blocks, embed_dim)[:, :-1, -1, :], (0, 0, 1, 0)
            )  # batch_size, blocks, embed_dim
            value = torch.nn.functional.pad(
                value.view(batch_size, blocks, seq_len // blocks, embed_dim)[:, :-1, -1, :], (0, 0, 1, 0)
            )  # batch_size, blocks, embed_dim
            return self.dense_attn(query, key, value, sample).view(batch_size, seq_len, embed_dim)

    def summary_spread_attn(self, query, key, value, sample):
        blocks, _, spread = (
            self.blocks,
            self.block_ctx,
            self.spread,
        )  # block_ctx is seq_len // blocks for complete seq_len ie seq_len = n_ctx. Sampling has less l
        batch_size, seq_len, embed_dim = value.shape  # For sample, q_l = 1, k_l = v_l = sample_t
        if sample:
            assert False, "Not yet implemented"
            # key = torch.nn.functional.pad(k,(0,0,block_ctx,(-l)%block_ctx)).view(batch_size, -1, block_ctx, embed_dim)[:,:-1,-spread:,:].contiguous().view(batch_size, -1, embed_dim)
            # value = torch.nn.functional.pad(value,(0,0,block_ctx,(-l)%block_ctx)).view(batch_size, -1, block_ctx, embed_dim)[:,:-1,-spread:,:].contiguous().view(batch_size, -1, embed_dim)
            # return self.dense_attn(query, key, value, sample).view(batch_size, 1, embed_dim)
        else:
            key = (
                torch.nn.functional.pad(
                    key.view(batch_size, blocks, seq_len // blocks, embed_dim)[:, :-1, -spread:, :], (0, 0, 0, 0, 1, 0)
                )
                .contiguous()
                .view(batch_size, blocks * spread, embed_dim)
            )  # batch_size, blocks * spread, embed_dim
            value = (
                torch.nn.functional.pad(
                    value.view(batch_size, blocks, seq_len // blocks, embed_dim)[:, :-1, -spread:, :],
                    (0, 0, 0, 0, 1, 0),
                )
                .contiguous()
                .view(batch_size, blocks * spread, embed_dim)
            )  # batch_size, blocks * spread, embed_dim
            return self.dense_attn(query, key, value, sample).view(batch_size, seq_len, embed_dim)

    def prime_attn(self, query, key, value, sample):
        prime_len = self._prime_len
        key = key[:, :prime_len]
        value = value[:, :prime_len]
        return self.dense_attn(query, key, value, sample)

    def decode_attn(self, query, key, value, sample):
        assert (
            key.shape[1] == value.shape[1] == self.encoder_dims
        ), f"k: {key.shape}, v: {value.shape}, enc_dims: {self.encoder_dims}"
        return self.dense_attn(query, key, value, sample)

    def factored_qkv(self, hidden_states, lyric_encoder_states=None, sample=False):
        curr_ctx = hidden_states.shape[1]
        assert lyric_encoder_states is None
        query, key, value = hidden_states.chunk(3, dim=2)
        if sample:
            self.sample_t += curr_ctx
            key, value = self._append_cache(key, value)
            l_cache = self._suff_cache_len()
            if self._cache_len() > l_cache:
                self._slice_cache(-l_cache)
            if curr_ctx > 1:
                if self.attn_func != 0:
                    query = self._pad_to_block_ctx(query, query=True)
                    key = self._pad_to_block_ctx(key)
                    value = self._pad_to_block_ctx(value)
                sample = False
            else:
                key = self.cache["key"]
                value = self.cache["value"]
        return query, key, value, sample

    def prime_qkv(self, hidden_states, lyric_encoder_states=None, sample=False):
        curr_ctx = hidden_states.shape[1]
        assert lyric_encoder_states is None
        query, key, value = hidden_states.chunk(3, dim=2)
        if sample:
            if self._cache_len() < self._prime_len:
                self._append_cache(key, value)
            if self._cache_len() > self._prime_len:
                self._slice_cache(0, self._prime_len)
            key, value = self.cache["key"], self.cache["value"]
            self.sample_t += curr_ctx
        return query, key, value, sample

    def decode_qkv(self, hidden_states, lyric_encoder_states=None, sample=False):
        curr_ctx = hidden_states.shape[1]
        query = hidden_states
        if sample:
            if self.sample_t == 0:
                self.cache["key"], self.cache["value"] = self.c_enc_kv(
                    lyric_encoder_states.type_as(hidden_states)
                ).chunk(2, dim=2)
            key, value = self.cache["key"], self.cache["value"]
            self.sample_t += curr_ctx
        else:
            key, value = self.c_enc_kv(lyric_encoder_states.type_as(hidden_states)).chunk(2, dim=2)
        return query, key, value, sample

    def forward(self, hidden_states, lyric_encoder_states=None, sample=False):
        curr_ctx = hidden_states.shape[1]
        hidden_states = self.c_attn(hidden_states)
        query, key, value, sample = self.qkv(hidden_states, lyric_encoder_states=lyric_encoder_states, sample=sample)
        a = self.attn(query, key, value, sample)
        if a.shape[1] != curr_ctx:
            offset = self._offset(curr_ctx)
            a = a[:, offset : offset + curr_ctx, :].contiguous()
        a = self.c_proj(a)
        return self.resid_dropout(a)

    @property
    def _prime_len(self):
        prime_len = self.prime_len
        prime_blocks = (prime_len // self.blocks) + 1
        return prime_blocks * self.blocks

    def _offset(self, curr_ctx):
        if self.attn_func == 0:
            return 0
        return (self.sample_t - curr_ctx) % self.block_ctx

    def _pad_to_block_ctx(self, hidden_states, query=False):
        seq_len = hidden_states.shape[1]
        offset = self._offset(seq_len) if query else 0
        n_blocks = (seq_len + offset + self.block_ctx - 1) // self.block_ctx
        pad = n_blocks * self.block_ctx - seq_len - offset
        if pad == 0 and offset == 0:
            return hidden_states
        else:
            return F.pad(hidden_states, (0, 0, offset, pad))

    def _cache_len(self):
        return 0 if "key" not in self.cache else self.cache["key"].shape[1]

    def _suff_cache_len(self):
        """
        Precondition:
            key and value are appended with the current context and self.sample_t reflects the 1-indexed sample
            location in the context.
        """
        if self.attn_func == 0:
            return self.sample_t
        elif self.attn_func == 1:
            return (self.sample_t - 1) % self.block_ctx + 1
        elif self.attn_func == 2:
            return self.sample_t
        elif self.attn_func == 3:
            if self.sample_t <= self.block_ctx:
                return self.sample_t
            else:
                curr_block = (self.sample_t - 1) % self.block_ctx + 1
                prev_block = self.block_ctx
                return curr_block + prev_block
        elif self.attn_func == 6:
            return self.encoder_dims
        elif self.attn_func == 7:
            return min(self.sample_t, self._prime_len)
        else:
            raise NotImplementedError()

    def _slice_cache(self, start, end=None):
        self.cache["key"] = self.cache["key"][:, start:end]
        self.cache["value"] = self.cache["value"][:, start:end]

    def _append_cache(self, key, value):
        if "key" not in self.cache:
            self.cache["key"] = key
            self.cache["value"] = value
        else:
            old_key, old_value = key, value
            key = torch.cat([self.cache["key"], old_key], dim=1)
            value = torch.cat([self.cache["value"], old_value], dim=1)
            del self.cache["key"]
            del self.cache["value"]
            del old_key
            del old_value
            self.cache["key"] = key
            self.cache["value"] = value
        return self.cache["key"], self.cache["value"]

    def del_cache(self):
        self.sample_t = 0
        if "key" in self.cache:
            del self.cache["key"]
        if "value" in self.cache:
            del self.cache["value"]
        self.cache = {}

    def check_cache(self, n_samples, sample_t, fp16):
        assert self.sample_t == sample_t, f"{self.sample_t} != {sample_t}"
        if sample_t == 0:
            assert self.cache == {}
        else:
            dtype = {True: torch.float16, False: torch.float32}[fp16]
            l_cache = self._suff_cache_len()
            assert self.cache["key"].shape == (n_samples, l_cache, self.n_state)
            assert self.cache["value"].shape == (n_samples, l_cache, self.n_state)
            assert self.cache["key"].dtype == dtype, f"Expected {dtype}, got {self.cache['key'].dtype}"
            assert self.cache["value"].dtype == dtype, f"Expected {dtype}, got {self.cache['value'].dtype}"


class JukeboxBlock(nn.Module):
    def __init__(
        self,
        width,
        n_ctx,
        num_heads,
        attn_dropout=0.0,
        resid_dropout=0.0,
        afn="gelu",
        scale=True,
        mask=False,
        zero_out=False,
        init_scale=1.0,
        res_scale=1.0,
        m_attn=0.25,
        m_mlp=1.0,
        checkpoint_attn=0,
        checkpoint_mlp=0,
        attn_func=0,
        blocks=None,
        spread=None,
        encoder_dims=None,
        prime_len=None,
    ):
        super().__init__()
        self.attn = JukeboxAttention(
            width=width,
            n_ctx=n_ctx,
            n_state=int(m_attn * width),
            num_heads=num_heads,
            attn_dropout=attn_dropout,
            resid_dropout=resid_dropout,
            scale=scale,
            mask=mask,
            zero_out=zero_out,
            init_scale=init_scale,
            checkpoint_attn=checkpoint_attn,
            attn_func=attn_func,
            blocks=blocks,
            spread=spread,
            encoder_dims=encoder_dims,
            prime_len=prime_len,
        )

        self.layer_norm_0 = JukeboxLayerNorm(width)
        self.mlp = JukeboxMLP(
            width=width,
            n_state=int(m_mlp * width),
            resid_dropout=resid_dropout,
            afn=afn,
            zero_out=zero_out,
            init_scale=init_scale,
        )
        self.layer_norm_1 = JukeboxLayerNorm(width)
        self.res_scale = res_scale

        # TODO either support checkpointing for faster inference or get rid of this
        self.checkpoint_attn = checkpoint_attn
        self.checkpoint_mlp = checkpoint_mlp

        self.width = width
        self.attn_func = attn_func

    def forward(self, hidden_states, lyric_encoder_states, sample=False):
        residuals = hidden_states
        hidden_states = self.layer_norm_0(hidden_states)
        hidden_states = self.attn(hidden_states, lyric_encoder_states, sample)

        output_states = self.layer_norm_1(residuals + hidden_states)
        output_states = self.mlp(output_states)
        if self.res_scale == 1.0:
            output = residuals + hidden_states + output_states
        else:
            output = residuals + self.res_scale * (hidden_states + output_states)
        return output


class JukeboxTransformer(nn.Module):
    def __init__(
        self,
        width,
        n_ctx,
        num_heads,
        n_depth,
        attn_dropout=0.0,
        resid_dropout=0.0,
        afn="gelu",
        scale=True,
        mask=False,
        zero_out=False,
        init_scale=1.0,
        res_scale=False,
        m_attn=0.25,
        m_mlp=1.0,
        checkpoint_attn=0,
        checkpoint_mlp=0,
        checkpoint_res=0,
        attn_order=0,
        blocks=None,
        spread=None,
        encoder_dims=None,
        prime_len=None,
    ):
        super().__init__()
        self.width = width
        self.n_ctx = n_ctx
        self.encoder_dims = encoder_dims
        self.blocks = blocks
        if blocks is not None:
            self.block_ctx = n_ctx // blocks
        self.prime_len = prime_len
        self.num_heads = num_heads

        res_scale = 1.0 / n_depth if res_scale else 1.0

        # Orders of attn_func
        attn_func = {
            0: lambda d: 0,  # Complete dense attn
            1: lambda d: [1, 2][d % 2],  # Alternate row and column attn
            2: lambda d: [1, 2, 3][d % 3],  # Alternate row, column and previous row attn
            3: lambda d: [1, 4][d % 2],  # Alternate row and last column
            4: lambda d: [1, 5][d % 2],  # Alternate row and last k columns
            5: lambda d: [1, 4, 1, 1][d % 4],  # Alternate row, last column, row, row
            6: lambda d: [1, 2, 3, 6][d % 4],
            7: lambda d: [*[1, 2, 3] * 5, 6][d % 16],
            8: lambda d: [1, 2, 3, 1, 2, 3, 1, 2, 3, 6][d % 10],  # Used by separated_enc_dec model with lyrics
            9: lambda d: [1, 2, 3, 0][d % 4],
            10: lambda d: [*[1, 2, 3, 1, 2, 3, 1, 2, 3], *[1, 2, 3, 1, 2, 3, 1, 2, 3, 6] * 7][
                d % 79
            ],  # Used by large separated_enc_dec model with lyrics
            11: lambda d: [6, 6, 0][d % 3] if d % 16 == 15 else [1, 2, 3][d % 3],
            12: lambda d: [7, 7, 0][d % 3]
            if d % 16 == 15
            else [1, 2, 3][d % 3],  # Used by single_enc_dec model with lyrics
        }[attn_order]

        # attn_cycle = {0: 1, 1: 2, 2: 3, 3: 2, 4: 2, 5: 4, 6: 4, 7: 16, 8: 10, 9: 4, 10: 79, 11: 16, 12: 16}[attn_order]
        # assert n_depth % attn_cycle == 0, f'Depth {n_depth} not a multiple of cycle {attn_cycle} for attn_order {attn_order}'

        def attn_block(d):
            return JukeboxBlock(
                width=width,
                n_ctx=n_ctx,
                num_heads=num_heads,
                attn_dropout=attn_dropout,
                resid_dropout=resid_dropout,
                afn=afn,
                scale=scale,
                mask=mask,
                zero_out=zero_out if attn_func(d) != 6 else True,
                init_scale=init_scale,
                res_scale=res_scale,
                m_attn=m_attn,
                m_mlp=m_mlp,
                checkpoint_attn=checkpoint_attn,
                checkpoint_mlp=checkpoint_mlp,
                attn_func=attn_func(d),
                blocks=blocks,
                spread=spread,
                encoder_dims=encoder_dims,
                prime_len=prime_len,
            )

        self.checkpoint_res = checkpoint_res
        self._attn_mods = nn.ModuleList()
        for d in range(n_depth):
            self._attn_mods.append(attn_block(d))

        self.saved_attn_weights = []

    def set_record_attn(self, record_attn):
        """
        Arguments:
            record_attn (bool or set): Makes forward prop dump self-attention
                softmaxes to self.saved_attn_weights. Either a set of layer indices indicating which layers to store,
                or a boolean value indicating whether to dump all.
        """

        def _should_record_attn(layer_idx):
            if isinstance(record_attn, bool):
                return record_attn
            return layer_idx in record_attn

        for i, layer in enumerate(self._attn_mods):
            layer.attn.record_attn = _should_record_attn(i)
        if record_attn:
            assert self.saved_attn_weights == []
            for layer in self._attn_mods:
                assert layer.attn.w is None
        else:
            self.saved_attn_weights = []
            for layer in self._attn_mods:
                layer.attn.w = None

    def forward(self, hidden_states, lyric_encoder_states=None, sample=False, fp16=False, fp16_out=False):
        if fp16:
            hidden_states = hidden_states.half()

        # Blocks
        for i, attn_layer in enumerate(self._attn_mods):
            if attn_layer.attn_func == 6:  # attend to the lyrics
                hidden_states = attn_layer(hidden_states, lyric_encoder_states=lyric_encoder_states, sample=sample)
            else:
                hidden_states = attn_layer(hidden_states, lyric_encoder_states=None, sample=sample)
            if attn_layer.attn.record_attn:
                self.saved_attn_weights.append(attn_layer.attn.w)
        if not fp16_out:
            hidden_states = hidden_states.float()
        return hidden_states

    def check_cache(self, n_samples, sample_t, fp16):
        for attn_layer in self._attn_mods:
            attn_layer.attn.check_cache(n_samples, sample_t, fp16)

    def del_cache(self):
        for attn_layer in self._attn_mods:
            attn_layer.attn.del_cache()


class JukeboxPositionalEmbedding(nn.Module):
    def __init__(self, input_shape, width, init_scale=1.0, pos_init=False):
        super().__init__()
        self.input_shape = input_shape
        self.input_dims = np.prod(input_shape)
        self.pos_init = pos_init
        self.pos_emb = nn.Parameter(get_normal(self.input_dims, width, std=0.01 * init_scale))

    def forward(self):
        if self.pos_init:
            pos_emb = sum([self._pos_embs[i](self.pos[:, i]) for i in range(len(self.input_shape))])
        else:
            pos_emb = self.pos_emb
        return pos_emb


# Most important renaming has to happen here
class JukeboxConditionalAutoregressive(nn.Module):
    def __init__(
        self,
        input_shape,
        embed_dim,
        width=128,
        depth=2,
        heads=1,
        attn_dropout=0.0,
        resid_dropout=0.0,
        emb_dropout=0.0,
        mask=True,
        zero_out=False,
        init_scale=1.0,
        res_scale=False,
        pos_init=False,
        m_attn=0.25,
        m_mlp=1,
        checkpoint_res=0,
        checkpoint_attn=0,
        checkpoint_mlp=0,
        attn_order=0,
        blocks=None,
        spread=None,
        audio_conditioning=False,
        metadata_conditioning=False,
        encoder_dims=0,
        only_encode=False,
        merged_decoder=False,
        prime_len=None,
        afn="quick_gelu",
    ):
        """
        - input_shape : respective dimension of the different inputs (lyrics/music_tokens)
        - embed_dim : either equals to the dimension of the codebook, or the sum of n_vocab (lyrics) and codeboook
        dimension, if the model combines lyrics and music tokens, or simply n_vocab if the model is a seperate encoder
        for the lyric tokens.
        - encoder_dims : input dimension of the lyric encoder.
        - audio_conditioning : whether or not the prior supports conditionning on audio.
        - metadata_conditioning : whether or not the prior supports conditionning on artitst, genres, lyrics and
          timing. When
        False, the start token is random.
        - prime_len : for now len of the lyric hidden states
        """
        super().__init__()
        self.input_shape = input_shape
        self.input_dims = input_dims = np.prod(input_shape)
        self.encoder_dims = encoder_dims
        self.embed_dim = embed_dim
        self.width = width
        self.depth = depth

        self.embed_tokens = nn.Embedding(embed_dim, width)
        nn.init.normal_(self.embed_tokens.weight, std=0.02 * init_scale)
        self.embed_tokens_dropout = nn.Dropout(emb_dropout)
        self.metadata_conditioning = metadata_conditioning
        self.audio_conditioning = audio_conditioning
        if not metadata_conditioning:
            self.start_token = nn.Parameter(get_normal(1, width, std=0.01 * init_scale))

        self.pos_emb = JukeboxPositionalEmbedding(
            input_shape=input_shape, width=width, init_scale=init_scale, pos_init=pos_init
        )
        self.pos_emb_dropout = nn.Dropout(emb_dropout)

        self.transformer = JukeboxTransformer(
            width=width,
            n_ctx=input_dims,
            num_heads=heads,
            n_depth=depth,
            attn_dropout=attn_dropout,
            resid_dropout=resid_dropout,
            afn=afn,
            scale=True,
            mask=mask,
            zero_out=zero_out,
            init_scale=init_scale,
            res_scale=res_scale,
            m_attn=m_attn,
            m_mlp=m_mlp,
            checkpoint_attn=checkpoint_attn,
            checkpoint_mlp=checkpoint_mlp,
            checkpoint_res=checkpoint_res,
            attn_order=attn_order,
            blocks=blocks,
            spread=spread,
            encoder_dims=encoder_dims,
            prime_len=prime_len,
        )
        # TODO rename prime_len
        self.only_encode = only_encode
        self.prime_len = prime_len
        # TODO rename fc_pro_out to LM head an probably use HF's linking trick
        if merged_decoder:
            # Merged piped model uses this setup
            self.add_cond_after_transformer = False
            self.share_embed_tokens_fc_proj_out = False
        else:
            self.add_cond_after_transformer = True
            self.share_embed_tokens_fc_proj_out = True

        if not only_encode:
            # TODO rename fc_pro_out to LM head an probably use HF's linking trick
            self.fc_proj_out = nn.Linear(width, embed_dim, bias=False)
            if self.share_embed_tokens_fc_proj_out:
                self.fc_proj_out.weight = self.embed_tokens.weight
            self.loss = torch.nn.CrossEntropyLoss()

    def preprocess(self, tokens):
        # Input: hidden_states is NHWC and uint8. Converted to NL and long
        # Can include stuff like bitpacking, reordering here.
        N = tokens.shape[0]
        return tokens.view(N, -1).long()

    def postprocess(self, tokens, sample_tokens=None):
        # Convert back from NL and long to NHWC
        N = tokens.shape[0]
        assert (0 <= tokens).all() and (tokens < self.embed_dim).all()
        if sample_tokens is None or sample_tokens == self.input_dims:
            return tokens.view(N, *self.input_shape)
        else:
            return tokens.view(N, -1)

    def forward(
        self,
        tokens,
        audio_conditioning=None,
        metadata_conditioning=None,
        lyric_encoder_states=None,
        fp16=False,
        loss_full=False,
        encode=False,
        get_preds=False,
        get_acts=False,
        get_sep_loss=False,
    ):
        """
        - tokens : composed of both music tokens and lyrics tokens or just music tokens
        """
        # Preprocess.
        with torch.no_grad():
            tokens = self.preprocess(tokens)

        N = tokens.shape[0]
        if not self.audio_conditioning:
            audio_conditioning = torch.zeros((N, 1, self.width), device=tokens.device, dtype=torch.float)

        target = tokens  # Target
        hidden_states = self.embed_tokens(tokens)  # music_tokens embedding
        hidden_states = roll(hidden_states, 1)  # Shift by 1, and fill in start token
        if self.metadata_conditioning:
            hidden_states[:, 0] = metadata_conditioning.view(N, self.width)
        else:
            hidden_states[:, 0] = self.start_token

        hidden_states = (
            self.embed_tokens_dropout(hidden_states) + self.pos_emb_dropout(self.pos_emb()) + audio_conditioning
        )  # Pos emb and dropout

        hidden_states = self.transformer(
            hidden_states, lyric_encoder_states=lyric_encoder_states, fp16=fp16
        )  # Transformer
        if self.add_cond_after_transformer:  # Piped doesnt add x_cond
            hidden_states = hidden_states + audio_conditioning

        acts = hidden_states
        if self.only_encode:
            return hidden_states
        hidden_states = self.fc_proj_out(hidden_states)  # Predictions

        if get_sep_loss:
            # TODO rename x_prime and x_gen. Prime is related to primed sampling
            # TODO rename prime_length, prime_loss  (related au primed_sample)
            x_prime = hidden_states[:, : self.prime_len].reshape(-1, self.embed_dim)
            x_gen = hidden_states[:, self.prime_len :].reshape(-1, self.embed_dim)

            prime_loss = F.cross_entropy(x_prime, target[:, : self.prime_len].reshape(-1)) / np.log(2.0)
            gen_loss = F.cross_entropy(x_gen, target[:, self.prime_len :].reshape(-1)) / np.log(2.0)

            loss = (prime_loss, gen_loss)  # Note order! Prime is first
        else:
            loss = F.cross_entropy(hidden_states.view(-1, self.embed_dim), target.view(-1)) / np.log(2.0)  # Loss

        if get_preds:
            return loss, hidden_states
        elif get_acts:
            return loss, acts
        else:
            return loss, None

    def get_emb(self, sample_t, n_samples, tokens, audio_conditioning, metadata_conditioning):
        N, D = n_samples, self.input_dims
        if sample_t == 0:
            hidden_states = torch.empty(n_samples, 1, self.width).to(audio_conditioning.device)
            if self.metadata_conditioning:
                hidden_states[:, 0] = metadata_conditioning.view(N, self.width)
            else:
                hidden_states[:, 0] = self.start_token
        else:
            hidden_states = self.embed_tokens(tokens)
        if audio_conditioning.shape == (N, D, self.width):
            cond = audio_conditioning[:, sample_t : sample_t + 1, :]
        else:
            cond = audio_conditioning
        hidden_states = (
            hidden_states + self.pos_emb()[sample_t : sample_t + 1] + cond
        )  # Pos emb, dropout is identity at eval time
        return hidden_states, cond

    def sample(
        self,
        n_samples,
        audio_conditioning=None,
        metadata_conditioning=None,
        lyric_encoder_states=None,
        fp16=False,
        temp=1.0,
        top_k=0,
        top_p=0.0,
        get_preds=False,
        sample_tokens=None,
    ):
        if sample_tokens is None:
            sample_tokens = self.input_dims
        N, _ = n_samples, self.input_dims

        if not self.audio_conditioning:
            audio_conditioning = torch.zeros((N, 1, self.width), dtype=torch.float)

        with torch.no_grad():
            sampled_tokens, tokens = [], None
            if get_preds:
                preds = []

            for sample_t in get_range(range(0, sample_tokens)):
                hidden_states, cond = self.get_emb(
                    sample_t, n_samples, tokens, audio_conditioning, metadata_conditioning
                )
                self.transformer.check_cache(n_samples, sample_t, fp16)
                hidden_states = self.transformer(
                    hidden_states, lyric_encoder_states=lyric_encoder_states, sample=True, fp16=fp16
                )
                if self.add_cond_after_transformer:
                    hidden_states = hidden_states + cond
                hidden_states = self.fc_proj_out(hidden_states)  # Predictions
                if get_preds:
                    preds.append(hidden_states.clone())
                # Adjust logits
                hidden_states = hidden_states / temp
                hidden_states = filter_logits(hidden_states, top_k=top_k, top_p=top_p)
                tokens = torch.distributions.Categorical(
                    logits=hidden_states
                ).sample()  # Sample and replace hidden_states
                sampled_tokens.append(tokens.clone())
            del tokens
            self.transformer.del_cache()

            tokens = torch.cat(sampled_tokens, dim=1)
            if get_preds:
                preds = torch.cat(preds, dim=1)
            tokens = self.postprocess(tokens, sample_tokens)
        if get_preds:
            return tokens, preds
        else:
            return tokens

    def primed_sample(
        self,
        n_samples,
        hidden_states,
        audio_conditioning=None,
        metadata_conditioning=None,
        lyric_encoder_states=None,
        fp16=False,
        temp=1.0,
        top_k=0,
        top_p=0.0,
        get_preds=False,
        chunk_size=None,
        sample_tokens=None,
    ):
        if sample_tokens is None:
            sample_tokens = self.input_dims
        # Preprocess.
        with torch.no_grad():
            hidden_states = self.preprocess(hidden_states)

        sampled_audio = torch.split(hidden_states, 1, dim=1)
        sampled_audio = list(sampled_audio)

        N, _ = n_samples, self.input_dims

        if not self.audio_conditioning:
            audio_conditioning = torch.zeros((N, 1, self.width), dtype=torch.float).to(hidden_states.device)

        with torch.no_grad():
            if get_preds:
                preds = []

            # Fill up key/value cache for past context by runing forward pass.
            # We do so in chunks instead of doing the whole past in one forward pass to reduce max memory usage.
            if chunk_size is None:
                chunk_size = len(sampled_audio)
            # assert len(sampled_audio) % chunk_size == 0, f'expected {len(sampled_audio)} to be divisible by {chunk_size}'
            chunk_sizes = split_chunks(len(sampled_audio), chunk_size)
            x_primes = []
            start = 0
            hidden_states = None

            for current_chunk_size in get_range(chunk_sizes):
                sampled_audio_prime, conds_prime = [], []
                for sample_t in range(start, start + current_chunk_size):
                    # TODO rename x_prime, con_prime
                    x_prime, cond_prime = self.get_emb(
                        sample_t, n_samples, hidden_states, audio_conditioning, metadata_conditioning
                    )
                    hidden_states = sampled_audio[sample_t]
                    sampled_audio_prime.append(x_prime)
                    conds_prime.append(cond_prime)
                start = start + current_chunk_size

                x_prime, cond_prime = torch.cat(sampled_audio_prime, dim=1), torch.cat(conds_prime, dim=1)
                del sampled_audio_prime
                del conds_prime
                if not get_preds:
                    del cond_prime
                x_prime = self.transformer(x_prime, lyric_encoder_states=lyric_encoder_states, sample=True, fp16=fp16)

                if get_preds:
                    if self.add_cond_after_transformer:
                        x_prime = x_prime + cond_prime
                    del cond_prime
                    x_primes.append(x_prime)
                else:
                    del x_prime

            if get_preds:
                x_prime = torch.cat(x_primes, dim=1)
                x_prime = self.fc_proj_out(x_prime)  # Predictions
                preds.append(x_prime)

            empty_cache()
            self.transformer.check_cache(n_samples, len(sampled_audio), fp16)

            hidden_states = sampled_audio[-1]
            empty_cache()

            for sample_t in get_range(range(len(sampled_audio), sample_tokens)):
                hidden_states, cond = self.get_emb(
                    sample_t, n_samples, hidden_states, audio_conditioning, metadata_conditioning
                )
                self.transformer.check_cache(n_samples, sample_t, fp16)
                hidden_states = self.transformer(
                    hidden_states, lyric_encoder_states=lyric_encoder_states, sample=True, fp16=fp16
                )  # Transformer
                if self.add_cond_after_transformer:
                    hidden_states = hidden_states + cond
                hidden_states = self.fc_proj_out(hidden_states)  # Predictions
                if get_preds:
                    preds.append(hidden_states)
                # Adjust logits
                hidden_states = hidden_states / temp
                hidden_states = filter_logits(hidden_states, top_k=top_k, top_p=top_p)
                hidden_states = torch.distributions.Categorical(
                    logits=hidden_states
                ).sample()  # Sample and replace hidden_states
                assert hidden_states.shape == (n_samples, 1)
                sampled_audio.append(hidden_states.clone())

            del hidden_states
            self.transformer.del_cache()

            hidden_states = torch.cat(sampled_audio, dim=1)
            if get_preds:
                preds = torch.cat(preds, dim=1)
            hidden_states = self.postprocess(hidden_states, sample_tokens)
        if get_preds:
            return hidden_states, preds
        else:
            return hidden_states


def filter_logits(logits, top_k=0, top_p=0.0, filter_value=-float("Inf")):
    """Filter a distribution of logits using top-k and/or nucleus (top-p) filtering
    Args:
        logits: logits distribution shape (vocabulary size)
        top_k >0: keep only top key tokens with highest probability (top-k filtering).
        top_p >0.0: keep the top tokens with cumulative probability >= top_p (nucleus filtering).
    """
    # assert logits.dim() == 2  # batch size 1 for now - could be updated for more but the code would be less clear
    logits = logits.clone()
    top_k = min(top_k, logits.size(-1))  # Safety check
    assert (top_k == 0) or (top_p == 0.0)
    if top_k > 0:
        # Remove all tokens with a probability less than the last token of the top-k
        indices_to_remove = logits < torch.topk(logits, top_k, dim=-1)[0][..., -1:]
        logits[indices_to_remove] = filter_value

    if top_p > 0.0:
        sorted_logits, sorted_indices = torch.sort(logits, descending=True, dim=-1)
        cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probs > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0

        # indices_to_remove = sorted_indices[sorted_indices_to_remove]
        indices_to_remove = torch.zeros_like(logits, dtype=torch.uint8).scatter_(
            dim=-1, index=sorted_indices, src=sorted_indices_to_remove
        )
        logits[indices_to_remove] = filter_value
    return logits


def get_normal(*shape, std=0.01):
    w = torch.empty(shape)
    nn.init.normal_(w, std=std)
    return w


def roll(hidden_states, n):
    return torch.cat((hidden_states[:, -n:], hidden_states[:, :-n]), dim=1)


def split_chunks(length, chunk_size):
    n_passes = (length + chunk_size - 1) // chunk_size
    chunk_sizes = [*[chunk_size] * (n_passes - 1), (length - 1) % chunk_size + 1]
    assert sum(chunk_sizes) == length
    return chunk_sizes


# second most important renaming
class MusicTokenConditioner(nn.Module):
    """
    The MusicTokenConditioner takes music tokens as an input (coresponding to vocabularies in the VQ-VAE codebook) and
    upsamples it using a single layer of decoder convolution block (the same is used in the VQ-VAE).

    The embedding layer is different from the vaqvae's bottleneck

    """

    def __init__(
        self, input_shape, embed_dim, down_t, stride_t, out_width, init_scale, zero_out, res_scale, **block_kwargs
    ):
        super().__init__()
        self.width = out_width
        self.embed_tokens = nn.Embedding(embed_dim, out_width)
        nn.init.normal_(self.embed_tokens.weight, std=0.02 * init_scale)

        # MusicTokenConditioner, takes as input either uper level tokens, upsamples them to feed them to the next level?
        self.upsampler = DecoderConvBock(
            self.width, self.width, down_t, stride_t, **block_kwargs, zero_out=zero_out, res_scale=res_scale
        )
        self.layer_norm = JukeboxLayerNorm(self.width)

    def preprocess(self, hidden_states):
        hidden_states = hidden_states.permute(0, 2, 1)  # NTC -> NCT
        return hidden_states

    def postprocess(self, hidden_states):
        hidden_states = hidden_states.permute(0, 2, 1)  # NCT -> NTC
        return hidden_states

    def forward(self, music_tokens, raw_audio_conditionning=None):
        """
        Args :
            - music_tokens : int or long, in range(codebook_dim)
            - raw_audio_conditionning : used when prime sampling, raw audio information that conditions
            the generation
        """
        if raw_audio_conditionning is None:
            raw_audio_conditionning = 0.0
        # Embed music_tokens
        music_tokens = music_tokens.long()
        hidden_states = self.embed_tokens(music_tokens)
        hidden_states = hidden_states + raw_audio_conditionning

        # Run conditioner
        hidden_states = self.preprocess(hidden_states)
        hidden_states = self.upsampler(hidden_states)
        hidden_states = self.postprocess(hidden_states)
        hidden_states = self.layer_norm(hidden_states)
        return hidden_states


def flip(hidden_states):
    def _flip(hidden_states):
        return hidden_states.permute(0, 2, 1).contiguous()

    if isinstance(hidden_states, (list, tuple)):
        return [flip(z) for z in hidden_states]
    return _flip(hidden_states)


class SimpleEmbedding(nn.Module):
    def __init__(self, embed_dim, out_width, init_scale):
        super().__init__()
        self.embed_dim = embed_dim
        self.emb = nn.Embedding(embed_dim, out_width)

    def forward(self, y):
        return self.emb(y)


class RangeEmbedding(nn.Module):
    # Interpolating
    # Interpolate so that [pos_start, pos_end] <-> position tensor of length n_ctx
    #
    # Binning
    # For each pos in position tensor, find its bin
    # [start,end) mapped to [0,1,...,bins-1]
    # [start,end) -> [0,1) -> [0, bins) -> floor -> [0,...,bins-1]
    # NOTE: Open ended interval on right, so start <= pos < end, not <= end
    def __init__(self, n_time, embed_dim, range, out_width, init_scale, clamp=False):
        super().__init__()
        self.n_time = n_time
        self.embed_dim = embed_dim
        self.emb = nn.Embedding(embed_dim, out_width)
        nn.init.normal_(self.emb.weight, std=0.01 * init_scale)
        self.pos_min, self.pos_max = range
        self.clamp = clamp

    def forward(self, pos_start, pos_end=None):
        # Check if [pos_start,pos_end] in [pos_min, pos_max)
        assert len(pos_start.shape) == 2, f"Expected shape with 2 dims, got {pos_start.shape}"
        assert (self.pos_min <= pos_start).all() and (
            pos_start < self.pos_max
        ).all(), f"Range is [{self.pos_min},{self.pos_max}), got {pos_start}"
        pos_start = pos_start.float()
        if pos_end is not None:
            assert len(pos_end.shape) == 2, f"Expected shape with 2 dims, got {pos_end.shape}"
            if self.clamp:
                pos_end = pos_end.clamp(self.pos_min, self.pos_max)
            assert (self.pos_min <= pos_end).all() and (
                pos_end <= self.pos_max
            ).all(), f"Range is [{self.pos_min},{self.pos_max}), got {pos_end}"
            pos_end = pos_end.float()
        # Interpolate so that [pos_start, ..., pos_end] <-> position tensor of length n_ctx
        n_time = self.n_time
        if n_time != 1:
            assert pos_end is not None
            interpolation = (
                torch.arange(0, n_time, dtype=torch.float, device=pos_start.device).view(1, n_time) / n_time
            )
            position = pos_start + (pos_end - pos_start) * interpolation
        else:
            position = pos_start

        # Bin each value to bins_
        normalised_position = (position - self.pos_min) / (self.pos_max - self.pos_min)  # [0,1)
        bins_ = (
            (self.embed_dim * normalised_position).floor().long().detach()
        )  # [0,1) -> [0,1..,embed_dim) -> [0,1...,embed_dim-1]
        return self.emb(bins_)


class LabelConditioner(nn.Module):
    def __init__(
        self,
        metadata_dims,
        timing_dims,
        sampling_rate,
        min_duration,
        max_duration,
        n_time,
        out_width,
        init_scale,
        max_nb_genres,
        include_time_signal,
    ):
        super().__init__()
        self.n_time = n_time
        self.out_width = out_width
        # TODO rename bins
        bow_genre_bins, artist_bins = metadata_dims
        self.max_nb_genres = max_nb_genres
        self.bow_genre_emb = SimpleEmbedding(bow_genre_bins, out_width, init_scale)
        self.artist_emb = SimpleEmbedding(artist_bins, out_width, init_scale)
        self.include_time_signal = include_time_signal
        if self.include_time_signal:
            t_ranges = (
                (min_duration * sampling_rate, max_duration * sampling_rate),  # Total length
                (0.0, max_duration * sampling_rate),  # Absolute pos
                (0.0, 1.0),
            )  # Relative pos
            assert len(t_ranges) == 3, f"Expecting (total, absolute, relative) ranges, got {t_ranges}"
            total_length_range, absolute_pos_range, relative_pos_range = t_ranges
            self.total_length_emb = RangeEmbedding(1, timing_dims, total_length_range, out_width, init_scale)
            self.absolute_pos_emb = RangeEmbedding(n_time, timing_dims, absolute_pos_range, out_width, init_scale)
            self.relative_pos_emb = RangeEmbedding(
                n_time, timing_dims, relative_pos_range, out_width, init_scale, clamp=True
            )

    def forward(self, metadata):
        total_length, offset, length, artist, genre = (
            metadata[:, 0:1],
            metadata[:, 1:2],
            metadata[:, 2:3],
            metadata[:, 3:4],
            metadata[:, 4:],
        )
        # Start embedding of length 1
        artist_emb = self.artist_emb(artist)
        # Empty genre slots are denoted by -1. We mask these out.
        mask = (genre >= 0).float().unsqueeze(2)
        genre_emb = (self.bow_genre_emb(genre.clamp(0)) * mask).sum(dim=1, keepdim=True)
        start_emb = genre_emb + artist_emb
        # assert_shape(start_emb, (N, 1, self.out_width))

        # Pos embedding of length n_ctx
        if self.include_time_signal:
            start, end = offset, offset + length
            total_length, start, end = total_length.float(), start.float(), end.float()
            pos_emb = (
                self.total_length_emb(total_length)
                + self.absolute_pos_emb(start, end)
                + self.relative_pos_emb(start / total_length, end / total_length)
            )
        else:
            pos_emb = None
        return start_emb, pos_emb


class JukeboxPrior(nn.Module):
    """
    Model the prior on vq codes conditioned on timing, artist, genre, lyrics and codes from levels above. To condition
    on the timing, genre and artist, we use the LabelConditioner class To condition on the codes from the level above,
    we use the MusicTokenConditioner class To condition on lyrics, we allow two types of priors:
    - Separate Encoder Decoder: This is the usual encoder-decoder style transformer. The encoder transformer
      autoregressively
    models the lyrics, and we use its last layer to produce keys/values that are attened to by the decoder transformer
    - Single Encoder Decoder: This is a simplification where we combine them into a single model. We merge the text
      vocab
    and VQ vocab into a single large vocab, and the lyric tokens and VQ tokens into a single longer sequence of tokens
    which we autoregressively model together. # TODO this explains the input bins that are different from the lower lvl
    transformers.

    Question : why are the embeddings from the vq-vae not used? Or am I crazy? In the forward it is used, but not in
    the primed sample or sample functions. If the model is not trained using these/ uses the forward differently then I
    guess it is fine but otherwise it looks strange.
    """

    def __init__(self, config, level, encoder=None, decoder=None):
        super().__init__()

        # Passing functions instead of the vqvae module to avoid getting params, only used in the
        # forward loop
        self.encoder = encoder
        self.decoder = decoder

        vqvae_music_tokens_shapes = config.vqvae_music_tokens_shapes

        def rescale(music_tokens_shape):
            return (music_tokens_shape[0] * config.prior_n_ctx[-level - 1] // vqvae_music_tokens_shapes[level][0],)

        music_tokens_shapes = [rescale(music_tokens_shape) for music_tokens_shape in vqvae_music_tokens_shapes]
        self.lyric_conditioning = config.lyric_conditioning[-level - 1]
        self.nb_relevant_lyric_tokens = config.nb_relevant_lyric_tokens[-level - 1]

        # TODO rename prime loss fraction
        self.prime_loss_fraction = config.prime_loss_fraction[-level - 1]

        self.copy_input = config.copy_input
        if self.copy_input:
            config.bins = config.prior_latent_dim

        self.music_tokens_shapes = music_tokens_shapes
        self.levels = len(self.music_tokens_shapes)

        self.music_tokens_shape = self.music_tokens_shapes[level]

        self.level = level

        self.latent_dim = config.prior_latent_dim

        prior_kwargs = dict(
            input_shape=(config.prior_n_ctx[-level - 1],),
            embed_dim=config.prior_latent_dim,
            width=config.prior_width[-level - 1],
            depth=config.prior_depth[-level - 1],
            heads=config.prior_n_heads[-level - 1],
            attn_order=config.prior_attn_order[-level - 1],
            blocks=config.prior_blocks,
            spread=config.prior_spread,
            attn_dropout=config.prior_attn_dropout,
            resid_dropout=config.prior_resid_dropout,
            emb_dropout=config.prior_emb_dropout,
            zero_out=config.prior_zero_out,
            res_scale=config.prior_res_scale,
            pos_init=config.prior_pos_init,
            init_scale=config.prior_init_scale[-level - 1],
            m_attn=config.prior_m_attn,
        )

        if config.lyric_conditioning and not config.single_enc_dec[-level - 1]:
            # TODO rename to encoder_kwargs as they are used both
            # when single and not
            lyric_enc_kwargs = dict(
                embed_dim=config.prime_n_vocab,  # previously bins
                width=config.prime_width[-level - 1],
                depth=config.prime_depth[-level - 1],
                heads=config.prime_heads,
                attn_order=config.prime_attn_order[-level - 1],
                blocks=config.prime_blocks,
                spread=config.prime_spread,
                attn_dropout=config.prime_attn_dropout,
                resid_dropout=config.prime_resid_dropout,
                emb_dropout=config.prime_emb_dropout,
                zero_out=config.prime_zero_out,
                res_scale=config.prime_res_scale,
                pos_init=config.prime_pos_init,
                init_scale=config.prime_init_scale[-level - 1],
                m_attn=config.prime_m_attn,
                m_mlp=config.prime_m_mlp,
            )
        else:
            lyric_enc_kwargs = dict(embed_dim=config.prime_n_vocab)

        audio_conditioning_kwargs = dict(
            out_width=config.prior_width[-level - 1],
            init_scale=config.prior_init_scale[-level - 1],
            width=config.cond_width[-level - 1],
            depth=config.cond_depth[-level - 1],
            m_conv=config.cond_m_conv,
            dilation_growth_rate=config.cond_dilation_growth_rate[-level - 1],
            dilation_cycle=config.cond_dilation_cycle[-level - 1],
            zero_out=config.cond_zero_out,
            res_scale=config.cond_res_scale[-level - 1],
            checkpoint_res=config.cond_c_res[-level - 1],
        )  # have to keep this else names wrong

        metadata_conditioning_kwargs = dict(
            out_width=config.prior_width[-level - 1],
            init_scale=config.prior_init_scale[-level - 1],
            metadata_dims=config.metadata_dims[-level - 1],  # rename to metadata_dims
            timing_dims=config.timing_dims,  # rename to timing_dims or timing_intervals
            sampling_rate=config.sampling_rate,
            min_duration=config.min_duration,
            max_duration=config.max_duration,
            max_nb_genres=config.max_nb_genres,
        )

        # Audio conditioning
        self.audio_conditioning = level != (self.levels - 1)
        self.cond_level = level + 1

        # metadata conditioning
        self.metadata_conditioning = config.metadata_conditioning

        self.single_enc_dec = config.single_enc_dec[-level - 1]
        # Audio conditioning : conditioning on music tokens (either from audio or from previous levels or both)
        if self.audio_conditioning:
            self.conditioner_blocks = nn.ModuleList()

            def conditioner_block(_level):
                return MusicTokenConditioner(
                    input_shape=music_tokens_shapes[_level],
                    embed_dim=config.prior_latent_dim,
                    down_t=config.cond_downs_t[_level],
                    stride_t=config.cond_strides_t[_level],
                    **audio_conditioning_kwargs,
                )

            # if dist.get_rank() == 0: print(f"Conditioning on 1 above level(s)")
            self.conditioner_blocks.append(conditioner_block(self.cond_level))

        # metadata conditioning : contioning on timing, genres, and artist
        if self.metadata_conditioning:
            self.n_time = self.music_tokens_shape[0]  # Assuming STFT=TF order and raw=T1 order, so T is first dim
            self.metadata_embedding = LabelConditioner(
                n_time=self.n_time, include_time_signal=not self.audio_conditioning, **metadata_conditioning_kwargs
            )

        if config.single_enc_dec[-level - 1]:
            # Single encoder-decoder transformer
            self.prior_shapes = [(self.nb_relevant_lyric_tokens,), prior_kwargs.pop("input_shape")]
            self.prior_embed_dim = [lyric_enc_kwargs["embed_dim"], prior_kwargs.pop("embed_dim")]
            self.prior_dims = [np.prod(shape) for shape in self.prior_shapes]
            self.prior_embed_dim_shift = np.cumsum([0, *self.prior_embed_dim])[:-1]
            self.prior_width = prior_kwargs["width"]

            # lyrics_enc_loss_dims was the prime loss dims, gen is for the generated tokens.
            # what is the shape of the lyrics loss?

            self.lyrics_enc_loss_dims, self.gen_loss_dims = self.prior_dims[0], self.prior_dims[1]
            self.total_loss_dims = self.lyrics_enc_loss_dims + self.gen_loss_dims
            self.prior = JukeboxConditionalAutoregressive(
                input_shape=(sum(self.prior_dims),),
                embed_dim=sum(self.prior_embed_dim),
                audio_conditioning=(self.audio_conditioning or self.metadata_conditioning),
                metadata_conditioning=True,
                prime_len=self.lyrics_enc_loss_dims,
                **prior_kwargs,
            )

        else:
            # Separate encoder-decoder transformer
            if self.nb_relevant_lyric_tokens != 0 and self.lyric_conditioning:
                lyric_enc_input_shape = (self.nb_relevant_lyric_tokens,)
                self.lyrics_enc_loss_dims = np.prod(lyric_enc_input_shape)
                self.lyric_acts_width, self.lyric_enc_width = lyric_enc_kwargs["width"], prior_kwargs["width"]
                self.lyric_encoder = JukeboxConditionalAutoregressive(
                    input_shape=lyric_enc_input_shape,
                    audio_conditioning=False,
                    metadata_conditioning=False,
                    only_encode=True,
                    **lyric_enc_kwargs,
                )
                self.lyric_encoder.proj_in = JukeboxConv1D(self.lyric_acts_width, self.lyric_enc_width)
                self.lyric_encoder.final_layer_norm = JukeboxLayerNorm(self.lyric_enc_width)
                self.lyric_enc_dim = lyric_enc_kwargs["embed_dim"]
                self.lyric_encoder.lm_head = nn.Linear(self.lyric_enc_width, self.lyric_enc_dim, bias=False)
                nn.init.normal_(self.lyric_encoder.lm_head.weight, std=0.02 * prior_kwargs["init_scale"])
            else:
                self.lyrics_enc_loss_dims = 0
            self.gen_loss_dims = np.prod(self.music_tokens_shape)
            self.total_loss_dims = self.lyrics_enc_loss_dims + self.gen_loss_dims

            # prior on the tokens
            self.prior = JukeboxConditionalAutoregressive(
                audio_conditioning=(self.audio_conditioning or self.metadata_conditioning),
                metadata_conditioning=self.metadata_conditioning,
                encoder_dims=self.lyrics_enc_loss_dims,
                merged_decoder=config.merged_decoder[-level - 1],
                **prior_kwargs,
            )

        self.n_ctx = self.gen_loss_dims
        self.downsamples = calculate_strides(config.cond_strides_t, config.cond_downs_t)
        self.cond_downsample = self.downsamples[level + 1] if level != self.levels - 1 else None
        self.raw_to_tokens = np.prod(self.downsamples[: level + 1])
        self.sample_length = self.n_ctx * self.raw_to_tokens

        print(
            f"Level:{level}, Cond downsample:{self.cond_downsample}, Raw to tokens:{self.raw_to_tokens}, Sample"
            f" length:{self.sample_length}"
        )

    def get_metadata(self, labels, start, total_length, offset, get_indices=False):
        metadata = labels.clone()
        metadata[:, 0] = total_length
        # Set sample_length to match this level
        metadata[:, 2] = int(self.sample_length)

        # Set offset
        metadata[:, 1:2] = int(offset * self.raw_to_tokens) + int(start * self.raw_to_tokens)
        # here since metadata has the full token_list, ze just need to selected the ones that are relevant

        # Set lyric tokens
        metadata, indices = self.set_metadata_lyric_tokens(metadata)
        if get_indices:
            return metadata, indices
        else:
            return metadata

    def set_metadata_lyric_tokens(self, labels):
        """
        Processes the full labels to only retreive the relevant lyric tokens and keep the metadata conditioning tokens.
        """
        if self.nb_relevant_lyric_tokens > 0:
            tokens_list = torch.zeros(
                (labels.shape[0], self.nb_relevant_lyric_tokens), dtype=torch.long, device=labels.device
            )
            indices_list = []  # whats the index of each current character in original array
            for idx in range(labels.shape[0]):
                full_tokens = labels.clone()[:, 4 + self.metadata_embedding.max_nb_genres :]
                total_length, offset, duration = labels[idx, 0], labels[idx, 1], labels[idx, 2]
                tokens, indices = get_relevant_lyric_tokens(
                    full_tokens, self.nb_relevant_lyric_tokens, total_length, offset, duration
                )
                tokens_list[idx, :] = tokens
                indices_list.append(indices)

            return (
                torch.cat((labels[:, : 4 + self.metadata_embedding.max_nb_genres], tokens_list), dim=-1),
                indices_list,
            )
        else:
            return labels, None

    def get_music_tokens_conds(self, music_tokens, start, end):
        """
        Extracts current level's conditioning music tokens.
        """
        if self.level != self.levels - 1:
            assert start % self.cond_downsample == end % self.cond_downsample == 0
            music_tokens_cond = music_tokens[self.level + 1][
                :, start // self.cond_downsample : end // self.cond_downsample
            ]
            assert music_tokens_cond.shape[1] == self.n_ctx // self.cond_downsample
            music_tokens_conds = [music_tokens_cond]
        else:
            music_tokens_conds = None
        return music_tokens_conds

    def prior_preprocess(self, tokens, conds):
        """
        Shifts the input tokens to account for the dictionnary merge. The prior_embed_dim_shift give by how much. the
        music tokens should be shifted by + nb_vocab.
        """
        N = tokens[0].shape[0]
        for i in range(len(tokens)):
            tokens[i] = (tokens[i] + int(self.prior_embed_dim_shift[i])).view(N, -1)

        for i in range(len(conds)):
            cond, _, dims = conds[i], self.prior_shapes[i], self.prior_dims[i]
            if cond is None:
                conds[i] = torch.zeros((N, dims, self.prior_width), dtype=torch.float, device=tokens[0].device)

        return torch.cat(tokens, dim=1), torch.cat(conds, dim=1)

    def prior_postprocess(self, tokens):
        """
        Shifts back the input tokens if the model is uses an encoder decoder architecture. As the embedding layer is
        shared, prior_embed_dim_shift shifts the music token ids by
         - nb_vocab.
        Returns : only returns the music tokens
        """
        N = tokens.shape[0]
        # dim (nb_lyric_tokens, vqvae_codebook dim = latent_dim of the model)
        dims = (self.prior_dims[0], tokens.shape[1] - self.prior_dims[0])
        tokens = list(torch.split(tokens, dims, dim=1))

        # Some of the input tokens might be shifted to take into account the voccabulary fusion
        for i in range(len(tokens)):
            shape = self.prior_shapes[i]
            _, bins_shift = int(self.prior_embed_dim[i]), int(self.prior_embed_dim_shift[i])  # bins, -> _,
            tokens[i] = (tokens[i] - bins_shift).view(N, -1, *shape[1:])
            tokens[i] = torch.clamp(
                tokens[i], min=0
            )  # If not masking loss, model may have generated lyric/midi tokens which are now shifted <0 by bin_shift

        return tokens[-1]

    def embed_tokens(self, music_tokens_conds):
        """
        Embeds the upper level music tokens and upsamples them to provide as audio conditioning.
        """
        music_tokens_conds = music_tokens_conds[: self.cond_level - self.level]
        audio_conditioning = None
        for music_tokens_cond, conditioner_block in reversed(list(zip(music_tokens_conds, self.conditioner_blocks))):
            audio_conditioning = conditioner_block(music_tokens_cond, audio_conditioning)
        return audio_conditioning

    # Used in the forward pass
    def encode(self, hidden_states, start_level=None, end_level=None, bs_chunks=1):
        """
        Encodes the hidden states (raw audio) using the VQVAE's encoder. Returns latent_states.
        """
        if start_level is None:
            start_level = self.level
        if end_level is None:
            end_level = self.levels
        # Get latents
        with torch.no_grad():
            latent_states = self.encoder(
                hidden_states, start_level=start_level, end_level=end_level, bs_chunks=bs_chunks
            )
        return latent_states

    def decode(self, music_tokens, start_level=None, end_level=None, bs_chunks=1):
        """
        Usamples the sequence of codebook vectors to a raw audio.
        """
        if start_level is None:
            start_level = self.level
        if end_level is None:
            end_level = self.levels
        with torch.no_grad():
            output = self.decoder(music_tokens, start_level=start_level, end_level=end_level, bs_chunks=bs_chunks)
        return output

    def get_cond(self, music_tokens_conds, metadata):
        """
        Converts the tokens to the input_embeddings. Splits the lyrics and the metadata. Lyric tokens can be None
        """
        if metadata is not None:
            n_labels = metadata.shape[1] - self.nb_relevant_lyric_tokens
            metadata, lyric_tokens = metadata[:, :n_labels], metadata[:, n_labels:]
        else:
            metadata, lyric_tokens = None, None
        metadata_conditioning, metadata_pos = (
            self.metadata_embedding(metadata) if self.metadata_conditioning else (None, None)
        )
        audio_conditioning = self.embed_tokens(music_tokens_conds) if self.audio_conditioning else metadata_pos
        return audio_conditioning, metadata_conditioning, lyric_tokens

    def sample(
        self,
        n_samples,
        music_tokens=None,
        music_tokens_conds=None,
        metadata=None,
        fp16=False,
        temp=1.0,
        top_k=0,
        top_p=0.0,
        chunk_size=None,
        sample_tokens=None,
    ):
        """
        Ancestral sampling a window of tokens using the provided conditioning and metadatas
        """
        no_past_context = music_tokens is None or music_tokens.shape[1] == 0
        name = {True: "Ancestral", False: "Primed"}[no_past_context]
        print(f"{name} sampling {n_samples} samples with temp={temp}, top_k={top_k}, top_p={top_p}")

        with torch.no_grad():
            # Currently audio_conditioning only uses immediately above layer
            audio_conditioning, metadata_conditioning, lyric_tokens = self.get_cond(music_tokens_conds, metadata)
            if self.single_enc_dec:
                if no_past_context:
                    music_tokens, audio_conditioning = self.prior_preprocess(
                        [lyric_tokens], [None, audio_conditioning]
                    )
                else:
                    music_tokens, audio_conditioning = self.prior_preprocess(
                        [lyric_tokens, music_tokens], [None, audio_conditioning]
                    )
                if sample_tokens is not None:
                    sample_tokens += self.nb_relevant_lyric_tokens
                tokens = self.prior.primed_sample(
                    n_samples,
                    music_tokens,
                    audio_conditioning,
                    metadata_conditioning,
                    fp16=fp16,
                    temp=temp,
                    top_k=top_k,
                    top_p=top_p,
                    chunk_size=chunk_size,
                    sample_tokens=sample_tokens,
                )
                music_tokens = self.prior_postprocess(tokens)
            else:
                lyric_encoder_states = self.get_lyric_encoder_states(lyric_tokens, fp16=fp16, sample=True)
                if no_past_context:
                    music_tokens = self.prior.sample(
                        n_samples,
                        audio_conditioning,
                        metadata_conditioning,
                        lyric_encoder_states,
                        fp16=fp16,
                        temp=temp,
                        top_k=top_k,
                        top_p=top_p,
                        sample_tokens=sample_tokens,
                    )
                else:
                    music_tokens = self.prior.primed_sample(
                        n_samples,
                        music_tokens,
                        audio_conditioning,
                        metadata_conditioning,
                        lyric_encoder_states,
                        fp16=fp16,
                        temp=temp,
                        top_k=top_k,
                        top_p=top_p,
                        chunk_size=chunk_size,
                        sample_tokens=sample_tokens,
                    )
        return music_tokens

    def get_lyric_encoder_states(self, lyric_tokens, fp16=False, sample=False):
        """
        Retreive the last hidden_states of the lyric encoder that will be attended to by the decoder. Forwards through
        the lyric encoder.
        """
        if self.nb_relevant_lyric_tokens != 0 and self.lyric_conditioning:
            if sample:
                self.lyric_encoder = self.lyric_encoder.to(lyric_tokens.device)
            lyric_acts = self.lyric_encoder(lyric_tokens, None, None, None, fp16=fp16)
            lyric_acts = self.lyric_encoder.proj_in(lyric_acts)
            lyric_encoder_states = self.lyric_encoder.final_layer_norm(lyric_acts)
            if sample:
                #self.lyric_encoder.cpu()
                if fp16:
                    lyric_encoder_states = lyric_encoder_states.half()
        else:
            lyric_encoder_states = None
        return lyric_encoder_states

    def get_lyric_enc_loss(self, lyric_encoder_states, target_lyrics):
        """
        Computes the loss for the lyric encoder, next token prediction.
        """
        if self.lyric_conditioning:
            lyric_encoder_states = lyric_encoder_states.float()
            lyric_encoder_states = self.lyric_encoder.lm_head(lyric_encoder_states)
            lyric_enc_loss = nn.functional.cross_entropy(
                lyric_encoder_states.view(-1, self.lyric_enc_dim), target_lyrics.view(-1)
            ) / np.log(2.0)
        else:
            lyric_enc_loss = torch.tensor(0.0)
        return lyric_enc_loss

    def forward_tokens(
        self, music_tokens, music_tokens_conds=[], metadata=None, fp16=False, get_preds=False, get_attn_weights=False
    ):
        """
        Applies a forward pass using the conditioning tokens. Different from the classif forward as it does not use the
        vqvae's encoding layers.

        Args:
            get_attn_weights (bool or set): Makes forward prop dump
                self-attention softmaxes to self.prior.transformer.saved_attn_weights. Either a set of layer indices
                indicating which layers to store, or a boolean value indicating whether to dump all.
        """
        if get_attn_weights:
            self.prior.transformer.set_record_attn(get_attn_weights)
        audio_conditioning, metadata_conditioning, lyric_tokens = self.get_cond(music_tokens_conds, metadata)
        if self.copy_input:
            lyric_tokens = music_tokens[:, : self.nb_relevant_lyric_tokens]

        if self.single_enc_dec:  # the preprocess returns the full tokens, shifted
            tokens, audio_conditioning = self.prior_preprocess(
                [lyric_tokens, music_tokens], [None, audio_conditioning]
            )
            (prime_loss, gen_loss), preds = self.prior(
                tokens, audio_conditioning, metadata_conditioning, fp16=fp16, get_sep_loss=True, get_preds=get_preds
            )
        else:
            lyric_encoder_states = self.get_lyric_encoder_states(lyric_tokens, fp16=fp16)
            prime_loss = self.get_lyric_enc_loss(lyric_encoder_states, lyric_tokens)
            gen_loss, preds = self.prior(
                music_tokens,
                audio_conditioning,
                metadata_conditioning,
                lyric_encoder_states,
                fp16=fp16,
                get_preds=get_preds,
            )
        loss = (self.prime_loss_fraction * prime_loss * self.lyrics_enc_loss_dims / self.total_loss_dims) + (
            gen_loss * self.gen_loss_dims / self.total_loss_dims
        )
        metrics = dict(
            bpd=gen_loss.clone().detach(), prime_loss=prime_loss.clone().detach(), gen_loss=gen_loss.clone().detach()
        )
        if get_preds:
            metrics["preds"] = preds.clone().detach()
        if get_attn_weights:
            saved_attn_weights = self.prior.transformer.saved_attn_weights
            self.prior.transformer.set_record_attn(False)
            return saved_attn_weights
        else:
            return loss, metrics

    def forward(self, hidden_states, metadata=None, fp16=False, decode=False, get_preds=False):
        batch_size = hidden_states.shape[0]
        music_tokens, *music_tokens_conds = self.encode(hidden_states, bs_chunks=batch_size)
        loss, metrics = self.forward_tokens(
            music_tokens=music_tokens,
            music_tokens_conds=music_tokens_conds,
            metadata=metadata,
            fp16=fp16,
            get_preds=get_preds,
        )
        if decode:
            dequantised_states = self.decode([music_tokens, *music_tokens_conds])
        else:
            dequantised_states = None
        return dequantised_states, loss, metrics


class JukeboxPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = JukeboxConfig
    base_model_prefix = "transformer"

    def _init_weights(self, module):
        std = self.config.init_std
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    def __init__(self, *inputs, **kwargs):
        super().__init__(*inputs, **kwargs)


JUKEBOX_START_DOCSTRING = r"""

    This model inherits from [`PreTrainedModel`]. Check the superclass documentation for the generic methods the
    library implements for all its model (such as downloading or saving, resizing the input embeddings, pruning heads
    etc.)

    This model is also a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) subclass.
    Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage
    and behavior.

    Parameters:
        config (`JukeboxConfig`): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""


def split_batch(obj, n_samples, split_size):
    n_passes = (n_samples + split_size - 1) // split_size
    if isinstance(obj, torch.Tensor):
        return torch.split(obj, split_size, dim=0)
    elif isinstance(obj, list):
        return list(zip(*[torch.split(item, split_size, dim=0) for item in obj]))
    elif obj is None:
        return [None] * n_passes
    else:
        raise TypeError("Unknown input type")


# Break total_length into hops/windows of size n_ctx separated by hop_length
def get_starts(total_length, n_ctx, hop_length):
    starts = []
    for start in range(0, total_length - n_ctx + hop_length, hop_length):
        if start + n_ctx >= total_length:
            # Last hop could be smaller, we make it n_ctx to maximise context
            start = total_length - n_ctx
        starts.append(start)
    return starts


# FIXME, consumes too much RAM so should probably be removed
def get_alignment(music_tokens, labels, prior, level, fp16, config):
    """
    Should compute the lyric to music token alignment, but for now it cannot be used.
    """
    level = level - 1  # Top level used
    n_ctx = prior.n_ctx
    tokens = music_tokens[level]
    batch_size, total_length = tokens.shape[0], tokens.shape[1]
    if total_length < n_ctx:
        padding_length = n_ctx - total_length
        tokens = torch.cat(
            [tokens, torch.zeros(batch_size, n_ctx - total_length, dtype=tokens.dtype, device=tokens.device)], dim=1
        )
        total_length = tokens.shape[1]
    else:
        padding_length = 0

    hop_length = int(config.hop_fraction[-level - 1] * prior.n_ctx)
    alignment_head, alignment_layer = config.prior_alignment_head[0], config.prior_alignment_layer[0]
    attn_layers = set([alignment_layer])
    alignment_hops = {}
    indices_hops = {}
    prior.to(tokens.device)
    empty_cache()
    for start in get_starts(total_length, n_ctx, hop_length):
        end = start + n_ctx

        # set metadata offset, sample_length and lyrics tokens
        metadata, indices_hop = prior.get_metadata(labels, start, config.sample_length, get_indices=True, offset=0)

        tokens_bs = torch.chunk(tokens, batch_size, dim=0)
        metadata_bs = torch.chunk(metadata, batch_size, dim=0)
        w_hops = []
        for tokens_i, metadata_i in zip(tokens_bs, metadata_bs):
            w_hop = prior.forward_tokens(
                tokens_i[:, start:end], [], metadata_i, fp16=fp16, get_attn_weights=attn_layers
            )
            w_hops.append(w_hop[0][:, alignment_head])
            del w_hop
        w = torch.cat(w_hops, dim=0)
        del w_hops
        alignment_hop = w.float().numpy()
        del w

        # alignment_hop has shape (bs, n_ctx, nb_relevant_lyric_tokens)
        # indices_hop is a list of len=bs, each entry of len hps.nb_relevant_lyric_tokens
        indices_hops[start] = indices_hop
        alignment_hops[start] = alignment_hop
    empty_cache()

    # Combine attn for each hop into attn for full range
    # Use indices to place them into correct place for corresponding source tokens
    alignments = []
    for item in range(batch_size):
        # Note each item has different length lyrics
        full_tokens = labels[:, 3:]
        alignment = np.zeros((total_length, len(full_tokens) + 1))
        for start in reversed(get_starts(total_length, n_ctx, hop_length)):
            end = start + n_ctx
            alignment_hop = alignment_hops[start][item]
            indices = indices_hops[start][item]

            alignment[start:end, indices] = alignment_hop
        alignment = alignment[: total_length - padding_length, :-1]  # remove token padding, and last lyric index
        alignments.append(alignment)
    return alignments


def save_wav(fname, lvl, metas, aud, sampling_rate):
    import soundfile

    aud = torch.clamp(aud, -1, 1).cpu().numpy()
    for i in list(range(aud.shape[0])):
        if metas is not None:
            # twitter prompts or inputs are in the form of a dictionnary
            artists, genres, lyrics = list(metas)[i].values()
            path = f"{fname}/lvl_{lvl}-{artists}-{genres}-{lyrics[:5]}-{i}.wav"
            soundfile.write(path, aud[i], samplerate=sampling_rate, format="wav")
        else:
            soundfile.write(f"{fname}/lvl_{lvl}-sample-{i}.wav", aud[i], samplerate=sampling_rate, format="wav")


def load_audio(file, sampling_rate, offset, duration, mono=False):
    import librosa

    # Librosa loads more filetypes than soundfile
    raw_audio, _ = librosa.load(
        file, sr=sampling_rate, mono=mono, offset=offset / sampling_rate, duration=duration / sampling_rate
    )
    if len(raw_audio.shape) == 1:
        raw_audio = raw_audio.reshape((1, -1))
    return raw_audio


def load_prompts(audio_files, duration, offset, hps):
    n_samples = len(audio_files)
    raw_audio_list = []
    for audio_file in audio_files:
        raw_audio = load_audio(
            audio_file, sampling_rate=hps.sampling_rate, duration=duration, offset=offset, mono=True
        )
        raw_audio = raw_audio.T  # CT -> TC
        raw_audio_list.append(raw_audio)
    while len(raw_audio_list) < n_samples:
        raw_audio_list.extend(raw_audio_list)
    raw_audio_list = raw_audio_list[:n_samples]
    raw_audio = torch.stack([torch.from_numpy(raw_audio) for raw_audio in raw_audio_list])
    return raw_audio


# a little bit of renaming to do here, especially regarind "z"
@add_start_docstrings(
    "The bare JUKEBOX Model from which you can sample",
    JUKEBOX_START_DOCSTRING,
)
class JukeboxModel(JukeboxPreTrainedModel):
    _no_split_modules = ["JukeboxBlock"]

    def __init__(self, config):
        super().__init__(config)
        self.vqvae = JukeboxVQVAE(config)
        config.vqvae_music_tokens_shapes = self.vqvae.music_tokens_shapes
        self.priors = nn.ModuleList([JukeboxPrior(config, level=i) for i in range(config.nb_priors)])

    # Sample a partial window of length<n_ctx with tokens_to_sample new tokens on level=level
    def sample_partial_window(self, music_tokens, labels, offset, sampling_kwargs, level, tokens_to_sample):
        prior = self.priors[level]
        sampled_tokens = music_tokens[level]
        n_ctx = prior.n_ctx
        nb_sampled_tokens = sampled_tokens.shape[1]
        if nb_sampled_tokens < n_ctx - tokens_to_sample:
            sampling_kwargs["sample_tokens"] = nb_sampled_tokens + tokens_to_sample
            start = 0
        else:
            sampling_kwargs["sample_tokens"] = n_ctx
            start = nb_sampled_tokens - n_ctx + tokens_to_sample

        return self.sample_single_window(music_tokens, labels, offset, sampling_kwargs, level, start)

    # Sample a single window of length=n_ctx at position=start on level=level
    def sample_single_window(self, music_tokens, labels, offset, sampling_kwargs, level, start):
        prior = self.priors[level]
        n_samples = music_tokens[-1].shape[0]
        n_ctx = prior.n_ctx
        end = start + n_ctx
        # get music_tokens already sampled at current level
        previous_sampled_tokens = music_tokens[level][:, start:end]

        if "sample_tokens" in sampling_kwargs:
            # Support sampling a window shorter than n_ctx
            sample_tokens = sampling_kwargs["sample_tokens"]
            if sample_tokens is None:
                sample_tokens = end - start

        else:
            sample_tokens = end - start
        conditioning_tokens, new_tokens = (
            previous_sampled_tokens.shape[1],
            sample_tokens - previous_sampled_tokens.shape[1],
        )

        print(
            f"Sampling {sample_tokens} tokens for [{start},{start+sample_tokens}]. Conditioning on"
            f" {conditioning_tokens} tokens"
        )

        if new_tokens <= 0:
            # Nothing new to sample
            return music_tokens

        # get music_tokens_conds from level above
        music_tokens_conds = prior.get_music_tokens_conds(music_tokens, start, end)
        # if there are no levels above should return None!

        # set metadata offset, sample_length and lyrics tokens
        metadata = prior.get_metadata(labels, start, self.config.sample_length, offset)

        empty_cache()
        max_batch_size = sampling_kwargs["max_batch_size"]
        del sampling_kwargs["max_batch_size"]

        music_tokens_list = split_batch(previous_sampled_tokens, n_samples, max_batch_size)
        music_tokens_conds_list = split_batch(music_tokens_conds, n_samples, max_batch_size)
        metadata_list = split_batch(metadata, n_samples, max_batch_size)
        tokens = []
        for music_tokens_i, music_tokens_conds_i, metadata_i in zip(
            music_tokens_list, music_tokens_conds_list, metadata_list
        ):
            tokens_i = prior.sample(
                n_samples=music_tokens_i.shape[0],
                music_tokens=music_tokens_i,
                music_tokens_conds=music_tokens_conds_i,
                metadata=metadata_i,
                **sampling_kwargs,
            )
            tokens.append(tokens_i)
        sampled_tokens = torch.cat(tokens, dim=0)

        sampling_kwargs["max_batch_size"] = max_batch_size

        # Update music_tokens with new sample
        music_tokens_new = sampled_tokens[:, -new_tokens:]
        music_tokens[level] = torch.cat([music_tokens[level], music_tokens_new], dim=1)
        return music_tokens

    # Sample total_length tokens at level=level with hop_length=hop_length
    def sample_level(self, music_tokens, labels, offset, sampling_kwargs, level, total_length, hop_length):
        print(f"Sampling level {level}")
        if total_length >= self.priors[level].n_ctx:
            for start in get_range(get_starts(total_length, self.priors[level].n_ctx, hop_length)):
                music_tokens = self.sample_single_window(music_tokens, labels, offset, sampling_kwargs, level, start)

        else:
            music_tokens = self.sample_partial_window(
                music_tokens, labels, offset, sampling_kwargs, level, total_length
            )
        return music_tokens

    # Sample multiple levels
    def _sample(
        self,
        music_tokens,
        labels,
        sample_levels,
        metas=None,
        chunk_size=32,
        sampling_temperature=0.98,
        lower_batch_size=16,
        max_batch_size=16,
        sample_length_in_seconds=24,
        alignments=None,
        sample_tokens=None,
        offset=0,
        save_results=True,
        sample_length=None,
    ):
        top_prior = self.priors[-1]
        total_length = (
            sample_length
            if sample_length is not None
            else (int(sample_length_in_seconds * self.config.sampling_rate) // top_prior.raw_to_tokens)
            * top_prior.raw_to_tokens
        )
        sampling_kwargs = [
            dict(
                temp=0.99,
                fp16=False,
                max_batch_size=lower_batch_size,
                chunk_size=chunk_size,
                sample_tokens=sample_tokens,
            ),
            dict(
                temp=0.99,
                fp16=False,
                max_batch_size=lower_batch_size,
                chunk_size=chunk_size,
                sample_tokens=sample_tokens,
            ),
            dict(
                temp=sampling_temperature,
                fp16=False,
                max_batch_size=max_batch_size,
                chunk_size=chunk_size,
                sample_tokens=sample_tokens,
            ),
        ]
        self.start_time = time.strftime("%Y-%m-%d-%Hh%M")
        if sample_levels is None:
            sample_levels = range(len(self.priors))
        for level in reversed(sample_levels):
            self.config.sample_length = total_length  # total length of the signal, might be bit different
            # from the actual generated length
            self.priors[level].to(music_tokens[level].device).eval()
            empty_cache()
            # Set correct total_length, hop_length, labels and sampling_kwargs for level
            total_length = self.config.sample_length // self.priors[level].raw_to_tokens
            hop_length = int(self.config.hop_fraction[-level - 1] * self.priors[level].n_ctx)

            music_tokens = self.sample_level(
                music_tokens, labels[level], offset, sampling_kwargs[level], level, total_length, hop_length
            )

            #self.priors[level].to("cpu")
            empty_cache()
            self.vqvae.to(music_tokens[level].device)
            # Decode sample
            with torch.no_grad():
                raw_audio = self.vqvae.decode(
                    music_tokens[level:], start_level=level, bs_chunks=music_tokens[level].shape[0]
                )
            #self.vqvae.to("cpu")

            if save_results:
                logdir = f"{self.start_time}/level_{level}"
                if not os.path.exists(logdir):
                    os.makedirs(logdir)
                save_wav(logdir, level, metas=metas, aud=raw_audio, sampling_rate=self.config.sampling_rate)
                if alignments is None and self.priors[-1] is not None and self.priors[-1].nb_relevant_lyric_tokens > 0:
                    empty_cache()
                    # alignments = get_alignment(
                    #     music_tokens,
                    #     labels[-1],
                    #     self.priors[-1],
                    #     level,
                    #     sampling_kwargs[-1]["fp16"],
                    #     self.config,
                    # )
                    pass  # consumes too much ram
        return music_tokens

    # Generate ancestral samples given a list of artists and genres
    def ancestral_sample(self, labels, n_samples=1, **sampling_kwargs):
        sample_levels = sampling_kwargs.pop("sample_levels", list(range(len(self.priors))))
        music_tokens = [
            torch.zeros(n_samples, 0, dtype=torch.long, device=labels[0].device) for _ in range(len(self.priors))
        ]
        music_tokens = self._sample(music_tokens, labels, sample_levels, **sampling_kwargs)
        return music_tokens

    # Continue ancestral sampling from previously saved codes
    def continue_sample(self, music_tokens, labels, **sampling_kwargs):
        sample_levels = list(range(len(self.priors)))
        music_tokens = self._sample(music_tokens, labels, sample_levels, **sampling_kwargs)
        return music_tokens

    # Upsample given already generated upper-level codes
    def upsample(self, music_tokens, labels, **sampling_kwargs):
        sample_levels = list(range(len(self.priors) - 1))
        music_tokens = self._sample(music_tokens, labels, sample_levels, **sampling_kwargs)
        return music_tokens

    # Prompt the model with raw audio input (dimension: NTC) and generate continuations
    def primed_sample(self, raw_audio, labels, **sampling_kwargs):
        sample_levels = list(range(len(self.priors)))
        self.vqvae.to(raw_audio.device)
        with torch.no_grad():
            music_tokens = self.vqvae.encode(
                raw_audio, start_level=0, end_level=len(self.priors), bs_chunks=raw_audio.shape[0]
            )
        #self.vqvae.to("cpu")
        music_tokens = self._sample(music_tokens, labels, sample_levels, **sampling_kwargs)
        return music_tokens
