# -*- coding: utf-8 -*-

"""New-style base module for all KGE models."""

from __future__ import annotations

import logging
from abc import ABC
from collections import defaultdict
from operator import itemgetter
from typing import Any, ClassVar, Generic, Iterable, List, Mapping, Optional, Sequence, Tuple, Type, Union, cast

import torch
from torch import nn

from .base import Model
from ..nn.emb import EmbeddingSpecification, RepresentationModule
from ..nn.modules import Interaction, interaction_resolver
from ..regularizers import Regularizer
from ..triples import CoreTriplesFactory
from ..typing import HeadRepresentation, InductiveMode, RelationRepresentation, TailRepresentation
from ..utils import check_shapes

__all__ = [
    "_NewAbstractModel",
    "ERModel",
]

logger = logging.getLogger(__name__)

EmbeddingSpecificationHint = Union[
    None,
    EmbeddingSpecification,
    RepresentationModule,
    Sequence[Union[EmbeddingSpecification, RepresentationModule]],
]


class _NewAbstractModel(Model, ABC):
    """An abstract class for knowledge graph embedding models (KGEMs).

    The only function that needs to be implemented for a given subclass is
    :meth:`Model.forward`. The job of the :meth:`Model.forward` function, as
    opposed to the completely general :meth:`torch.nn.Module.forward` is
    to take indices for the head, relation, and tails' respective representation(s)
    and to determine a score.

    Subclasses of Model can decide however they want on how to store entities' and
    relations' representations, how they want to be looked up, and how they should
    be scored. The :class:`ERModel` provides a commonly useful implementation
    which allows for the specification of one or more entity representations and
    one or more relation representations in the form of :class:`pykeen.nn.Embedding`
    as well as a matching instance of a :class:`pykeen.nn.Interaction`.
    """

    #: The default regularizer class
    regularizer_default: ClassVar[Optional[Type[Regularizer]]] = None
    #: The default parameters for the default regularizer class
    regularizer_default_kwargs: ClassVar[Optional[Mapping[str, Any]]] = None

    can_slice_h = True
    can_slice_r = True
    can_slice_t = True

    def _reset_parameters_(self):  # noqa: D401
        """Reset all parameters of the model in-place."""
        # cf. https://github.com/mberr/ea-sota-comparison/blob/6debd076f93a329753d819ff4d01567a23053720/src/kgm/utils/torch_utils.py#L317-L372   # noqa:E501
        # Make sure that all modules with parameters do have a reset_parameters method.
        uninitialized_parameters = set(map(id, self.parameters()))
        parents = defaultdict(list)

        # Recursively visit all sub-modules
        task_list = []
        for name, module in self.named_modules():
            # skip self
            if module is self:
                continue

            # Track parents for blaming
            for p in module.parameters():
                parents[id(p)].append(module)

            # call reset_parameters if possible
            if hasattr(module, "reset_parameters"):
                task_list.append((name.count("."), module))

        # initialize from bottom to top
        # This ensures that specialized initializations will take priority over the default ones of its components.
        for module in map(itemgetter(1), sorted(task_list, reverse=True, key=itemgetter(0))):
            module.reset_parameters()
            uninitialized_parameters.difference_update(map(id, module.parameters()))

        # emit warning if there where parameters which were not initialised by reset_parameters.
        if len(uninitialized_parameters) > 0:
            logger.warning(
                "reset_parameters() not found for all modules containing parameters. "
                "%d parameters where likely not initialized.",
                len(uninitialized_parameters),
            )

            # Additional debug information
            for i, p_id in enumerate(uninitialized_parameters, start=1):
                logger.debug("[%3d] Parents to blame: %s", i, parents.get(p_id))

    def _instantiate_default_regularizer(self, **kwargs) -> Optional[Regularizer]:
        """Instantiate the regularizer from this class's default settings.

        :param kwargs: Additional keyword arguments to be passed through to the ``__init__()`` function of the
            default regularizer, if one is set.

        :returns: If the default regularizer is None, None is returned.
        """
        if self.regularizer_default is None:
            return None

        _kwargs = dict(self.regularizer_default_kwargs or {})
        _kwargs.update(kwargs)
        return self.regularizer_default(**_kwargs)

    def post_parameter_update(self) -> None:
        """Has to be called after each parameter update."""
        for module in self.modules():
            if module is self:
                continue
            if hasattr(module, "post_parameter_update"):
                module.post_parameter_update()

    def collect_regularization_term(self):  # noqa: D102
        return sum(
            regularizer.pop_regularization_term()
            for regularizer in self.modules()
            if isinstance(regularizer, Regularizer)
        )


