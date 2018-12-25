from __future__ import print_function
from __future__ import absolute_import
from __future__ import division

import warnings

from keras.models import Model
from keras.layers import Dense
from keras.layers import Dropout
from keras.layers import Activation
from keras.layers import Reshape
from keras.layers import Conv2D, Conv3D
from keras.layers import Conv2DTranspose, Conv3DTranspose
from keras.layers import UpSampling2D, UpSampling3D
from keras.layers import MaxPooling2D, MaxPooling3D
from keras.layers import AveragePooling2D, AveragePooling3D
from keras.layers import GlobalMaxPooling2D, GlobalMaxPooling3D
from keras.layers import GlobalAveragePooling2D, GlobalAveragePooling3D
from keras.layers import Input
from keras.layers import concatenate
from keras.layers import BatchNormalization
from keras.layers import LeakyReLU
from keras.regularizers import l2
from keras.utils.layer_utils import convert_all_kernels_in_model
from keras.utils.data_utils import get_file
from keras.engine.topology import get_source_inputs
from keras_applications.imagenet_utils import _obtain_input_shape
from keras.applications.imagenet_utils import decode_predictions
from keras.applications.imagenet_utils import preprocess_input as _preprocess_input
import keras.backend as K

from keras.optimizers import Adam
from keras.utils import multi_gpu_model

from keras_contrib.layers.convolutional import SubPixelUpscaling

from unet3d.metrics import dice_coefficient_loss, get_label_dice_coefficient_function, dice_coefficient
from unet3d.metrics import minh_dice_coef_loss, dice_coefficient_loss, minh_dice_coef_metric
from unet3d.metrics import weighted_dice_coefficient_loss, soft_dice_loss, soft_dice_numpy, tversky_loss
from unet3d.metrics import tv_minh_loss


def name_or_none(prefix, name):
    return prefix + name if (prefix is not None and name is not None) else None


