# -*- coding: utf-8 -*-

"""Implementation of ranked based evaluator."""

from abc import abstractmethod
import itertools as itt
import logging
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, fields
from typing import (
    ClassVar,
    Collection,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    NamedTuple,
    Optional,
    Sequence,
    SupportsFloat,
    Tuple,
    Union,
    cast,
)

import numpy as np
import pandas as pd
import torch
from scipy import stats
from typing_extensions import Literal
from class_resolver import Resolver

from .evaluator import Evaluator, MetricResults, prepare_filter_triples
from ..triples.triples_factory import CoreTriplesFactory
from ..typing import MappedTriples

__all__ = [
    "compute_rank_from_scores",
    "RankBasedEvaluator",
    "RankBasedMetricResults",
    "MetricKey",
    "resolve_metric_name",
    "metric_resolver",
]

logger = logging.getLogger(__name__)

# typing
Side = Literal["head", "tail"]
ExtendedSide = Union[Side, Literal["both"]]
# SIDE_HEAD, SIDE_TAIL = typing.get_args(Side) # Python >= 3.8
SIDE_HEAD: Side = "head"
SIDE_TAIL: Side = "tail"
SIDE_BOTH: ExtendedSide = "both"

# REAL_SIDES: Tuple[Side, ...] = typing.get_args(Side)  # Python >= 3.8
REAL_SIDES: Tuple[Side, ...] = (SIDE_HEAD, SIDE_TAIL)
SIDES: Tuple[ExtendedSide, ...] = cast(Tuple[ExtendedSide, ...], REAL_SIDES) + (SIDE_BOTH,)

RankType = Literal["optimistic", "realistic", "pessimistic"]
# RANK_TYPES: Tuple[RankType, ...] = typing.get_args(RankType) # Python >= 3.8
RANK_TYPES: Tuple[RankType, ...] = ("optimistic", "realistic", "pessimistic")
RANK_OPTIMISTIC, RANK_REALISTIC, RANK_PESSIMISTIC = RANK_TYPES


@dataclass
class ValueRange:
    """A value range description."""

    #: the lower bound
    lower: Optional[float] = None

    #: whether the lower bound is inclusive
    lower_inclusive: bool = False

    #: the upper bound
    upper: Optional[float] = None

    #: whether the upper bound is inclusive
    upper_inclusive: bool = False

    def __contains__(self, x: SupportsFloat) -> bool:
        if self.lower is not None:
            if x < self.lower:
                return False
            if not self.lower_inclusive and x == self.lower:
                return False
        if self.upper is not None:
            if x > self.upper:
                return False
            if not self.upper_inclusive and x == self.upper:
                return False
        return True


class RankBasedMetric:
    """A base class for rank-based metrics."""

    # TODO: verify interpretation
    #: whether it is increasing, i.e., larger values are better
    increasing: ClassVar[bool] = False

    #: the value range (as string)
    value_range: ClassVar[Optional[ValueRange]] = None

    #: the supported rank types. Most of the time equal to all rank types
    supported_rank_types: ClassVar[Collection[RankType]] = RANK_TYPES

    #: synonyms for this metric
    synonyms: ClassVar[Collection[str]] = tuple()

    #: whether the metric requires the number of candidates for each ranking task
    needs_candidates: ClassVar[bool] = False

    @abstractmethod
    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:
        """
        Evaluate the metric.

        :param ranks: shape: s
            the individual ranks
        :param num_candidates: shape: s
            the number of candidates for each individual ranking task
        """
        raise NotImplementedError


class ArithmeticMeanRank(RankBasedMetric):
    """The (arithmetic) mean rank."""

    value_range = ValueRange(lower=1, lower_inclusive=True, upper=math.inf)
    synonyms = ("mean_rank", "mr")

    @staticmethod
    def call(ranks: np.ndarray) -> float:
        return np.mean(ranks).item()

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return ArithmeticMeanRank.call(ranks)


