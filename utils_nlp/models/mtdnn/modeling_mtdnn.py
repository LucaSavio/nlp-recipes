# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.


# This script reuses some code from
# https://github.com/huggingface/transformers

import logging
import os

import torch
from torch import nn
from transformers import (
    BertConfig,
    BertModel,
    BertPreTrainedModel,
    PretrainedConfig,
    PreTrainedModel,
    RobertaModel,
)

from fairseq.models.roberta import RobertaModel as FairseqRobertModel
from utils_nlp.dataset.url_utils import maybe_download
from utils_nlp.models.mtdnn.common.average_meter import AverageMeter
from utils_nlp.models.mtdnn.common.linear_pooler import LinearPooler
from utils_nlp.models.mtdnn.common.types import DataFormat, EncoderModelType, TaskType
from utils_nlp.models.mtdnn.configuration_mtdnn import MTDNNConfig
from utils_nlp.models.mtdnn.common.archive_maps import PRETRAINED_MODEL_ARCHIVE_MAP

logger = logging.getLogger(__name__)


class MTDNNPretrainedModel(BertPreTrainedModel):
    config_class = MTDNNConfig
    pretrained_model_archive_map = PRETRAINED_MODEL_ARCHIVE_MAP
    load_tf_weights = lambda model, config, path: None
    base_model_prefix = "mtdnn"

    def __init__(self, config):
        super(MTDNNPretrainedModel, self).__init__(config)
        if not isinstance(config, PretrainedConfig):
            raise ValueError(
                "Parameter config in `{}(config)` should be an instance of class `PretrainedConfig`. "
                "To create a model from a pretrained model use "
                "`model = {}.from_pretrained(PRETRAINED_MODEL_NAME)`".format(
                    self.__class__.__name__, self.__class__.__name__
                )
            )
        # Save config in model
        self.config = config


class MTDNNModel(MTDNNPretrainedModel, BertModel):
    def __init__(self, config: MTDNNConfig):
        super(MTDNNModel, self).__init__(config)
        self.config = config
        self.local_updates = 0
        self.train_loss = AverageMeter()
        self.dropout_list = nn.ModuleList()
        self.encoder_type = config.encoder_type
        self.config_dict = self.config.to_dict()
        self.mtdnn_config = MTDNNConfig.from_dict(self.config_dict)

        # Setup the baseline model
        # Define the encoder based on config options
        self.bert_config = BertConfig.from_dict(self.mtdnn_config)
        self.bert = BertModel(self.bert_config)
        self.hidden_size = self.bert_config.hidden_size
        if self.encoder_type == EncoderModelType.ROBERTA:
            self.bert = FairseqRobertModel.from_pretrained(config.init_checkpoint)
            self.hidden_size = self.bert.args.encoder_embed_dim
            self.pooler = LinearPooler(hidden_size)

        # Dump other features if value is set to true
        if config.dump_feature:
            return

        # Set decoder and scoring list parameters
        self.decoder_opts = config.decoder_opts
        self.scoring_list = nn.ModuleList()

        # Update bert parameters
        if config.update_bert_opt > 0:
            for param in self.bert.parameters():
                param.requires_grad = False

        # Set task specific paramaters
        self.task_types = config.task_types
        self.task_dropout_p = config.tasks_dropout_p
        self.n_class = config.n_class
        for task, label in enumerate(self.n_class):
            task_type = self.task_types[task]
            dropout = DropoutWrapper(
                self.task_dropout_p[task], config["enable_variational_dropout"]
            )
            self.dropout_list.append(dropout)
            if task_type == TaskType.Span:
                out_proj = nn.Linear(hidden_size, 2)
            elif task_type == TaskType.SequenceLabeling:
                out_proj = nn.Linear(hidden_size, label)
            else:
                out_proj = nn.Linear(hidden_size, label)
            self.scoring_list.append(out_proj)

        self._my_init()

    def init_encoder(self, encoder_type: int = 1):
        """ Set the model encoder during initialization
        encoder_type set to 1 means BERT, 2 means RoBERTa
        """
        pass

    def _my_init(self):
        def init_weights(module):
            if isinstance(module, (nn.Linear, nn.Embedding)):
                # Slightly different from the TF version which uses truncated_normal for initialization
                # cf https://github.com/pytorch/pytorch/pull/5617
                module.weight.data.normal_(mean=0.0, std=0.02 * self.opt["init_ratio"])
            elif isinstance(module, BertLayerNorm):
                # Slightly different from the BERT pytorch version, which should be a bug.
                # Note that it only affects on training from scratch. For detailed discussions, please contact xiaodl@.
                # Layer normalization (https://arxiv.org/abs/1607.06450)
                # support both old/latest version
                if "beta" in dir(module) and "gamma" in dir(module):
                    module.beta.data.zero_()
                    module.gamma.data.fill_(1.0)
                else:
                    module.bias.data.zero_()
                    module.weight.data.fill_(1.0)
            if isinstance(module, nn.Linear):
                module.bias.data.zero_()

        self.apply(init_weights)

    def forward(
        self, input_ids, token_type_ids, attention_mask, premise_mask=None, hyp_mask=None, task_id=0
    ):
        if self.encoder_type == EncoderModelType.ROBERTA:
            sequence_output = self.bert.extract_features(input_ids)
            pooled_output = self.pooler(sequence_output)
        else:
            all_encoder_layers, pooled_output = self.bert(input_ids, token_type_ids, attention_mask)
            sequence_output = all_encoder_layers[-1]

        decoder_opt = self.decoder_opts[task_id]
        task_type = self.task_types[task_id]
        if task_type == TaskType.Span:
            assert decoder_opt != 1
            sequence_output = self.dropout_list[task_id](sequence_output)
            logits = self.scoring_list[task_id](sequence_output)
            start_scores, end_scores = logits.split(1, dim=-1)
            start_scores = start_scores.squeeze(-1)
            end_scores = end_scores.squeeze(-1)
            return start_scores, end_scores
        elif task_type == TaskType.SequenceLabeling:
            pooled_output = all_encoder_layers[-1]
            pooled_output = self.dropout_list[task_id](pooled_output)
            pooled_output = pooled_output.contiguous().view(-1, pooled_output.size(2))
            logits = self.scoring_list[task_id](pooled_output)
            return logits
        else:
            if decoder_opt == 1:
                max_query = hyp_mask.size(1)
                assert max_query > 0
                assert premise_mask is not None
                assert hyp_mask is not None
                hyp_mem = sequence_output[:, :max_query, :]
                logits = self.scoring_list[task_id](
                    sequence_output, hyp_mem, premise_mask, hyp_mask
                )
            else:
                pooled_output = self.dropout_list[task_id](pooled_output)
                logits = self.scoring_list[task_id](pooled_output)
            return logits


if __name__ == "__main__":
    config = MTDNNConfig()
    b = MTDNNModel(config)
    print(b.config_class)
    print(b.config)
    print(b.embeddings)
    print(b.encoder)
    print(b.pooler)
    print(b.pretrained_model_archive_map)
    print(b.base_model_prefix)
    print(b.bert_config)
