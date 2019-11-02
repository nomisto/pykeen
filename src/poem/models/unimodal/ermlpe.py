# -*- coding: utf-8 -*-

"""An implementation of the extension to ERMLP."""

import logging
from typing import Optional, Type

import torch
from torch import nn

from ..base import BaseModule
from ..init import embedding_xavier_normal_
from ...loss_functions import BCEAfterSigmoid
from ...triples import TriplesFactory
from ...typing import Loss

__all__ = ['ERMLPE']

log = logging.getLogger(__name__)


class ERMLPE(BaseModule):
    r"""An extension of ERMLP proposed by [sharifzadeh2019]_.

    This model uses a neural network-based approach similar to ERMLP and with slight modifications.
    In ERMLP, the model is:

    .. math::

        f(h, r, t) = \textbf{w}^{T} g(\textbf{W} [\textbf{h}; \textbf{r}; \textbf{t}])

    whereas in ERMPLE the model is:

    .. math::

        f(h, r, t) = \textbf{t}^{T} f(\textbf{W} (g(\textbf{W} [\textbf{h}; \textbf{r}]))

    including dropouts and batch-norms between each two hidden layers.
    ConvE can be seen as a special case of ERMLPE that contains the unnecessary inductive bias of convolutional
    filters. The aim of this model is to show that lifting this bias from ConvE (which simply leaves us with a
    modified ERMLP model), not only reduces the number of parameters but also improves performance.

    """

    hpo_default = dict(
        embedding_dim=dict(type=int, low=50, high=350, q=25),
        hidden_dim=dict(type=int, low=50, high=450, q=25),
        input_dropout=dict(type=float, low=0.0, high=0.8, q=0.1),
        hidden_dropout=dict(type=float, low=0.0, high=0.8, q=0.1),
    )

    criterion_default: Type[Loss] = BCEAfterSigmoid
    criterion_default_kwargs = {}

    def __init__(
        self,
        triples_factory: TriplesFactory,
        entity_embeddings: Optional[nn.Embedding] = None,
        relation_embeddings: Optional[nn.Embedding] = None,
        hidden_dim: int = 300,
        input_dropout: float = 0.2,
        hidden_dropout: float = 0.3,
        embedding_dim: int = 200,
        criterion: Optional[Loss] = None,
        preferred_device: Optional[str] = None,
        random_seed: Optional[int] = None,
        init: bool = True,
    ) -> None:
        super().__init__(
            triples_factory=triples_factory,
            embedding_dim=embedding_dim,
            entity_embeddings=entity_embeddings,
            criterion=criterion,
            preferred_device=preferred_device,
            random_seed=random_seed,
        )
        self.hidden_dim = hidden_dim
        self.input_dropout = input_dropout
        # Embeddings
        self.entity_embeddings = entity_embeddings
        self.relation_embeddings = relation_embeddings

        self.linear1 = nn.Linear(2 * self.embedding_dim, self.hidden_dim)
        self.linear2 = nn.Linear(self.hidden_dim, self.embedding_dim)
        self.input_dropout = nn.Dropout(self.input_dropout)
        self.mlp = nn.Sequential(
            self.linear1,
            nn.Dropout(hidden_dropout),
            nn.BatchNorm1d(self.hidden_dim),
            nn.ReLU(),
            self.linear2,
            nn.Dropout(hidden_dropout),
            nn.BatchNorm1d(self.embedding_dim),
            nn.ReLU()
        )

        if init:
            self.init_empty_weights_()

    def init_empty_weights_(self):  # noqa: D102
        if self.entity_embeddings is None:
            self.entity_embeddings = nn.Embedding(self.num_entities, self.embedding_dim)
            embedding_xavier_normal_(self.entity_embeddings)
        if self.relation_embeddings is None:
            self.relation_embeddings = nn.Embedding(self.num_relations, self.embedding_dim)
            embedding_xavier_normal_(self.relation_embeddings)

        return self

    def clear_weights_(self):  # noqa: D102
        self.entity_embeddings = None
        self.relation_embeddings = None
        return self

    def forward_owa(self, batch: torch.LongTensor) -> torch.FloatTensor:  # noqa: D102
        # Get embeddings
        h = self.entity_embeddings(batch[:, 0]).view(-1, self.embedding_dim)
        r = self.relation_embeddings(batch[:, 1]).view(-1, self.embedding_dim)
        t = self.entity_embeddings(batch[:, 2])

        # Concatenate them
        x_s = torch.cat([h, r], dim=-1)
        x_s = self.input_dropout(x_s)

        # Predict t embedding
        x_t = self.mlp(x_s)

        # compare with all t's
        # For efficient calculation, each of the calculated [h, r] rows has only to be multiplied with one t row
        x = (x_t.view(-1, self.embedding_dim) * t).sum(dim=1, keepdim=True)
        # The application of the sigmoid during training is automatically handled by the default criterion.

        return x

    def forward_cwa(self, batch: torch.LongTensor) -> torch.FloatTensor:  # noqa: D102
        h = self.entity_embeddings(batch[:, 0]).view(-1, self.embedding_dim)
        r = self.relation_embeddings(batch[:, 1]).view(-1, self.embedding_dim)
        t = self.entity_embeddings.weight.transpose(1, 0)

        # Concatenate them
        x_s = torch.cat([h, r], dim=-1)
        x_s = self.input_dropout(x_s)

        # Predict t embedding
        x_t = self.mlp(x_s)

        x = x_t @ t
        # The application of the sigmoid during training is automatically handled by the default criterion.

        return x

    def forward_inverse_cwa(self, batch: torch.LongTensor) -> torch.FloatTensor:  # noqa: D102
        h = self.entity_embeddings.weight
        r = self.relation_embeddings(batch[:, 0]).view(-1, self.embedding_dim)
        t = self.entity_embeddings(batch[:, 1]).view(-1, self.embedding_dim)

        batch_size = t.shape[0]

        # Extend each batch of "r" with shape [batch_size, dim] to [batch_size, dim * num_entities]
        r = torch.repeat_interleave(r, self.num_entities, dim=1).view(-1, self.embedding_dim)
        # Extend each h with shape [num_entities, dim] to [batch_size * num_entities, dim]
        h = torch.repeat_interleave(h, batch_size, dim=0)

        # Concatenate them
        x_s = torch.cat([h, r], dim=-1)
        x_s = self.input_dropout(x_s)

        # Predict t embedding
        x_t = self.mlp(x_s)

        x = x_t.view(batch_size, self.num_entities, self.embedding_dim) @ t.unsqueeze(dim=2)
        x = x.squeeze(dim=-1)
        # The application of the sigmoid during training is automatically handled by the default criterion.

        return x