def DenseNetFCN_3D(input_shape, nb_dense_block=5, growth_rate=16, nb_layers_per_block=4,
                   reduction=0.0, dropout_rate=0.0, weight_decay=1E-4, init_conv_filters=48,
                   include_top=True, weights=None, input_tensor=None, classes=1, activation='softmax',
                   upsampling_conv=128, upsampling_type='deconv', early_transition=False,
                   transition_pooling='max', initial_kernel_size=(3, 3, 3),
                   initial_learning_rate=0.00001,
                   metrics=minh_dice_coef_metric,
                   loss_function="weighted"):
    '''Instantiate the DenseNet FCN architecture.
        Note that when using TensorFlow,
        for best performance you should set
        `image_data_format='channels_last'` in your Keras config
        at ~/.keras/keras.json.
        # Arguments
            nb_dense_block: number of dense blocks to add to end (generally = 3)
            growth_rate: number of filters to add per dense block
            nb_layers_per_block: number of layers in each dense block.
                Can be a positive integer or a list.
                If positive integer, a set number of layers per dense block.
                If list, nb_layer is used as provided. Note that list size must
                be (nb_dense_block + 1)
            reduction: reduction factor of transition blocks.
                Note : reduction value is inverted to compute compression.
            dropout_rate: dropout rate
            weight_decay: weight decay factor
            init_conv_filters: number of layers in the initial convolution layer
            include_top: whether to include the fully-connected
                layer at the top of the network.
            weights: one of `None` (random initialization) or
                'cifar10' (pre-training on CIFAR-10)..
            input_tensor: optional Keras tensor (i.e. output of `layers.Input()`)
                to use as image input for the model.
            input_shape: optional shape tuple, only to be specified
                if `include_top` is False (otherwise the input shape
                has to be `(32, 32, 3)` (with `channels_last` dim ordering)
                or `(3, 32, 32)` (with `channels_first` dim ordering).
                It should have exactly 3 inputs channels,
                and width and height should be no smaller than 8.
                E.g. `(200, 200, 3)` would be one valid value.
            classes: optional number of classes to classify images
                into, only to be specified if `include_top` is True, and
                if no `weights` argument is specified.
            activation: Type of activation at the top layer. Can be one of 'softmax' or 'sigmoid'.
                Note that if sigmoid is used, classes must be 1.
            upsampling_conv: number of convolutional layers in upsampling via subpixel convolution
            upsampling_type: Can be one of 'deconv', 'upsampling' and
                'subpixel'. Defines type of upsampling algorithm used.
            batchsize: Fixed batch size. This is a temporary requirement for
                computation of output shape in the case of Deconvolution2D layers.
                Parameter will be removed in next iteration of Keras, which infers
                output shape of deconvolution layers automatically.
            early_transition: Start with an extra initial transition down and end with an extra
                transition up to reduce the network size.
            initial_kernel_size: The first Conv3D kernel might vary in size based on the
                application, this parameter makes it configurable.

        # Returns
            A Keras model instance.
    '''

    if weights not in {None}:
        raise ValueError('The `weights` argument should be '
                         '`None` (random initialization) as no '
                         'model weights are provided.')

    upsampling_type = upsampling_type.lower()

    if upsampling_type not in ['upsampling', 'deconv', 'subpixel']:
        raise ValueError('Parameter "upsampling_type" must be one of "upsampling", '
                         '"deconv" or "subpixel".')

    if input_shape is None:
        raise ValueError(
            'For fully convolutional models, input shape must be supplied.')

    if type(nb_layers_per_block) is not list and nb_dense_block < 1:
        raise ValueError('Number of dense layers per block must be greater than 1. Argument '
                         'value was %d.' % (nb_layers_per_block))

    if activation not in ['softmax', 'sigmoid']:
        raise ValueError('activation must be one of "softmax" or "sigmoid"')

    if activation == 'sigmoid' and classes != 1:
        raise ValueError(
            'sigmoid activation can only be used when classes = 1')

    # Determine proper input shape
    min_size = 2 ** nb_dense_block

    if K.image_data_format() == 'channels_first':
        if input_shape is not None:
            if ((input_shape[1] is not None and input_shape[1] < min_size) or
                    (input_shape[2] is not None and input_shape[2] < min_size) or
                    (input_shape[3] is not None and input_shape[2] < min_size)):
                raise ValueError('Input size must be at least ' +
                                 str(min_size) + 'x' + str(min_size) + ', got '
                                                                       '`input_shape=' + str(input_shape) + '`')
        else:
            input_shape = (classes, None, None, None)
    else:
        if input_shape is not None:
            if ((input_shape[0] is not None and input_shape[0] < min_size) or
                    (input_shape[1] is not None and input_shape[1] < min_size) or
                    (input_shape[2] is not None and input_shape[2] < min_size)):
                raise ValueError('Input size must be at least ' +
                                 str(min_size) + 'x' + str(min_size) + ', got '
                                                                       '`input_shape=' + str(input_shape) + '`')
        else:
            input_shape = (None, None, None, classes)

    if input_tensor is None:
        img_input = Input(shape=input_shape)
    else:
        if not K.is_keras_tensor(input_tensor):
            img_input = Input(tensor=input_tensor, shape=input_shape)
        else:
            img_input = input_tensor

    x = __create_fcn_dense_net(classes, img_input, include_top, nb_dense_block, growth_rate,
                               reduction, dropout_rate, weight_decay,
                               nb_layers_per_block, upsampling_conv, upsampling_type,
                               init_conv_filters, input_shape, activation,
                               early_transition, transition_pooling, initial_kernel_size)

    # Ensure that the model takes into account
    # any potential predecessors of `input_tensor`.
    if input_tensor is not None:
        inputs = get_source_inputs(input_tensor)
    else:
        inputs = img_input
    # Create model.
    model = Model(inputs, x, name='fcn-densenet')

    if not isinstance(metrics, list):
        metrics = [metrics]

    try:
        model = multi_gpu_model(model, gpus=2)
        print('!! train on multi gpus')
    except:
        print('!! train on single gpu')
        pass

    if loss_function == "tversky":
        loss = tversky_loss
    elif loss_function == "minh":
        loss = minh_dice_coef_loss
    elif loss_function == "tv_minh":
        loss = tv_minh_loss
    else:
        loss = weighted_dice_coefficient_loss

    model.compile(optimizer=Adam(lr=initial_learning_rate, beta_1=0.9, beta_2=0.999),
                  loss=loss, metrics=metrics)
    return model


