r"""
This module is a collection of *factories* for creating objects of datasets,
models, optimizers and other useful components. For example, a ResNet-50
visual backbone can be created as:

    .. code-block:: python

        >>> # Explicitly by name, args and kwargs:
        >>> backbone = VisualBackboneFactory.create(
        ...     "torchvision::resnet50", pretrained=False
        ... )
        >>> # Directly from a config object:
        >>> _C = Config(override_list=["MODEL.VISUAL.NAME", "torchvision::resnet50"])
        >>> backbone = VisualBackboneFactory.from_config(_C)

Creating directly from :class:`~virtex.config.Config` is fast and simple, and
ensures minimal changes throughout the codebase upon any change in the call
signature of underlying class; or config hierarchy. Refer description of
specific factories for more details.
"""
from functools import partial
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

import albumentations as alb
from torch import nn, optim

from virtex.config import Config
import virtex.data as vdata
from virtex.data import transforms as T
from virtex.data.tokenizers import SentencePieceBPETokenizer
import virtex.models as vmodels
from virtex.modules import visual_backbones, textual_heads
from virtex.optim import Lookahead, lr_scheduler


class Factory(object):
    r"""
    Base class for all factories. All factories must inherit this base class
    and follow these guidelines for a consistent behavior:

    * Factory objects cannot be instantiated, doing ``factory = SomeFactory()``
      is illegal. Child classes should not implement ``__init__`` methods.
    * All factories must have an attribute named ``PRODUCTS`` of type
      ``Dict[str, Callable]``, which associates each class with a unique string
      name which can be used to create it.
    * All factories must implement one classmethod, :meth:`from_config` which
      contains logic for creating an object directly by taking name and other
      arguments directly from :class:`~virtex.config.Config`. They can use
      :meth:`create` already implemented in this base class.
    * :meth:`from_config` should not use too many extra arguments than the
      config itself, unless necessary (such as model parameters for optimizer).
    """

    PRODUCTS: Dict[str, Callable] = {}

    def __init__(self):
        raise ValueError(
            f"""Cannot instantiate {self.__class__.__name__} object, use
            `create` classmethod to create a product from this factory.
            """
        )

    @classmethod
    def create(cls, name: str, *args, **kwargs) -> Any:
        r"""Create an object by its name, args and kwargs."""
        if name not in cls.PRODUCTS:
            raise KeyError(f"{cls.__class__.__name__} cannot create {name}.")

        return cls.PRODUCTS[name](*args, **kwargs)

    @classmethod
    def from_config(cls, config: Config) -> Any:
        r"""Create an object directly from config."""
        raise NotImplementedError


class TokenizerFactory(Factory):
    r"""
    Factory to create text tokenizers. This codebase ony supports one tokenizer
    for now, but having a dedicated factory makes it easy to add more if needed.

    Possible choices: ``{"SentencePieceBPETokenizer"}``.
    """

    PRODUCTS: Dict[str, Callable] = {
        "SentencePieceBPETokenizer": SentencePieceBPETokenizer
    }

    @classmethod
    def from_config(cls, config: Config) -> SentencePieceBPETokenizer:
        r"""
        Create a tokenizer directly from config.

        Parameters
        ----------
        config: virtex.config.Config
            Config object with all the parameters.
        """

        _C = config

        tokenizer = cls.create(
            "SentencePieceBPETokenizer",
            vocab_path=_C.DATA.TOKENIZER_VOCAB,
            model_path=_C.DATA.TOKENIZER_MODEL,
        )
        return tokenizer


