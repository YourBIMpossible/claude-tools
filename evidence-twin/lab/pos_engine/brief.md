<context_brief packet_id="lab-pos-engine" head="{head}">
Scope confidence: high
Sources: prompt_symbol, ripgrep

## Lexical
- [ripgrep] alpha at src/engine/k_c.py:4  |  return value * 3 + 2
  why: dispatch("alpha") -> KERNELS[sum(ord) % 12 = 2] = k_c
</context_brief>
