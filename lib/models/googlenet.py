# -*- coding: utf-8 -*-


from __future__ import print_function
from collections import OrderedDict
import os

import numpy
try:
    from PIL import Image
    available = True
except ImportError as e:
    available = False
    _import_error = e

from chainer.dataset.convert import concat_examples
from chainer.dataset import download
from chainer.functions.activation.relu import relu
from chainer.functions.activation.softmax import softmax
from chainer.functions.array.reshape import reshape
from chainer.functions.math.sum import sum
from chainer.functions.pooling.average_pooling_2d import average_pooling_2d
from chainer.functions.pooling.max_pooling_2d import max_pooling_2d
from chainer.functions.normalization.local_response_normalization import (
    local_response_normalization)
from chainer.functions.noise.dropout import dropout
from chainer.initializers import constant
from chainer.initializers import uniform
from chainer import link
from chainer.links.connection.convolution_2d import Convolution2D
from chainer.links.connection.inception import Inception
from chainer.links.connection.linear import Linear
from chainer.serializers import npz
from chainer.utils import imgproc
from chainer.variable import Variable


class GoogLeNet(link.Chain):

    """A pre-trained GoogLeNet model provided by BVLC [1].

    When you specify the path of the pre-trained chainer model serialized as
    a ``.npz`` file in the constructor, this chain model automatically
    initializes all the parameters with it.
    This model would be useful when you want to extract a semantic feature
    vector per image, or fine-tune the model on a different dataset.

    If you want to manually convert the pre-trained caffemodel to a chainer
    model that can be specified in the constructor,
    please use ``convert_caffemodel_to_npz`` classmethod instead.

    .. [1] https://github.com/BVLC/caffe/tree/master/models/bvlc_googlenet `_

    Args:
        pretrained_model (str): the destination of the pre-trained
            chainer model serialized as a ``.npz`` file.
            If this argument is specified as ``auto``,
            it automatically downloads the caffemodel from the internet.
            Note that in this case the converted chainer model is stored
            on ``$CHAINER_DATASET_ROOT/pfnet/chainer/models`` directory,
            where ``$CHAINER_DATASET_ROOT`` is set as
            ``$HOME/.chainer/dataset`` unless you specify another value
            as a environment variable. The converted chainer model is
            automatically used from the second time.
            If the argument is specified as ``None``, all the parameters
            are not initialized by the pre-trained model, but the default
            initializer used in the original paper, i.e.,
            ``chainer.initializers.GlorotUniform(scale=1.0)``.

    Attributes:
        available_layers (list of str): The list of available layer names
            used by ``__call__`` and ``extract`` methods.

    """

    image_mean = numpy.array([104, 117, 123], dtype=numpy.float32)  # BGR

    def __init__(self, pretrained_model='auto'):
        if pretrained_model:
            # As a sampling process is time-consuming,
            # we employ a zero initializer for faster computation.
            kwargs = {'initialW': constant.Zero()}
        else:
            # employ default initializers used in the original paper
            kwargs = {'initialW': uniform.GlorotUniform(scale=1.0)}
        super(GoogLeNet, self).__init__(
            conv1=Convolution2D(3,  64, 7, stride=2, pad=3, **kwargs),
            conv2_reduce=Convolution2D(64,  64, 1, **kwargs),
            conv2=Convolution2D(64, 192, 3, stride=1, pad=1, **kwargs),
            inc3a=Inception(192,  64,  96, 128, 16,  32,  32),
            inc3b=Inception(256, 128, 128, 192, 32,  96,  64),
            inc4a=Inception(480, 192,  96, 208, 16,  48,  64),
            inc4b=Inception(512, 160, 112, 224, 24,  64,  64),
            inc4c=Inception(512, 128, 128, 256, 24,  64,  64),
            inc4d=Inception(512, 112, 144, 288, 32,  64,  64),
            inc4e=Inception(528, 256, 160, 320, 32, 128, 128),
            inc5a=Inception(832, 256, 160, 320, 32, 128, 128),
            inc5b=Inception(832, 384, 192, 384, 48, 128, 128),
            loss3_fc=Linear(1024, 1000, **kwargs),

            loss1_conv=Convolution2D(512, 128, 1, **kwargs),
            loss1_fc1=Linear(2048, 1024, **kwargs),
            loss1_fc2=Linear(1024, 1000, **kwargs),

            loss2_conv=Convolution2D(528, 128, 1, **kwargs),
            loss2_fc1=Linear(2048, 1024, **kwargs),
            loss2_fc2=Linear(1024, 1000, **kwargs)
        )
        if pretrained_model == 'auto':
            _retrieve(
                'bvlc_googlenet.npz',
                'http://dl.caffe.berkeleyvision.org/bvlc_googlenet.caffemodel',
                self)
        elif pretrained_model:
            npz.load_npz(pretrained_model, self)
        self.functions = OrderedDict([
            ('conv1', [self.conv1, relu]),
            ('pool1', [_max_pooling_2d, _local_response_normalization]),
            ('conv2_reduce', [self.conv2_reduce, relu]),
            ('conv2', [self.conv2, relu, _local_response_normalization]),
            ('pool2', [_max_pooling_2d]),
            ('inception_3a', [self.inc3a]),
            ('inception_3b', [self.inc3b]),
            ('pool3', [_max_pooling_2d]),
            ('inception_4a', [self.inc4a]),
            ('inception_4b', [self.inc4b]),
            ('inception_4c', [self.inc4c]),
            ('inception_4d', [self.inc4d]),
            ('inception_4e', [self.inc4e]),
            ('pool4', [_max_pooling_2d]),
            ('inception_5a', [self.inc5a]),
            ('inception_5b', [self.inc5b]),
            ('pool5', [_average_pooling_2d_k7]),
            ('loss3_fc', [_dropout, self.loss3_fc]),
            ('prob', [softmax]),
            # Since usually the following outputs are not used, they are put
            # after 'prob' to be skipped for efficiency.
            ('loss1_fc2', [_average_pooling_2d_k5, self.loss1_conv, relu,
                           self.loss1_fc1, relu, self.loss1_fc2]),
            ('loss2_fc2', [_average_pooling_2d_k5, self.loss2_conv, relu,
                           self.loss2_fc1, relu, self.loss2_fc2])
        ])

    @property
    def available_layers(self):
        return list(self.functions.keys())

    @classmethod
    def convert_caffemodel_to_npz(cls, path_caffemodel, path_npz):
        """Converts a pre-trained caffemodel to a chainer model.

        Args:
            path_caffemodel (str): Path of the pre-trained caffemodel.
            path_npz (str): Path of the converted chainer model.
        """

        # As CaffeFunction uses shortcut symbols,
        # we import CaffeFunction here.
        from chainer.links.caffe.caffe_function import CaffeFunction
        caffemodel = CaffeFunction(path_caffemodel)
        chainermodel = cls(pretrained_model=None)
        _transfer_googlenet(caffemodel, chainermodel)
        npz.save_npz(path_npz, chainermodel, compression=False)

    def __call__(self, x, layers=['prob'], train=False):
        """Computes all the feature maps specified by ``layers``.

        Args:
            x (~chainer.Variable): Input variable. It should be prepared by
            ``prepare`` function.
            layers (list of str): The list of layer names you want to extract.
            train (bool): If ``True``, Dropout runs in training mode.

        Returns:
            Dictionary of ~chainer.Variable: A directory in which
            the key contains the layer name and the value contains
            the corresponding feature map variable.

        """

        h = x
        activations = {}
        inception_4a_cache = None
        inception_4d_cache = None
        target_layers = set(layers)
        for key, funcs in self.functions.items():
            if len(target_layers) == 0:
                break

            if key == 'loss1_fc2':
                h = inception_4a_cache
            elif key == 'loss2_fc2':
                h = inception_4d_cache

            for func in funcs:
                h = func(h)

            if key in target_layers:
                activations[key] = h
                target_layers.remove(key)

            if key == 'inception_4a':
                inception_4a_cache = h
            elif key == 'inception_4d':
                inception_4d_cache = h

        return activations

    def extract(self, images, layers=['pool5'], size=(224, 224)):
        """Extracts all the feature maps of given images.

        The difference of directly executing ``__call__`` is that
        it directly accepts images as an input and automatically
        transforms them to a proper variable. That is,
        it is also interpreted as a shortcut method that implicitly calls
        ``prepare`` and ``__call__`` functions.

        Args:
            images (iterable of PIL.Image or numpy.ndarray): Input images.
            layers (list of str): The list of layer names you want to extract.
            size (pair of ints): The resolution of resized images used as
                an input of CNN. All the given images are not resized
                if this argument is ``None``, but the resolutions of
                all the images should be the same.
            train (bool): If ``True``, Dropout runs in training mode.
            volatile (~chainer.Flag): Volatility flag used for input variables.

        Returns:
            Dictionary of ~chainer.Variable: A directory in which
            the key contains the layer name and the value contains
            the corresponding feature map variable.

        """

        x = concat_examples([prepare(img, size=size) for img in images])
        x = Variable(self.xp.asarray(x))
        return self(x, layers=layers)

    def predict(self, images, oversample=True):
        """Computes all the probabilities of given images.

        Args:
            images (iterable of PIL.Image or numpy.ndarray): Input images.
            oversample (bool): If ``True``, it averages results across
                center, corners, and mirrors. Otherwise, it uses only the
                center.

        Returns:
            ~chainer.Variable: Output that contains the class probabilities
            of given images.

        """

        x = concat_examples([prepare(img, size=(256, 256)) for img in images])
        if oversample:
            x = imgproc.oversample(x, crop_dims=(224, 224))
        else:
            x = x[:, :, 16:240, 16:240]
        # Set volatile option to ON to reduce memory consumption
        x = Variable(self.xp.asarray(x))
        y = self(x, layers=['prob'])['prob']
        if oversample:
            n = y.data.shape[0] // 10
            y_shape = y.data.shape[1:]
            y = reshape(y, (n, 10) + y_shape)
            y = sum(y, axis=1) / 10
        return y