class ImageTransformsFactory(Factory):
    r"""
    Factory to create image transformations for common preprocessing and data
    augmentations. These are a mix of default transformations from
    `albumentations <https://albumentations.readthedocs.io/en/latest/>`_ and
    some extended ones defined in :mod:`virtex.data.transforms`.

    This factory does not implement :meth:`from_config` method. It is only used
    by :class:`PretrainingDatasetFactory` and :class:`DownstreamDatasetFactory`.

    Possible choices: ``{"center_crop", "horizontal_flip", "random_resized_crop",
    "normalize", "global_resize", "color_jitter", "smallest_resize"}``.
    """

    # fmt: off
    PRODUCTS: Dict[str, Callable] = {
        # Input resize transforms: whenever selected, these are always applied.
        # These transforms require one position argument: image dimension.
        "random_resized_crop": partial(
            T.RandomResizedSquareCrop, scale=(0.2, 1.0), ratio=(0.75, 1.333), p=1.0
        ),
        "center_crop": partial(T.CenterSquareCrop, p=1.0),
        "smallest_resize": partial(alb.SmallestMaxSize, p=1.0),
        "global_resize": partial(T.SquareResize, p=1.0),

        # Keep hue limits small in color jitter because it changes color drastically
        # and captions often mention colors. Apply with higher probability.
        "color_jitter": partial(
            T.ColorJitter, brightness=0.4, contrast=0.4, saturation=0.4, hue=0.1, p=0.8
        ),
        "horizontal_flip": partial(T.HorizontalFlip, p=0.5),

        # Color normalization: whenever selected, always applied.
        "normalize": partial(
            alb.Normalize, mean=T.IMAGENET_COLOR_MEAN, std=T.IMAGENET_COLOR_STD, p=1.0
        ),
    }
    # fmt: on

    @classmethod
    def from_config(cls, config: Config):
        r"""Augmentations cannot be created from config, only :meth:`create`."""
        raise NotImplementedError


class PretrainingDatasetFactory(Factory):
    r"""
    Factory to create :class:`~torch.utils.data.Dataset` s for pretraining
    VirTex models. Datasets are created depending on pretraining task used.
    Typically these datasets either provide image-caption pairs, or only images
    from COCO Captions dataset (serialized to an LMDB file).

    As an exception, the dataset for ``multilabel_classification`` provides
    COCO images and labels of their bounding box annotations.

    Possible choices: ``{"bicaptioning", "captioning", "token_classification",
    "multilabel_classification"}``.
    """

    PRODUCTS: Dict[str, Callable] = {
        "bicaptioning": vdata.CaptioningDataset,
        "captioning": vdata.CaptioningDataset,
        "token_classification": vdata.CaptioningDataset,
        "multilabel_classification": vdata.MultiLabelClassificationDataset,
    }

    @classmethod
    def from_config(cls, config: Config, split: str = "train"):
        r"""
        Create a dataset directly from config. Names in this factory match with
        names in :class:`PretrainingModelFactory` because both use same config
        parameter ``MODEL.NAME`` to create objects.

        Parameters
        ----------
        config: virtex.config.Config
            Config object with all the parameters.
        split: str, optional (default = "train")
            Which split to load for the dataset. One of ``{"train", "val"}``.
        """

        _C = config
        # Every dataset needs these two args.
        kwargs = {"data_root": _C.DATA.ROOT, "split": split}

        # Create a list of image transformations based on transform names.
        image_transform_list: List[Callable] = []

        for name in getattr(_C.DATA, f"IMAGE_TRANSFORM_{split.upper()}"):
            # Pass dimensions if cropping / resizing, else rely on the defaults
            # as per `ImageTransformsFactory`.
            if "resize" in name or "crop" in name:
                image_transform_list.append(
                    ImageTransformsFactory.create(name, _C.DATA.IMAGE_CROP_SIZE)
                )
            else:
                image_transform_list.append(ImageTransformsFactory.create(name))

        kwargs["image_transform"] = alb.Compose(image_transform_list)

        # Add dataset specific kwargs.
        if _C.MODEL.NAME != "multilabel_classification":
            tokenizer = TokenizerFactory.from_config(_C)
            kwargs.update(
                tokenizer=tokenizer,
                max_caption_length=_C.DATA.MAX_CAPTION_LENGTH,
                use_single_caption=_C.DATA.USE_SINGLE_CAPTION,
                percentage=_C.DATA.USE_PERCENTAGE if split == "train" else 100.0,
            )

        # Dataset names match with model names (and ofcourse pretext names).
        return cls.create(_C.MODEL.NAME, **kwargs)


