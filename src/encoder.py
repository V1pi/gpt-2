"""Byte pair encoding utilities"""

import os
import json
import regex as re
from functools import lru_cache

@lru_cache()
def bytes_to_unicode():
    """
    Returns list of utf-8 byte and a corresponding list of unicode strings.
    The reversible bpe codes work on unicode strings.
    This means you need a large # of unicode characters in your vocab if you want to avoid UNKs.
    When you're at something like a 10B token dataset you end up needing around 5K for decent coverage.
    This is a signficant percentage of your normal, say, 32K bpe vocab.
    To avoid that, we want lookup tables between utf-8 bytes and unicode strings.
    And avoids mapping to whitespace/control characters the bpe code barfs on.
    """
    bs = list(range(ord("!"), ord("~")+1))+list(range(ord("¡"), ord("¬")+1))+list(range(ord("®"), ord("ÿ")+1))
    cs = bs[:]
    n = 0
    for b in range(2**8):
        if b not in bs:
            bs.append(b)
            cs.append(2**8+n)
            n += 1
    cs = [chr(n) for n in cs]
    return dict(zip(bs, cs))

def get_pairs(word):
    """Return set of symbol pairs in a word.

    Word is represented as tuple of symbols (symbols being variable-length strings).
    """
    pairs = set()
    prev_char = word[0]
    for char in word[1:]:
        pairs.add((prev_char, char))
        prev_char = char
    return pairs

class Encoder:
    def __init__(self, encoder, bpe_merges, errors='replace'):
        self.encoder = encoder
        self.decoder = {v:k for k,v in self.encoder.items()}
        self.errors = errors # how to handle errors in decoding
        self.byte_encoder = bytes_to_unicode()
        self.byte_decoder = {v:k for k, v in self.byte_encoder.items()}
        self.bpe_ranks = dict(zip(bpe_merges, range(len(bpe_merges))))
        self.cache = {}

        # Should haved added re.IGNORECASE so BPE merges can happen for capitalized versions of contractions
        self.pat = re.compile(r"""'s|'t|'re|'ve|'m|'ll|'d| ?\p{L}+| ?\p{N}+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+""")

        if '<|endoftext|>' not in self.encoder:
            raise ValueError("Could not determine <|endoftext|> token in encoder file")

    def bpe(self, token):
        if token in self.cache:
            return self.cache[token]
        word = tuple(token)
        pairs = get_pairs(word)

        if not pairs:
            return token

        while True:
            bigram = min(pairs, key = lambda pair: self.bpe_ranks.get(pair, float('inf')))
            if bigram not in self.bpe_ranks:
                break
            first, second = bigram
            new_word = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                    new_word.extend(word[i:j])
                    i = j
                except:
                    new_word.extend(word[i:])
                    break

                if word[i] == first and i < len(word)-1 and word[i+1] == second:
                    new_word.append(first+second)
                    i += 2
                else:
                    new_word.append(word[i])
                    i += 1
            new_word = tuple(new_word)
            word = new_word
            if len(word) == 1:
                break
            else:
                pairs = get_pairs(word)
        word = ' '.join(word)
        self.cache[token] = word
        while len(self.cache) > 1000:
          self.cache.popitem()
        return word

    def encode(self, text):
        parts = text.split('<|endoftext|>')
        mid = None
        bpe_tokens = []
        for part in parts:
            if mid is not None:
                bpe_tokens.append(mid)
            else:
                mid = self.encoder['<|endoftext|>']
            for token in re.findall(self.pat, part):
                token = ''.join(self.byte_encoder[b] for b in token.encode('utf-8'))
                bpe_tokens.extend(self.encoder[bpe_token] for bpe_token in self.bpe(token).split(' '))
        return bpe_tokens

    def decode(self, tokens):
        text = ''.join([self.decoder[token] for token in tokens])
        text = bytearray([self.byte_decoder[c] for c in text]).decode('utf-8', errors=self.errors)
        return text

try:
  from tokenizers import Tokenizer, models, pre_tokenizers, decoders
  from transformers import GPT2TokenizerFast
  import sys
  use_high_speed_tokenizer = True
  sys.stderr.write('Using high-speed tokenizer\n')
except:
  use_high_speed_tokenizer = False