def _prepare_representation_module_list(
    representations: EmbeddingSpecificationHint,
    num_embeddings: int,
    shapes: Sequence[str],
    label: str,
    skip_checks: bool = False,
) -> Sequence[RepresentationModule]:
    """Normalize list of representations and wrap into nn.ModuleList."""
    # Important: use ModuleList to ensure that Pytorch correctly handles their devices and parameters
    if representations is None:
        representations = []
    if not isinstance(representations, Sequence):
        representations = [representations]
    if not skip_checks and len(representations) != len(shapes):
        raise ValueError(
            f"Interaction function requires {len(shapes)} {label} representations, but "
            f"{len(representations)} were given.",
        )
    modules = []
    for r in representations:
        if not isinstance(r, RepresentationModule):
            assert isinstance(r, EmbeddingSpecification)
            r = r.make(num_embeddings=num_embeddings)
        if r.max_id < num_embeddings:
            raise ValueError(
                f"{r} only provides {r.max_id} {label} representations, but should provide {num_embeddings}.",
            )
        elif r.max_id > num_embeddings:
            logger.warning(
                f"{r} provides {r.max_id} {label} representations, although only {num_embeddings} are needed."
                f"While this is not necessarily wrong, it can indicate an error where the number of {label} "
                f"representations was chosen wrong.",
            )
        modules.append(r)
    if not skip_checks:
        check_shapes(
            *zip(
                (r.shape for r in modules),
                shapes,
            ),
            raise_on_errors=True,
        )
    return nn.ModuleList(modules)


def repeat_if_necessary(
    scores: torch.FloatTensor,
    representations: Sequence[RepresentationModule],
    num: int,
) -> torch.FloatTensor:
    """
    Repeat score tensor if necessary.

    If a model does not have entity/relation representations, the scores for
    `score_{h,t}` / `score_r` are always the same. For efficiency, they are thus
    only computed once, but to meet the API, they have to be brought into the correct shape afterwards.

    :param scores: shape: (batch_size, ?)
        the score tensor
    :param representations:
        the representations. If empty (i.e. no representations for this 1:n scoring), repetition needs to be applied
    :param num:
        the number of times to repeat, if necessary.

    :return:
        the score tensor, which has been repeated, if necessary
    """
    if representations:
        return scores
    return scores.repeat(1, num)