class InverseArithmeticMeanRank(RankBasedMetric):
    """The inverse arithmetic mean rank."""

    value_range = ValueRange(lower=0, lower_inclusive=False, upper=1, upper_inclusive=True)
    increasing = True

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return np.reciprocal(np.mean(ranks)).item()


class GeometricMeanRank(RankBasedMetric):
    """The geometric mean rank."""

    value_range = ValueRange(lower=1, lower_inclusive=True, upper=math.inf)
    synonyms = ("gmr",)

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return stats.gmean(ranks).item()


class InverseGeometricMeanRank(RankBasedMetric):
    """The inverse geometric mean rank."""

    value_range = ValueRange(lower=0, lower_inclusive=False, upper=1, upper_inclusive=True)
    increasing = True

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return np.reciprocal(stats.gmean(ranks)).item()


class HarmonicMeanRank(RankBasedMetric):
    """The harmonic mean rank."""

    value_range = ValueRange(lower=1, lower_inclusive=True, upper=math.inf)
    synonyms = ("hmr",)

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return stats.hmean(ranks).item()


class InverseHarmonicMeanRank(RankBasedMetric):
    """The inverse harmonic mean rank."""

    value_range = ValueRange(lower=0, lower_inclusive=False, upper=1, upper_inclusive=True)
    synonyms = ("mean_reciprocal_rank", "mrr")
    increasing = True

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return np.reciprocal(stats.hmean(ranks)).item()


class MedianRank(RankBasedMetric):
    """The median rank."""

    value_range = ValueRange(lower=1, lower_inclusive=True, upper=math.inf)

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return np.median(ranks).item()


class InverseMedianRank(RankBasedMetric):
    """The inverse median rank."""

    value_range = ValueRange(lower=0, lower_inclusive=False, upper=1, upper_inclusive=True)
    increasing = True

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return np.reciprocal(np.median(ranks)).item()


class StandardDeviation(RankBasedMetric):
    """The ranks' standard deviation."""

    value_range = ValueRange(lower=0, lower_inclusive=True, upper=math.inf)
    synonyms = ("rank_std", "std")

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return np.std(ranks).item()


class Variance(RankBasedMetric):
    """The ranks' variance."""

    value_range = ValueRange(lower=0, lower_inclusive=True, upper=math.inf)
    synonyms = ("rank_var", "var")

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return np.var(ranks).item()


class MedianAbsoluteDeviation(RankBasedMetric):
    """The ranks' median absolute deviation (MAD)."""

    value_range = ValueRange(lower=0, lower_inclusive=True, upper=math.inf)
    synonyms = ("rank_mad", "mad")

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return stats.median_absolute_deviation(ranks).item()


class Count(RankBasedMetric):
    """The ranks' count."""

    value_range = ValueRange(lower=0, lower_inclusive=True, upper=math.inf)
    increasing = True

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return float(ranks.size)


class HitsAtK(RankBasedMetric):
    """The Hits@k."""

    value_range = ValueRange(lower=0, lower_inclusive=True, upper=1, upper_inclusive=True)
    synonyms = ("H@k", "Hits@k")
    increasing = True

    def __init__(self, k: int = 10) -> None:
        super().__init__()
        self.k = k

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return np.less_equal(ranks, self.k).mean().item()


class AdjustedArithmeticMeanRank(RankBasedMetric):
    """The adjusted arithmetic mean rank (AMR)."""

    value_range = ValueRange(lower=0, lower_inclusive=True, upper=2, upper_inclusive=False)
    synonyms = ("adjusted_mean_rank", "amr", "aamr")
    supported_rank_types = (RANK_REALISTIC,)
    needs_candidates = True

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return (ArithmeticMeanRank.call(ranks) / expected_mean_rank(num_candidates=num_candidates)).item()