def __conv_block(ip, nb_filter, bottleneck=False, dropout_rate=None, weight_decay=1e-4,
                 block_prefix=None, instance_normalization=True, activation=LeakyReLU):
    '''
    Adds a convolution layer (with batch normalization and relu),
    and optionally a bottleneck layer.

    # Arguments
        ip: Input tensor
        nb_filter: integer, the dimensionality of the output space
            (i.e. the number output of filters in the convolution)
        bottleneck: if True, adds a bottleneck convolution block
        dropout_rate: dropout rate
        weight_decay: weight decay factor
        block_prefix: str, for unique layer naming

     # Input shape
        4D tensor with shape:
        `(samples, channels, rows, cols)` if data_format='channels_first'
        or 4D tensor with shape:
        `(samples, rows, cols, channels)` if data_format='channels_last'.

    # Output shape
        4D tensor with shape:
        `(samples, filters, new_rows, new_cols)` if data_format='channels_first'
        or 4D tensor with shape:
        `(samples, new_rows, new_cols, filters)` if data_format='channels_last'.
        `rows` and `cols` values might have changed due to stride.

    # Returns
        output tensor of block
    '''
    with K.name_scope('ConvBlock'):
        concat_axis = 1 if K.image_data_format() == 'channels_first' else -1

        if instance_normalization:
            try:
                from keras_contrib.layers.normalization import InstanceNormalization
            except ImportError:
                raise ImportError("Install keras_contrib in order to use instance normalization."
                                  "\nTry: pip install git+https://www.github.com/farizrahman4u/keras-contrib.git")
            x = InstanceNormalization(
                axis=concat_axis, epsilon=1.1e-5, name=name_or_none(block_prefix, '_in'))(ip)
        else:
            x = BatchNormalization(
                axis=concat_axis, epsilon=1.1e-5, name=name_or_none(block_prefix, '_bn'))(ip)
        if activation is None:
            x = Activation('relu')(x)
        else:
            x = activation()(x)

        if bottleneck:
            inter_channel = nb_filter * 4

            x = Conv3D(inter_channel, (1, 1, 1), kernel_initializer='he_normal', padding='same', use_bias=False,
                       kernel_regularizer=l2(weight_decay), name=name_or_none(block_prefix, '_bottleneck_conv2D'))(x)
            if instance_normalization:
                try:
                    from keras_contrib.layers.normalization import InstanceNormalization
                except ImportError:
                    raise ImportError("Install keras_contrib in order to use instance normalization."
                                      "\nTry: pip install git+https://www.github.com/farizrahman4u/keras-contrib.git")
                x = InstanceNormalization(
                    axis=concat_axis, epsilon=1.1e-5, name=name_or_none(block_prefix, '_bottleneck_in'))(x)
            else:
                x = BatchNormalization(
                    axis=concat_axis, epsilon=1.1e-5, name=name_or_none(block_prefix, '_bottleneck_bn'))(x)
            if activation is None:
                x = Activation('relu')(x)
            else:
                x = activation()(x)

        x = Conv3D(nb_filter, (3, 3, 3), kernel_initializer='he_normal', padding='same', use_bias=False,
                   name=name_or_none(block_prefix, '_conv2D'))(x)
        if dropout_rate:
            x = Dropout(dropout_rate)(x)

    return x


def __dense_block(x, nb_layers, nb_filter, growth_rate, bottleneck=False, dropout_rate=None,
                  weight_decay=1e-4, grow_nb_filters=True, return_concat_list=False, 
                  block_prefix=None):
    '''
    Build a dense_block where the output of each conv_block is fed
    to subsequent ones

    # Arguments
        x: input keras tensor
        nb_layers: the number of conv_blocks to append to the model
        nb_filter: integer, the dimensionality of the output space
            (i.e. the number output of filters in the convolution)
        growth_rate: growth rate of the dense block
        bottleneck: if True, adds a bottleneck convolution block to
            each conv_block
        dropout_rate: dropout rate
        weight_decay: weight decay factor
        grow_nb_filters: if True, allows number of filters to grow
        return_concat_list: set to True to return the list of
            feature maps along with the actual output
        block_prefix: str, for block unique naming

    # Return
        If return_concat_list is True, returns a list of the output
        keras tensor, the number of filters and a list of all the
        dense blocks added to the keras tensor

        If return_concat_list is False, returns a list of the output
        keras tensor and the number of filters
    '''
    with K.name_scope('DenseBlock'):
        concat_axis = 1 if K.image_data_format() == 'channels_first' else -1

        x_list = [x]

        for i in range(nb_layers):
            cb = __conv_block(x, growth_rate, bottleneck, dropout_rate, weight_decay,
                              block_prefix=name_or_none(block_prefix, '_%i' % i))
            x_list.append(cb)

            x = concatenate([x, cb], axis=concat_axis)

            if grow_nb_filters:
                nb_filter += growth_rate

        if return_concat_list:
            return x, nb_filter, x_list
        else:
            return x, nb_filter


