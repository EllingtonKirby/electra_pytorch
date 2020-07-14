# To add a new cell, type '# %%'
# To add a new markdown cell, type '# %% [markdown]'
# %%
from IPython.core.debugger import set_trace as bk
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import partial
import torch
from torch import nn
import wandb
import nlp
from transformers import ElectraForMaskedLM, ElectraForPreTraining, ElectraTokenizerFast, ElectraConfig
from fastai2.callback.wandb import *
from fastai2.text.all import *
from _utils.huggingface import *
from _utils.utils import *
from _utils.would_like_to_pr import LabelSmoothingCrossEntropyFlat


# %%
SIZE = 'small'
assert SIZE in ['small', 'base', 'large']
I = ['small', 'base', 'large'].index(SIZE)
CONFIG = {
  'mask_prob':[0.15, 0.15, 0.25],
  'lr':[5e-4, 2e-4, 2e-4],
  'bs':[128, 256, 2048],
  'steps':[10**6, 766*1000, 400*1000],
  'max_length':[128, 512, 512],
}
config = {k:vs[I] for k,vs in CONFIG.items()}
config.update({
  'use_fp16': True,
  'sort_sample': True,
  'smooth_label': True,
  'shuffle': True,
})
print(config)

model_config = ElectraConfig.from_pretrained(f'google/electra-{SIZE}-discriminator')
hf_tokenizer = ElectraTokenizerFast.from_pretrained(f"google/electra-{SIZE}-generator")

# %% [markdown]
# # 1. Load Data

# %%
cache_dir=Path.home()/"datasets"
cache_dir.mkdir(exist_ok=True)
if SIZE in ['small', 'base']:
  wiki_cache_dir = cache_dir/"wikipedia/20200501.en/1.0.0"
  book_cache_dir = cache_dir/"bookcorpus/plain_text/1.0.0"
  wbdl_cache_dir = cache_dir/"wikibook_dl"
  wbdl_cache_dir.mkdir(exist_ok=True)
max_length = config['max_length']


# %%
if not cache_dir.exists():
  print('create cache direcotry')
  cache_dir.mkdir(parents=True) # create all parents needed

if SIZE in ['small', 'base']:
  
  # wiki
  if (wiki_cache_dir/f"wiki_electra_{max_length}.arrow").exists():
    print('loading the electra data (wiki)')
    wiki = nlp.Dataset.from_file(str(wiki_cache_dir/f"wiki_electra_{max_length}.arrow"))
  else:
    print('load/download wiki dataset')
    wiki = nlp.load_dataset('wikipedia', '20200501.en', cache_dir=cache_dir)['train']
  
    print('load/make tokenized wiki dataset')
    wiki = HF_TokenizeTfm(wiki, cols={'text':'tokids'}, hf_tokenizer=hf_tokenizer, remove_original=True).map(cache_file_name=str(wiki_cache_dir/'wiki_tokenized.arrow'))
  
    print('creat data from wiki dataset for ELECTRA')
    wiki = ELECTRADataTransform(wiki, in_col='tokids', out_col='inpput_ids', max_length=max_length, cls_idx=hf_tokenizer.cls_token_id, sep_idx=hf_tokenizer.sep_token_id).map(cache_file_name=str(wiki_cache_dir/f"wiki_electra_{max_length}.arrow"))

  # bookcorpus
  if (book_cache_dir/f"book_electra_{max_length}.arrow").exists():
    print('loading the electra data (BookCorpus)')
    book = nlp.Dataset.from_file(str(book_cache_dir/f"book_electra_{max_length}.arrow"))
  else:
    print('load/download BookCorpus dataset')
    book = nlp.load_dataset('/home/yisiang/nlp/datasets/bookcorpus/bookcorpus.py', cache_dir=cache_dir)['train']
 
    print('load/make tokenized BookCorpus dataset')
    book = HF_TokenizeTfm(book, cols={'text':'tokids'}, hf_tokenizer=hf_tokenizer, remove_original=True).map(cache_file_name=str(book_cache_dir/'book_tokenized.arrow'))
  
    print('creat data from BookCorpus dataset for ELECTRA')
    book = ELECTRADataTransform(book, in_col='tokids', out_col='inpput_ids', max_length=max_length, cls_idx=hf_tokenizer.cls_token_id, sep_idx=hf_tokenizer.sep_token_id).map(cache_file_name=str(book_cache_dir/f"book_electra_{max_length}.arrow"))

  wb_data = HF_MergedDataset(wiki, book)
  wb_dsets = HF_Datasets({'train': wb_data}, cols=['inpput_ids'], hf_toker=hf_tokenizer)
  dls = wb_dsets.dataloaders(bs=config['bs'],pad_idx=hf_tokenizer.pad_token_id, 
                             shuffle_train=config['shuffle'], drop_last=False, 
                             srtkey_fc=None if config['sort_sample'] else False, 
                             cache_dir=Path.home()/'datasets/wikibook_dl', cache_name='dl_{split}.json')

else: # for large size
  pass

