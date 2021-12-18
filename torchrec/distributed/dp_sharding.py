#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from typing import List, Optional, Dict, Any, Tuple

import torch
from torch.distributed._sharding_spec import ShardMetadata
from torchrec.distributed.embedding_lookup import (
    GroupedPooledEmbeddingsLookup,
    GroupedEmbeddingsLookup,
)
from torchrec.distributed.embedding_sharding import (
    EmbeddingSharding,
    group_tables,
    BasePooledEmbeddingDist,
    BaseSequenceEmbeddingDist,
    BaseSparseFeaturesDist,
    SequenceShardingContext,
    BaseEmbeddingLookup,
)
from torchrec.distributed.embedding_types import (
    GroupedEmbeddingConfig,
    SparseFeatures,
    ShardedEmbeddingTable,
    EmbeddingComputeKernel,
    BaseGroupedFeatureProcessor,
)
from torchrec.distributed.types import Awaitable, NoWait, ParameterSharding, ShardingEnv
from torchrec.modules.embedding_configs import EmbeddingTableConfig


class DpSparseFeaturesDist(BaseSparseFeaturesDist):
    """
    Distributes sparse features (input) to be data-parallel.

    """

    def __init__(self) -> None:
        super().__init__()

    def forward(
        self,
        sparse_features: SparseFeatures,
    ) -> Awaitable[SparseFeatures]:
        """
        No-op as sparse features are already distributed in data-parallel fashion.

        Call Args:
            sparse_features (SparseFeatures): input sparse features.

        Returns:
            Awaitable[SparseFeatures]: awaitable of SparseFeatures.

        """
        return NoWait(sparse_features)


class DpPooledEmbeddingDist(BasePooledEmbeddingDist):
    """
    Distributes pooled embeddings to be data-parallel.

    """

    def __init__(self) -> None:
        super().__init__()

    def forward(self, local_embs: torch.Tensor) -> Awaitable[torch.Tensor]:
        """
        No-op as pooled embeddings are already distributed in data-parallel fashion.

        Call Args:
            local_embs (torch.Tensor): output sequence embeddings.

        Returns:
            Awaitable[torch.Tensor]: awaitable of pooled embeddings tensor.

        """

        return NoWait(local_embs)


class DpSequenceEmbeddingDist(BaseSequenceEmbeddingDist):
    """
    Distributes sequence embeddings to be data-parallel.

    """

    def __init__(self) -> None:
        super().__init__()

    def forward(
        self, sharding_ctx: SequenceShardingContext, local_embs: torch.Tensor
    ) -> Awaitable[torch.Tensor]:
        """
        No-op as sequence embeddings are already distributed in data-parallel fashion.

        Call Args:
            local_embs (torch.Tensor): output sequence embeddings.

        Returns:
            Awaitable[torch.Tensor]: awaitable of pooled embeddings tensor.

        """

        return NoWait(local_embs)