def __transition_block(ip, nb_filter, compression=1.0, weight_decay=1e-4, block_prefix=None,
                       transition_pooling='max', instance_normalization=True, 
                       activation=LeakyReLU):
    '''
    Adds a pointwise convolution layer (with batch normalization and relu),
    and an average pooling layer. The number of output convolution filters
    can be reduced by appropriately reducing the compression parameter.

    # Arguments
        ip: input keras tensor
        nb_filter: integer, the dimensionality of the output space
            (i.e. the number output of filters in the convolution)
        compression: calculated as 1 - reduction. Reduces the number
            of feature maps in the transition block.
        weight_decay: weight decay factor
        block_prefix: str, for block unique naming

    # Input shape
        4D tensor with shape:
        `(samples, channels, rows, cols)` if data_format='channels_first'
        or 4D tensor with shape:
        `(samples, rows, cols, channels)` if data_format='channels_last'.

    # Output shape
        4D tensor with shape:
        `(samples, nb_filter * compression, rows / 2, cols / 2)`
        if data_format='channels_first'
        or 4D tensor with shape:
        `(samples, rows / 2, cols / 2, nb_filter * compression)`
        if data_format='channels_last'.

    # Returns
        a keras tensor
    '''
    with K.name_scope('Transition'):
        concat_axis = 1 if K.image_data_format() == 'channels_first' else -1

        if instance_normalization:
            try:
                from keras_contrib.layers.normalization import InstanceNormalization
            except ImportError:
                raise ImportError("Install keras_contrib in order to use instance normalization."
                                  "\nTry: pip install git+https://www.github.com/farizrahman4u/keras-contrib.git")
            x = InstanceNormalization(
                axis=concat_axis, epsilon=1.1e-5, name=name_or_none(block_prefix, '_in'))(ip)
        else:
            x = BatchNormalization(
                axis=concat_axis, epsilon=1.1e-5, name=name_or_none(block_prefix, '_bn'))(ip)
        if activation is None:
            x = Activation('relu')(x)
        else:
            x = activation()(x)
        x = Conv3D(int(nb_filter * compression), (1, 1, 1), kernel_initializer='he_normal', padding='same',
                   use_bias=False, kernel_regularizer=l2(weight_decay), name=name_or_none(block_prefix, '_conv2D'))(x)
        if transition_pooling == 'avg':
            x = AveragePooling3D((2, 2, 2), strides=(2, 2, 2))(x)
        elif transition_pooling == 'max':
            x = MaxPooling3D((2, 2, 2), strides=(2, 2, 2))(x)

        return x


def __transition_up_block(ip, nb_filters, type='deconv', weight_decay=1E-4, block_prefix=None):
    '''Adds an upsampling block. Upsampling operation relies on the the type parameter.

    # Arguments
        ip: input keras tensor
        nb_filters: integer, the dimensionality of the output space
            (i.e. the number output of filters in the convolution)
        type: can be 'upsampling', 'subpixel', 'deconv'. Determines
            type of upsampling performed
        weight_decay: weight decay factor
        block_prefix: str, for block unique naming

    # Input shape
        4D tensor with shape:
        `(samples, channels, rows, cols)` if data_format='channels_first'
        or 4D tensor with shape:
        `(samples, rows, cols, channels)` if data_format='channels_last'.

    # Output shape
        4D tensor with shape:
        `(samples, nb_filter, rows * 2, cols * 2)` if data_format='channels_first'
        or 4D tensor with shape:
        `(samples, rows * 2, cols * 2, nb_filter)` if data_format='channels_last'.

    # Returns
        a keras tensor
    '''
    with K.name_scope('TransitionUp'):

        if type == 'upsampling':
            x = UpSampling3D(name=name_or_none(
                block_prefix, '_upsampling'))(ip)
        elif type == 'subpixel':
            x = Conv3D(nb_filters, (3, 3, 3), activation='relu', padding='same', kernel_regularizer=l2(weight_decay),
                       use_bias=False, kernel_initializer='he_normal', name=name_or_none(block_prefix, '_conv2D'))(ip)
            x = SubPixelUpscaling(scale_factor=2, name=name_or_none(
                block_prefix, '_subpixel'))(x)
            x = Conv3D(nb_filters, (3, 3, 3), activation='relu', padding='same', kernel_regularizer=l2(weight_decay),
                       use_bias=False, kernel_initializer='he_normal', name=name_or_none(block_prefix, '_conv2D'))(x)
        else:
            x = Conv3DTranspose(nb_filters, (3, 3, 3), activation='relu', padding='same', strides=(2, 2, 2),
                                kernel_initializer='he_normal', kernel_regularizer=l2(weight_decay),
                                name=name_or_none(block_prefix, '_conv2DT'))(ip)
        return x