# %% [markdown]
# # 2. Masked language model objective
# %% [markdown]
# ## 2.1 MLM objective callback

# %%
"""
Modified from HuggingFace/transformers (https://github.com/huggingface/transformers/blob/0a3d0e02c5af20bfe9091038c4fd11fb79175546/src/transformers/data/data_collator.py#L102). It is
- few ms faster: intead of a[b] a on gpu b on cpu, tensors here are all in the same device
- few tens of us faster: in how we create special token mask
- doesn't require huggingface tokenizer
- cost you only 20 ms on a (128,128) tensor, so dynamic masking is cheap   
"""
# https://github.com/huggingface/transformers/blob/1789c7daf1b8013006b0aef6cb1b8f80573031c5/examples/run_language_modeling.py#L179
def mask_tokens(inputs, mask_token_index, vocab_size, special_token_indices, mlm_probability=0.15, ignore_index=-100):
  """ Prepare masked tokens inputs/labels for masked language modeling: 80% MASK, 10% random, 10% original. """
  "ignore_index in nn.CrossEntropy is default to -100, so you don't need to specify ignore_index in loss"
  
  device = inputs.device
  labels = inputs.clone()
  # We sample a few tokens in each sequence for masked-LM training (with probability mlm_probability defaults to 0.15 in Bert/RoBER
  probability_matrix = torch.full(labels.shape, mlm_probability, device=device)
  special_tokens_mask = torch.full(inputs.shape, False, dtype=torch.bool, device=device)
  for sp_id in special_token_indices:
    special_tokens_mask = special_tokens_mask | (inputs==sp_id)
  probability_matrix.masked_fill_(special_tokens_mask, value=0.0)
  mlm_mask = torch.bernoulli(probability_matrix).bool()
  labels[~mlm_mask] = ignore_index  # We only compute loss on masked tokens

  # 80% of the time, we replace masked input tokens with mask_token
  mask_token_mask = torch.bernoulli(torch.full(labels.shape, 0.8, device=device)).bool() & mlm_mask
  inputs[mask_token_mask] = mask_token_index

  # 10% of the time, we replace masked input tokens with random word
  replace_token_mask = torch.bernoulli(torch.full(labels.shape, 0.5, device=device)).bool() & mlm_mask & ~mask_token_mask
  random_words = torch.randint(vocab_size, labels.shape, dtype=torch.long, device=device)
  inputs[replace_token_mask] = random_words[replace_token_mask]

  # The rest of the time (10% of the time) we keep the masked input tokens unchanged
  return inputs, labels, ~mlm_mask

class MaskedLMCallback(Callback):
  @delegates(mask_tokens)
  def __init__(self, mask_tok_id, special_tok_ids, vocab_size, ignore_index=-100, output_ignore_mask=False, **kwargs):
    self.ignore_index = ignore_index
    self.output_ignore_mask = output_ignore_mask
    self.mask_tokens = partial(mask_tokens,
                               mask_token_index=mask_tok_id,
                               special_token_indices=special_tok_ids,
                               vocab_size=vocab_size,
                               ignore_index=-100,
                               **kwargs)

  def begin_batch(self):
    text_indices = self.xb[0]
    masked_inputs, labels, ignored = self.mask_tokens(text_indices)
    if self.output_ignore_mask:
      self.learn.xb, self.learn.yb = (masked_inputs, ignored), (labels,)
    else:
      self.learn.xb, self.learn.yb = (masked_inputs,), (labels,)

  def mask(self, tokids):
    # a function could be used w/o learner
    return self.mask_tokens(tokids)

  @delegates(TfmdDL.show_batch)
  def show_batch(self, dl, verbose=True, show_ignore_idx=None, **kwargs):
    b = dl.one_batch()
    masked_inputs, labels, ignored = self.mask_tokens(b[0])
    if show_ignore_idx:
      labels[labels==self.ignore_index] = show_ignore_idx
    if verbose: 
      print("We won't count loss from position where y is ignore index")
      print("Notice 1. Positions have label token in y will be either [Mask]/other token/orginal token in x")
      print("Notice 2. Special tokens (CLS, SEP) won't be masked.")
      print("Notice 3. Dynamic masking: every time you run gives you different results.")
    dl.show_batch(b=(masked_inputs, labels), **kwargs)


# %%
mlm_cb = MaskedLMCallback(mask_tok_id=hf_tokenizer.mask_token_id, 
                          special_tok_ids=hf_tokenizer.all_special_ids, 
                          vocab_size=hf_tokenizer.vocab_size,
                          mlm_probability=config['mask_prob'],
                          output_ignore_mask=True)

# %% [markdown]
# # 3. ELECTRA (replaced token detection objective)
# 
# see details in paper [ELECTRA: Pre-training Text Encoders as Discriminators Rather Than Generators](https://arxiv.org/abs/2003.10555)

