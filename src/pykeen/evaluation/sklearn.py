# -*- coding: utf-8 -*-

"""Implementation of wrapper around sklearn metrics."""

from dataclasses import dataclass, field, fields, make_dataclass
from typing import Any, Dict, Optional, Tuple, Union

import numpy as np
import rexmex.metrics.classification as rmc
import torch
from dataclasses_json import dataclass_json
from rexmex.utils import binarize

from .evaluator import Evaluator, MetricResults
from ..typing import MappedTriples
from ..utils import fix_dataclass_init_docs

__all__ = [
    "SklearnEvaluator",
    "SklearnMetricResults",
]


def interval(func) -> str:
    """Get the math notation for the range of this metric."""
    left = "[" if func.lower_inclusive else "("
    right = "]" if func.upper_inclusive else ")"
    lower: Union[int, float]
    upper: Union[int, float]
    try:
        lower = int(func.lower)
    except OverflowError:
        lower = func.lower
        left = "("
    try:
        upper = int(func.upper)
    except OverflowError:
        upper = func.upper
        right = ")"
    return f"{left}{lower}, {upper}{right}"


#: Functions with the right signature in the :mod:`rexmex.metrics.classification` that are not themselves metrics
EXCLUDE_CLASSIFIERS = {
    rmc.pr_auc_score,
}

_fields = [
    (
        func.__name__,
        float,
        field(
            metadata=dict(
                name=func.name,
                doc=func.description or "",
                link=func.link,
                range=interval(func),
                increasing=func.higher_is_better,
                f=binarize(func) if func.binarize else func,
            )
        ),
    )
    for func in rmc.classifications
    if func not in EXCLUDE_CLASSIFIERS and func.duplicate_of is None
]

SklearnMetricResultsBase = make_dataclass(
    "SklearnMetricResultsBase",
    _fields,
    bases=(MetricResults,),
)


@fix_dataclass_init_docs
@dataclass_json
@dataclass
class SklearnMetricResults(SklearnMetricResultsBase):  # type: ignore
    """Results from computing metrics."""

    @classmethod
    def from_scores(cls, y_true, y_score):
        """Return an instance of these metrics from a given set of true and scores."""
        return SklearnMetricResults(**{f.name: f.metadata["f"](y_true, y_score) for f in fields(cls)})

    def get_metric(self, name: str) -> float:  # noqa: D102
        return getattr(self, name)


class SklearnEvaluator(Evaluator):
    """An evaluator that uses a Scikit-learn metric."""

    all_scores: Dict[Tuple[Any, ...], np.ndarray]
    all_positives: Dict[Tuple[Any, ...], np.ndarray]

    def __init__(self, **kwargs):
        super().__init__(
            filtered=False,
            requires_positive_mask=True,
            **kwargs,
        )
        self.all_scores = {}
        self.all_positives = {}

    def _process_scores(
        self,
        keys: torch.LongTensor,
        scores: torch.FloatTensor,
        positive_mask: torch.FloatTensor,
        head_side: bool,
    ) -> None:
        # Transfer to cpu and convert to numpy
        scores = scores.detach().cpu().numpy()
        positive_mask = positive_mask.detach().cpu().numpy()
        keys = keys.detach().cpu().numpy()

        # Ensure that each key gets counted only once
        for i in range(keys.shape[0]):
            # include head_side flag into key to differentiate between (h, r) and (r, t)
            key = (head_side,) + tuple(map(int, keys[i]))
            self.all_scores[key] = scores[i]
            self.all_positives[key] = positive_mask[i]

    def process_tail_scores_(
        self,
        hrt_batch: MappedTriples,
        true_scores: torch.FloatTensor,
        scores: torch.FloatTensor,
        dense_positive_mask: Optional[torch.FloatTensor] = None,
    ) -> None:  # noqa: D102
        if dense_positive_mask is None:
            raise KeyError("Sklearn evaluators need the positive mask!")

        self._process_scores(keys=hrt_batch[:, :2], scores=scores, positive_mask=dense_positive_mask, head_side=False)

    def process_head_scores_(
        self,
        hrt_batch: MappedTriples,
        true_scores: torch.FloatTensor,
        scores: torch.FloatTensor,
        dense_positive_mask: Optional[torch.FloatTensor] = None,
    ) -> None:  # noqa: D102
        if dense_positive_mask is None:
            raise KeyError("Sklearn evaluators need the positive mask!")

        self._process_scores(keys=hrt_batch[:, 1:], scores=scores, positive_mask=dense_positive_mask, head_side=True)

    def finalize(self) -> SklearnMetricResults:  # noqa: D102
        # Important: The order of the values of an dictionary is not guaranteed. Hence, we need to retrieve scores and
        # masks using the exact same key order.
        all_keys = list(self.all_scores.keys())
        # TODO how to define a cutoff on y_scores to make binary?
        # see: https://github.com/xptree/NetMF/blob/77286b826c4af149055237cef65e2a500e15631a/predict.py#L25-L33
        y_score = np.concatenate([self.all_scores[k] for k in all_keys], axis=0).flatten()
        y_true = np.concatenate([self.all_positives[k] for k in all_keys], axis=0).flatten()

        # Clear buffers
        self.all_positives.clear()
        self.all_scores.clear()

        return SklearnMetricResults.from_scores(y_true, y_score)
