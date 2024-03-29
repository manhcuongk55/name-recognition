#!/usr/bin/python
# encoding: utf-8

import collections

import Levenshtein
import numpy as np
import tensorflow as tf
import torch
import torch.nn as nn
from PIL import Image
from torchvision.transforms import ToTensor, Normalize

mean = [0.485, 0.456, 0.406]
std = [0.229, 0.224, 0.225]


class BeamSearchDecoder():
    def __init__(self, lib, corpus, chars, word_chars, beam_width=20, lm_type='Words', lm_smoothing=0.01, tfsess=None):
        word_beam_search_module = tf.load_op_library(lib)
        self.mat = tf.placeholder(tf.float32)
        corpus = open(corpus).read()
        chars = open(chars).read()
        word_chars = open(word_chars).read()

        self.beamsearch_decoder = word_beam_search_module.word_beam_search(self.mat, beam_width, lm_type, lm_smoothing,
                                                                           corpus, chars, word_chars)
        self.tfsess = tfsess or tf.Session()
        self.idx2char = dict(zip(range(0, len(chars)), chars))

    def beamsearch(self, mat):
        mat = np.concatenate((mat[:, :, 1:], mat[:, :, :1]), axis=-1)
        results = self.tfsess.run(self.beamsearch_decoder, {self.mat: mat})
        return results

    def decode(self, preds_idx):
        return [''.join([self.idx2char[idx] for idx in row if idx < len(self.idx2char)]) for row in preds_idx]


def resizePadding(img, width, height):
    desired_w, desired_h = width, height  # (width, height)
    img_w, img_h = img.size  # old_size[0] is in (width, height) format
    ratio = 1.0 * img_w / img_h
    new_w = int(desired_h * ratio)
    new_w = new_w if desired_w == None else min(desired_w, new_w)
    img = img.resize((new_w, desired_h), Image.ANTIALIAS)

    # padding image
    if desired_w != None and desired_w > new_w:
        new_img = Image.new("RGB", (desired_w, desired_h), color=255)
        new_img.paste(img, (0, 0))
        img = new_img

    img = ToTensor()(img)
    img = Normalize(mean, std)(img)

    return img


def maxWidth(sizes, height):
    ws = [int(height * (1.0 * size[0] / size[1])) for size in sizes]
    maxw = max(ws)

    return maxw


class strLabelConverter(object):
    """Convert between str and label.

    NOTE:
        Insert `blank` to the alphabet for CTC.

    Args:
        alphabet (str): set of the possible characters.
        ignore_case (bool, default=True): whether or not to ignore all of the case.
    """

    def __init__(self, alphabet, ignore_case=True):
        self._ignore_case = ignore_case
        if self._ignore_case:
            alphabet = alphabet.lower()
        self.alphabet = alphabet + '-'  # for `-1` index

        self.dict = {}
        for i, char in enumerate(alphabet):
            # NOTE: 0 is reserved for 'blank' required by wrap_ctc
            self.dict[char] = i + 1

    def encode(self, text):
        """Support batch or single str.

        Args:
            text (str or list of str): texts to convert.

        Returns:
            torch.IntTensor [length_0 + length_1 + ... length_{n - 1}]: encoded texts.
            torch.IntTensor [n]: length of each text.
        """
        if isinstance(text, str):
            text = [
                self.dict[char.lower() if self._ignore_case else char]
                for char in text
            ]
            length = [len(text)]
        elif isinstance(text, collections.Iterable):
            length = [len(s) for s in text]
            text = ''.join(text)
            text, _ = self.encode(text)

        return (torch.IntTensor(text), torch.IntTensor(length))

    def decode(self, t, length, raw=False):
        """Decode encoded texts back into strs.

        Args:
            torch.IntTensor [length_0 + length_1 + ... length_{n - 1}]: encoded texts.
            torch.IntTensor [n]: length of each text.

        Raises:
            AssertionError: when the texts and its length does not match.

        Returns:
            text (str or list of str): texts to convert.
        """
        if length.numel() == 1:
            length = length[0]
            assert t.numel() == length, "text with length: {} does not match declared length: {}".format(t.numel(),
                                                                                                         length)
            if raw:
                return ''.join([self.alphabet[i - 1] for i in t])
            else:
                char_list = []
                for i in range(length):
                    if t[i] != 0 and (not (i > 0 and t[i - 1] == t[i])):
                        char_list.append(self.alphabet[t[i] - 1])
                return ''.join(char_list)
        else:
            # batch mode
            assert t.numel() == length.sum(), "texts with length: {} does not match declared length: {}".format(
                t.numel(), length.sum())
            texts = []
            index = 0
            for i in range(length.numel()):
                l = length[i]
                texts.append(
                    self.decode(
                        t[index:index + l], torch.IntTensor([l]), raw=raw))
                index += l
            return texts


class averager(object):
    """Compute average for `torch.Variable` and `torch.Tensor`. """

    def __init__(self):
        self.reset()

    def add(self, v):
        if type(v) is list:
            self.n_count += len(v)
            self.sum += sum(v)
        else:
            self.n_count += 1
            self.sum += v

    def reset(self):
        self.n_count = 0
        self.sum = 0

    def val(self):
        res = 0
        if self.n_count != 0:
            res = self.sum / float(self.n_count)
        return res


def oneHot(v, v_length, nc):
    batchSize = v_length.size(0)
    maxLength = v_length.max()
    v_onehot = torch.FloatTensor(batchSize, maxLength, nc).fill_(0)
    acc = 0
    for i in range(batchSize):
        length = v_length[i]
        label = v[acc:acc + length].view(-1, 1).long()
        v_onehot[i, :length].scatter_(1, label, 1.0)
        acc += length
    return v_onehot


def loadData(v, data):
    v.resize_(data.size()).copy_(data)


def prettyPrint(v):
    print('Size {0}, Type: {1}'.format(str(v.size()), v.data.type()))
    print('| Max: %f | Min: %f | Mean: %f' % (v.max().data[0], v.min().data[0],
                                              v.mean().data[0]))


def assureRatio(img):
    """Ensure imgH <= imgW."""
    b, c, h, w = img.size()
    if h > w:
        main = nn.UpsamplingBilinear2d(size=(h, h), scale_factor=None)
        img = main(img)
    return img


def cer_loss_one_image(sim_pred, label):
    loss = Levenshtein.distance(sim_pred, label) * 1.0 / max(len(sim_pred), len(label))
    return loss


def cer_loss(sim_preds, labels):
    losses = []
    for i in range(len(sim_preds)):
        pred = sim_preds[i]
        text = labels[i]

        loss = cer_loss_one_image(pred, text)
        losses.append(loss)
    return losses
