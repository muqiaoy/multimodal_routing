import torch
from torch import nn
import torch.nn.functional as F
import sys
from src.multimodal_transformer.modules.transformer import TransformerEncoder


class MULTModel(nn.Module):
    def __init__(self, orig_d_l, orig_d_a, orig_d_v, d_l, d_a, d_v, vonly, aonly, lonly, num_heads, layers, self_layers, attn_dropout, \
                                        attn_dropout_a, attn_dropout_v, relu_dropout, res_dropout, out_dropout, embed_dropout, attn_mask):
        """
        Construct a MulT model.
        """
        super(MULTModel, self).__init__()
        self.orig_d_l, self.orig_d_a, self.orig_d_v = orig_d_l, orig_d_a, orig_d_v
        self.d_l, self.d_a, self.d_v = d_l, d_a, d_v
        self.vonly = vonly
        self.aonly = aonly
        self.lonly = lonly
        self.num_heads = num_heads
        self.layers = layers
        self.self_layers = self_layers
        self.attn_dropout = attn_dropout
        self.attn_dropout_a = attn_dropout_a
        self.attn_dropout_v = attn_dropout_v
        self.relu_dropout = relu_dropout
        self.res_dropout = res_dropout
        self.out_dropout = out_dropout
        self.embed_dropout = embed_dropout
        self.attn_mask = attn_mask

        combined_dim = self.d_l + self.d_a + self.d_v

        self.partial_mode = self.lonly + self.aonly + self.vonly
        if self.partial_mode == 1:
            combined_dim = 2 * self.d_l   # assuming d_l == d_a == d_v
        else:
            combined_dim = 2 * (self.d_l + self.d_a + self.d_v)
        
        # 1. Temporal convolutional layers
        self.proj_l = nn.Conv1d(self.orig_d_l, self.d_l, kernel_size=1, padding=0, bias=False)
        self.proj_a = nn.Conv1d(self.orig_d_a, self.d_a, kernel_size=1, padding=0, bias=False)
        self.proj_v = nn.Conv1d(self.orig_d_v, self.d_v, kernel_size=1, padding=0, bias=False)
        # Self attention for l only, a only, v only
        self.trans_l = self.get_network(self_type='l_only', layers=self.self_layers)
        self.trans_a = self.get_network(self_type='a_only', layers=self.self_layers)
        self.trans_v = self.get_network(self_type='v_only', layers=self.self_layers)
        
        # 2. Crossmodal Attentions
        if self.lonly:
            self.trans_l_with_a = self.get_network(self_type='la')
            self.trans_l_with_v = self.get_network(self_type='lv')
        if self.aonly:
            self.trans_a_with_l = self.get_network(self_type='al')
            self.trans_a_with_v = self.get_network(self_type='av')
        if self.vonly:
            self.trans_v_with_l = self.get_network(self_type='vl')
            self.trans_v_with_a = self.get_network(self_type='va')
        
        self.final_lav = nn.Linear(3 * d_l, d_l)
    def get_network(self, self_type='l', layers=-1):
        if self_type in ['l', 'al', 'vl']:
            embed_dim, attn_dropout = self.d_l, self.attn_dropout
        elif self_type in ['a', 'la', 'va']:
            embed_dim, attn_dropout = self.d_a, self.attn_dropout_a
        elif self_type in ['v', 'lv', 'av']:
            embed_dim, attn_dropout = self.d_v, self.attn_dropout_v
        elif self_type == 'l_mem':
            embed_dim, attn_dropout = 2*self.d_l, self.attn_dropout
        elif self_type == 'a_mem':
            embed_dim, attn_dropout = 2*self.d_a, self.attn_dropout
        elif self_type == 'v_mem':
            embed_dim, attn_dropout = 2*self.d_v, self.attn_dropout
        elif self_type == "l_only":
            embed_dim, attn_dropout = self.d_l, self.attn_dropout
        elif self_type == "a_only":
            embed_dim, attn_dropout = self.d_a, self.attn_dropout
        elif self_type == "v_only":
            embed_dim, attn_dropout = self.d_v, self.attn_dropout
        else:
            raise ValueError("Unknown network type")
        
        return TransformerEncoder(embed_dim=embed_dim,
                                  num_heads=self.num_heads,
                                  layers=max(self.layers, layers),
                                  attn_dropout=attn_dropout,
                                  relu_dropout=self.relu_dropout,
                                  res_dropout=self.res_dropout,
                                  embed_dropout=self.embed_dropout,
                                  attn_mask=self.attn_mask)
            
    def forward(self, x_l, x_a, x_v):
        """
        text, audio, and vision should have dimension [batch_size, seq_len, n_features]
        """
        x_l = F.dropout(x_l.transpose(1, 2), p=self.embed_dropout, training=self.training)
        x_a = x_a.transpose(1, 2)
        x_v = x_v.transpose(1, 2)
       
        # Project the textual/visual/audio features
        proj_x_l = x_l if self.orig_d_l == self.d_l else self.proj_l(x_l)
        proj_x_a = x_a if self.orig_d_a == self.d_a else self.proj_a(x_a)
        proj_x_v = x_v if self.orig_d_v == self.d_v else self.proj_v(x_v)

        proj_x_a = proj_x_a.permute(2, 0, 1)
        proj_x_v = proj_x_v.permute(2, 0, 1)
        proj_x_l = proj_x_l.permute(2, 0, 1)

        if self.lonly:
            # (V,A) --> L
            h_l_only = self.trans_l(proj_x_l)
            h_l_with_as = self.trans_l_with_a(proj_x_l, proj_x_a, proj_x_a)    # Dimension (L, N, d_l)
            h_l_with_vs = self.trans_l_with_v(proj_x_l, proj_x_v, proj_x_v)    # Dimension (L, N, d_l)

        if self.aonly:
            # (L,V) --> A
            h_a_only = self.trans_a(proj_x_a)
            h_a_with_ls = self.trans_a_with_l(proj_x_a, proj_x_l, proj_x_l)
            h_a_with_vs = self.trans_a_with_v(proj_x_a, proj_x_v, proj_x_v)

        if self.vonly:
            # (L,A) --> V
            h_v_only = self.trans_v(proj_x_v)
            h_v_with_ls = self.trans_v_with_l(proj_x_v, proj_x_l, proj_x_l)
            h_v_with_as = self.trans_v_with_a(proj_x_v, proj_x_a, proj_x_a)

        h_l_last = h_l_only[-1] # bs, d_l
        h_a_last = h_a_only[-1]
        h_v_last = h_v_only[-1]

        h_la_last = h_l_with_as[-1] # bs, d_l
        h_av_last = h_a_with_vs[-1]
        h_vl_last = h_v_with_ls[-1]
        h_lav_last = torch.cat([h_a_with_ls, h_v_with_as, h_l_with_vs], dim=2)[-1] # bs, d_l * 3
        return h_l_last, h_a_last, h_v_last, h_la_last, h_av_last, h_vl_last, self.final_lav(h_lav_last)