def prepare(image, size=(224, 224)):
    """Converts the given image to the numpy array for ResNets.

    Note that you have to call this method before ``__call__``
    because the pre-trained GoogLeNet model requires to resize the given
    image, covert the RGB to the BGR, subtract the mean,
    and permute the dimensions before calling.

    Args:
        image (PIL.Image or numpy.ndarray): Input image.
            If an input is ``numpy.ndarray``, its shape must be
            ``(height, width)``, ``(height, width, channels)``,
            or ``(channels, height, width)``, and
            the order of the channels must be RGB.
        size (pair of ints): Size of converted images.
            If ``None``, the given image is not resized.

    Returns:
        numpy.ndarray: The converted output array.

    """

    if not available:
        raise ImportError('PIL cannot be loaded. Install Pillow!\n'
                          'The actual import error is as follows:\n' +
                          str(_import_error))
    if isinstance(image, numpy.ndarray):
        if image.ndim == 3:
            if image.shape[0] == 1:
                image = image[0, :, :]
            elif image.shape[0] == 3:
                image = image.transpose((1, 2, 0))
        image = Image.fromarray(image.astype(numpy.uint8))
    image = image.convert('RGB')
    if size:
        image = image.resize(size)
    image = numpy.asarray(image, dtype=numpy.float32)
    image = image[:, :, ::-1]
    image -= GoogLeNet.image_mean
    image = image.transpose((2, 0, 1))
    return image


