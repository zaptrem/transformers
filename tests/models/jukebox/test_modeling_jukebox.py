# coding=utf-8
# Copyright 2020 The HuggingFace Team. All rights reserved.
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
import timeit
import unittest

import numpy as np

from transformers import JukeboxConfig, is_torch_available
from transformers.testing_utils import require_torch
from transformers.trainer_utils import set_seed


# from datasets import load_dataset


# from transformers.testing_utils import require_torch, slow, torch_device


if is_torch_available():
    import torch

    from transformers import JukeboxModel, JukeboxTokenizer  # ,JUKEBOX_PRETRAINED_MODEL_ARCHIVE_LIST


@require_torch
class JukeboxModelTest(unittest.TestCase):
    all_model_classes = (JukeboxModel,) if is_torch_available() else ()

    metas = dict(
        artist="Zac Brown Band",
        genres="Country",
        offset=0,
        lyrics="""I met a traveller from an antique land,
    Who said—“Two vast and trunkless legs of stone
    Stand in the desert. . . . Near them, on the sand,
    Half sunk a shattered visage lies, whose frown,
    And wrinkled lip, and sneer of cold command,
    Tell that its sculptor well those passions read
    Which yet survive, stamped on these lifeless things,
    The hand that mocked them, and the heart that fed;
    And on the pedestal, these words appear:
    My name is Ozymandias, King of Kings;
    Look on my Works, ye Mighty, and despair!
    Nothing beside remains. Round the decay
    Of that colossal Wreck, boundless and bare
    The lone and level sands stretch far away
    """,
    )
    # @slow
    def test_model(self):
        set_seed(0)

        config = JukeboxConfig(
            n_ctx=(256, 256, 256),
            width=[128, 64, 32],
            depth=[2, 2, 2],
            priors_width=[128, 64, 32],
            cond_width=[128, 128, 64],
            l_bins=128,
            vq_vae_codebook_dimension=128,
            vq_vae_emmbedding_width=128,
            sr=44100,
            attn_order=[12, 2, 2],
            n_heads=[2, 1, 1],
            t_bins=64,
            single_enc_dec=[True, False, False],
            labels=True,
            n_vocab=79,
            sample_length=44032
            # allows the use of label conditionning. Has to be
            # True if the single_enc_dec is set to true apparently
            # ntokens also have to be set to the nb of lyric tokens
        )

        model = JukeboxModel.from_pretrained("ArthurZ/jukebox-dummy").eval()
        tokenizer = JukeboxTokenizer.from_pretrained("ArthurZ/jukebox")

        # Checks

        import random

        seed = 0
        random.seed(seed)
        np.random.seed(seed)

        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)

        sample = model.priors[2].sample(1, y=torch.Tensor([[44100.0, 0, 44100.0] + 514 * [0]]).long(), chunk_size=32)

        expected_samples = torch.Tensor(
            [
                [
                    121,
                    67,
                    16,
                    111,
                    54,
                    84,
                    0,
                    0,
                    41,
                    0,
                    14,
                    0,
                    0,
                    49,
                    20,
                    12,
                    5,
                    0,
                    58,
                    83,
                    0,
                    61,
                    0,
                    29,
                    0,
                    36,
                    42,
                    62,
                    75,
                    0,
                    88,
                    51,
                    0,
                    0,
                    20,
                    110,
                    39,
                    20,
                    85,
                    0,
                    0,
                    0,
                    76,
                    0,
                    32,
                    17,
                    99,
                    0,
                    127,
                    103,
                    78,
                    0,
                    0,
                    125,
                    82,
                    0,
                    38,
                    74,
                    0,
                    41,
                    38,
                    0,
                    0,
                    127,
                    45,
                    0,
                    2,
                    99,
                    0,
                    88,
                    84,
                    86,
                    5,
                    70,
                    0,
                    0,
                    0,
                    0,
                    23,
                    0,
                    0,
                    5,
                    0,
                    0,
                    3,
                    28,
                    47,
                    1,
                    32,
                    0,
                    9,
                    98,
                    111,
                    0,
                    66,
                    0,
                    0,
                    0,
                    59,
                    48,
                    0,
                    123,
                    61,
                    37,
                    13,
                    121,
                    24,
                    122,
                    101,
                    0,
                    68,
                    13,
                    31,
                    0,
                    57,
                    0,
                    24,
                    13,
                    85,
                    0,
                    0,
                    68,
                    0,
                    105,
                    0,
                    105,
                    0,
                    50,
                    0,
                    0,
                    64,
                    0,
                    14,
                    103,
                    0,
                    0,
                    0,
                    77,
                    26,
                    33,
                    0,
                    79,
                    55,
                    57,
                    0,
                    37,
                    0,
                    0,
                    79,
                    53,
                    0,
                    111,
                    83,
                    58,
                    41,
                    70,
                    1,
                    28,
                    109,
                    56,
                    0,
                    98,
                    80,
                    0,
                    100,
                    62,
                    126,
                    0,
                    0,
                    23,
                    0,
                    0,
                    43,
                    114,
                    23,
                    44,
                    0,
                    68,
                    53,
                    0,
                    0,
                    84,
                    0,
                    0,
                    0,
                    4,
                    123,
                    0,
                    0,
                    99,
                    36,
                    78,
                    0,
                    0,
                    45,
                    16,
                    75,
                    111,
                    95,
                    62,
                    36,
                    0,
                    52,
                    92,
                    33,
                    71,
                    3,
                    0,
                    110,
                    0,
                    0,
                    0,
                    124,
                    0,
                    0,
                    0,
                    2,
                    0,
                    101,
                    125,
                    0,
                    0,
                    0,
                    3,
                    0,
                    0,
                    123,
                    0,
                    0,
                    85,
                    0,
                    99,
                    0,
                    36,
                    107,
                    77,
                    0,
                    4,
                    41,
                    73,
                    0,
                    66,
                    43,
                    19,
                    0,
                    0,
                    124,
                    0,
                    55,
                    32,
                    0,
                    0,
                    0,
                    0,
                    90,
                    96,
                ]
            ]
        )

        self.assertTrue(np.allclose(sample, expected_samples))

        with torch.no_grad():
            x = model.vqvae.decode([sample], start_level=1, end_level=2, bs_chunks=sample.shape[0])

        expected_x = torch.Tensor(
            [
                0.0595,
                0.0952,
                0.0354,
                0.1182,
                0.0312,
                0.1063,
                0.0306,
                0.1336,
                0.0369,
                0.0902,
                0.0332,
                0.1230,
                0.0322,
                0.1036,
                0.0332,
                0.1352,
                0.0382,
                0.0941,
                0.0302,
                0.1226,
                0.0313,
                0.1077,
                0.0316,
                0.1375,
                0.0392,
                0.0961,
                0.0303,
                0.1233,
                0.0342,
                0.1067,
                0.0334,
                0.1359,
                0.0404,
                0.0963,
                0.0309,
                0.1218,
                0.0319,
                0.1069,
                0.0323,
                0.1373,
                0.0398,
                0.0952,
                0.0310,
                0.1237,
                0.0348,
                0.1058,
                0.0336,
                0.1370,
                0.0410,
                0.0954,
                0.0306,
                0.1224,
                0.0331,
                0.1081,
                0.0323,
                0.1365,
                0.0410,
                0.0982,
                0.0331,
                0.1223,
                0.0368,
                0.1070,
                0.0338,
                0.1359,
                0.0416,
                0.0976,
                0.0328,
                0.1214,
                0.0346,
                0.1087,
                0.0328,
                0.1364,
                0.0393,
                0.0973,
                0.0333,
                0.1236,
                0.0361,
                0.1074,
                0.0337,
                0.1361,
                0.0409,
                0.0967,
                0.0322,
                0.1222,
                0.0342,
                0.1090,
                0.0320,
                0.1374,
                0.0398,
                0.0985,
                0.0331,
                0.1231,
                0.0362,
                0.1074,
                0.0335,
                0.1360,
                0.0410,
                0.0971,
                0.0325,
                0.1220,
            ]
        )

        first_100 = x.squeeze(-1)[0][0:100]
        self.assertTrue(torch.allclose(first_100, expected_x, atol=1e-4))

        sampling_temperature = 0.98
        lower_batch_size = 16
        max_batch_size = 16
        lower_level_chunk_size = 32
        chunk_size = 32
        sampling_kwargs = [
            dict(temp=0.99, fp16=False, max_batch_size=lower_batch_size, chunk_size=lower_level_chunk_size),
            dict(temp=0.99, fp16=False, max_batch_size=lower_batch_size, chunk_size=lower_level_chunk_size),
            dict(temp=sampling_temperature, fp16=False, max_batch_size=max_batch_size, chunk_size=chunk_size),
        ]
        config.hop_fraction = [0.125, 0.5, 0.5]
        config.n_samples = 1

        tokens = tokenizer(
            "Alan Jackson",
            "rock",
            "old town road",
            total_length=config.sample_length_in_seconds * config.sr,
            sample_length=32768,
            offset=0,
            duration=1,
        )

        inputs, _ = tokens["input_ids"], tokens["attention_masks"]

        ys = np.array([[inputs]] * 3, dtype=np.int64)
        ys = torch.stack([torch.from_numpy(y) for y in ys], dim=0).to("cpu").long()

        start = timeit.default_timer()
        zs = model.ancestral_sample(ys, sampling_kwargs, config)
        print(f"time to sample : {timeit.default_timer() - start}")
        print(zs)
        top_50_expected_zs = torch.Tensor(
            [
                33,
                90,
                94,
                17,
                88,
                88,
                31,
                65,
                127,
                112,
                26,
                58,
                107,
                5,
                89,
                53,
                80,
                48,
                98,
                68,
                1,
                33,
                80,
                80,
                126,
                2,
                53,
                8,
                16,
                45,
                35,
                64,
                75,
                10,
                16,
                11,
                65,
                39,
                85,
                17,
                112,
                44,
                68,
                63,
                16,
                127,
                35,
                90,
                51,
                27,
            ]
        )

        self.assertTrue(torch.allclose(zs[0][0][0:50], top_50_expected_zs.long(), atol=1e-4))

    def test_conditioning(self):
        pass
        # x,x_conds and y_conds should be the same  before calling the sampling
        # start and end embeding
        # expected conditioning to match

    def prepare_inputs(self,model, model_id, chunk_size =32):
        tokenizer = JukeboxTokenizer.from_pretrained(model_id)
         # create sampling parameters
        sampling_temperature = 0.98
        lower_batch_size = 16
        max_batch_size = 16
        lower_level_chunk_size = 32
        sampling_kwargs = [
            dict(temp=0.99, fp16=False, max_batch_size=lower_batch_size, chunk_size=lower_level_chunk_size, sample_tokens=10),
            dict(temp=0.99, fp16=False, max_batch_size=lower_batch_size, chunk_size=lower_level_chunk_size, sample_tokens=10),
            dict(temp=sampling_temperature, fp16=False, max_batch_size=max_batch_size, chunk_size=chunk_size, sample_tokens=10),
        ]

        sample_length_in_seconds = 24     
        top_prior = model.priors[-1]
        total_length = (int(sample_length_in_seconds*model.config.sr)//top_prior.raw_to_tokens)*top_prior.raw_to_tokens
        tokens = tokenizer(**self.metas, sample_length =  top_prior.sample_length, total_length = total_length)
        inputs, _ = tokens["input_ids"], tokens["attention_masks"]

       
        labels = [inputs.copy() for i in range(3)]
        labels[1]["y"] = labels[1]["y"][:, :9]
        labels[0]["y"] = labels[0]["y"][:, :9]

        return labels, sampling_kwargs

    # @slow 
    def test_1b_lyrics(self):
        set_seed(0)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.enabled = False

        model_id = "ArthurZ/jukebox-1b-lyrics"
        model = JukeboxModel.from_pretrained(model_id).eval() 
        
        labels, sampling_kwargs = self.prepare_inputs(model,model_id)

        zs = [torch.zeros(1, 0, dtype=torch.long).cpu() for _ in range(3)]
        zs = model._sample(zs, labels, sampling_kwargs, [2], model.config)

        # TODO generate the original outputs
        EXPECTED_OUTPUT = torch.tensor(
            [
                1489,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                1434,
                1434,
                653,
                1357,
                653,
                1434,
                1434,
                1536,
                1599,
                710,
            ]
        )
        assert torch.allclose(zs[-1][0, :30], EXPECTED_OUTPUT)

        zs[-1] = torch.cat((zs[-1], torch.zeros(1, 2048 - zs[-1].shape[-1]).cpu()), dim=-1)
        zs = model._sample(zs, labels, sampling_kwargs, [1], model.config)
        # TODO find the expected outputs
        EXPECTED_OUTPUT = torch.tensor(
            [
                1489,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                1434,
                1434,
                653,
                1357,
                653,
                1434,
                1434,
                1536,
                1599,
                710,
            ]
        )
        assert torch.allclose(zs[-2][0, :30], EXPECTED_OUTPUT)

        zs[-2] = torch.cat((zs[-2], torch.zeros(1, 4096 - zs[-2].shape[-1]).cpu()), dim=-1)
        zs = model._sample(zs, labels, sampling_kwargs, [0], model.config)
        # TODO find the expected outputs
        EXPECTED_OUTPUT = torch.tensor(
            [
                1489,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                1434,
                1434,
                653,
                1357,
                653,
                1434,
                1434,
                1536,
                1599,
                710,
            ]
        )
        assert torch.allclose(zs[0][0, :30], EXPECTED_OUTPUT)

    def test_5b_lyrics(self):        
        set_seed(0)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.enabled = False

        model_id = "ArthurZ/jukebox-5b-lyrics"
        model = JukeboxModel.from_pretrained(model_id).eval() 
        
        labels, sampling_kwargs = self.prepare_inputs(model_id, model.priors[-1].sample_length, chunk_size = 16)

        zs = [torch.zeros(1, 0, dtype=torch.long).cpu() for _ in range(len(model.priors))]
        zs = model._sample(zs, labels, sampling_kwargs, [2], model.config)
        EXPECTED_OUTPUT = torch.tensor(
            [
                1489,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                1434,
                1434,
                653,
                1357,
                653,
                1434,
                1434,
                1536,
                1599,
                710,
            ]
        )
        assert torch.allclose(zs[-1][0, :30], EXPECTED_OUTPUT)


        zs[-1] = torch.cat((zs[-1], torch.zeros(1, 2048 - zs[-1].shape[-1]).cpu()), dim=-1)
        zs = model._sample(zs, labels, sampling_kwargs, [1], model.config)
        # TODO find the expected outputs
        EXPECTED_OUTPUT = torch.tensor(
            [
                1489,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                1434,
                1434,
                653,
                1357,
                653,
                1434,
                1434,
                1536,
                1599,
                710,
            ]
        )
        assert torch.allclose(zs[-2][0, :30], EXPECTED_OUTPUT)

        zs[-2] = torch.cat((zs[-2], torch.zeros(1, 4096 - zs[-2].shape[-1]).cpu()), dim=-1)
        zs = model._sample(zs, labels, sampling_kwargs, [0], model.config)
        # TODO find the expected outputs
        EXPECTED_OUTPUT = torch.tensor(
            [
                1489,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                653,
                1434,
                1434,
                653,
                1357,
                653,
                1434,
                1434,
                1536,
                1599,
                710,
            ]
        )
        assert torch.allclose(zs[0][0, :30], EXPECTED_OUTPUT)


if __name__ == "__main__":
    tester = JukeboxModelTest()
    tester.test_1b_lyrics()
    tester.test_5b_lyrics()