class DownstreamDatasetFactory(Factory):
    r"""
    Factory to create :class:`~torch.utils.data.Dataset` s for evaluating
    VirTex models on downstream tasks.

    Possible choices: ``{"datasets/VOC2007", "datasets/imagenet"}``.
    """

    PRODUCTS: Dict[str, Callable] = {
        "datasets/VOC2007": vdata.VOC07ClassificationDataset,
        "datasets/imagenet": vdata.ImageNetDataset,
        "datasets/inaturalist": vdata.INaturalist2018Dataset,
    }

    @classmethod
    def from_config(cls, config: Config, split: str = "train"):
        r"""
        Create a dataset directly from config. Names in this factory are paths
        of dataset directories (relative to the project directory), because
        config parameter ``DATA.ROOT`` is used to create objects.

        Parameters
        ----------
        config: virtex.config.Config
            Config object with all the parameters.
        split: str, optional (default = "train")
            Which split to load for the dataset. One of ``{"trainval", "test"}``
            for VOC2007, or one of ``{"train", "val"}`` for ImageNet.
        """

        _C = config
        # Every dataset needs these two args.
        kwargs = {"data_root": _C.DATA.ROOT, "split": split}

        # For VOC2007, `IMAGE_TRANSFORM_TRAIN` is used for "trainval" split and
        # `IMAGE_TRANSFORM_VAL` is used fo "test" split.
        image_transform_names: List[str] = list(
            _C.DATA.IMAGE_TRANSFORM_TRAIN
            if "train" in split
            else _C.DATA.IMAGE_TRANSFORM_VAL
        )
        # Create a list of image transformations based on names.
        image_transform_list: List[Callable] = []

        for name in image_transform_names:
            # Pass dimensions for resize/crop, else rely on the defaults.
            if name in {"random_resized_crop", "center_crop", "global_resize"}:
                transform = ImageTransformsFactory.create(name, 224)
            elif name in {"smallest_resize"}:
                transform = ImageTransformsFactory.create(name, 256)
            else:
                transform = ImageTransformsFactory.create(name)

            image_transform_list.append(transform)

        kwargs["image_transform"] = alb.Compose(image_transform_list)

        return cls.create(_C.DATA.ROOT, **kwargs)


class VisualBackboneFactory(Factory):
    r"""
    Factory to create :mod:`~virtex.modules.visual_backbones`. This factory
    supports any ResNet-like model from
    `Torchvision <https://pytorch.org/docs/stable/torchvision/models.html>`_.
    Use the method name for model as in torchvision, for example,
    ``torchvision::resnet50``, ``torchvision::wide_resnet50_2`` etc.

    Possible choices: ``{"blind", "torchvision"}``.
    """

    PRODUCTS: Dict[str, Callable] = {
        "blind": visual_backbones.BlindVisualBackbone,
        "torchvision": visual_backbones.TorchvisionVisualBackbone,
    }

    @classmethod
    def from_config(cls, config: Config) -> visual_backbones.VisualBackbone:
        r"""
        Create a visual backbone directly from config.

        Parameters
        ----------
        config: virtex.config.Config
            Config object with all the parameters.
        """

        _C = config
        kwargs = {"visual_feature_size": _C.MODEL.VISUAL.FEATURE_SIZE}

        if "torchvision" in _C.MODEL.VISUAL.NAME:
            # Check the name for models from torchvision.
            cnn_name = _C.MODEL.VISUAL.NAME.split("::")[-1]
            kwargs["pretrained"] = _C.MODEL.VISUAL.PRETRAINED
            kwargs["frozen"] = _C.MODEL.VISUAL.FROZEN

            return cls.create("torchvision", cnn_name, **kwargs)
        else:
            return cls.create(_C.MODEL.VISUAL.NAME, **kwargs)