class AdjustedArithmeticMeanRankIndex(RankBasedMetric):
    """The adjusted arithmetic mean rank index (AMRI)."""

    value_range = ValueRange(lower=-1, lower_inclusive=True, upper=1, upper_inclusive=True)
    synonyms = ("adjusted_mean_rank_index", "amri", "aamri")
    increasing = True
    supported_rank_types = (RANK_REALISTIC,)
    needs_candidates = True

    def __call__(self, ranks: np.ndarray, num_candidates: Optional[np.ndarray] = None) -> float:  # noqa: D102
        return (
            1.0
            - (
                (ArithmeticMeanRank.call(ranks) - 1.0) / (expected_mean_rank(num_candidates=num_candidates) - 1.0)
            ).item()
        )


metric_resolver = Resolver.from_subclasses(
    base=RankBasedMetric,
    default=InverseArithmeticMeanRank,  # mrr
)

# TODO: use function resolver
# ARITHMETIC_MEAN_RANK = "arithmetic_mean_rank"  # also known as mean rank (MR)
# GEOMETRIC_MEAN_RANK = "geometric_mean_rank"
# HARMONIC_MEAN_RANK = "harmonic_mean_rank"
# MEDIAN_RANK = "median_rank"
INVERSE_ARITHMETIC_MEAN_RANK = "inverse_arithmetic_mean_rank"
INVERSE_GEOMETRIC_MEAN_RANK = "inverse_geometric_mean_rank"
INVERSE_HARMONIC_MEAN_RANK = "inverse_harmonic_mean_rank"  # also known as mean reciprocal rank (MRR)
INVERSE_MEDIAN_RANK = "inverse_median_rank"

RANK_STD = "rank_std"
RANK_VARIANCE = "rank_var"
RANK_MAD = "rank_mad"
RANK_COUNT = "rank_count"


# TODO: adjusted metrics
ADJUSTED_ARITHMETIC_MEAN_RANK = "adjusted_arithmetic_mean_rank"
ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX = "adjusted_arithmetic_mean_rank_index"
TYPES_REALISTIC_ONLY = {ADJUSTED_ARITHMETIC_MEAN_RANK, ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX}
METRIC_SYNONYMS = {
    "adjusted_mean_rank": ADJUSTED_ARITHMETIC_MEAN_RANK,
    "adjusted_mean_rank_index": ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX,
    "amr": ADJUSTED_ARITHMETIC_MEAN_RANK,
    "aamr": ADJUSTED_ARITHMETIC_MEAN_RANK,
    "amri": ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX,
    "aamri": ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX,
}


class MetricKey(NamedTuple):
    """A key for the kind of metric to resolve."""

    name: str
    side: ExtendedSide
    rank_type: RankType
    k: Optional[int]

    def __str__(self) -> str:  # noqa: D105
        components = [self.name, self.side, self.rank_type]
        if self.k:
            components.append(str(self.k))
        return ".".join(components)


@dataclass
class Ranks:
    """Ranks for different ranking types."""

    #: The optimistic rank is the rank when assuming all options with an equal score are placed
    #: behind the current test triple.
    #: shape: (batch_size,)
    optimistic: torch.FloatTensor

    #: The realistic rank is the average of the optimistic and pessimistic rank, and hence the expected rank
    #: over all permutations of the elements with the same score as the currently considered option.
    #: shape: (batch_size,)
    realistic: torch.FloatTensor

    #: The pessimistic rank is the rank when assuming all options with an equal score are placed
    #: in front of current test triple.
    #: shape: (batch_size,)
    pessimistic: torch.FloatTensor

    #: The number of options is the number of items considered in the ranking. It may change for
    #: filtered evaluation
    #: shape: (batch_size,)
    number_of_options: torch.LongTensor

    def to_type_dict(self) -> Mapping[RankType, torch.FloatTensor]:
        """Return mapping from rank-type to rank value tensor."""
        return {
            RANK_OPTIMISTIC: self.optimistic,
            RANK_REALISTIC: self.realistic,
            RANK_PESSIMISTIC: self.pessimistic,
        }