def _transfer_inception(src, dst, names):
    for name in names:
        chain = getattr(dst, 'inc{}'.format(name))
        src_prefix = 'inception_{}/'.format(name)
        chain.conv1.W.data[:] = src[src_prefix + '1x1'].W.data
        chain.conv1.b.data[:] = src[src_prefix + '1x1'].b.data
        chain.proj3.W.data[:] = src[src_prefix + '3x3_reduce'].W.data
        chain.proj3.b.data[:] = src[src_prefix + '3x3_reduce'].b.data
        chain.conv3.W.data[:] = src[src_prefix + '3x3'].W.data
        chain.conv3.b.data[:] = src[src_prefix + '3x3'].b.data
        chain.proj5.W.data[:] = src[src_prefix + '5x5_reduce'].W.data
        chain.proj5.b.data[:] = src[src_prefix + '5x5_reduce'].b.data
        chain.conv5.W.data[:] = src[src_prefix + '5x5'].W.data
        chain.conv5.b.data[:] = src[src_prefix + '5x5'].b.data
        chain.projp.W.data[:] = src[src_prefix + 'pool_proj'].W.data
        chain.projp.b.data[:] = src[src_prefix + 'pool_proj'].b.data


def _transfer_googlenet(src, dst):
    # 1 #################################################################
    dst.conv1.W.data[:] = src['conv1/7x7_s2'].W.data
    dst.conv1.b.data[:] = src['conv1/7x7_s2'].b.data

    # 2 #################################################################
    dst.conv2_reduce.W.data[:] = src['conv2/3x3_reduce'].W.data
    dst.conv2_reduce.b.data[:] = src['conv2/3x3_reduce'].b.data
    dst.conv2.W.data[:] = src['conv2/3x3'].W.data
    dst.conv2.b.data[:] = src['conv2/3x3'].b.data

    # 3, 4, 5 ###########################################################
    _transfer_inception(src, dst, ['3a', '3b',
                                   '4a', '4b', '4c', '4d', '4e',
                                   '5a', '5b'])

    # outputs ############################################################
    dst.loss1_conv.W.data[:] = src['loss1/conv'].W.data
    dst.loss1_conv.b.data[:] = src['loss1/conv'].b.data
    dst.loss1_fc1.W.data[:] = src['loss1/fc'].W.data
    dst.loss1_fc1.b.data[:] = src['loss1/fc'].b.data
    dst.loss1_fc2.W.data[:] = src['loss1/classifier'].W.data
    dst.loss1_fc2.b.data[:] = src['loss1/classifier'].b.data

    dst.loss2_conv.W.data[:] = src['loss2/conv'].W.data
    dst.loss2_conv.b.data[:] = src['loss2/conv'].b.data
    dst.loss2_fc1.W.data[:] = src['loss2/fc'].W.data
    dst.loss2_fc1.b.data[:] = src['loss2/fc'].b.data
    dst.loss2_fc2.W.data[:] = src['loss2/classifier'].W.data
    dst.loss2_fc2.b.data[:] = src['loss2/classifier'].b.data

    dst.loss3_fc.W.data[:] = src['loss3/classifier'].W.data
    dst.loss3_fc.b.data[:] = src['loss3/classifier'].b.data