class TextualHeadFactory(Factory):
    r"""
    Factory to create :mod:`~virtex.modules.textual_heads`. Architectural
    hyperparameters for transformers can be specified as ``name::*``.
    For example, ``transformer_postnorm::L1_H1024_A16_F4096`` would create a
    transformer textual head with ``L = 1`` layers, ``H = 1024`` hidden size,
    ``A = 16`` attention heads, and ``F = 4096`` size of feedforward layers.

    Textual head should be ``"none"`` for pretraining tasks which do not
    involve language modeling, such as ``"token_classification"``.

    Possible choices: ``{"transformer_postnorm", "transformer_prenorm", "none"}``.
    """

    # fmt: off
    PRODUCTS: Dict[str, Callable] = {
        "transformer_prenorm": partial(textual_heads.TransformerTextualHead, norm_type="pre"),
        "transformer_postnorm": partial(textual_heads.TransformerTextualHead, norm_type="post"),
        "none": textual_heads.LinearTextualHead,
    }
    # fmt: on

    @classmethod
    def from_config(
        cls, config: Config, tokenizer: Optional[SentencePieceBPETokenizer] = None
    ) -> nn.Module:
        r"""
        Create a textual head directly from config.

        Parameters
        ----------
        config: virtex.config.Config
            Config object with all the parameters.
        tokenizer: virtex.data.tokenizers.SentencePieceBPETokenizer, optional (default = None)
            A tokenizer which has the mapping between word tokens and their
            integer IDs.
        """

        _C = config
        tokenizer = tokenizer or TokenizerFactory.from_config(_C)

        # Get architectural hyper-params as per name by matching regex.
        name, architecture = _C.MODEL.TEXTUAL.NAME.split("::")
        architecture = re.match(r"L(\d+)_H(\d+)_A(\d+)_F(\d+)", architecture)

        num_layers = int(architecture.group(1))
        hidden_size = int(architecture.group(2))
        attention_heads = int(architecture.group(3))
        feedforward_size = int(architecture.group(4))

        kwargs = {
            "vocab_size": tokenizer.get_vocab_size(),
            "hidden_size": hidden_size,
        }

        if "transformer" in _C.MODEL.TEXTUAL.NAME:
            kwargs.update(
                num_layers=num_layers,
                attention_heads=attention_heads,
                feedforward_size=feedforward_size,
                dropout=_C.MODEL.TEXTUAL.DROPOUT,
                padding_idx=tokenizer.token_to_id("[UNK]"),
                max_caption_length=_C.DATA.MAX_CAPTION_LENGTH,
            )
        return cls.create(name, **kwargs)


class PretrainingModelFactory(Factory):
    r"""
    Factory to create :mod:`~virtex.models` for different pretraining tasks.

    Possible choices: ``{"captioning", "bicaptioning", "token_classification",
    "multilabel_classification"}``.
    """

    PRODUCTS: Dict[str, Callable] = {
        "captioning": vmodels.ForwardCaptioningModel,
        "bicaptioning": vmodels.BidirectionalCaptioningModel,
        "token_classification": vmodels.TokenClassificationModel,
        "multilabel_classification": vmodels.MultiLabelClassificationModel,
    }

    @classmethod
    def from_config(
        cls, config: Config, tokenizer: Optional[SentencePieceBPETokenizer] = None
    ) -> nn.Module:
        r"""
        Create a model directly from config.

        Parameters
        ----------
        config: virtex.config.Config
            Config object with all the parameters.
        tokenizer: virtex.data.tokenizers.SentencePieceBPETokenizer, optional (default = None)
            A tokenizer which has the mapping between word tokens and their
            integer IDs.
        """

        _C = config
        tokenizer = tokenizer or TokenizerFactory.from_config(_C)

        if _C.MODEL.NAME == "multilabel_classification":
            # Pass a dummy tokenizer object to TextualHeadFactory for
            # `multilabel_classification`, which can return vocab size as `81`
            # (80 COCO categories + background).
            class DummyTokenizer(object):
                def get_vocab_size(self) -> int:
                    return 81

            tokenizer = DummyTokenizer()  # type: ignore

        # Build visual and textual streams based on config.
        visual = VisualBackboneFactory.from_config(_C)
        textual = TextualHeadFactory.from_config(_C, tokenizer)

        # Add model specific kwargs. Refer call signatures of specific models
        # for matching kwargs here.
        kwargs = {}
        if "captioning" in _C.MODEL.NAME:
            kwargs.update(
                max_decoding_steps=_C.DATA.MAX_CAPTION_LENGTH,
                sos_index=tokenizer.token_to_id("[SOS]"),
                eos_index=tokenizer.token_to_id("[EOS]"),
            )

        elif _C.MODEL.NAME == "token_classification":
            kwargs.update(
                ignore_indices=[
                    tokenizer.token_to_id("[UNK]"),
                    tokenizer.token_to_id("[SOS]"),
                    tokenizer.token_to_id("[EOS]"),
                    tokenizer.token_to_id("[MASK]"),
                ],
            )
        elif _C.MODEL.NAME == "multilabel_classification":
            kwargs.update(
                ignore_indices=[0],  # background index
            )

        return cls.create(_C.MODEL.NAME, visual, textual, **kwargs)