class HighSpeedTokenizer(object):
  def __init__(self, vocab_path, bpe_merges_path):
    if vocab_path in [
        'models/117M/encoder.json',
        'models/345M/encoder.json',
        'models/774M/encoder.json',
        'models/1558M/encoder.json',
      ] and bpe_merges_path in [
        'models/117M/vocab.bpe',
        'models/345M/vocab.bpe',
        'models/774M/vocab.bpe',
        'models/1558M/vocab.bpe',
      ]:
      sys.stderr.write('Using pretrained GPT2 tokenizer\n')
      sys.stderr.flush()
      tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")
    else:
      tokenizer = Tokenizer(BPE(vocab_path, bpe_merges_path))
      # Use the byte level
      add_prefix_spaces = False # Whether to automatically prefix the sequences with a space if none found
      tokenizer.with_pre_tokenizer(pre_tokenizers.ByteLevel.new(add_prefix_spaces))
      tokenizer.with_decoder(decoders.ByteLevel.new())
      # Setup truncation if needed
      truncate = False
      max_length = 1024
      if truncate:
        stride = 0
        strategy = 'longest_first' # Can also be `only_first` or `only_second`
        tokenizer.with_truncation(max_length, stride, strategy)
      # Setup padding if needed
      padding = False
      # Whether to always pad to max_length. If this is false, we will pad to the
      # longest sequence in the batch.
      pad_to_max_length = False
      padding_side = "right" # Can also be "left"
      pad_token_id = 0
      pad_token_type_id = 0
      pad_token = "[PAD]"
      if padding:
        tokenizer.with_padding(
          max_length if pad_to_max_length else None,
          padding_side,
          pad_token_id,
          pad_token_type_id,
          pad_token
        )
    self.tokenizer = tokenizer
    with open(vocab_path) as f:
      self.encoder = json.load(f)
    if '<|endoftext|>' not in self.encoder:
      raise ValueError('Could not determine <|endoftext|> token in encoder file {!r}'.format(vocab_path))


  # GPT2Tokenizer and Tokenizer has different ways of fetching token ids
  def tokenize(self, text, encoder=None):
    if encoder is None:
      encoder = self.tokenizer
    result = encoder.encode(text)
    if isinstance(result, list):
        return result
    return result.ids


  def encode(self, text):
    tokens = []
    lines = text.splitlines()
    c = '\n'
    n = len(lines) - 1
    for i, line in enumerate(lines):
      if i >= n:
        c = ''
      parts = line.split('<|endoftext|>')
      parts[-1] += c
      mid = None
      for part in parts:
        if mid is not None:
          tokens.append(mid)
        else:
          mid = self.encoder['<|endoftext|>']
        encoding = self.tokenize(part)
        tokens.extend(encoding)
    if text.endswith('\n'):
      tokens.extend(self.tokenize('\n'))
    return tokens

  def decode(self, tokens):
    text = self.tokenizer.decode(tokens, False)
    return text

class SpaceSeparatedTokenizer(object):
  def __init__(self, vocab_path):
    with open(vocab_path) as f:
      self.encoder = json.load(f)
      self.decoder = {v: k for k, v in self.encoder.items()}

  def decode(self, tokens):
    return ' '.join([self.decoder[token] for token in tokens]).replace(' \n ', '\n')
    # sep = None
    # lines = []
    # line = []
    # for token in tokens:
    #   c = self.decoder[token]
    #   if c == '\n':
    #     lines.append(' '.join(line) + '\n')
    #     line = []
    #   else:
    #     line.append(c)
    # return ''.join(lines) + ' '.join(line)

  def encode(self, text):
    tokens = []
    for line in text.splitlines():
      for word in line.split():
        tokens.append(self.encoder[word])
      if '\n' in self.encoder:
        tokens.append(self.encoder['\n'])
    if len(tokens) > 0 and '\n' in self.encoder:
      if tokens[-1] == self.encoder['\n'] and not text.endswith('\n'):
        tokens.pop()
    return tokens

def get_encoder(model_name):
    vocab_path = os.path.join('models', model_name, 'encoder.json')
    bpe_merges_path = os.path.join('models', model_name, 'vocab.bpe')
    if not os.path.isfile(bpe_merges_path) and os.path.isfile(vocab_path):
      return SpaceSeparatedTokenizer(vocab_path)
    if use_high_speed_tokenizer:
      return HighSpeedTokenizer(vocab_path=vocab_path, bpe_merges_path=bpe_merges_path)
    with open(vocab_path, 'r') as f:
        encoder = json.load(f)
    with open(bpe_merges_path, 'r', encoding="utf-8") as f:
        bpe_data = f.read()
    bpe_merges = [tuple(merge_str.split()) for merge_str in bpe_data.split('\n')[1:-1]]
    return Encoder(
        encoder=encoder,
        bpe_merges=bpe_merges,
    )
