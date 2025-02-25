import torch
from .fx_tracer import HFTracer
from typing import (
    Any,
    Callable,
    Dict,
    Optional,
    Union,
)

import torch.nn.functional as F

# GPT2 Attention patterns
def AttnPattern(query, key, value, causal_mask, mask_value, inv_scale, device, dropout_p):
    attn_weights = torch.matmul(query, key.transpose(-1, -2))
    attn_weights = attn_weights / torch.full(
        [], inv_scale, dtype=torch.float16, device=device
    )
    attn_weights = torch.where(causal_mask, attn_weights.to(torch.float16), mask_value)
    attn_weights = torch.nn.functional.softmax(attn_weights, dim=-1)
    attn_weights = attn_weights.type(torch.float16)
    attn_weights = torch.nn.functional.dropout(attn_weights, p=dropout_p)
    attn_output = torch.matmul(attn_weights, value)
    return attn_output


def AttnReplacement(q, k, v, causal_mask, mask_value, inv_scale, device, dropout_p):
    return torch.ops.aten.scaled_dot_product_attention(
        q,
        k,
        v,
        attn_mask=None,
        dropout_p=dropout_p,
        is_causal=True,
        scale=1.0 / inv_scale
    )

# NanoGPT Attention patterns
def AttnPattern1(q, k, v, causal_mask, mask_value, scale, dropout_p):
    att = (q @ k.transpose(-2, -1)) * scale
    att = att.masked_fill(causal_mask, mask_value)
    att = F.softmax(att, dim=-1)
    att = torch.nn.functional.dropout(att, p=dropout_p)
    y = att @ v  # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
    return y


def AttnReplacement1(q, k, v, causal_mask, mask_value, scale, dropout_p):
    return torch.ops.aten.scaled_dot_product_attention(
        q,
        k,
        v,
        attn_mask=None,
        dropout_p=dropout_p,
        is_causal=True,
        scale=scale
    )

# LLaMA Attention pattern
def AttnPattern2(query, key, value, attn_mask, min_val, inv_scale):
    attn_weights = torch.matmul(query, key.transpose(2, 3))
    attn_weights = attn_weights / inv_scale
    attn_weights = attn_weights + attn_mask
    attn_weights = torch.max(attn_weights, min_val)
    attn_weights = torch.nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32)
    attn_weights = attn_weights.to(torch.float16)
    attn_output = torch.matmul(attn_weights, value)
    return attn_output


def AttnReplacement2(q, k, v, attn_mask, min_val, inv_scale):
    return torch.ops.aten.scaled_dot_product_attention(
        q,
        k,
        v,
        attn_mask=None,
        dropout_p=0.0,
        is_causal=True,
        scale=1.0 / inv_scale
    )

# Bloom Attention pattern
# Note, Bloom attention uses a alibi attention mask, which is not causal.
# Replacement is not mathematically equivalent
def AttnPattern3(alibi, query, key, value, alpha, attn_mask, min_val, dropout_p, batch_size, num_heads, merge_head, q_len, kv_len, head_dim):
    matmul_result = alibi.baddbmm(
            batch1=query,
            batch2=key,
            beta=1.0,
            alpha=alpha,
        )
    attention_scores = matmul_result.view(batch_size, num_heads, q_len, kv_len)
    attention_scores = attention_scores.to(torch.float)
    attn_weights = torch.masked_fill(attention_scores, attn_mask, min_val)
    attention_probs = F.softmax(attn_weights, dim=-1, dtype=torch.float32).to(torch.float16)
    attention_probs = torch.nn.functional.dropout(attention_probs, p=dropout_p)
    attention_probs_reshaped = attention_probs.view(merge_head, q_len, kv_len)
    context_layer = torch.bmm(attention_probs_reshaped, value)
    context_layer = context_layer.view(batch_size, num_heads, q_len, head_dim)
    return context_layer


def AttnReplacement3(alibi, query, key, value, alpha, attn_mask, min_val, dropout_p, batch_size, num_heads, merge_head, q_len, kv_len, head_dim):
    # q, v: (batch_size * self.num_heads, q_length, self.head_dim)
    # k: (batch_size * self.num_heads, self.head_dim, q_length)
    query = query.reshape(batch_size, num_heads, q_len, head_dim)
    key = key.reshape(batch_size, num_heads, head_dim, q_len)
    value = value.reshape(batch_size, num_heads, q_len, head_dim)
    key = key.transpose(-1, -2)

    context_layer = torch.ops.aten.scaled_dot_product_attention(
        query,
        key,
        value,
        attn_mask=None,
        dropout_p=dropout_p,
        is_causal=True,
        scale=alpha
    )
    return context_layer