class OptimizerFactory(Factory):
    r"""Factory to create optimizers. Possible choices: ``{"sgd", "adamw"}``."""

    PRODUCTS: Dict[str, Callable] = {"sgd": optim.SGD, "adamw": optim.AdamW}

    @classmethod
    def from_config(
        cls, config: Config, named_parameters: Iterable[Any]
    ) -> optim.Optimizer:
        r"""
        Create an optimizer directly from config.

        Parameters
        ----------
        config: virtex.config.Config
            Config object with all the parameters.
        named_parameters: Iterable
            Named parameters of model (retrieved by ``model.named_parameters()``)
            for the optimizer. We use named parameters to set different LR and
            turn off weight decay for certain parameters based on their names.
        """

        _C = config

        # Set different learning rate for CNN and rest of the model during
        # pretraining. This doesn't matter for downstream evaluation because
        # there are no modules with "cnn" in their name.
        # Also turn off weight decay for layer norm and bias in textual stream.
        param_groups = []
        for name, param in named_parameters:
            wd = 0.0 if re.match(_C.OPTIM.NO_DECAY, name) else _C.OPTIM.WEIGHT_DECAY
            lr = _C.OPTIM.CNN_LR if "cnn" in name else _C.OPTIM.LR
            param_groups.append({"params": [param], "lr": lr, "weight_decay": wd})

        if _C.OPTIM.OPTIMIZER_NAME == "sgd":
            kwargs = {"momentum": _C.OPTIM.SGD_MOMENTUM}
        else:
            kwargs = {}

        optimizer = cls.create(_C.OPTIM.OPTIMIZER_NAME, param_groups, **kwargs)
        if _C.OPTIM.USE_LOOKAHEAD:
            optimizer = Lookahead(
                optimizer, k=_C.OPTIM.LOOKAHEAD_STEPS, alpha=_C.OPTIM.LOOKAHEAD_ALPHA
            )
        return optimizer


class LRSchedulerFactory(Factory):
    r"""
    Factory to create LR schedulers. All schedulers have a built-in LR warmup
    schedule before actual LR scheduling (decay) starts.

    Possible choices: ``{"none", "multistep", "linear", "cosine"}``.
    """

    PRODUCTS: Dict[str, Callable] = {
        "none": lr_scheduler.LinearWarmupNoDecayLR,
        "multistep": lr_scheduler.LinearWarmupMultiStepLR,
        "linear": lr_scheduler.LinearWarmupLinearDecayLR,
        "cosine": lr_scheduler.LinearWarmupCosineAnnealingLR,
    }

    @classmethod
    def from_config(
        cls, config: Config, optimizer: optim.Optimizer
    ) -> optim.lr_scheduler.LambdaLR:
        r"""
        Create an LR scheduler directly from config.

        Parameters
        ----------
        config: virtex.config.Config
            Config object with all the parameters.
        optimizer: torch.optim.Optimizer
            Optimizer on which LR scheduling would be performed.
        """

        _C = config
        kwargs = {
            "total_steps": _C.OPTIM.NUM_ITERATIONS,
            "warmup_steps": _C.OPTIM.WARMUP_STEPS,
        }
        # Multistep LR requires multiplicative factor and milestones.
        if _C.OPTIM.LR_DECAY_NAME == "multistep":
            kwargs.update(
                gamma=_C.OPTIM.LR_GAMMA,
                milestones=_C.OPTIM.LR_STEPS,
            )

        return cls.create(_C.OPTIM.LR_DECAY_NAME, optimizer, **kwargs)