def compute_rank_from_scores(
    true_score: torch.FloatTensor,
    all_scores: torch.FloatTensor,
) -> Ranks:
    """Compute ranks given scores.

    :param true_score: torch.Tensor, shape: (batch_size, 1)
        The score of the true triple.
    :param all_scores: torch.Tensor, shape: (batch_size, num_entities)
        The scores of all corrupted triples (including the true triple).

    :return:
        a data structure containing the (filtered) ranks.
    """
    # The optimistic rank is the rank when assuming all options with an equal score are placed behind the currently
    # considered. Hence, the rank is the number of options with better scores, plus one, as the rank is one-based.
    optimistic_rank = (all_scores > true_score).sum(dim=1) + 1

    # The pessimistic rank is the rank when assuming all options with an equal score are placed in front of the
    # currently considered. Hence, the rank is the number of options which have at least the same score minus one
    # (as the currently considered option in included in all options). As the rank is one-based, we have to add 1,
    # which nullifies the "minus 1" from before.
    pessimistic_rank = (all_scores >= true_score).sum(dim=1)

    # The realistic rank is the average of the optimistic and pessimistic rank, and hence the expected rank over
    # all permutations of the elements with the same score as the currently considered option.
    realistic_rank = (optimistic_rank + pessimistic_rank).float() * 0.5

    # We set values which should be ignored to NaN, hence the number of options which should be considered is given by
    number_of_options = torch.isfinite(all_scores).sum(dim=1)

    return Ranks(
        optimistic=optimistic_rank,
        realistic=realistic_rank,
        pessimistic=pessimistic_rank,
        number_of_options=number_of_options,
    )


RANK_TYPE_SYNONYMS: Mapping[str, RankType] = {
    "best": RANK_OPTIMISTIC,
    "worst": RANK_PESSIMISTIC,
    "avg": RANK_REALISTIC,
    "average": RANK_REALISTIC,
}

_SIDE_PATTERN = "|".join(SIDES)
_TYPE_PATTERN = "|".join(itt.chain(RANK_TYPES, RANK_TYPE_SYNONYMS.keys()))
METRIC_PATTERN = re.compile(
    rf"(?P<name>[\w@]+)(\.(?P<side>{_SIDE_PATTERN}))?(\.(?P<type>{_TYPE_PATTERN}))?(\.(?P<k>\d+))?",
)
HITS_PATTERN = re.compile(r"(hits_at_|hits@|h@)(?P<k>\d+)")


def resolve_metric_name(name: str) -> MetricKey:
    """Functional metric name normalization."""
    match = METRIC_PATTERN.match(name)
    if not match:
        raise ValueError(f"Invalid metric name: {name}")
    side: Union[str, ExtendedSide]
    rank_type: Union[str, RankType]
    k: Union[None, str, int]
    name, side, rank_type, k = [match.group(key) for key in ("name", "side", "type", "k")]

    # normalize metric name
    if not name:
        raise ValueError("A metric name must be provided.")
    # handle spaces and case
    name = name.lower().replace(" ", "_")

    # special case for hits_at_k
    match = HITS_PATTERN.match(name)
    if match:
        name = "hits_at_k"
        k = match.group("k")
    if name == "hits_at_k":
        if k is None:
            k = 10
        # TODO: Fractional?
        try:
            k = int(k)
        except ValueError as error:
            raise ValueError(f"Invalid k={k} for hits_at_k") from error
        if k < 0:
            raise ValueError(f"For hits_at_k, you must provide a positive value of k, but found {k}.")
    assert k is None or isinstance(k, int)

    # synonym normalization
    name = METRIC_SYNONYMS.get(name, name)

    # normalize side
    side = side or SIDE_BOTH
    side = side.lower()
    if side not in SIDES:
        raise ValueError(f"Invalid side: {side}. Allowed are {SIDES}.")

    # normalize rank type
    rank_type = rank_type or RANK_REALISTIC
    rank_type = rank_type.lower()
    rank_type = RANK_TYPE_SYNONYMS.get(rank_type, rank_type)
    if rank_type not in RANK_TYPES:
        raise ValueError(f"Invalid rank type: {rank_type}. Allowed are {RANK_TYPES}.")
    if rank_type != RANK_REALISTIC and name in TYPES_REALISTIC_ONLY:
        raise ValueError(f"Invalid rank type for {name}: {rank_type}. Allowed type: {RANK_REALISTIC}")

    return MetricKey(name, side, rank_type, k)  # type: ignore