# OPT Attention pattern
def AttnPattern4(query, key, value, attn_mask, min_val, scale, dropout_p, batch_size, num_heads, merge_head, tgt_len, src_len, head_dim):
    query = query * scale
    query = query.view(batch_size, tgt_len, num_heads, head_dim).transpose(1, 2).contiguous()
    query = query.view(merge_head, -1, head_dim)
    attn_weights = torch.bmm(query, key.transpose(1, 2))
    attn_weights = attn_weights.view(batch_size, num_heads, tgt_len, src_len) + attn_mask
    attn_weights = torch.max(attn_weights, min_val)
    attn_weights = attn_weights.view(merge_head, tgt_len, src_len)
    attn_weights = torch.nn.functional.softmax(attn_weights, dim=-1)
    attn_probs = torch.nn.functional.dropout(attn_weights, p=dropout_p)
    attn_output = torch.bmm(attn_probs, value)
    attn_output = attn_output.view(batch_size, num_heads, tgt_len, head_dim)
    return attn_output


def AttnReplacement4(query, key, value, attn_mask, min_val, scale, dropout_p, batch_size, num_heads, merge_head, tgt_len, src_len, head_dim):
    # key: (bsz * self.num_heads, -1, self.head_dim)
    # val: (bsz * self.num_heads, -1, self.head_dim)
    query = query.view(batch_size, tgt_len, num_heads, head_dim).transpose(1, 2).contiguous()
    key = key.reshape(batch_size, num_heads, src_len, head_dim)
    value = value.reshape(batch_size, num_heads, tgt_len, head_dim)

    context_layer = torch.ops.aten.scaled_dot_product_attention(
        query,
        key,
        value,
        attn_mask=None,
        dropout_p=dropout_p,
        is_causal=True,
        scale=scale
    )
    return context_layer


# LLaMA-2 Attention pattern
def AttnPattern5(query, key, value, attn_mask, inv_scale):
    attn_weights = torch.matmul(query, key.transpose(2, 3))
    attn_weights = attn_weights / inv_scale
    attn_weights = attn_weights + attn_mask
    attn_weights = attn_weights.float()
    attn_weights = torch.nn.functional.softmax(attn_weights, dim=-1)
    attn_weights = attn_weights.type_as(query)
    attn_output = torch.matmul(attn_weights, value)
    return attn_output


def AttnReplacement5(q, k, v, attn_mask, inv_scale):
    return torch.ops.aten.scaled_dot_product_attention(
        q,
        k,
        v,
        attn_mask=None,
        dropout_p=0.0,
        is_causal=True,
        scale=1.0 / inv_scale
    )


# LLaMA aten attention op pattern
def LLaMAAttnPattern(query, key, value, attn_mask, min_val, inv_scale, batch, num_head, fused_batch, seq_len, head_dim):
    transpose_3 = torch.ops.aten.transpose.int(key, 2, 3)
    expand_2 = torch.ops.aten.expand.default(query, [batch, num_head, seq_len, head_dim])
    clone = torch.ops.aten.clone.default(expand_2, memory_format = torch.contiguous_format)
    _unsafe_view_3 = torch.ops.aten._unsafe_view.default(clone, [fused_batch, seq_len, head_dim])
    expand_3 = torch.ops.aten.expand.default(transpose_3, [batch, num_head, head_dim, seq_len])
    clone_1 = torch.ops.aten.clone.default(expand_3, memory_format = torch.contiguous_format)
    _unsafe_view_4 = torch.ops.aten._unsafe_view.default(clone_1, [fused_batch, head_dim, seq_len])
    bmm = torch.ops.aten.bmm.default(_unsafe_view_3, _unsafe_view_4)
    _unsafe_view_5 = torch.ops.aten._unsafe_view.default(bmm, [batch, num_head, seq_len, seq_len])
    div = torch.ops.aten.div.Tensor(_unsafe_view_5, inv_scale)
    add_5 = torch.ops.aten.add.Tensor(div, attn_mask)
    maximum = torch.ops.aten.maximum.default(add_5, min_val)
    _softmax = torch.ops.aten._softmax.default(maximum, -1, False)
    _to_copy_10 = torch.ops.aten._to_copy.default(_softmax, dtype = torch.float16)
    expand_4 = torch.ops.aten.expand.default(_to_copy_10, [batch, num_head, seq_len, seq_len])
    view_8 = torch.ops.aten.view.default(expand_4, [fused_batch, seq_len, seq_len]);  expand_4 = None
    expand_5 = torch.ops.aten.expand.default(value, [batch, num_head, seq_len, head_dim])
    clone_2 = torch.ops.aten.clone.default(expand_5, memory_format = torch.contiguous_format)
    _unsafe_view_6 = torch.ops.aten._unsafe_view.default(clone_2, [fused_batch, seq_len, head_dim])
    bmm_1 = torch.ops.aten.bmm.default(view_8, _unsafe_view_6)
    _unsafe_view_5 = torch.ops.aten._unsafe_view.default(bmm_1, [batch, num_head, seq_len, head_dim])
    return _softmax, _unsafe_view_5