# %%
class ELECTRAModel(nn.Module):
  
  def __init__(self, generator, discriminator, pad_idx):
    super().__init__()
    self.generator, self.discriminator = generator,discriminator
    self.pad_idx = pad_idx

  def forward(self, masked_inp_ids, ignored):
    # masked_inp_ids: (B,L)
    # ignored: (B,L)

    non_pad = masked_inp_ids != self.pad_idx
    gen_logits = self.generator(masked_inp_ids) # (B, L, vocab size)

    # tokens output by generator
    pred_toks = gen_logits.argmax(dim=-1) # (B, L)
    # use predicted token to fill 15%(mlm prob) positions
    generated = ignored * masked_inp_ids + ~ignored * pred_toks # (B,L)
    # is masked token and not equal to predicted
    is_replaced = (generated != masked_inp_ids) # (B, L)
    
    disc_logits = self.discriminator(generated) # (B, L)

    return gen_logits, disc_logits, is_replaced, non_pad

class ELECTRALoss():
  def __init__(self, pad_idx, loss_weights=(1.0, 50.0), label_smooth=False):
    self.pad_idx = pad_idx
    self.loss_weights = loss_weights
    self.gen_loss_fc = LabelSmoothingCrossEntropyFlat() if label_smooth else CrossEntropyLossFlat()
    self.disc_loss_fc = nn.BCEWithLogitsLoss()
    
  def __call__(self, pred, targ_ids):
    gen_logits, disc_logits, is_replaced = [t.to(dtype=torch.float) for t in pred[:-1]]
    non_pad = pred[-1]
    gen_loss = self.gen_loss_fc(gen_logits, targ_ids) # ignore position where targ_id==-100
    disc_logits = disc_logits.masked_select(non_pad) # 1d tensor
    is_replaced = is_replaced.masked_select(non_pad) # 1d tensor
    disc_loss = self.disc_loss_fc(disc_logits, is_replaced)
    return gen_loss * self.loss_weights[0] + disc_loss * self.loss_weights[1]

# %% [markdown]
# # 4. Learning rate schedule

# %%
def linear_warmup_and_decay(pct_now, lr_max, end_lr, decay_power, total_steps,warmup_pct=None, warmup_steps=None):
  assert warmup_pct or warmup_steps
  if warmup_steps: warmup_pct = warmup_steps/total_steps
  """
  end_lr: the end learning rate for linear decay
  warmup_pct: percentage of training steps to for linear increase
  pct_now: percentage of traning steps we have gone through, notice pct_now=0.0 when calculating lr for first batch.
  """
  """
  pct updated after_batch, but global_step (in tf) seems to update before optimizer step,
  so pct is actually (global_step -1)/total_steps 
  """
  fixed_pct_now = pct_now + 1/total_steps
  """
  According to source code of the official repository, it seems they merged two lr schedule (warmup and linear decay)
  sequentially, instead of split training into two phases for each, this might because they think when in the early
  phase of training, pct is low, and thus the decaying formula makes little difference to lr.
  """
  decayed_lr = (lr_max-end_lr) * (1-fixed_pct_now)**decay_power + end_lr # https://www.tensorflow.org/api_docs/python/tf/compat/v1/train/polynomial_decay
  warmed_lr = decayed_lr * min(1.0, fixed_pct_now / warmup_pct) # https://github.com/google-research/electra/blob/81f7e5fc98b0ad8bfd20b641aa8bc9e6ac00c8eb/model/optimization.py#L44
  return warmed_lr


# %%
lr_shedule = ParamScheduler({'lr': partial(linear_warmup_and_decay,
                                            lr_max=config['lr'],
                                            end_lr=0.0,
                                            decay_power=1,
                                            warmup_steps=10000,
                                            total_steps=config['steps'])})

# %% [markdown]
# # 5. Train

# %%
cb_dir=Path.home()/'checkpoints'
cb_dir.mkdir(exist_ok=True)

def now_time():
  now_time = datetime.now(timezone(timedelta(hours=+8)))
  name = str(now_time)[6:-13].replace(' ', '_').replace(':', '-')
  return name


# %%
electra_model = ELECTRAModel(HF_Model(ElectraForMaskedLM, model_config, hf_tokenizer,variable_sep=True), 
                             HF_Model(ElectraForPreTraining, model_config, hf_tokenizer,variable_sep=True),
                             hf_tokenizer.mask_token_id,)
electra_loss_func = ELECTRALoss(pad_idx=hf_tokenizer.pad_token_id, label_smooth=config['smooth_label']) # label smooth applied only on loss of generator

dls.to(torch.device('cuda:2'))
run_name = now_time()
print(run_name)
learn = Learner(dls, electra_model,
                loss_func=electra_loss_func,
                opt_func=partial(Adam, eps=1e-6,),
                path=str(cb_dir),
                model_dir='electra_pretrain',
                cbs=[mlm_cb,
                    RunSteps(config['steps'], [0.5, 1.0], run_name+"_{percent}"),
                    ]
                )
if config['use_fp16']: learn = learn.to_fp16()
learn.fit(9999, cbs=[lr_shedule])
learn.save(run_name)