class RankBasedMetricResults(MetricResults):
    """Results from computing metrics."""

    def __init__(self, results: Mapping[Tuple[str, ExtendedSide, RankType], float]):
        """Initialize the results."""
        self.results = results

    def get_metric(self, name: str) -> float:
        """Get the rank-based metric.

        :param name: The name of the metric, created by concatenating three parts:

            1. The side (one of "head", "tail", or "both"). Most publications exclusively report "both".
            2. The type (one of "optimistic", "pessimistic", "realistic")
            3. The metric name ("adjusted_mean_rank_index", "adjusted_mean_rank", "mean_rank, "mean_reciprocal_rank",
               "inverse_geometric_mean_rank",
               or "hits@k" where k defaults to 10 but can be substituted for an integer. By default, 1, 3, 5, and 10
               are available. Other K's can be calculated by setting the appropriate variable in the
               ``evaluation_kwargs`` in the :func:`pykeen.pipeline.pipeline` or setting ``ks`` in the
               :class:`pykeen.evaluation.RankBasedEvaluator`.

            In general, all metrics are available for all combinations of sides/types except AMR and AMRI, which
            are only calculated for the average type. This is because the calculation of the expected MR in the
            optimistic and pessimistic case scenarios is still an active area of research and therefore has no
            implementation yet.
        :return: The value for the metric
        :raises ValueError: if an invalid name is given.

        Get the average MR

        >>> metric_results.get('both.realistic.mean_rank')

        If you only give a metric name, it assumes that it's for "both" sides and "realistic" type.

        >>> metric_results.get('adjusted_mean_rank_index')

        This function will do its best to infer what's going on if you only specify one part.

        >>> metric_results.get('left.mean_rank')
        >>> metric_results.get('optimistic.mean_rank')

        Get the default Hits @ K (where $k=10$)

        >>> metric_results.get('hits@k')

        Get a given Hits @ K

        >>> metric_results.get('hits@5')
        """
        metric, side, rank_type, k = resolve_metric_name(name)
        if not metric.startswith("hits"):
            return self.results[metric, side, rank_type]
        raise NotImplementedError

    def to_flat_dict(self):  # noqa: D102
        return {f"{side}.{rank_type}.{metric_name}": value for side, rank_type, metric_name, value in self._iter_rows()}

    def to_df(self) -> pd.DataFrame:
        """Output the metrics as a pandas dataframe."""
        return pd.DataFrame(list(self._iter_rows()), columns=["Side", "Type", "Metric", "Value"])

    def _iter_rows(self) -> Iterable[Tuple[ExtendedSide, RankType, str, Union[float, int]]]:
        for side, rank_type in itt.product(SIDES, RANK_TYPES):
            for k, v in self.hits_at_k[side][rank_type].items():
                yield side, rank_type, f"hits_at_{k}", v
            for f in fields(self):
                if f.name == "hits_at_k":
                    continue
                side_data = getattr(self, f.name)[side]
                if rank_type in side_data:
                    yield side, rank_type, f.name, side_data[rank_type]