def LLaMAAttnReplacement(query, key, value, attn_mask, min_val, inv_scale, batch, num_head, fused_batch, seq_len, head_dim):
    # q, k, v needs to be transposed for flash attn v2 
    query = query.transpose(1, 2)
    key = key.transpose(1, 2)
    value = value.transpose(1, 2)
    out, q_pad, k_pad, v_pad, out_pad, softmax_lse, S_dmask, rng_state = torch.ops.byteir.flash_attn_fwd(
        query,
        key,
        value,
        0.0,
        1.0/inv_scale,
        True,
        False
    )
    # output also needs to be transposed
    out = out.transpose(1, 2)
    return out, out


def canonicalize_graph_before_replacement(gm):
    for n in gm.graph.nodes:
        if n.op == "call_module":
            submod = gm.get_submodule(n.target)
            if isinstance(submod, torch.nn.Dropout):
                with gm.graph.inserting_before(n):
                    new_node = gm.graph.call_function(torch.nn.functional.dropout, args=n.args, kwargs={'p': submod.p, 'training': submod.training, 'inplace': submod.inplace})
                    n.replace_all_uses_with(new_node)
                    gm.graph.erase_node(n)
        if n.op == "call_function":
            if n.target == torch.nn.functional.softmax:
                # canonicalize softmax keyword args
                new_args = {}
                new_args['dim'] = n.kwargs['dim']
                if '_stacklevel' not in n.kwargs:
                    new_args['_stacklevel'] = 3
                else:
                    new_args['_stacklevel'] = n.kwargs['_stacklevel']
                if 'dtype' not in n.kwargs:
                    new_args['dtype'] = None
                else:
                    new_args['dtype'] = n.kwargs['dtype']
                n.kwargs = new_args
            elif n.target == torch.nn.functional.dropout:
                # canonicalize softmax keyword args
                new_args = {}
                new_args['p'] = n.kwargs['p']
                if 'training' not in n.kwargs:
                    new_args['training'] = True
                else:
                    new_args['training'] = n.kwargs['training']
                if 'inplace' not in n.kwargs:
                    new_args['inplace'] = False
                else:
                    new_args['inplace'] = n.kwargs['inplace']
                n.kwargs = new_args

    gm.graph.lint()
    gm.recompile()
    return gm


# HuggingFace symbolic trace
# FIXME: workaround to trace torch.full
def hf_symbolic_trace(
    root: Union[torch.nn.Module, Callable[..., Any]],
    concrete_args: Optional[Dict[str, Any]] = None,
) -> torch.fx.GraphModule:
    tracer = HFTracer()
    graph = tracer.trace(root, concrete_args)
    name = (
        root.__class__.__name__ if isinstance(root, torch.nn.Module) else root.__name__
    )
    return torch.fx.GraphModule(tracer.root, graph, name)

def fx_replace_attn_pattern(gm: torch.fx.GraphModule):
    gm = canonicalize_graph_before_replacement(gm)
    # Need hf_symbolic_trace to trace torch.full
    torch.fx.replace_pattern(gm, hf_symbolic_trace(AttnPattern), AttnReplacement)
    torch.fx.replace_pattern(gm, AttnPattern1, AttnReplacement1)
    torch.fx.replace_pattern(gm, AttnPattern2, AttnReplacement2)
    torch.fx.replace_pattern(gm, AttnPattern3, AttnReplacement3)
    torch.fx.replace_pattern(gm, AttnPattern4, AttnReplacement4)
    torch.fx.replace_pattern(gm, AttnPattern5, AttnReplacement5)
    torch.fx.replace_pattern(gm, LLaMAAttnPattern, LLaMAAttnReplacement)
    return gm