class ERModel(
    Generic[HeadRepresentation, RelationRepresentation, TailRepresentation],
    _NewAbstractModel,
):
    """A commonly useful base for KGEMs using embeddings and interaction modules.

    This model does not use post-init hooks to automatically initialize all of its
    parameters. Rather, the call to :func:`Model.reset_parameters_` happens at the end of
    ``ERModel.__init__``. This is possible because all trainable parameters should necessarily
    be passed through the ``super().__init__()`` in subclasses of :class:`ERModel`.

    Other code can still be put after the call to ``super().__init__()`` in subclasses, such as
    registering regularizers (as done in :class:`pykeen.models.ConvKB` and :class:`pykeen.models.TransH`).
    ---
    citation:
        author: Ali
        year: 2021
        link: https://jmlr.org/papers/v22/20-825.html
        github: pykeen/pykeen
    """

    #: The entity representations
    entity_representations: Sequence[RepresentationModule]

    #: The relation representations
    relation_representations: Sequence[RepresentationModule]

    #: The weight regularizers
    weight_regularizers: List[Regularizer]

    #: The interaction function
    interaction: Interaction

    def __init__(
        self,
        *,
        triples_factory: CoreTriplesFactory,
        interaction: Union[
            str,
            Interaction[HeadRepresentation, RelationRepresentation, TailRepresentation],
            Type[Interaction[HeadRepresentation, RelationRepresentation, TailRepresentation]],
        ],
        interaction_kwargs: Optional[Mapping[str, Any]] = None,
        entity_representations: EmbeddingSpecificationHint = None,
        relation_representations: EmbeddingSpecificationHint = None,
        skip_checks: bool = False,
        **kwargs,
    ) -> None:
        """Initialize the module.

        :param triples_factory:
            The triples factory facilitates access to the dataset.
        :param interaction: The interaction module (e.g., TransE)
        :param interaction_kwargs:
            Additional key-word based parameters given to the interaction module's constructor, if not already
            instantiated.
        :param entity_representations: The entity representation or sequence of representations
        :param relation_representations: The relation representation or sequence of representations
        :param skip_checks:
            whether to skip entity representation checks.
        :param kwargs:
            Keyword arguments to pass to the base model
        """
        super().__init__(triples_factory=triples_factory, **kwargs)
        self.interaction = interaction_resolver.make(interaction, pos_kwargs=interaction_kwargs)
        self.entity_representations = _prepare_representation_module_list(
            representations=entity_representations,
            num_embeddings=triples_factory.num_entities,
            shapes=self.interaction.entity_shape,
            label="entity",
            skip_checks=self.interaction.tail_entity_shape is not None or skip_checks,
        )
        self.relation_representations = _prepare_representation_module_list(
            representations=relation_representations,
            num_embeddings=triples_factory.num_relations,
            shapes=self.interaction.relation_shape,
            label="relation",
        )
        # Comment: it is important that the regularizers are stored in a module list, in order to appear in
        # model.modules(). Thereby, we can collect them automatically.
        self.weight_regularizers = nn.ModuleList()
        # Explicitly call reset_parameters to trigger initialization
        self.reset_parameters_()

    def append_weight_regularizer(
        self,
        parameter: Union[str, nn.Parameter, Iterable[Union[str, nn.Parameter]]],
        regularizer: Regularizer,
    ) -> None:
        """Add a model weight to a regularizer's weight list, and register the regularizer with the model.

        :param parameter:
            The parameter, either as name, or as nn.Parameter object. A list of available parameter names is shown by
             `sorted(dict(self.named_parameters()).keys())`.
        :param regularizer:
            The regularizer instance which will regularize the weights.

        :raises KeyError: If an invalid parameter name was given
        """
        # normalize input
        if isinstance(parameter, (str, nn.Parameter)):
            parameter = [parameter]
        weights: Mapping[str, nn.Parameter] = dict(self.named_parameters())
        for param in parameter:
            if isinstance(param, str):
                if param not in weights:
                    raise KeyError(f"Invalid parameter_name={parameter}. Available are: {sorted(weights.keys())}.")
                param: nn.Parameter = weights[param]  # type: ignore
            regularizer.add_parameter(parameter=param)
        self.weight_regularizers.append(regularizer)

    def forward(
        self,
        h_indices: torch.LongTensor,
        r_indices: torch.LongTensor,
        t_indices: torch.LongTensor,
        slice_size: Optional[int] = None,
        slice_dim: int = 0,
        *,
        mode: Optional[InductiveMode],
    ) -> torch.FloatTensor:
        """Forward pass.

        This method takes head, relation and tail indices and calculates the corresponding scores.
        It supports broadcasting.

        :param h_indices:
            The head indices.
        :param r_indices:
            The relation indices.
        :param t_indices:
            The tail indices.
        :param slice_size:
            The slice size.
        :param slice_dim:
            The dimension along which to slice
        :param mode:
            The pass mode, which is None in the transductive setting and one of "training",
            "validation", or "testing" in the inductive setting.

        :return:
            The scores

        :raises NotImplementedError:
            if score repetition becomes necessary
        """
        if not self.entity_representations or not self.relation_representations:
            raise NotImplementedError("repeat scores not implemented for general case.")
        h, r, t = self._get_representations(h=h_indices, r=r_indices, t=t_indices, mode=mode)
        return self.interaction.score(h=h, r=r, t=t, slice_size=slice_size, slice_dim=slice_dim)

    def score_hrt(self, hrt_batch: torch.LongTensor, *, mode: Optional[InductiveMode] = None) -> torch.FloatTensor:
        """Forward pass.

        This method takes head, relation and tail of each triple and calculates the corresponding score.

        :param hrt_batch: shape: (batch_size, 3), dtype: long
            The indices of (head, relation, tail) triples.
        :param mode:
            The pass mode, which is None in the transductive setting and one of "training",
            "validation", or "testing" in the inductive setting.

        :return: shape: (batch_size, 1), dtype: float
            The score for each triple.
        """
        # Note: slicing cannot be used here: the indices for score_hrt only have a batch
        # dimension, and slicing along this dimension is already considered by sub-batching.
        # Note: we do not delegate to the general method for performance reasons
        # Note: repetition is not necessary here
        h, r, t = self._get_representations(h=hrt_batch[:, 0], r=hrt_batch[:, 1], t=hrt_batch[:, 2], mode=mode)
        return self.interaction.score_hrt(h=h, r=r, t=t)

    def score_t(
        self, hr_batch: torch.LongTensor, *, slice_size: Optional[int] = None, mode: Optional[InductiveMode] = None
    ) -> torch.FloatTensor:
        """Forward pass using right side (tail) prediction.

        This method calculates the score for all possible tails for each (head, relation) pair.

        :param hr_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, relation) pairs.
        :param slice_size:
            The slice size.
        :param mode:
            The pass mode, which is None in the transductive setting and one of "training",
            "validation", or "testing" in the inductive setting.

        :return: shape: (batch_size, num_entities), dtype: float
            For each h-r pair, the scores for all possible tails.
        """
        h, r, t = self._get_representations(h=hr_batch[:, 0], r=hr_batch[:, 1], t=None, mode=mode)
        return repeat_if_necessary(
            scores=self.interaction.score_t(h=h, r=r, all_entities=t, slice_size=slice_size),
            representations=self.entity_representations,
            num=self.num_entities,
        )

    def score_h(
        self, rt_batch: torch.LongTensor, *, slice_size: Optional[int] = None, mode: Optional[InductiveMode] = None
    ) -> torch.FloatTensor:
        """Forward pass using left side (head) prediction.

        This method calculates the score for all possible heads for each (relation, tail) pair.

        :param rt_batch: shape: (batch_size, 2), dtype: long
            The indices of (relation, tail) pairs.
        :param slice_size:
            The slice size.
        :param mode:
            The pass mode, which is None in the transductive setting and one of "training",
            "validation", or "testing" in the inductive setting.

        :return: shape: (batch_size, num_entities), dtype: float
            For each r-t pair, the scores for all possible heads.
        """
        h, r, t = self._get_representations(h=None, r=rt_batch[:, 0], t=rt_batch[:, 1], mode=mode)
        return repeat_if_necessary(
            scores=self.interaction.score_h(all_entities=h, r=r, t=t, slice_size=slice_size),
            representations=self.entity_representations,
            num=self.num_entities,
        )

    def score_r(
        self, ht_batch: torch.LongTensor, *, slice_size: Optional[int] = None, mode: Optional[InductiveMode] = None
    ) -> torch.FloatTensor:
        """Forward pass using middle (relation) prediction.

        This method calculates the score for all possible relations for each (head, tail) pair.

        :param ht_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, tail) pairs.
        :param slice_size:
            The slice size.
        :param mode:
            The pass mode, which is None in the transductive setting and one of "training",
            "validation", or "testing" in the inductive setting.

        :return: shape: (batch_size, num_relations), dtype: float
            For each h-t pair, the scores for all possible relations.
        """
        h, r, t = self._get_representations(h=ht_batch[:, 0], r=None, t=ht_batch[:, 1], mode=mode)
        return repeat_if_necessary(
            scores=self.interaction.score_r(h=h, all_relations=r, t=t, slice_size=slice_size),
            representations=self.relation_representations,
            num=self.num_relations,
        )

    def _get_representations(
        self,
        h: Optional[torch.LongTensor],
        r: Optional[torch.LongTensor],
        t: Optional[torch.LongTensor],
        *,
        mode: Optional[InductiveMode],
    ) -> Tuple[HeadRepresentation, RelationRepresentation, TailRepresentation]:
        """Get representations for head, relation and tails."""
        hr, rr, tr = [
            [representation.forward_unique(indices=indices) for representation in representations]
            for indices, representations in (
                (h, self.entity_representations),
                (r, self.relation_representations),
                (t, self.entity_representations),
            )
        ]
        # normalization
        return cast(
            Tuple[HeadRepresentation, RelationRepresentation, TailRepresentation],
            tuple(x[0] if len(x) == 1 else x for x in (hr, rr, tr)),
        )