class RankBasedEvaluator(Evaluator):
    """A rank-based evaluator for KGE models."""

    #: the actual rank data
    ranks: MutableMapping[RankType, MutableMapping[Side, List[np.ndarray]]]

    #: the number of choices for each ranking task; relevant for expected metrics
    number_of_options: MutableMapping[Side, List[np.ndarray]]

    #: the rank-based metrics to compute
    metrics: Mapping[str, RankBasedMetric]

    def __init__(
        self,
        ks: Optional[Iterable[Union[int, float]]] = None,
        filtered: bool = True,
        **kwargs,
    ):
        r"""Initialize rank-based evaluator.

        :param ks:
            The values for which to calculate hits@k. Defaults to $\{1, 3, 5, 10\}$.
        :param filtered:
            Whether to use the filtered evaluation protocol. If enabled, ranking another true triple higher than the
            currently considered one will not decrease the score.
        :param kwargs: Additional keyword arguments that are passed to the base class.
        """
        super().__init__(
            filtered=filtered,
            requires_positive_mask=False,
            **kwargs,
        )
        ks = tuple(ks) if ks is not None else (1, 3, 5, 10)
        for k in ks:
            if isinstance(k, float) and not (0 < k < 1):
                raise ValueError(
                    "If k is a float, it should represent a relative rank, i.e. a value between 0 and 1 (excl.)",
                )
        metrics = [
            metric_resolver.make(query=query) for query in metric_resolver.lookup_dict.values() if query is not HitsAtK
        ] + [metric_resolver.make(HitsAtK, k=k) for k in ks]
        self.metrics = {metric_resolver.normalize_inst(metric): metric for metric in metrics}
        self.ranks = {rank_type: {side: [] for side in REAL_SIDES} for rank_type in RANK_TYPES}
        self.number_of_options = defaultdict(list)

    def _update_ranks_(
        self,
        true_scores: torch.FloatTensor,
        all_scores: torch.FloatTensor,
        side: Side,
        hrt_batch: MappedTriples,
    ) -> None:
        """Shared code for updating the stored ranks for head/tail scores.

        :param true_scores: shape: (batch_size,)
        :param all_scores: shape: (batch_size, num_entities)
        """
        batch_ranks = compute_rank_from_scores(
            true_score=true_scores,
            all_scores=all_scores,
        )
        for rank_type, ranks in batch_ranks.to_type_dict().items():
            self.ranks[rank_type][side].append(ranks.detach().cpu().numpy())
        self.number_of_options[side].append(batch_ranks.number_of_options.detach().cpu().numpy())

    def process_tail_scores_(
        self,
        hrt_batch: MappedTriples,
        true_scores: torch.FloatTensor,
        scores: torch.FloatTensor,
        dense_positive_mask: Optional[torch.FloatTensor] = None,
    ) -> None:  # noqa: D102
        self._update_ranks_(true_scores=true_scores, all_scores=scores, side=SIDE_TAIL, hrt_batch=hrt_batch)

    def process_head_scores_(
        self,
        hrt_batch: MappedTriples,
        true_scores: torch.FloatTensor,
        scores: torch.FloatTensor,
        dense_positive_mask: Optional[torch.FloatTensor] = None,
    ) -> None:  # noqa: D102
        self._update_ranks_(true_scores=true_scores, all_scores=scores, side=SIDE_HEAD, hrt_batch=hrt_batch)

    @staticmethod
    def _get_for_side(
        mapping: Mapping[Side, List[np.ndarray]],
        side: ExtendedSide,
    ) -> np.ndarray:
        values: List[np.ndarray]
        if side in REAL_SIDES:
            values = mapping.get(side, [])  # type: ignore
            return np.concatenate(values).astype(dtype=np.float64)
        assert side == SIDE_BOTH
        return np.concatenate([RankBasedEvaluator._get_for_side(mapping=mapping, side=_side) for _side in REAL_SIDES])

    def finalize(self) -> RankBasedMetricResults:  # noqa: D102
        result: MutableMapping[Tuple[str, ExtendedSide, RankType], float] = dict()

        for side in SIDES:
            num_candidates = self._get_for_side(mapping=self.number_of_options, side=side)
            if len(num_candidates) < 1:
                logger.warning(f"No num_candidates for side={side}")
                continue
            for rank_type in RANK_TYPES:
                ranks = self._get_for_side(mapping=self.ranks[rank_type], side=side)
                if len(ranks) < 1:
                    logger.warning(f"No ranks for side={side}, rank_type={rank_type}")
                    continue
                for metric_name, metric in self.metrics.items():
                    if rank_type not in metric.supported_rank_types:
                        continue
                    result[(metric_name, side, rank_type)] = metric(ranks=ranks, num_candidates=num_candidates)
        # Clear buffers
        self.ranks.clear()
        self.number_of_options.clear()
        return RankBasedMetricResults(result)