def _max_pooling_2d(x):
    return max_pooling_2d(x, ksize=3, stride=2)


def _local_response_normalization(x):
    return local_response_normalization(x, n=5, k=1, alpha=1e-4/5)


def _average_pooling_2d_k5(x):
    return average_pooling_2d(x, ksize=5, stride=3)


def _average_pooling_2d_k7(x):
    return average_pooling_2d(x, ksize=7, stride=1)


def _dropout(x):
    return dropout(x, ratio=0.4)


def _make_npz(path_npz, url, model):
    path_caffemodel = download.cached_download(url)
    print('Now loading caffemodel (usually it may take few minutes)')
    GoogLeNet.convert_caffemodel_to_npz(path_caffemodel, path_npz)
    npz.load_npz(path_npz, model)
    return model


def _retrieve(name_npz, url, model):
    root = download.get_dataset_directory('pfnet/chainer/models/')
    path = os.path.join(root, name_npz)
    return download.cache_or_load_file(
        path, lambda path: _make_npz(path, url, model),
        lambda path: npz.load_npz(path, model))


if __name__ == '__main__':
    import matplotlib.pyplot as plt
    from synset_words import get_synset_words

    images = []
    for filename in os.listdir('ILSVRC2012'):
        images.append(plt.imread(os.path.join('ILSVRC2012', filename)))
        break  # TODO: Remove this line

    model = GoogLeNet()
    y = model.predict(images)

    top_10 = numpy.argsort(y.data[0])[:-10:-1]
    synset_words = get_synset_words()
    for i in top_10:
        print(y.data[0][i], synset_words[i][1])
    print()