class DpEmbeddingSharding(EmbeddingSharding):
    """
    Shards embedding bags using data-parallel, with no table sharding i.e.. a given
    embedding table is replicated across all ranks.

    """

    def __init__(
        self,
        embedding_configs: List[
            Tuple[EmbeddingTableConfig, ParameterSharding, torch.Tensor]
        ],
        env: ShardingEnv,
        device: Optional[torch.device] = None,
        is_sequence: bool = False,
    ) -> None:
        super().__init__()
        self._env = env
        self._device = device
        self._is_sequence = is_sequence
        sharded_tables_per_rank = self._shard(embedding_configs)
        self._grouped_embedding_configs_per_rank: List[
            List[GroupedEmbeddingConfig]
        ] = []
        self._score_grouped_embedding_configs_per_rank: List[
            List[GroupedEmbeddingConfig]
        ] = []
        (
            self._grouped_embedding_configs_per_rank,
            self._score_grouped_embedding_configs_per_rank,
        ) = group_tables(sharded_tables_per_rank)
        self._grouped_embedding_configs: List[
            GroupedEmbeddingConfig
        ] = self._grouped_embedding_configs_per_rank[env.rank]
        self._score_grouped_embedding_configs: List[
            GroupedEmbeddingConfig
        ] = self._score_grouped_embedding_configs_per_rank[env.rank]

    def _shard(
        self,
        embedding_configs: List[
            Tuple[EmbeddingTableConfig, ParameterSharding, torch.Tensor]
        ],
    ) -> List[List[ShardedEmbeddingTable]]:
        world_size = self._env.world_size
        tables_per_rank: List[List[ShardedEmbeddingTable]] = [
            [] for i in range(world_size)
        ]
        for config in embedding_configs:
            for rank in range(world_size):
                tables_per_rank[rank].append(
                    ShardedEmbeddingTable(
                        num_embeddings=config[0].num_embeddings,
                        embedding_dim=config[0].embedding_dim,
                        name=config[0].name,
                        embedding_names=config[0].embedding_names,
                        data_type=config[0].data_type,
                        feature_names=config[0].feature_names,
                        pooling=config[0].pooling,
                        is_weighted=config[0].is_weighted,
                        has_feature_processor=config[0].has_feature_processor,
                        local_rows=config[2].size(0),
                        local_cols=config[2].size(1),
                        compute_kernel=EmbeddingComputeKernel(config[1].compute_kernel),
                        local_metadata=None,
                        global_metadata=None,
                        weight_init_max=config[0].weight_init_max,
                        weight_init_min=config[0].weight_init_min,
                    )
                )
        return tables_per_rank

    def create_input_dist(self) -> DpSparseFeaturesDist:
        return DpSparseFeaturesDist()

    def create_lookup(
        self,
        fused_params: Optional[Dict[str, Any]],
        feature_processor: Optional[BaseGroupedFeatureProcessor] = None,
    ) -> BaseEmbeddingLookup:
        if self._is_sequence:
            return GroupedEmbeddingsLookup(
                grouped_configs=self._grouped_embedding_configs,
                fused_params=fused_params,
                pg=self._env.process_group,
                device=self._device,
            )
        else:
            return GroupedPooledEmbeddingsLookup(
                grouped_configs=self._grouped_embedding_configs,
                grouped_score_configs=self._score_grouped_embedding_configs,
                fused_params=fused_params,
                pg=self._env.process_group,
                device=self._device,
                feature_processor=feature_processor,
            )

    def create_pooled_output_dist(self) -> DpPooledEmbeddingDist:
        return DpPooledEmbeddingDist()

    def create_sequence_output_dist(self) -> DpSequenceEmbeddingDist:
        return DpSequenceEmbeddingDist()

    def embedding_dims(self) -> List[int]:
        embedding_dims = []
        for grouped_config in self._grouped_embedding_configs:
            embedding_dims.extend(grouped_config.embedding_dims())
        for grouped_config in self._score_grouped_embedding_configs:
            embedding_dims.extend(grouped_config.embedding_dims())
        return embedding_dims

    def embedding_names(self) -> List[str]:
        embedding_names = []
        for grouped_config in self._grouped_embedding_configs:
            embedding_names.extend(grouped_config.embedding_names())
        for grouped_config in self._score_grouped_embedding_configs:
            embedding_names.extend(grouped_config.embedding_names())
        return embedding_names

    def embedding_shard_metadata(self) -> List[Optional[ShardMetadata]]:
        embedding_shard_metadata = []
        for grouped_config in self._grouped_embedding_configs:
            embedding_shard_metadata.extend(grouped_config.embedding_shard_metadata())
        for grouped_config in self._score_grouped_embedding_configs:
            embedding_shard_metadata.extend(grouped_config.embedding_shard_metadata())
        return embedding_shard_metadata

    def id_list_feature_names(self) -> List[str]:
        id_list_feature_names = []
        for grouped_config in self._grouped_embedding_configs:
            id_list_feature_names.extend(grouped_config.feature_names())
        return id_list_feature_names

    def id_score_list_feature_names(self) -> List[str]:
        id_score_list_feature_names = []
        for grouped_config in self._score_grouped_embedding_configs:
            id_score_list_feature_names.extend(grouped_config.feature_names())
        return id_score_list_feature_names