def sample_negatives(
    evaluation_triples: MappedTriples,
    side: Side,
    additional_filter_triples: Union[None, MappedTriples, List[MappedTriples]] = None,
    num_samples: int = 50,
    max_id: Optional[int] = None,
) -> torch.LongTensor:
    """
    Sample true negatives for sampled evaluation.

    :param evaluation_triples: shape: (n, 3)
        the evaluation triples
    :param side:
        the side for which to generate negatives
    :param additional_filter_triples:
        additional true triples which are to be filtered
    :param num_samples: >0
        the number of samples
    :param max_id:
        the maximum Id for the given side

    :return: shape: (n, num_negatives)
        the negatives for the selected side prediction
    """
    additional_filter_triples = prepare_filter_triples(
        mapped_triples=evaluation_triples,
        additional_filter_triples=additional_filter_triples,
    )
    # TODO: update for relation
    max_id = max_id or (additional_filter_triples[:, [0, 2]].max().item() + 1)
    columns = ["head", "relation", "tail"]
    num_triples = evaluation_triples.shape[0]
    df = pd.DataFrame(data=evaluation_triples.numpy(), columns=columns)
    all_df = pd.DataFrame(data=additional_filter_triples.numpy(), columns=columns)
    id_df = df.reset_index()
    all_ids = set(range(max_id))
    this_negatives = torch.empty(size=(num_triples, num_samples), dtype=torch.long)
    other = [c for c in columns if c != side]
    group: pd.DataFrame
    for _, group in pd.merge(id_df, all_df, on=other, suffixes=["_eval", "_all"]).groupby(
        by=other,
    ):
        pool = list(all_ids.difference(group[f"{side}_all"].unique().tolist()))
        if len(pool) < num_samples:
            logger.warning(
                f"There are less than num_samples={num_samples} candidates for side={side}, triples={group}.",
            )
            # repeat
            pool = int(math.ceil(num_samples / len(pool))) * pool
        for i in group["index"].unique():
            this_negatives[i, :] = torch.as_tensor(
                data=random.sample(population=pool, k=num_samples),
                dtype=torch.long,
            )
    return this_negatives


