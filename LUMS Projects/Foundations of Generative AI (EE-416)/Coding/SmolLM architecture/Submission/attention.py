import math 
import torch 
import torch.nn as nn 
import torch.nn.functional as F

def rotate_half(x):
    """
    Rotates the left half of a tensor along its final dimension.

    This function is used in Rotary Positional Embeddings (RoPE) to apply a 
    complex-valued rotation by swapping the two halves of the last dimension
    with a sign flip.

    Given an input tensor `x` of shape (..., head_dim), it splits `x` into 
    two equal halves along the last dimension, then swaps them while negating 
    the second half.

    Args:
        x (torch.Tensor): Input tensor of shape (..., head_dim), where head_dim must be even.

    Returns:
        torch.Tensor: The rotated tensor of the same shape as `x`, where the two halves 
                      are swapped with sign inversion on the second half.
    """
    x1, x2 = x[..., : x.size(-1) // 2], x[..., x.size(-1) // 2 :]
    return torch.cat([-x2, x1], dim=-1)

def apply_rotary_pos_emb(q, k, cos, sin, position_ids=None, unsqueeze_dim=None):
    """
    Applies Rotary Positional Embeddings (RoPE) to the query and key tensors.

    Rotary Positional Embeddings (RoPE) encode positional information directly 
    into the query and key representations by rotating them in a complex-valued 
    space. This method enhances the model's ability to capture relative positions.

    Args:
        q (torch.Tensor): Query tensor of shape (batch, num_heads, seq_len, head_dim).
        k (torch.Tensor): Key tensor of shape (batch, num_heads, seq_len, head_dim).
        cos (torch.Tensor): Precomputed cosine values for RoPE of shape (seq_len, head_dim).
        sin (torch.Tensor): Precomputed sine values for RoPE of shape (seq_len, head_dim).
        position_ids (torch.Tensor, optional): Position indices of shape (batch, seq_len).
                                               Defaults to None, which assumes sequential positions.
        unsqueeze_dim (int, optional): If provided, expands `cos` and `sin` along this dimension
                                       to facilitate broadcasting.

    Returns:
        Tuple[torch.Tensor, torch.Tensor]: The rotated query and key tensors with the same shape
                                           as the input (batch, num_heads, seq_len, head_dim).
    """
    if unsqueeze_dim is not None:
        cos = cos.unsqueeze(unsqueeze_dim)
        sin = sin.unsqueeze(unsqueeze_dim)

    q_rotated = q * cos + rotate_half(q) * sin
    k_rotated = k * cos + rotate_half(k) * sin

    return q_rotated, k_rotated

class RotaryEmbedder(nn.Module):
    def __init__(self, dim, base):
        super().__init__()
        # ===================== DO NOT CHANGE THE INIT FUNCTION! =====================
        # Precompute frequency for sine/cosine embeddings
        self.freq = 1.0 / (base ** (torch.arange(0, dim, 2, dtype=torch.float32) / dim))

    @torch.no_grad()
    def forward(self, x):
        seq_len = x.size(1)
        positions = torch.arange(seq_len, dtype=torch.float32, device=x.device)
        angles = torch.einsum("i,j->ij", positions, self.freq)
        
        cos_emb = torch.cos(angles).to(x.dtype)
        sin_emb = torch.sin(angles).to(x.dtype)
        
        return cos_emb, sin_emb

class GroupedQueryAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        # ===================== DO NOT CHANGE THE INIT FUNCTION! =====================
        # Model dimensions and attention configurations
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_heads
        self.head_dim = config.hidden_size // self.num_heads
        self.kv_heads = config.kv_heads  # Number of key-value heads
        self.rope_theta = 10000.0  # Scaling factor for rotary embeddings

        # Linear projections for queries, keys, values, and output
        self.q_proj = nn.Linear(config.hidden_size, self.num_heads * self.head_dim, bias=False)
        self.k_proj = nn.Linear(config.hidden_size, self.kv_heads * self.head_dim, bias=False)
        self.v_proj = nn.Linear(config.hidden_size, self.kv_heads * self.head_dim, bias=False)
        self.o_proj = nn.Linear(config.hidden_size, config.hidden_size, bias=False)

        # Rotary embedding generator
        self.rotary_emb = RotaryEmbedder(base=self.rope_theta, dim=self.head_dim)
    
    def _repeat_kv(self, x, n_rep):
        """
        Expands the number of key-value attention heads by repeating them.

        This function is used in grouped query attention (GQA) and multi-query attention (MQA) 
        to duplicate key-value heads `n_rep` times, so they can be shared across multiple query heads.

        Args:
            x (torch.Tensor): A tensor of shape (batch, num_key_value_heads, seq_len, head_dim),
                            representing the key or value tensor.
            n_rep (int): The number of times to repeat each key-value head.

        Returns:
            torch.Tensor: A tensor of shape (batch, num_key_value_heads * n_rep, seq_len, head_dim),
                        where each key-value head is repeated `n_rep` times.
        """
        batch_size, num_kv_heads, seq_len, head_dim = x.shape
        x = x.unsqueeze(2).expand(batch_size, num_kv_heads, n_rep, seq_len, head_dim)
        return x.reshape(batch_size, num_kv_heads * n_rep, seq_len, head_dim)


    def forward(self, x: torch.Tensor, attention_mask=None):
        batch_size, seq_len, _ = x.shape

        q = self.q_proj(x).view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)
        k = self.k_proj(x).view(batch_size, seq_len, self.kv_heads, self.head_dim).transpose(1, 2)
        v = self.v_proj(x).view(batch_size, seq_len, self.kv_heads, self.head_dim).transpose(1, 2)

        cos, sin = self.rotary_emb(q)
        q, k = apply_rotary_pos_emb(q, k, cos, sin)

        n_rep = self.num_heads // self.kv_heads
        k = self._repeat_kv(k, n_rep)
        v = self._repeat_kv(v, n_rep)

        attn_scores = torch.matmul(q, k.transpose(-1, -2)) / math.sqrt(self.head_dim)

        if attention_mask is not None:
            attn_scores += attention_mask

        attn_weights = F.softmax(attn_scores, dim=-1)

        context = torch.matmul(attn_weights, v)
        context = context.transpose(1, 2).reshape(batch_size, seq_len, self.hidden_size)
        return self.o_proj(context)