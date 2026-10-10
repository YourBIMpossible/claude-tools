<context_brief packet_id="lab-pos-region-v2" head="{head}">
Scope confidence: high
Sources: config, ripgrep

## Lexical
- [config] tenant north -> REGION=r7 (deploy/tenants.env, outside this repository)  |  north=r7
  why: tenant named in the prompt
- [ripgrep] LIMIT at src/regions/r7.py:3  |  LIMIT = 17
  why: quota limit for region r7
</context_brief>