class SampledRankBasedEvaluator(RankBasedEvaluator):
    """
    A rank-based evaluator using sampled negatives instead of all negatives, cf. [teru2020]_.

    Notice that this evaluator yields optimistic estimations of the metrics evaluated on all entities,
    cf. https://arxiv.org/abs/2106.06935.
    """

    #: the negative samples for each side
    negative_samples: Mapping[Side, torch.LongTensor]

    def __init__(
        self,
        evaluation_factory: CoreTriplesFactory,
        *,
        additional_filter_triples: Union[None, MappedTriples, List[MappedTriples]] = None,
        num_negatives: Optional[int] = None,
        negatives: Optional[Mapping[Side, Optional[torch.LongTensor]]] = None,
        **kwargs,
    ):
        """
        Initialize the evaluator.

        :param evaluation_factory:
            the factory with evaluation triples
        :param negatives: shape: (num_triples, num_negatives)
            the entity IDs of negative samples for head/tail prediction for each evaluation triple
        :param kwargs:
            additional keyword-based arguments passed to RankBasedEvaluator.__init__
        """
        super().__init__(**kwargs)
        if negatives is None:
            negatives = {side: None for side in REAL_SIDES}
        # make sure that negatives is mutable
        negatives = dict(negatives)
        for side in negatives.keys():
            # default for inductive LP by [teru2020]
            if negatives[side] is not None:
                continue
            logger.info(
                f"Sampling {num_negatives} negatives for each of the "
                f"{evaluation_factory.num_triples} evaluation triples.",
            )
            num_negatives = num_negatives or 50
            if num_negatives > evaluation_factory.num_entities:
                raise ValueError("Cannot use more negative samples than there are entities.")
            negatives[side] = sample_negatives(
                evaluation_triples=evaluation_factory.mapped_triples,
                side=side,
                additional_filter_triples=additional_filter_triples,
                max_id=evaluation_factory.num_entities,
                num_samples=num_negatives,
            )

        # verify input
        for side, side_negatives in negatives.items():
            assert side_negatives is not None
            if side_negatives.shape[0] != evaluation_factory.num_triples:
                raise ValueError(f"Negatives for side={side} are in wrong shape: {side_negatives.shape}")
        self.triple_to_index = {(h, r, t): i for i, (h, r, t) in enumerate(evaluation_factory.mapped_triples.tolist())}
        self.negative_samples = negatives
        self.num_entities = evaluation_factory.num_entities

    def _update_ranks_(
        self,
        true_scores: torch.FloatTensor,
        all_scores: torch.FloatTensor,
        side: Side,
        hrt_batch: MappedTriples,
    ) -> None:  # noqa: D102
        # TODO: do not require to compute all scores beforehand
        triple_indices = [self.triple_to_index[h, r, t] for h, r, t in hrt_batch.cpu().tolist()]
        negative_entity_ids = self.negative_samples[side][triple_indices]
        negative_scores = all_scores[
            torch.arange(hrt_batch.shape[0], device=hrt_batch.device).unsqueeze(dim=-1),
            negative_entity_ids,
        ]
        # super.evaluation assumes that the true scores are part of all_scores
        scores = torch.cat([true_scores, negative_scores], dim=-1)
        super()._update_ranks_(true_scores=true_scores, all_scores=scores, side=side, hrt_batch=hrt_batch)
        # write back correct num_entities
        # TODO: should we give num_entities in the constructor instead of inferring it every time ranks are processed?
        self.num_entities = all_scores.shape[1]


def numeric_expected_value(
    metric: str,
    num_candidates: Union[Sequence[int], np.ndarray],
    num_samples: int,
) -> float:
    """
    Compute expected metric value by summation.

    Depending on the metric, the estimate may not be very accurate and converage slowly, cf.
    https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.rv_discrete.expect.html
    """
    metric_func = metric_resolver.make(metric)
    num_candidates = np.asarray(num_candidates)
    generator = np.random.default_rng()
    expectation = 0
    for _ in range(num_samples):
        ranks = generator.integers(low=0, high=num_candidates)
        expectation += metric_func(ranks)
    return expectation / num_samples


# TODO: closed-forms for other metrics?


def expected_mean_rank(
    num_candidates: Union[Sequence[int], np.ndarray],
) -> float:
    r"""
    Calculate the expected mean rank under random ordering.

    .. math ::

        E[MR] = \frac{1}{n} \sum \limits_{i=1}^{n} \frac{1 + CSS[i]}{2}
              = \frac{1}{2}(1 + \frac{1}{n} \sum \limits_{i=1}^{n} CSS[i])

    :param num_candidates:
        the number of candidates for each individual rank computation

    :return:
        the expected mean rank
    """
    return 0.5 * (1 + np.mean(np.asanyarray(num_candidates)))


def expected_hits_at_k(
    num_candidates: Union[Sequence[int], np.ndarray],
    k: int,
) -> float:
    r"""
    Calculate the expected Hits@k under random ordering.

    .. math ::

        E[Hits@k] = \frac{1}{n} \sum \limits_{i=1}^{n} min(\frac{k}{CSS[i]}, 1.0)

    :param num_candidates:
        the number of candidates for each individual rank computation

    :return:
        the expected Hits@k value
    """
    return k * np.mean(np.reciprocal(np.asanyarray(num_candidates, dtype=float)).clip(min=None, max=1 / k))