def __create_fcn_dense_net(nb_classes, img_input, include_top, nb_dense_block=5, growth_rate=12,
                           reduction=0.0, dropout_rate=None, weight_decay=1e-4,
                           nb_layers_per_block=4, nb_upsampling_conv=128, upsampling_type='deconv',
                           init_conv_filters=48, input_shape=None, activation='softmax',
                           early_transition=False, transition_pooling='max', initial_kernel_size=(3, 3, 3),
                           instance_normalization=True, activation_x=LeakyReLU):
    ''' Build the DenseNet-FCN model

    # Arguments
        nb_classes: number of classes
        img_input: tuple of shape (channels, rows, columns) or (rows, columns, channels)
        include_top: flag to include the final Dense layer
        nb_dense_block: number of dense blocks to add to end (generally = 3)
        growth_rate: number of filters to add per dense block
        reduction: reduction factor of transition blocks. Note : reduction value is inverted to compute compression
        dropout_rate: dropout rate
        weight_decay: weight decay
        nb_layers_per_block: number of layers in each dense block.
            Can be a positive integer or a list.
            If positive integer, a set number of layers per dense block.
            If list, nb_layer is used as provided. Note that list size must
            be (nb_dense_block + 1)
        nb_upsampling_conv: number of convolutional layers in upsampling via subpixel convolution
        upsampling_type: Can be one of 'upsampling', 'deconv' and 'subpixel'. Defines
            type of upsampling algorithm used.
        input_shape: Only used for shape inference in fully convolutional networks.
        activation: Type of activation at the top layer. Can be one of 'softmax' or 'sigmoid'.
                    Note that if sigmoid is used, classes must be 1.
        early_transition: Start with an extra initial transition down and end with an extra
            transition up to reduce the network size.
        transition_pooling: 'max' for max pooling (default), 'avg' for average pooling,
            None for no pooling. Please note that this default differs from the DenseNet
            paper in accordance with the DenseNetFCN paper.
        initial_kernel_size: The first Conv3D kernel might vary in size based on the
            application, this parameter makes it configurable.

    # Returns
        a keras tensor

    # Raises
        ValueError: in case of invalid argument for `reduction`,
            `nb_dense_block` or `nb_upsampling_conv`.
    '''
    with K.name_scope('DenseNetFCN'):
        concat_axis = 1 if K.image_data_format() == 'channels_first' else -1

        if concat_axis == 1:  # channels_first dim ordering
            _, rows, cols, heights = input_shape
        else:
            rows, cols, heights, _ = input_shape

        if reduction != 0.0:
            if not (reduction <= 1.0 and reduction > 0.0):
                raise ValueError(
                    '`reduction` value must lie between 0.0 and 1.0')

        # check if upsampling_conv has minimum number of filters
        # minimum is set to 12, as at least 3 color channels are needed for correct upsampling
        if not (nb_upsampling_conv > 12 and nb_upsampling_conv % 4 == 0):
            raise ValueError('Parameter `nb_upsampling_conv` number of channels must '
                             'be a positive number divisible by 4 and greater than 12')

        # layers in each dense block
        if type(nb_layers_per_block) is list or type(nb_layers_per_block) is tuple:
            nb_layers = list(nb_layers_per_block)  # Convert tuple to list

            if len(nb_layers) != (nb_dense_block + 1):
                raise ValueError('If `nb_dense_block` is a list, its length must be '
                                 '(`nb_dense_block` + 1)')

            bottleneck_nb_layers = nb_layers[-1]
            rev_layers = nb_layers[::-1]
            nb_layers.extend(rev_layers[1:])
        else:
            bottleneck_nb_layers = nb_layers_per_block
            nb_layers = [nb_layers_per_block] * (2 * nb_dense_block + 1)

        # compute compression factor
        compression = 1.0 - reduction

        # Initial convolution
        x = Conv3D(init_conv_filters, initial_kernel_size, kernel_initializer='he_normal', padding='same', name='initial_conv2D',
                   use_bias=False, kernel_regularizer=l2(weight_decay))(img_input)
        if instance_normalization:
            try:
                from keras_contrib.layers.normalization import InstanceNormalization
            except ImportError:
                raise ImportError("Install keras_contrib in order to use instance normalization."
                                  "\nTry: pip install git+https://www.github.com/farizrahman4u/keras-contrib.git")
            x = InstanceNormalization(
                axis=concat_axis, epsilon=1.1e-5, name='initial_in')(x)
        else:
            x = BatchNormalization(
                axis=concat_axis, epsilon=1.1e-5, name='initial_bn')(x)
        if activation_x is None:
            x = Activation('relu')(x)
        else:
            x = activation_x()(x)

        nb_filter = init_conv_filters

        skip_list = []

        if early_transition:
            x = __transition_block(x, nb_filter, compression=compression, weight_decay=weight_decay,
                                   block_prefix='tr_early', transition_pooling=transition_pooling)

        # Add dense blocks and transition down block
        for block_idx in range(nb_dense_block):
            x, nb_filter = __dense_block(x, nb_layers[block_idx], nb_filter, growth_rate, dropout_rate=dropout_rate,
                                         weight_decay=weight_decay, block_prefix='dense_%i' % block_idx)

            # Skip connection
            skip_list.append(x)

            # add transition_block
            x = __transition_block(x, nb_filter, compression=compression, weight_decay=weight_decay,
                                   block_prefix='tr_%i' % block_idx, transition_pooling=transition_pooling)

            # this is calculated inside transition_down_block
            nb_filter = int(nb_filter * compression)

        # The last dense_block does not have a transition_down_block
        # return the concatenated feature maps without the concatenation of the input
        _, nb_filter, concat_list = __dense_block(x, bottleneck_nb_layers, nb_filter, growth_rate,
                                                  dropout_rate=dropout_rate, weight_decay=weight_decay,
                                                  return_concat_list=True,
                                                  block_prefix='dense_%i' % nb_dense_block)

        skip_list = skip_list[::-1]  # reverse the skip list

        # Add dense blocks and transition up block
        for block_idx in range(nb_dense_block):
            n_filters_keep = growth_rate * \
                nb_layers[nb_dense_block + block_idx]

            # upsampling block must upsample only the feature maps (concat_list[1:]),
            # not the concatenation of the input with the feature maps (concat_list[0].
            l = concatenate(concat_list[1:], axis=concat_axis)

            t = __transition_up_block(l, nb_filters=n_filters_keep, type=upsampling_type, weight_decay=weight_decay,
                                      block_prefix='tr_up_%i' % block_idx)

            # concatenate the skip connection with the transition block
            x = concatenate([t, skip_list[block_idx]], axis=concat_axis)

            # Dont allow the feature map size to grow in upsampling dense blocks
            x_up, nb_filter, concat_list = __dense_block(x, nb_layers[nb_dense_block + block_idx + 1],
                                                         nb_filter=growth_rate, growth_rate=growth_rate,
                                                         dropout_rate=dropout_rate, weight_decay=weight_decay,
                                                         return_concat_list=True, grow_nb_filters=False,
                                                         block_prefix='dense_%i' % (nb_dense_block + 1 + block_idx))

        if early_transition:
            x_up = __transition_up_block(x_up, nb_filters=nb_filter, type=upsampling_type, weight_decay=weight_decay,
                                         block_prefix='tr_up_early')
        if include_top:
            x = Conv3D(nb_classes, (1, 1, 1), activation='linear',
                       padding='same', use_bias=False)(x_up)
            x = Activation(activation)(x)

            # if K.image_data_format() == 'channels_first':
            #     _, row, col, height = input_shape
            #     x = Reshape((nb_classes, row * col * height))(x)
            #     x = Activation(activation)(x)
            #     x = Reshape((nb_classes, row, col, height))(x)
            # else:
            #     row, col, height, _ = input_shape
            #     x = Reshape((row * col * height, nb_classes))(x)
            #     x = Activation(activation)(x)
            #     x = Reshape((row, col, height, nb_classes))(x)
        else:
            x = x_up

        return x
