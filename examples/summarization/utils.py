import os
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import Dataset, Sampler
from tqdm import tqdm

from transformers import BartTokenizer
from transformers.tokenization_utils import trim_batch


def load_pt(path):
    with open(path, "rb") as f:
        return torch.load(f)


PSEUDO_ID_SUFFIX = "pseudo_target.pkl"
T5_PREFIX = "summarize: "


def lmap(f, x):
    return list(map(f, x))


def encode_file(
    tokenizer,
    data_path,
    max_length,
    pad_to_max_length=True,
    return_tensors="pt",
    overwrite_cache=False,
    prefix="",
    tok_name="",
):
    cache_path = Path(f"{data_path}_{tok_name}{max_length}.pkl")
    if not overwrite_cache and cache_path.exists():
        return torch.load(cache_path.open("rb"))
    data_path = Path(data_path)
    examples = []
    lns = lmap(str.strip, data_path.open().readlines())
    lns = [prefix + text for text in lns]
    for text in tqdm(lns, desc=f"Tokenizing {data_path.name}"):
        tokenized = tokenizer.batch_encode_plus(
            [text],  # DONT ADD SPACES
            max_length=max_length,
            pad_to_max_length=pad_to_max_length,
            add_prefix_space=True,
            return_tensors=return_tensors,
        )
        examples.append(tokenized)
    torch.save(examples, cache_path)
    return examples


class SummarizationDataset(Dataset):
    pad_token_id = 1

    @classmethod
    def from_raw_data(
        cls,
        tokenizer,
        data_dir,
        type_path="train",
        max_source_length=1024,
        max_target_length=56,
        n_obs=None,
        overwrite_cache=False,
        tgt_suffix="",
    ):
        tok_name = "T5" if not isinstance(tokenizer, BartTokenizer) else ""
        prefix = T5_PREFIX if tok_name == "T5" else ""
        source = encode_file(
            tokenizer,
            os.path.join(data_dir, type_path + ".source"),
            max_source_length,
            overwrite_cache=overwrite_cache,
            prefix=prefix,
            tok_name=tok_name,
        )
        if type_path == "train":
            tgt_path = os.path.join(data_dir, type_path + ".target" + tgt_suffix)
        else:
            tgt_path = os.path.join(data_dir, type_path + ".target")

        target = encode_file(
            tokenizer, tgt_path, max_target_length, overwrite_cache=overwrite_cache, tok_name=tok_name
        )

        return cls(
            source, target, n_obs=n_obs, max_target_length=max_target_length, max_source_length=max_source_length
        )

    def __init__(self, source, target, max_source_length=1024, max_target_length=56, n_obs=None):
        if n_obs is not None:
            source = source[:n_obs]
            target = target[:n_obs]
        super().__init__()
        self.source = source
        self.target = target
        self.max_source_length = max_source_length
        self.max_target_length = max_target_length

    def __len__(self):
        return len(self.source)

    def __getitem__(self, index):
        source_ids = self.source[index]["input_ids"].squeeze()
        target_ids = self.target[index]["input_ids"].squeeze()
        src_mask = self.source[index]["attention_mask"].squeeze()
        return {"input_ids": source_ids, "attention_mask": src_mask, "decoder_input_ids": target_ids}

    @staticmethod
    def trim_seq2seq_batch(batch, pad_token_id):
        y = trim_batch(batch["decoder_input_ids"], pad_token_id)
        source_ids, source_mask = trim_batch(batch["input_ids"], pad_token_id, attention_mask=batch["attention_mask"])
        return source_ids, source_mask, y

    def collate_fn(self, batch) -> dict:
        input_ids = torch.stack([x["input_ids"] for x in batch])
        masks = torch.stack([x["attention_mask"] for x in batch])
        target_ids = torch.stack([x["decoder_input_ids"] for x in batch])
        pad_token_id = self.pad_token_id
        y = trim_batch(target_ids, pad_token_id)
        source_ids, source_mask = trim_batch(input_ids, pad_token_id, attention_mask=masks)
        batch = {"input_ids": source_ids, "attention_mask": source_mask, "decoder_input_ids": y}
        return batch

    @property
    def src_lens(self):
        return lmap(len, self.source)

    @property
    def tgt_lens(self):
        return lmap(len, self.target)

    def make_sortish_sampler(self, batch_size):
        return SortishSampler(self.source, batch_size)


class SortishSampler(Sampler):
    "Go through the text data by order of src length with a bit of randomness. From fastai repo."

    def __init__(
        self, data_source, bs,
    ):
        self.data_source, self.bs = data_source, bs

    def key(self, i):
        return len(self.data_source[i])

    def __len__(self) -> int:
        return len(self.data_source)

    def __iter__(self):
        idxs = np.random.permutation(len(self.data_source))
        sz = self.bs * 50
        ck_idx = [idxs[i : i + sz] for i in range(0, len(idxs), sz)]
        sort_idx = np.concatenate([sorted(s, key=self.key, reverse=True) for s in ck_idx])
        sz = self.bs
        ck_idx = [sort_idx[i : i + sz] for i in range(0, len(sort_idx), sz)]
        max_ck = np.argmax([self.key(ck[0]) for ck in ck_idx])  # find the chunk with the largest key,
        ck_idx[0], ck_idx[max_ck] = ck_idx[max_ck], ck_idx[0]  # then make sure it goes first.
        sort_idx = np.concatenate(np.random.permutation(ck_idx[1:])) if len(ck_idx) > 1 else np.array([], dtype=np.int)
        sort_idx = np.concatenate((ck_idx[0], sort_idx))
        return iter(sort_idx)